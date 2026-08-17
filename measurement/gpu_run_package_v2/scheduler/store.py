"""Crash-consistent filesystem store for scheduler state and artifacts."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .model import WorkUnit
from .state_machine import State, require_transition
from .validators import verify_complete


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class SchedulerStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.state_dir = self.root / "state"
        self.tmp_dir = self.root / ".tmp"
        self.complete_dir = self.root / "complete"
        self.abandoned_dir = self.root / "abandoned"
        for path in (
            self.root, self.state_dir, self.tmp_dir,
            self.complete_dir, self.abandoned_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".scheduler.lock"
        self.journal_path = self.root / "journal.jsonl"

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _state_path(self, work_unit_id: str) -> Path:
        return self.state_dir / f"{work_unit_id}.json"

    def initialize(self, unit: WorkUnit) -> dict[str, Any]:
        with self.locked():
            path = self._state_path(unit.work_unit_id)
            if path.exists():
                record = self._read(path)
                WorkUnit.from_dict(record["work_unit"])
                return record
            record = {
                "schema_version": "scheduler-state-v1",
                "work_unit": unit.as_dict(),
                "state": State.PENDING.value,
                "attempts": 0,
                "reason": None,
            }
            atomic_json(path, record)
            self._journal({
                "event": "INITIALIZED",
                "work_unit_id": unit.work_unit_id,
                "state": State.PENDING.value,
            })
            return record

    def load(self, unit_or_id: WorkUnit | str) -> dict[str, Any]:
        work_unit_id = (
            unit_or_id.work_unit_id
            if isinstance(unit_or_id, WorkUnit) else unit_or_id
        )
        return self._read(self._state_path(work_unit_id))

    def records(self) -> list[dict[str, Any]]:
        return [self._read(path) for path in sorted(self.state_dir.glob("*.json"))]

    def transition(
        self,
        unit: WorkUnit,
        target: State,
        *,
        reason: str | None = None,
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        with self.locked():
            path = self._state_path(unit.work_unit_id)
            record = self._read(path)
            current = State(record["state"])
            require_transition(current, target)
            if current is State.COMPLETE:
                raise ValueError("COMPLETE work units are immutable")
            record["state"] = target.value
            record["reason"] = reason
            if increment_attempt:
                record["attempts"] += 1
            atomic_json(path, record)
            self._journal({
                "event": "TRANSITION",
                "work_unit_id": unit.work_unit_id,
                "from": current.value,
                "to": target.value,
                "attempts": record["attempts"],
                "reason": reason,
            })
            return record

    def prepare_tmp(self, unit: WorkUnit, attempt: int) -> Path:
        path = self.tmp_dir / unit.work_unit_id
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            destination = self.abandoned_dir / (
                f"{unit.work_unit_id}.attempt-{attempt}"
            )
            suffix = 0
            while destination.exists():
                suffix += 1
                destination = self.abandoned_dir / (
                    f"{unit.work_unit_id}.attempt-{attempt}.{suffix}"
                )
            os.rename(path, destination)
            fsync_directory(self.tmp_dir)
            fsync_directory(self.abandoned_dir)
            path.mkdir(mode=0o700)
        fsync_directory(self.tmp_dir)
        return path

    def publish(self, unit: WorkUnit) -> Path:
        source = self.tmp_dir / unit.work_unit_id
        destination = self.complete_dir / unit.work_unit_id
        if destination.exists():
            raise FileExistsError(f"immutable COMPLETE exists: {destination}")
        if source.stat().st_dev != self.complete_dir.stat().st_dev:
            raise OSError("tmp and complete directories are not on one filesystem")
        self._fsync_tree(source)
        os.rename(source, destination)
        fsync_directory(self.tmp_dir)
        fsync_directory(self.complete_dir)
        return destination

    def make_complete_immutable(self, unit: WorkUnit) -> None:
        root = self.complete_dir / unit.work_unit_id
        for path in sorted(root.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        root.chmod(0o555)
        fsync_directory(self.complete_dir)

    def reconcile(self) -> dict[str, int]:
        recovered = corrupt = 0
        with self.locked():
            for complete in sorted(self.complete_dir.iterdir()):
                if not complete.is_dir() or complete.is_symlink():
                    continue
                state_path = self._state_path(complete.name)
                if not state_path.is_file():
                    corrupt += 1
                    continue
                record = self._read(state_path)
                unit = WorkUnit.from_dict(record["work_unit"])
                if unit.work_unit_id != complete.name:
                    corrupt += 1
                    continue
                errors = verify_complete(complete, unit)
                if errors:
                    corrupt += 1
                    self._journal({
                        "event": "RECONCILE_CORRUPT_COMPLETE",
                        "work_unit_id": unit.work_unit_id,
                        "errors": errors,
                    })
                    continue
                if record["state"] != State.COMPLETE.value:
                    previous = record["state"]
                    record["state"] = State.COMPLETE.value
                    record["reason"] = "recovered atomic rename before state commit"
                    atomic_json(state_path, record)
                    self._journal({
                        "event": "RECONCILED_COMPLETE",
                        "work_unit_id": unit.work_unit_id,
                        "from": previous,
                        "to": State.COMPLETE.value,
                    })
                    recovered += 1
                self.make_complete_immutable(unit)
        return {"recovered": recovered, "corrupt": corrupt}

    def _journal(self, event: dict[str, Any]) -> None:
        line = json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ) + "\n"
        with self.journal_path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(self.root)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"state file must be an object: {path}")
        return value

    @staticmethod
    def _fsync_tree(root: Path) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"symlinks are forbidden in work-unit output: {path}")
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for path in sorted(
            (item for item in root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            fsync_directory(path)
        fsync_directory(root)
