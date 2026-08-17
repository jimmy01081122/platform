#!/usr/bin/env python3
"""Linux process-tree containment for fail-closed Phase 7 execution."""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from explorations.moe_cycle_simulator.phase7.application.executor.common import M0Error


PR_SET_CHILD_SUBREAPER = 36


@dataclass(frozen=True, order=True)
class ProcessIdentity:
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class ProcessRecord:
    identity: ProcessIdentity
    ppid: int
    process_group: int
    state: str


def enable_child_subreaper() -> None:
    """Make orphaned grandchildren observable instead of allowing daemon escape."""

    if sys.platform != "linux":
        raise M0Error("formal Phase 7 process containment requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise M0Error(f"cannot enable child subreaper: errno={code}")


def _read_record(pid: int) -> ProcessRecord | None:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    close = raw.rfind(")")
    if close < 0:
        raise M0Error(f"malformed /proc stat for pid {pid}")
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        raise M0Error(f"truncated /proc stat for pid {pid}")
    try:
        return ProcessRecord(
            identity=ProcessIdentity(pid=pid, start_ticks=int(fields[19])),
            state=fields[0],
            ppid=int(fields[1]),
            process_group=int(fields[2]),
        )
    except ValueError as exc:
        raise M0Error(f"invalid /proc stat for pid {pid}") from exc


def process_identity(pid: int) -> ProcessIdentity:
    record = _read_record(pid)
    if record is None:
        raise M0Error(f"process disappeared before containment: pid={pid}")
    return record.identity


def _snapshot() -> dict[int, ProcessRecord]:
    result: dict[int, ProcessRecord] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise M0Error(f"cannot enumerate /proc: {exc}") from exc
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        record = _read_record(int(entry.name))
        if record is not None:
            result[record.identity.pid] = record
    return result


def _descendants(
    records: dict[int, ProcessRecord], roots: Iterable[int]
) -> set[ProcessIdentity]:
    frontier = set(roots)
    found: set[ProcessIdentity] = set()
    while frontier:
        next_frontier: set[int] = set()
        for record in records.values():
            if record.ppid in frontier and record.identity not in found:
                found.add(record.identity)
                next_frontier.add(record.identity.pid)
        frontier = next_frontier
    return found


def _is_live(identity: ProcessIdentity) -> bool:
    record = _read_record(identity.pid)
    return (
        record is not None
        and record.identity == identity
        and record.state not in {"Z", "X"}
    )


def _signal(identity: ProcessIdentity, signum: int) -> None:
    record = _read_record(identity.pid)
    if (
        record is None
        or record.identity != identity
        or record.state in {"Z", "X"}
    ):
        return
    try:
        os.kill(identity.pid, signum)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise M0Error(
            f"cannot signal contained pid={identity.pid} with signal={signum}"
        ) from exc


class ProcessTreeContainment:
    """Track one subprocess and every descendant, including daemonized workers."""

    def __init__(self) -> None:
        enable_child_subreaper()
        before = _snapshot()
        owner_record = before.get(os.getpid())
        if owner_record is None:
            raise M0Error("cannot observe containment owner in /proc")
        self.owner = owner_record.identity
        self.owner_process_group = owner_record.process_group
        self.baseline_descendants = _descendants(before, {self.owner.pid})
        self.root: ProcessIdentity | None = None
        self.root_process_group: int | None = None
        self.observed: set[ProcessIdentity] = set()

    def attach(self, pid: int) -> None:
        if self.root is not None:
            raise M0Error("process containment already has a root")
        record = _read_record(pid)
        if record is None:
            raise M0Error("subprocess disappeared before containment attachment")
        self.root = record.identity
        self.root_process_group = record.process_group
        self.observed.add(record.identity)

    def _targets(self) -> set[ProcessIdentity]:
        if self.root is None:
            return set()
        records = _snapshot()
        root_record = records.get(self.root.pid)
        if root_record is not None and root_record.identity == self.root:
            self.observed.add(self.root)
            self.observed.update(_descendants(records, {self.root.pid}))
        if (
            self.root_process_group is not None
            and self.root_process_group != self.owner_process_group
        ):
            self.observed.update(
                record.identity
                for record in records.values()
                if record.process_group == self.root_process_group
                and record.identity != self.owner
                and record.identity not in self.baseline_descendants
            )
        owner_record = records.get(self.owner.pid)
        if owner_record is not None and owner_record.identity == self.owner:
            adopted = _descendants(records, {self.owner.pid})
            self.observed.update(adopted - self.baseline_descendants)
        return {identity for identity in self.observed if _is_live(identity)}

    def _reap(self, identities: Iterable[ProcessIdentity]) -> None:
        for identity in identities:
            if identity == self.root:
                # subprocess.Popen remains the sole waiter for the direct child.
                continue
            try:
                os.waitpid(identity.pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def terminate(
        self, *, term_grace_seconds: float = 10.0, kill_grace_seconds: float = 10.0
    ) -> dict[str, object]:
        """Terminate and prove absence of all observed or newly adopted workers."""

        if self.root is None:
            return {
                "status": "NOT_STARTED",
                "term_signaled_pids": [],
                "kill_signaled_pids": [],
                "surviving_pids": [],
            }
        term_signaled: set[int] = set()
        kill_signaled: set[int] = set()
        deadline = time.monotonic() + term_grace_seconds
        while True:
            targets = self._targets()
            self._reap(self.observed)
            if not targets:
                break
            for identity in sorted(targets, reverse=True):
                _signal(identity, signal.SIGTERM)
                term_signaled.add(identity.pid)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        deadline = time.monotonic() + kill_grace_seconds
        while True:
            targets = self._targets()
            self._reap(self.observed)
            if not targets:
                break
            for identity in sorted(targets, reverse=True):
                _signal(identity, signal.SIGKILL)
                kill_signaled.add(identity.pid)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        self._reap(self.observed)
        survivors = sorted(identity.pid for identity in self._targets())
        if survivors:
            raise M0Error(f"contained process tree survived SIGKILL: {survivors}")
        return {
            "status": "CLEAN",
            "term_signaled_pids": sorted(term_signaled),
            "kill_signaled_pids": sorted(kill_signaled),
            "surviving_pids": [],
        }

    def assert_clean(
        self,
        *,
        term_grace_seconds: float = 10.0,
        kill_grace_seconds: float = 10.0,
    ) -> dict[str, object]:
        """Clean residual workers even when the direct child exited successfully."""

        return self.terminate(
            term_grace_seconds=term_grace_seconds,
            kill_grace_seconds=kill_grace_seconds,
        )
