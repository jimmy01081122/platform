"""RUN_ROOT-wide nonblocking execution lock and owner evidence."""
from __future__ import annotations

import fcntl
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .store import atomic_json


class ExecutionLockBusy(RuntimeError):
    pass


@dataclass
class ExecutionLease:
    owner: dict[str, Any]
    _fd: int
    active: bool = True

    def fileno(self) -> int:
        if not self.active:
            raise RuntimeError("execution lease is no longer active")
        return self._fd

    def __getitem__(self, key: str) -> Any:
        return self.owner[key]

    def assert_active(self) -> None:
        """Fail closed if the lease was released or its descriptor is invalid."""
        if not self.active:
            raise RuntimeError("execution lease is no longer active")
        os.fstat(self._fd)

    def inheritance_descriptor(self) -> dict[str, int]:
        self.assert_active()
        identity = os.fstat(self._fd)
        return {
            "fd": self._fd,
            "device": int(identity.st_dev),
            "inode": int(identity.st_ino),
            "owner_pid": int(self.owner["pid"]),
            "owner_start_ticks": int(self.owner["start_ticks"]),
        }


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return "unavailable"


def _process_start_ticks(pid: int) -> int:
    value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    closing = value.rfind(")")
    fields = value[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise RuntimeError("cannot establish execution-lock owner start ticks")
    return int(fields[19])


def read_owner(run_root: Path) -> dict[str, Any] | None:
    path = run_root / ".gpu-execution.owner.json"
    try:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@contextmanager
def execution_lock(run_root: Path) -> Iterator[ExecutionLease]:
    """Hold the one GPU execution lease shared by every session."""
    run_root.mkdir(parents=True, exist_ok=True)
    lock_path = run_root / ".gpu-execution.lock"
    owner_path = run_root / ".gpu-execution.owner.json"
    with lock_path.open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExecutionLockBusy("GPU execution lock is already held") from exc
        lock_identity = os.fstat(stream.fileno())
        owner = {
            "schema_version": "gpu-execution-owner-v2",
            "pid": os.getpid(),
            "start_ticks": _process_start_ticks(os.getpid()),
            "host": socket.gethostname(),
            "boot_id": _boot_id(),
            "started_epoch": time.time(),
            "lock_device": int(lock_identity.st_dev),
            "lock_inode": int(lock_identity.st_ino),
        }
        atomic_json(owner_path, owner)
        lease = ExecutionLease(owner, stream.fileno())
        try:
            yield lease
        finally:
            lease.active = False
            try:
                owner_path.unlink(missing_ok=True)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
