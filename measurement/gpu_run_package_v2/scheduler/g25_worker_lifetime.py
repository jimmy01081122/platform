"""Linux-only worker lifetime and parent-death enforcement for G2.5."""

from __future__ import annotations

import ctypes
import errno
import os
import select
import signal
import stat
import time
from pathlib import Path
from typing import Any

PR_SET_PDEATHSIG = 1
PR_GET_PDEATHSIG = 2
READY_BYTE = b"R"
GO_BYTE = b"G"
ACK_BYTE = b"A"
GUARD_INT_ENV_KEYS = (
    "G25_EXPECTED_PARENT_PID",
    "G25_EXPECTED_PARENT_START_TICKS",
    "G25_INHERITED_LEASE_FD",
    "G25_INHERITED_LEASE_DEVICE",
    "G25_INHERITED_LEASE_INODE",
    "G25_WORKER_READY_FD",
    "G25_WORKER_GO_FD",
    "G25_WORKER_ACK_FD",
    "G25_EXPECTED_CGROUP_DEVICE",
    "G25_EXPECTED_CGROUP_INODE",
)
GUARD_TEXT_ENV_KEYS = (
    "G25_EXPECTED_CGROUP_PATH",
    "G25_EXPECTED_CGROUP_MOUNTPOINT",
)
GUARD_ENV_KEYS = GUARD_INT_ENV_KEYS + GUARD_TEXT_ENV_KEYS


class WorkerLifetimeError(RuntimeError):
    pass


def read_process_start_ticks(pid: int) -> int:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise WorkerLifetimeError("process PID must be a positive integer")
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError as error:
        raise WorkerLifetimeError(f"cannot read process identity for PID {pid}") from error
    closing = value.rfind(")")
    fields = value[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise WorkerLifetimeError(f"malformed /proc/{pid}/stat")
    try:
        ticks = int(fields[19])
    except ValueError as error:
        raise WorkerLifetimeError(f"invalid start ticks for PID {pid}") from error
    if ticks <= 0:
        raise WorkerLifetimeError(f"non-positive start ticks for PID {pid}")
    return ticks


def process_group_members(pgid: int) -> list[int]:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        raise WorkerLifetimeError("process group ID must be a positive integer")
    members: list[int] = []
    for candidate in Path("/proc").iterdir():
        if not candidate.name.isdecimal():
            continue
        try:
            value = (candidate / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        closing = value.rfind(")")
        fields = value[closing + 2 :].split() if closing >= 0 else []
        if len(fields) < 3:
            continue
        try:
            member_pgrp = int(fields[2])
        except ValueError:
            continue
        if member_pgrp == pgid:
            members.append(int(candidate.name))
    return sorted(members)


def assert_process_group_empty(pgid: int) -> None:
    members = process_group_members(pgid)
    if members:
        raise WorkerLifetimeError(
            f"worker process group {pgid} still has members: {members}"
        )


def kill_and_drain_process_group(pgid: int, *, timeout_seconds: float = 5.0) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout_seconds
    while True:
        members = process_group_members(pgid)
        if not members:
            return
        if time.monotonic() >= deadline:
            raise WorkerLifetimeError(
                f"worker process group {pgid} remained populated after SIGKILL: {members}"
            )
        time.sleep(0.01)


def _prctl(option: int, argument: int | ctypes._SimpleCData) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.prctl
    function.argtypes = [
        ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong
    ]
    function.restype = ctypes.c_int
    value = argument if isinstance(argument, int) else ctypes.addressof(argument)
    result = int(function(option, value, 0, 0, 0))
    if result != 0:
        error_number = ctypes.get_errno()
        raise WorkerLifetimeError(
            f"prctl({option}) failed: {os.strerror(error_number)}"
        )
    return result


def install_parent_death_guard_from_environment() -> dict[str, Any]:
    """Install PDEATHSIG and cross the parent-controlled cgroup barrier."""
    try:
        values = {key: int(os.environ[key]) for key in GUARD_INT_ENV_KEYS}
        text_values = {key: os.environ[key] for key in GUARD_TEXT_ENV_KEYS}
    except (KeyError, TypeError, ValueError) as error:
        raise WorkerLifetimeError("worker lifetime guard environment is incomplete") from error
    expected_parent = values["G25_EXPECTED_PARENT_PID"]
    expected_start = values["G25_EXPECTED_PARENT_START_TICKS"]
    lease_fd = values["G25_INHERITED_LEASE_FD"]
    expected_device = values["G25_INHERITED_LEASE_DEVICE"]
    expected_inode = values["G25_INHERITED_LEASE_INODE"]
    ready_fd = values["G25_WORKER_READY_FD"]
    go_fd = values["G25_WORKER_GO_FD"]
    ack_fd = values["G25_WORKER_ACK_FD"]
    expected_cgroup_device = values["G25_EXPECTED_CGROUP_DEVICE"]
    expected_cgroup_inode = values["G25_EXPECTED_CGROUP_INODE"]
    expected_cgroup_path = text_values["G25_EXPECTED_CGROUP_PATH"]
    cgroup_mountpoint = Path(text_values["G25_EXPECTED_CGROUP_MOUNTPOINT"])
    if min(
        expected_parent, expected_start, lease_fd, expected_device,
        expected_inode, ready_fd, go_fd, ack_fd,
        expected_cgroup_device, expected_cgroup_inode,
    ) <= 0:
        raise WorkerLifetimeError("worker lifetime guard values must be positive")
    if (
        not expected_cgroup_path.startswith("/")
        or ".." in Path(expected_cgroup_path).parts
        or not cgroup_mountpoint.is_absolute()
    ):
        raise WorkerLifetimeError("worker cgroup identity is malformed")
    if os.getppid() != expected_parent:
        raise WorkerLifetimeError("worker parent PID differs before PDEATHSIG installation")
    if read_process_start_ticks(expected_parent) != expected_start:
        raise WorkerLifetimeError("worker parent identity differs before PDEATHSIG installation")
    try:
        lease_stat = os.fstat(lease_fd)
    except OSError as error:
        raise WorkerLifetimeError("worker did not inherit the execution lease FD") from error
    if (
        not stat.S_ISREG(lease_stat.st_mode)
        or lease_stat.st_dev != expected_device
        or lease_stat.st_ino != expected_inode
    ):
        raise WorkerLifetimeError("inherited execution lease identity differs")

    _prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL))
    observed_signal = ctypes.c_int(0)
    _prctl(PR_GET_PDEATHSIG, observed_signal)
    if observed_signal.value != int(signal.SIGKILL):
        raise WorkerLifetimeError("PR_GET_PDEATHSIG did not return SIGKILL")
    # Close the race where the parent exits between the first check and prctl.
    if (
        os.getppid() != expected_parent
        or read_process_start_ticks(expected_parent) != expected_start
    ):
        raise WorkerLifetimeError("worker parent identity changed during guard installation")
    try:
        if os.write(ready_fd, READY_BYTE) != len(READY_BYTE):
            raise WorkerLifetimeError("worker ready handshake was incomplete")
    except OSError as error:
        if error.errno != errno.EPIPE:
            raise WorkerLifetimeError("worker ready handshake failed") from error
        raise WorkerLifetimeError("worker supervisor disappeared before ready") from error
    finally:
        os.close(ready_fd)
    readable, _writable, _exceptional = select.select([go_fd], [], [], 10.0)
    received_go = os.read(go_fd, 2) if readable else b""
    os.close(go_fd)
    if received_go != GO_BYTE:
        raise WorkerLifetimeError("worker did not receive the cgroup execution grant")
    try:
        cgroup_rows = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise WorkerLifetimeError("worker cannot read its cgroup identity") from error
    if cgroup_rows != [f"0::{expected_cgroup_path}"]:
        raise WorkerLifetimeError("worker cgroup membership differs after execution grant")
    descriptor = os.open(
        cgroup_mountpoint,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        for component in Path(expected_cgroup_path.lstrip("/")).parts:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        identity = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    if (
        identity.st_dev != expected_cgroup_device
        or identity.st_ino != expected_cgroup_inode
    ):
        raise WorkerLifetimeError("worker cgroup device/inode differs after move")
    for key in GUARD_ENV_KEYS:
        os.environ.pop(key, None)
    try:
        if os.write(ack_fd, ACK_BYTE) != len(ACK_BYTE):
            raise WorkerLifetimeError("worker cgroup acknowledgement was incomplete")
    except OSError as error:
        raise WorkerLifetimeError("worker cgroup acknowledgement failed") from error
    finally:
        os.close(ack_fd)
    return {
        "schema_version": "g25-worker-lifetime-guard-v2",
        "mechanism": "systemd-delegated-cgroup-v2+pdeathsig-v2",
        "expected_parent_pid": expected_parent,
        "expected_parent_start_ticks": expected_start,
        "lease_fd": lease_fd,
        "lease_device": expected_device,
        "lease_inode": expected_inode,
        "pdeathsig": "SIGKILL",
        "pdeathsig_number": int(signal.SIGKILL),
        "ready_observed": True,
        "move_observed": True,
        "go_received": True,
        "membership_acknowledged": True,
        "cell_cgroup_path": expected_cgroup_path,
        "cell_cgroup_device": expected_cgroup_device,
        "cell_cgroup_inode": expected_cgroup_inode,
    }
