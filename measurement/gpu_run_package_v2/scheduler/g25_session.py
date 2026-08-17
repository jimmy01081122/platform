"""Crash-consistent storage for prospective G2.5 GPU qualification sessions.

This module is deliberately independent from the GPU dispatcher.  It is safe to
exercise with CPU-only tests and does not import or query a CUDA runtime.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .store import atomic_json, fsync_directory


ZERO_HASH = "0" * 64
APPLICATION_SESSION_ID = "granite-c1a-g25-qualification-r1-20260719"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_SCHEMA_PATH = PACKAGE_ROOT / "schemas/g25_application_terminal.schema.json"
FINAL_SEAL_SCHEMA_PATH = PACKAGE_ROOT / "schemas/g25_application_final_seal.schema.json"
SESSION_FILE_INVENTORY_SCHEMA_PATH = (
    PACKAGE_ROOT / "schemas/g25_session_file_inventory.schema.json"
)
EXTERNAL_SEAL_ANCHOR_SCHEMA_PATH = (
    PACKAGE_ROOT / "schemas/g25_external_seal_anchor.schema.json"
)
SESSION_INVENTORY_META_FILES = frozenset({
    "final_seal.json", "session_file_inventory.json",
})
SESSION_STATES = (
    "CREATED",
    "PREFLIGHTING",
    "READY",
    "DISPATCHING",
    "FINALIZING",
    "AUDITING",
    "SEALING",
    "TERMINAL_COMPLETE",
    "TERMINAL_INCOMPLETE",
    "TERMINAL_FAILED",
)
CELL_STATES = (
    "PENDING",
    "DISPATCHED",
    "RUNNING",
    "PROCESS_EXITED",
    "RAW_SAVED",
    "CLASSIFIED",
    "RECORDED",
    "INCOMPLETE",
)
SESSION_TRANSITIONS = {
    "CREATED": {"PREFLIGHTING", "FINALIZING"},
    "PREFLIGHTING": {"READY", "FINALIZING"},
    "READY": {"DISPATCHING", "FINALIZING"},
    "DISPATCHING": {"FINALIZING"},
    "FINALIZING": {"AUDITING"},
    "AUDITING": {"SEALING"},
    "SEALING": {
        "TERMINAL_COMPLETE", "TERMINAL_INCOMPLETE", "TERMINAL_FAILED",
    },
    "TERMINAL_COMPLETE": set(),
    "TERMINAL_INCOMPLETE": set(),
    "TERMINAL_FAILED": set(),
}
CELL_TRANSITIONS = {
    "PENDING": {"DISPATCHED", "INCOMPLETE"},
    "DISPATCHED": {"RUNNING", "INCOMPLETE"},
    "RUNNING": {"PROCESS_EXITED", "INCOMPLETE"},
    "PROCESS_EXITED": {"RAW_SAVED", "INCOMPLETE"},
    "RAW_SAVED": {"CLASSIFIED", "INCOMPLETE"},
    "CLASSIFIED": {"RECORDED", "INCOMPLETE"},
    "RECORDED": set(),
    "INCOMPLETE": set(),
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_descriptor(root: Path, path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"qualification evidence is not a regular file: {path}")
    relative = path.relative_to(root).as_posix()
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def qualification_evidence_inventory(root: Path) -> dict[str, Any]:
    """Derive the exact qualification session/ledger/verdict/cell/raw file set."""
    root = root.resolve(strict=True)

    def optional(relative: str) -> dict[str, Any] | None:
        path = root / relative
        return _artifact_descriptor(root, path) if path.exists() else None

    def directory(relative: str) -> list[dict[str, Any]]:
        path = root / relative
        if not path.is_dir() or path.is_symlink():
            return []
        children = list(path.iterdir())
        if any(child.is_symlink() or not child.is_file() for child in children):
            raise ValueError(f"qualification evidence directory is not flat: {relative}")
        return [
            _artifact_descriptor(root, child)
            for child in sorted(children, key=lambda item: item.name)
        ]

    return {
        "schema_version": "g25-qualification-evidence-inventory-v1",
        "session_id": root.name,
        "session": optional("session.json"),
        "ledger": optional("ledger.json"),
        "verdict": optional("verdict.json"),
        "cells": directory("cells"),
        "raw": directory("raw"),
    }


def _secure_session_file_descriptor(root: Path, path: Path) -> dict[str, Any]:
    """Hash one regular non-symlink session file through a stable descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"session entry is not a regular file: {path}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise ValueError(f"session file changed while hashing: {path}")
        return {
            "path": path.relative_to(root).as_posix(),
            "kind": "file",
            "bytes": int(before.st_size),
            "sha256": digest.hexdigest(),
        }
    finally:
        os.close(descriptor)


def session_file_inventory(root: Path) -> dict[str, Any]:
    """Derive the recursive exact session entry set, excluding two seal meta files."""
    unresolved_root = root.absolute()
    if unresolved_root.is_symlink():
        raise ValueError("application session root may not be a symlink")
    root = unresolved_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("application session root is not a directory")
    entries: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as stream:
            children = sorted(stream, key=lambda item: item.name)
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            metadata = child.stat(follow_symlinks=False)
            if relative in SESSION_INVENTORY_META_FILES:
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"seal meta path is not a regular file: {relative}")
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"session inventory rejects symlink: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                entries.append({"path": relative, "kind": "directory"})
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                entries.append(_secure_session_file_descriptor(root, path))
            else:
                raise ValueError(f"session inventory rejects special entry: {relative}")

    visit(root)
    entries.sort(key=lambda item: (item["path"], item["kind"]))
    return {
        "schema_version": "g25-session-file-inventory-v1",
        "session_id": root.name,
        "excluded_meta_files": sorted(SESSION_INVENTORY_META_FILES),
        "entry_count": len(entries),
        "file_count": sum(item["kind"] == "file" for item in entries),
        "directory_count": sum(item["kind"] == "directory" for item in entries),
        "entries": entries,
        "entries_sha256": canonical_hash(entries),
    }


def external_seal_anchor_path(root: Path) -> Path:
    root = root.resolve(strict=True)
    return root.parent / ".g25_seal_anchors" / f"{_safe_component(root.name, 'session_id')}.json"


def write_external_seal_anchor(
    root: Path,
    seal: Mapping[str, Any],
) -> dict[str, Any]:
    """Exclusively create a receipt outside the rewritable session directory."""
    root = root.resolve(strict=True)
    anchor_path = external_seal_anchor_path(root)
    anchor_root = anchor_path.parent
    try:
        os.mkdir(anchor_root, mode=0o700)
        fsync_directory(root.parent)
    except FileExistsError:
        if anchor_root.is_symlink() or not anchor_root.is_dir():
            raise ValueError("external seal anchor root is unsafe")
    inventory_descriptor = seal.get("session_file_inventory")
    if not isinstance(inventory_descriptor, Mapping):
        raise ValueError("final seal lacks the session inventory descriptor")
    final_seal_path = root / "final_seal.json"
    final_seal_descriptor = _artifact_descriptor(root, final_seal_path)
    payload = {
        "schema_version": "g25-external-seal-anchor-v1",
        "session_id": root.name,
        "session_relative_path": root.name,
        "terminal_bound_event_sha256": seal.get("terminal_bound_event_sha256"),
        "session_file_inventory": {
            **dict(inventory_descriptor),
            "entries_sha256": seal.get("session_file_inventory_entries_sha256"),
        },
        "final_seal": final_seal_descriptor,
    }
    anchor = {**payload, "anchor_payload_sha256": canonical_hash(payload)}
    rendered = canonical_bytes(anchor) + b"\n"
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(anchor_path, flags, 0o400)
    try:
        offset = 0
        while offset < len(rendered):
            written = os.write(descriptor, rendered[offset:])
            if written <= 0:
                raise OSError("external seal anchor write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(anchor_root)
    return {
        "path": str(anchor_path),
        "sha256": sha256_file(anchor_path),
        "record": anchor,
    }


def verify_external_seal_anchor(
    root: Path,
    seal: Mapping[str, Any],
    *,
    anchor_path: Path | None,
    expected_anchor_sha256: str | None,
) -> dict[str, Any]:
    """Verify an externally retained anchor hash against the live session seal."""
    import jsonschema

    root = root.resolve(strict=True)
    if (
        not isinstance(expected_anchor_sha256, str)
        or len(expected_anchor_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_anchor_sha256)
    ):
        raise ValueError("trusted external seal anchor SHA-256 is required")
    expected_path = external_seal_anchor_path(root)
    if anchor_path is None:
        raise ValueError("external seal anchor path is required")
    unresolved = anchor_path.absolute()
    if unresolved.is_symlink() or not unresolved.is_file():
        raise ValueError("external seal anchor must be a regular non-symlink file")
    resolved = unresolved.resolve(strict=True)
    if resolved != expected_path.resolve(strict=True):
        raise ValueError("external seal anchor path differs from the session sibling receipt")
    if sha256_file(resolved) != expected_anchor_sha256:
        raise ValueError("external seal anchor differs from the trusted SHA-256")
    record = _read_object(resolved)
    schema = json.loads(EXTERNAL_SEAL_ANCHOR_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(record)
    payload = {
        key: value for key, value in record.items()
        if key != "anchor_payload_sha256"
    }
    if record["anchor_payload_sha256"] != canonical_hash(payload):
        raise ValueError("external seal anchor payload hash differs")
    if record["session_id"] != root.name or record["session_relative_path"] != root.name:
        raise ValueError("external seal anchor session identity differs")
    if record["terminal_bound_event_sha256"] != seal.get(
        "terminal_bound_event_sha256"
    ):
        raise ValueError("external seal anchor terminal event differs")
    expected_inventory = {
        **dict(seal.get("session_file_inventory") or {}),
        "entries_sha256": seal.get("session_file_inventory_entries_sha256"),
    }
    if record["session_file_inventory"] != expected_inventory:
        raise ValueError("external seal anchor inventory identity differs")
    if record["final_seal"] != _artifact_descriptor(root, root / "final_seal.json"):
        raise ValueError("external seal anchor final-seal identity differs")
    return record


def _safe_component(value: str, label: str) -> str:
    candidate = Path(value)
    if (
        not value
        or candidate.is_absolute()
        or candidate.name != value
        or value in {".", ".."}
    ):
        raise ValueError(f"{label} must be one relative path component")
    return value


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


class G25SessionStore:
    """Exclusive session reservation, journal and materialized state store."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.clock = clock
        self.monotonic = monotonic
        self.fault = fault or (lambda _point: None)
        self.journal_path = self.root / "journal.jsonl"
        self.session_state_path = self.root / "session_state.json"
        self.cells_dir = self.root / "cell_state"
        self.raw_dir = self.root / "raw"

    @classmethod
    def create(
        cls,
        output_root: Path,
        session_id: str,
        expected_cell_ids: Sequence[str],
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        started_monotonic: float | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> "G25SessionStore":
        session_id = _safe_component(session_id, "session_id")
        cell_ids = [_safe_component(item, "cell_id") for item in expected_cell_ids]
        if len(cell_ids) != 12 or len(set(cell_ids)) != 12:
            raise ValueError("G2.5 session requires exactly 12 unique cell IDs")
        output_root = output_root.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        root = output_root / session_id
        os.mkdir(root, mode=0o700)
        fsync_directory(output_root)
        store = cls(
            root, clock=clock, monotonic=monotonic, fault=fault,
        )
        store.cells_dir.mkdir()
        store.raw_dir.mkdir()
        fsync_directory(root)
        started_monotonic = float(
            store.monotonic() if started_monotonic is None else started_monotonic
        )
        if not math.isfinite(started_monotonic):
            raise ValueError("monotonic clock must be finite")
        if float(store.monotonic()) < started_monotonic:
            raise ValueError("session start cannot be after the monotonic clock")
        for cell_id in cell_ids:
            atomic_json(store._cell_path(cell_id), {
                "schema_version": "g25-application-cell-state-v1",
                "session_id": session_id,
                "cell_id": cell_id,
                "state": "PENDING",
                "raw_descriptor": None,
                "classification": None,
                "reason": None,
                "last_event_sha256": ZERO_HASH,
            })
        atomic_json(store.session_state_path, {
            "schema_version": "g25-application-session-state-v1",
            "session_id": session_id,
            "state": "CREATED",
            "expected_cell_ids": cell_ids,
            "expected_cell_set_sha256": canonical_hash(cell_ids),
            "expected_cell_count": 12,
            "started_monotonic": started_monotonic,
            "terminal_reason": None,
            "audit_descriptor": None,
            "last_event_sha256": ZERO_HASH,
        })
        event = store.append_event(
            "SESSION_CREATED",
            payload={
                "expected_cell_count": 12,
                "expected_cell_set_sha256": canonical_hash(cell_ids),
            },
        )
        state = store.session_state()
        state["last_event_sha256"] = event["event_sha256"]
        store._write_state(store.session_state_path, state)
        return store

    @property
    def session_id(self) -> str:
        return self.session_state()["session_id"]

    def session_state(self) -> dict[str, Any]:
        return _read_object(self.session_state_path)

    def cell_state(self, cell_id: str) -> dict[str, Any]:
        return _read_object(self._cell_path(cell_id))

    def _cell_path(self, cell_id: str) -> Path:
        return self.cells_dir / f"{_safe_component(cell_id, 'cell_id')}.json"

    def _write_state(self, path: Path, value: Mapping[str, Any]) -> None:
        self.fault("before_state_write")
        atomic_json(path, dict(value))
        self.fault("after_state_write")

    def _journal_head(self) -> tuple[int, str]:
        if not self.journal_path.exists():
            return -1, ZERO_HASH
        last: dict[str, Any] | None = None
        with self.journal_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("journal entry must be an object")
                    last = value
        if last is None:
            return -1, ZERO_HASH
        return int(last["sequence"]), str(last["event_sha256"])

    def append_event(
        self,
        event_type: str,
        *,
        cell_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        sequence, previous = self._journal_head()
        session = self.session_state()
        if cell_id is not None:
            _safe_component(cell_id, "cell_id")
            if cell_id not in session["expected_cell_ids"]:
                raise ValueError("journal cell is outside the frozen 12-cell set")
        now = float(self.clock())
        elapsed = float(self.monotonic()) - float(session["started_monotonic"])
        if not math.isfinite(now) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("journal clocks must be finite and monotonic")
        body = {
            "schema_version": "g25-application-journal-event-v1",
            "sequence": sequence + 1,
            "session_id": session["session_id"],
            "event_type": event_type,
            "cell_id": cell_id,
            "observed_epoch": now,
            "elapsed_seconds": elapsed,
            "payload": dict(payload or {}),
            "previous_event_sha256": previous,
        }
        event = {**body, "event_sha256": canonical_hash(body)}
        rendered = canonical_bytes(event) + b"\n"
        self.fault("before_journal_append")
        descriptor = os.open(
            self.journal_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            offset = 0
            while offset < len(rendered):
                written = os.write(descriptor, rendered[offset:])
                if written <= 0:
                    raise OSError("journal append made no forward progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(self.root)
        self.fault("after_journal_fsync")
        return event

    def transition_session(
        self, target: str, *, reason: str | None = None,
        audit_descriptor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if target not in SESSION_STATES:
            raise ValueError(f"unknown G2.5 session state: {target}")
        state = self.session_state()
        current = state["state"]
        if target not in SESSION_TRANSITIONS[current]:
            raise ValueError(f"invalid G2.5 session transition: {current} -> {target}")
        event = self.append_event(
            "SESSION_TRANSITION",
            payload={"from": current, "to": target, "reason": reason},
        )
        state["state"] = target
        state["terminal_reason"] = reason
        if audit_descriptor is not None:
            state["audit_descriptor"] = dict(audit_descriptor)
        state["last_event_sha256"] = event["event_sha256"]
        self._write_state(self.session_state_path, state)
        return state

    def transition_cell(
        self,
        cell_id: str,
        target: str,
        *,
        reason: str | None = None,
        classification: str | None = None,
    ) -> dict[str, Any]:
        if target not in CELL_STATES:
            raise ValueError(f"unknown G2.5 cell state: {target}")
        state = self.cell_state(cell_id)
        current = state["state"]
        if target not in CELL_TRANSITIONS[current]:
            raise ValueError(f"invalid G2.5 cell transition: {current} -> {target}")
        if target == "RECORDED" and not state.get("raw_descriptor"):
            raise ValueError("RECORDED cell requires authoritative raw evidence")
        if target == "RECORDED" and not (classification or state.get("classification")):
            raise ValueError("RECORDED cell requires a classification")
        event = self.append_event(
            "CELL_TRANSITION",
            cell_id=cell_id,
            payload={
                "from": current,
                "to": target,
                "reason": reason,
                "classification": classification,
            },
        )
        state["state"] = target
        state["reason"] = reason
        if classification is not None:
            state["classification"] = classification
        state["last_event_sha256"] = event["event_sha256"]
        self._write_state(self._cell_path(cell_id), state)
        return state

    def write_raw(self, cell_id: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        state = self.cell_state(cell_id)
        if state["state"] != "PROCESS_EXITED":
            raise ValueError("raw evidence may only follow PROCESS_EXITED")
        path = self.raw_dir / f"{cell_id}.json"
        if path.exists():
            raise FileExistsError("raw evidence is immutable")
        atomic_json(path, dict(evidence))
        descriptor = {
            "path": f"raw/{path.name}",
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        event = self.append_event(
            "RAW_SAVED", cell_id=cell_id, payload=descriptor,
        )
        state["state"] = "RAW_SAVED"
        state["raw_descriptor"] = descriptor
        state["last_event_sha256"] = event["event_sha256"]
        self._write_state(self._cell_path(cell_id), state)
        return descriptor

    def mark_unfinished_cells(self, reason: str) -> list[str]:
        changed: list[str] = []
        for cell_id in self.session_state()["expected_cell_ids"]:
            state = self.cell_state(cell_id)
            if state["state"] not in {"RECORDED", "INCOMPLETE"}:
                self.transition_cell(cell_id, "INCOMPLETE", reason=reason)
                changed.append(cell_id)
        return changed


def _safe_raw(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("raw descriptor path is unsafe")
    resolved = (root / candidate).resolve(strict=True)
    resolved.relative_to(root.resolve())
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError("raw descriptor does not name a regular file")
    return resolved


def _audit_worker_io_manifest(
    root: Path, cell_id: str, expected_sha256: Any, raw_value: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        return [f"worker I/O manifest hash is missing: {cell_id}"]
    relative = f"worker_io/{cell_id}/io_manifest.json"
    try:
        path = _safe_raw(root, relative)
        if sha256_file(path) != expected_sha256:
            findings.append(f"worker I/O manifest hash drift: {cell_id}")
        value = _read_object(path)
        if (
            set(value) != {"schema_version", "session_id", "cell_id", "artifacts"}
            or value.get("schema_version") != "g25-worker-io-manifest-v1"
            or value.get("session_id") != APPLICATION_SESSION_ID
            or value.get("cell_id") != cell_id
            or not isinstance(value.get("artifacts"), dict)
        ):
            return findings + [f"worker I/O manifest shape differs: {cell_id}"]
        artifacts = value["artifacts"]
        expected_paths = {
            "cell_descriptor": f"descriptors/{cell_id}.json",
            "worker_evidence": f"worker_io/{cell_id}/worker_evidence.json",
            "supervisor": f"worker_io/{cell_id}/supervisor.json",
            "stdout_log": f"worker_io/{cell_id}/stdout.log",
            "stderr_log": f"worker_io/{cell_id}/stderr.log",
            "parent_runtime_closure": (
                f"worker_io/{cell_id}/parent_runtime_closure.json"
            ),
        }
        if set(artifacts) != set(expected_paths):
            return findings + [f"worker I/O artifact set differs: {cell_id}"]
        resolved_artifacts: dict[str, Path | None] = {}
        for name, artifact_relative in expected_paths.items():
            descriptor = artifacts[name]
            if not isinstance(descriptor, dict) or set(descriptor) != {"bytes", "sha256"}:
                findings.append(f"worker I/O descriptor differs: {cell_id}/{name}")
                continue
            if descriptor["bytes"] is None and descriptor["sha256"] is None:
                if name != "worker_evidence" or (root / artifact_relative).exists():
                    findings.append(f"worker I/O artifact unexpectedly absent: {cell_id}/{name}")
                resolved_artifacts[name] = None
                continue
            try:
                artifact = _safe_raw(root, artifact_relative)
                resolved_artifacts[name] = artifact
                if artifact.stat().st_size != descriptor["bytes"]:
                    findings.append(f"worker I/O byte count drift: {cell_id}/{name}")
                if sha256_file(artifact) != descriptor["sha256"]:
                    findings.append(f"worker I/O hash drift: {cell_id}/{name}")
            except Exception as exc:
                findings.append(f"worker I/O artifact invalid {cell_id}/{name}: {exc}")
        if findings:
            return findings

        descriptor_path = resolved_artifacts["cell_descriptor"]
        supervisor_path = resolved_artifacts["supervisor"]
        stdout_path = resolved_artifacts["stdout_log"]
        stderr_path = resolved_artifacts["stderr_log"]
        if not all((descriptor_path, supervisor_path, stdout_path, stderr_path)):
            return findings + [f"worker I/O replay inputs are incomplete: {cell_id}"]
        descriptor = _read_object(descriptor_path)
        supervisor = _read_object(supervisor_path)
        stdout = stdout_path.read_text(encoding="utf-8")
        stderr = stderr_path.read_text(encoding="utf-8")
        evidence_path = resolved_artifacts["worker_evidence"]
        evidence_payload = _read_object(evidence_path) if evidence_path is not None else None
        if supervisor.get("stdout_sha256") != hashlib.sha256(
            stdout.encode("utf-8")
        ).hexdigest():
            findings.append(f"supervisor/stdout hash differs: {cell_id}")
        if supervisor.get("stderr_sha256") != hashlib.sha256(
            stderr.encode("utf-8")
        ).hexdigest():
            findings.append(f"supervisor/stderr hash differs: {cell_id}")
        actual_evidence_hash = sha256_file(evidence_path) if evidence_path else None
        if supervisor.get("evidence_file_sha256") != actual_evidence_hash:
            findings.append(f"supervisor/evidence hash differs: {cell_id}")

        model_inventory = _read_object(root / "model_snapshot_inventory.json")
        expected_argv = [
            "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
            "--inhibit-cache",
            "/usr/bin/python3",
            "-I", "-S", "-B", "-X", "utf8",
            str((
                root / "snapshots/package/scripts/g25_isolated_bootstrap.py"
            ).resolve()),
            "worker",
            "--cell-descriptor", str(descriptor_path.resolve()),
            "--evidence-out", str((root / expected_paths["worker_evidence"]).resolve()),
            "--model-snapshot", model_inventory["absolute_path"],
        ]
        if supervisor.get("argv") != expected_argv:
            findings.append(f"supervisor worker argv differs: {cell_id}")
        parent = raw_value.get("parent_process")
        if not isinstance(parent, Mapping):
            findings.append(f"raw parent process is missing: {cell_id}")
        elif parent.get("worker_argv_sha256") != canonical_hash(expected_argv):
            findings.append(f"raw/supervisor worker argv hash differs: {cell_id}")

        try:
            from scripts.g25_qualification import normalize_worker_process_result

            replay_input = {
                **supervisor,
                "stdout": stdout,
                "stderr": stderr,
                "evidence_payload": evidence_payload,
                "io_manifest_sha256": expected_sha256,
            }
            rebuilt = normalize_worker_process_result(
                replay_input,
                selection=descriptor["selection"],
                ceiling=descriptor["ceiling"],
            )
            if rebuilt != dict(raw_value):
                findings.append(f"worker evidence/parent raw replay differs: {cell_id}")
        except Exception as exc:
            findings.append(f"worker raw replay failed {cell_id}: {exc}")
    except Exception as exc:
        findings.append(f"worker I/O manifest invalid {cell_id}: {exc}")
    return findings


def audit_partial_session(root: Path) -> dict[str, Any]:
    """Audit whatever durable evidence exists; never issue a qualification PASS."""
    root = root.resolve(strict=True)
    findings: list[str] = []
    entries: list[dict[str, Any]] = []
    previous = ZERO_HASH
    replay_session_state = "CREATED"
    replay_session_event = ZERO_HASH
    replay_cell_states: dict[str, str] = {}
    replay_cell_events: dict[str, str] = {}
    replay_raw_descriptors: dict[str, dict[str, Any]] = {}
    replay_bound_artifacts: dict[str, dict[str, Any]] = {}
    try:
        for index, line in enumerate(
            (root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        ):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("journal event is not an object")
            body = {key: value for key, value in event.items() if key != "event_sha256"}
            if event.get("sequence") != index:
                findings.append(f"journal sequence mismatch at {index}")
            if event.get("previous_event_sha256") != previous:
                findings.append(f"journal previous hash mismatch at {index}")
            expected_hash = canonical_hash(body)
            if event.get("event_sha256") != expected_hash:
                findings.append(f"journal event hash mismatch at {index}")
            previous = str(event.get("event_sha256", ZERO_HASH))
            entries.append(event)
            event_type = event.get("event_type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            cell_id = event.get("cell_id")
            if event_type == "SESSION_CREATED":
                replay_session_state = "CREATED"
                replay_session_event = previous
            elif event_type == "SESSION_TRANSITION":
                if payload.get("from") != replay_session_state:
                    findings.append(f"journal session transition source mismatch at {index}")
                replay_session_state = str(payload.get("to"))
                replay_session_event = previous
            elif event_type == "CELL_TRANSITION" and isinstance(cell_id, str):
                current = replay_cell_states.get(cell_id, "PENDING")
                if payload.get("from") != current:
                    findings.append(f"journal cell transition source mismatch at {index}")
                replay_cell_states[cell_id] = str(payload.get("to"))
                replay_cell_events[cell_id] = previous
            elif event_type == "RAW_SAVED" and isinstance(cell_id, str):
                current = replay_cell_states.get(cell_id, "PENDING")
                if current != "PROCESS_EXITED":
                    findings.append(f"journal raw-save source mismatch at {index}")
                if cell_id in replay_raw_descriptors:
                    findings.append(f"journal has duplicate raw-save event: {cell_id}")
                if set(payload) != {"path", "bytes", "sha256"}:
                    findings.append(f"journal raw descriptor fields differ: {cell_id}")
                else:
                    replay_raw_descriptors[cell_id] = dict(payload)
                replay_cell_states[cell_id] = "RAW_SAVED"
                replay_cell_events[cell_id] = previous
            elif event_type in {
                "AUTHORIZATION_BOUND", "PACKAGE_SNAPSHOT_FROZEN",
                "MODEL_SNAPSHOT_BOUND", "DYNAMIC_PREFLIGHT_BOUND",
            }:
                if event_type in replay_bound_artifacts:
                    findings.append(f"journal has duplicate bound artifact: {event_type}")
                elif not isinstance(payload, dict):
                    findings.append(f"journal bound artifact payload is invalid: {event_type}")
                else:
                    replay_bound_artifacts[event_type] = dict(payload)
    except Exception as exc:
        findings.append(f"journal unreadable: {type(exc).__name__}: {exc}")

    session: dict[str, Any] = {}
    expected: list[str] = []
    recorded: list[str] = []
    incomplete: list[str] = []
    try:
        session = _read_object(root / "session_state.json")
        expected = list(session["expected_cell_ids"])
        if (
            len(expected) != 12
            or len(set(expected)) != 12
            or session.get("expected_cell_count") != 12
            or session.get("expected_cell_set_sha256") != canonical_hash(expected)
        ):
            findings.append("session expected cell set is not the frozen 12-cell shape")
        if entries and session.get("session_id") != entries[0].get("session_id"):
            findings.append("session identity differs from journal")
        if session.get("state") != replay_session_state:
            findings.append("materialized session state differs from journal")
        if session.get("last_event_sha256") != replay_session_event:
            findings.append("materialized session event head differs from journal")
        unexpected_raw_cells = sorted(set(replay_raw_descriptors) - set(expected))
        for cell_id in unexpected_raw_cells:
            findings.append(f"journal raw evidence is outside expected cell set: {cell_id}")
        expected_raw_paths: set[str] = set()
        for cell_id in expected:
            cell = _read_object(root / "cell_state" / f"{cell_id}.json")
            if cell.get("cell_id") != cell_id or cell.get("session_id") != session.get("session_id"):
                findings.append(f"cell identity drift: {cell_id}")
            if cell.get("state") != replay_cell_states.get(cell_id, "PENDING"):
                findings.append(f"materialized cell state differs from journal: {cell_id}")
            if cell.get("last_event_sha256") != replay_cell_events.get(cell_id, ZERO_HASH):
                findings.append(f"materialized cell event head differs from journal: {cell_id}")
            journal_descriptor = replay_raw_descriptors.get(cell_id)
            materialized_descriptor = cell.get("raw_descriptor")
            if journal_descriptor != materialized_descriptor:
                findings.append(f"materialized raw descriptor differs from journal: {cell_id}")
            if journal_descriptor is not None:
                try:
                    if (
                        set(journal_descriptor) != {"path", "bytes", "sha256"}
                        or journal_descriptor.get("path") != f"raw/{cell_id}.json"
                        or not isinstance(journal_descriptor.get("bytes"), int)
                        or isinstance(journal_descriptor.get("bytes"), bool)
                        or journal_descriptor["bytes"] <= 0
                        or not isinstance(journal_descriptor.get("sha256"), str)
                        or len(journal_descriptor["sha256"]) != 64
                    ):
                        raise ValueError("raw descriptor shape or identity differs")
                    raw = _safe_raw(root, journal_descriptor["path"])
                    expected_raw_paths.add(journal_descriptor["path"])
                    if raw.stat().st_size != journal_descriptor["bytes"]:
                        findings.append(f"raw byte count drift: {cell_id}")
                    if sha256_file(raw) != journal_descriptor["sha256"]:
                        findings.append(f"raw hash drift: {cell_id}")
                    if session.get("session_id") == APPLICATION_SESSION_ID:
                        raw_value = _read_object(raw)
                        parent = raw_value.get("parent_process")
                        if not isinstance(parent, dict):
                            findings.append(f"raw lacks parent supervision evidence: {cell_id}")
                        else:
                            findings.extend(_audit_worker_io_manifest(
                                root, cell_id, parent.get("io_manifest_sha256"), raw_value
                            ))
                except Exception as exc:
                    findings.append(f"raw evidence invalid {cell_id}: {exc}")
            if cell.get("state") == "RECORDED":
                if not isinstance(journal_descriptor, dict):
                    findings.append(f"recorded cell lacks raw descriptor: {cell_id}")
                    continue
                if not cell.get("classification"):
                    findings.append(f"recorded cell lacks classification: {cell_id}")
                recorded.append(cell_id)
            else:
                incomplete.append(cell_id)
        actual_raw_paths = {
            path.relative_to(root).as_posix()
            for path in (root / "raw").iterdir()
            if path.is_file() and not path.is_symlink()
        }
        if actual_raw_paths != expected_raw_paths:
            findings.append("raw file inventory differs from journal descriptors")
        required_bound = {
            "AUTHORIZATION_BOUND", "PACKAGE_SNAPSHOT_FROZEN",
            "MODEL_SNAPSHOT_BOUND", "DYNAMIC_PREFLIGHT_BOUND",
        }
        if len(recorded) == 12 and session.get("session_id") == APPLICATION_SESSION_ID:
            for event_type in sorted(required_bound - set(replay_bound_artifacts)):
                findings.append(f"complete session lacks bound artifact: {event_type}")
        for event_type, descriptor in sorted(replay_bound_artifacts.items()):
            if not {"path", "bytes", "sha256"}.issubset(descriptor):
                findings.append(f"bound artifact descriptor fields differ: {event_type}")
                continue
            try:
                artifact = _safe_raw(root, str(descriptor["path"]))
                if artifact.stat().st_size != descriptor["bytes"]:
                    findings.append(f"bound artifact byte count drift: {event_type}")
                if sha256_file(artifact) != descriptor["sha256"]:
                    findings.append(f"bound artifact hash drift: {event_type}")
            except Exception as exc:
                findings.append(f"bound artifact invalid {event_type}: {exc}")
        if "PACKAGE_SNAPSHOT_FROZEN" in replay_bound_artifacts:
            from .g25_snapshot import audit_package_snapshot

            findings.extend(
                f"package snapshot: {finding}"
                for finding in audit_package_snapshot(root)
            )
    except Exception as exc:
        findings.append(f"state unreadable: {type(exc).__name__}: {exc}")

    complete_shape = len(recorded) == 12 and not incomplete and not findings
    return {
        "schema_version": "g25-application-partial-audit-v1",
        "session_id": session.get("session_id", root.name),
        "status": "COMPLETE_SHAPE_AUDITED" if complete_shape else "PARTIAL_AUDIT",
        "journal_event_count": len(entries),
        "journal_head_sha256": previous if entries else ZERO_HASH,
        "expected_cell_count": 12,
        "recorded_cell_count": len(recorded),
        "recorded_cell_ids": recorded,
        "missing_or_incomplete_cell_ids": incomplete,
        "finding_count": len(findings),
        "findings": findings,
        "ledger_eligible": complete_shape,
        "qualification_pass": False,
        "formal_gate_pass": False,
        "gpu_pilot_authorized": False,
    }


def audit_finalized_application(
    root: Path,
    *,
    seal_anchor: Path | None = None,
    expected_anchor_sha256: str | None = None,
) -> dict[str, Any]:
    """Recompute the complete post-terminal chain; terminal fields are never trusted."""
    import jsonschema

    root = root.resolve(strict=True)
    partial = audit_partial_session(root)
    findings = list(partial.get("findings", []))
    terminal: dict[str, Any] = {}
    seal: dict[str, Any] = {}
    try:
        terminal = _read_object(root / "terminal.json")
        seal = _read_object(root / "final_seal.json")
        terminal_schema = json.loads(TERMINAL_SCHEMA_PATH.read_text(encoding="utf-8"))
        seal_schema = json.loads(FINAL_SEAL_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(terminal_schema).validate(terminal)
        jsonschema.Draft202012Validator(seal_schema).validate(seal)
    except Exception as exc:
        findings.append(f"terminal/final-seal schema failure: {type(exc).__name__}: {exc}")

    entries: list[dict[str, Any]] = []
    try:
        entries = [
            json.loads(line)
            for line in (root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not entries or entries[-1].get("event_type") != "TERMINAL_BOUND":
            findings.append("journal does not end at exactly one TERMINAL_BOUND event")
        if sum(item.get("event_type") == "TERMINAL_BOUND" for item in entries) != 1:
            findings.append("journal TERMINAL_BOUND event count differs")
    except Exception as exc:
        findings.append(f"terminal journal cannot be replayed: {type(exc).__name__}: {exc}")

    if terminal and seal and entries:
        event = entries[-1]
        payload = event.get("payload")
        if not isinstance(payload, dict) or set(payload) != {
            "terminal_transition_head_sha256", "artifacts"
        }:
            findings.append("TERMINAL_BOUND payload shape differs")
            payload = {}
        artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
        required_artifacts = {
            "terminal", "application_audit", "qualification_audit", "session_state",
            "qualification_evidence_inventory",
        }
        if not isinstance(artifacts, dict) or set(artifacts) != required_artifacts:
            findings.append("TERMINAL_BOUND artifact set differs")
            artifacts = {}
        if seal.get("artifacts") != artifacts:
            findings.append("final seal artifact descriptors differ from journal")
        if seal.get("terminal_bound_event_sha256") != event.get("event_sha256"):
            findings.append("final seal journal head differs")
        if seal.get("terminal_bound_payload_sha256") != canonical_hash(payload):
            findings.append("final seal payload hash differs")
        previous = event.get("previous_event_sha256")
        if terminal.get("journal_head_sha256") != previous:
            findings.append("terminal does not bind the terminal transition head")
        if payload.get("terminal_transition_head_sha256") != previous:
            findings.append("TERMINAL_BOUND predecessor differs")
        if seal.get("terminal_transition_head_sha256") != previous:
            findings.append("final seal terminal transition head differs")

        try:
            descriptor = seal.get("session_file_inventory")
            if not isinstance(descriptor, dict) or set(descriptor) != {
                "path", "bytes", "sha256"
            } or descriptor.get("path") != "session_file_inventory.json":
                raise ValueError("session file inventory descriptor differs")
            inventory_path = _safe_raw(root, "session_file_inventory.json")
            if inventory_path.stat().st_size != descriptor["bytes"]:
                raise ValueError("session file inventory byte count differs")
            if sha256_file(inventory_path) != descriptor["sha256"]:
                raise ValueError("session file inventory hash differs")
            sealed_session_inventory = _read_object(inventory_path)
            inventory_schema = json.loads(
                SESSION_FILE_INVENTORY_SCHEMA_PATH.read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(inventory_schema).validate(
                sealed_session_inventory
            )
            if seal.get("session_file_inventory_entries_sha256") != (
                sealed_session_inventory.get("entries_sha256")
            ):
                raise ValueError("session file inventory entry hash differs")
            live_session_inventory = session_file_inventory(root)
            if sealed_session_inventory != live_session_inventory:
                findings.append(
                    "sealed recursive session file inventory differs from live session"
                )
        except Exception as exc:
            findings.append(
                "recursive session file inventory replay failed: "
                f"{type(exc).__name__}: {exc}"
            )

        loaded_artifacts: dict[str, dict[str, Any]] = {}
        for name, descriptor in artifacts.items():
            try:
                if not isinstance(descriptor, dict) or set(descriptor) != {
                    "path", "bytes", "sha256"
                }:
                    raise ValueError("descriptor shape differs")
                artifact = _safe_raw(root, str(descriptor["path"]))
                if artifact.stat().st_size != descriptor["bytes"]:
                    findings.append(f"final artifact byte count drift: {name}")
                if sha256_file(artifact) != descriptor["sha256"]:
                    findings.append(f"final artifact hash drift: {name}")
                loaded_artifacts[name] = _read_object(artifact)
            except Exception as exc:
                findings.append(f"final artifact invalid {name}: {exc}")

        application_audit = loaded_artifacts.get("application_audit", {})
        qualification_audit = loaded_artifacts.get("qualification_audit", {})
        session_state = loaded_artifacts.get("session_state", {})
        sealed_evidence_inventory = loaded_artifacts.get(
            "qualification_evidence_inventory", {}
        )
        if loaded_artifacts.get("terminal") != terminal:
            findings.append("terminal descriptor does not resolve to terminal.json")
        if terminal.get("application_audit_sha256") != canonical_hash(application_audit):
            findings.append("terminal application audit hash differs")
        if terminal.get("qualification_audit_sha256") != canonical_hash(qualification_audit):
            findings.append("terminal qualification audit hash differs")
        if terminal.get("qualification_audit_artifact") != artifacts.get(
            "qualification_audit", {}
        ).get("path"):
            findings.append("terminal qualification audit path differs")
        if terminal.get("session_state_sha256") != artifacts.get(
            "session_state", {}
        ).get("sha256"):
            findings.append("terminal session-state hash differs")
        if terminal.get("session_state") != session_state.get("state"):
            findings.append("terminal session state differs")

        try:
            live_evidence_inventory = qualification_evidence_inventory(root)
            if sealed_evidence_inventory != live_evidence_inventory:
                findings.append(
                    "sealed qualification evidence inventory differs from live artifacts"
                )
            if (
                live_evidence_inventory.get("session") is None
                or live_evidence_inventory.get("ledger") is None
                or live_evidence_inventory.get("verdict") is None
                or len(live_evidence_inventory.get("cells", [])) != 12
                or len(live_evidence_inventory.get("raw", [])) != 12
            ):
                findings.append("qualification evidence inventory is not complete")
        except Exception as exc:
            findings.append(
                "qualification evidence inventory replay failed: "
                f"{type(exc).__name__}: {exc}"
            )

        try:
            from scripts.g25_qualification import audit_session

            replayed_qualification_audit = audit_session(root)
            if replayed_qualification_audit != qualification_audit:
                findings.append(
                    "live qualification audit replay differs from sealed audit"
                )
        except Exception as exc:
            findings.append(
                f"live qualification audit failed: {type(exc).__name__}: {exc}"
            )

        application_clean = bool(
            application_audit.get("status") == "COMPLETE_SHAPE_AUDITED"
            and application_audit.get("ledger_eligible") is True
            and not application_audit.get("findings")
        )
        qualification_clean = bool(
            qualification_audit.get("status") == "complete"
            and not qualification_audit.get("findings")
        )
        derived_pass = bool(
            terminal.get("selection_pass") is True
            and terminal.get("disposition") == "EXECUTION_COMPLETE"
            and terminal.get("session_state") == "TERMINAL_COMPLETE"
            and terminal.get("gpu_cells") == 12
            and terminal.get("gpu_used") is True
            and terminal.get("deadline_ok") is True
            and application_clean
            and qualification_clean
            and terminal.get("formal_g3_r5_authorized") is False
            and terminal.get("paid_gpu_authorized") is False
            and terminal.get("resume") is False
            and terminal.get("retry_failed") is False
        )
        if terminal.get("application_audit_clean") is not application_clean:
            findings.append("terminal application-audit disposition differs")
        if terminal.get("qualification_audit_clean") is not qualification_clean:
            findings.append("terminal qualification-audit disposition differs")
        if terminal.get("gpu_used") is not (terminal.get("gpu_cells") > 0):
            findings.append("terminal GPU usage fields differ")
        if terminal.get("qualification_pass") is not derived_pass:
            findings.append("terminal qualification PASS is not derivable")
    else:
        derived_pass = False

    anchor_verified = False
    if seal:
        try:
            verify_external_seal_anchor(
                root,
                seal,
                anchor_path=seal_anchor,
                expected_anchor_sha256=expected_anchor_sha256,
            )
            anchor_verified = True
        except Exception as exc:
            findings.append(
                f"external seal anchor verification failed: {type(exc).__name__}: {exc}"
            )

    qualification_pass = bool(derived_pass and not findings)
    return {
        "schema_version": "g25-application-final-audit-v1",
        "session_id": terminal.get("session_id", root.name),
        "status": "complete" if qualification_pass else "incomplete",
        "finding_count": len(findings),
        "findings": findings,
        "partial_audit": partial,
        "terminal_sha256": sha256_file(root / "terminal.json")
        if (root / "terminal.json").is_file() else None,
        "final_seal_sha256": sha256_file(root / "final_seal.json")
        if (root / "final_seal.json").is_file() else None,
        "external_seal_anchor_verified": anchor_verified,
        "expected_external_seal_anchor_sha256": expected_anchor_sha256,
        "qualification_pass": qualification_pass,
        "formal_gate_pass": False,
        "paid_gpu_authorized": False,
    }
