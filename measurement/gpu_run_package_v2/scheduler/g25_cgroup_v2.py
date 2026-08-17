"""Fail-closed systemd/cgroup-v2 containment for G2.5 GPU execution.

The production factory accepts only a fixed delegated transient service.  Test
fixtures may instantiate ``CgroupV2Controller`` directly against a temporary
filesystem; the production discovery path never falls back to that fixture.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence


SYSTEMD_RUN = Path("/usr/bin/systemd-run")
SYSTEMCTL = Path("/usr/bin/systemctl")
APPLICATION_UNIT = "edgehetero-gpu-execution.service"
TERM_GRACE_SECONDS = 30
KILL_DRAIN_SECONDS = 5
SYSTEMD_PROPERTIES = (
    "Delegate=yes",
    "KillMode=control-group",
    "KillSignal=SIGTERM",
    "FinalKillSignal=SIGKILL",
    "SendSIGKILL=yes",
    "TimeoutStopSec=30s",
    "RuntimeMaxSec=7500s",
    "OOMPolicy=kill",
    "Restart=no",
    "TasksMax=512",
)
_CELL_ID = re.compile(r"[0-9a-f]{64}")


class CgroupV2Error(RuntimeError):
    pass


class CgroupUnavailable(CgroupV2Error):
    pass


class CgroupDrainError(CgroupV2Error):
    pass


@dataclass(frozen=True)
class ApplicationCgroupEvidence:
    mountpoint: str
    relative_path: str
    unit: str
    device: int
    inode: int
    cgroup_type: str
    kill_supported: bool
    delegated: bool
    systemd_properties: dict[str, str]


@dataclass
class CellCgroup:
    controller: "CgroupV2Controller"
    cell_id: str
    name: str
    relative_path: str
    path: Path
    dir_fd: int
    procs_fd: int
    events_fd: int
    kill_fd: int
    device: int
    inode: int
    moved_pid: int | None = None
    moved_start_ticks: int | None = None
    populated_zero_observed: bool = False
    closed: bool = False


@dataclass(frozen=True)
class DrainEvidence:
    initial_populated: int
    term_sent: bool
    term_sent_monotonic_ns: int | None
    term_grace_seconds: int
    cgroup_kill_written: bool
    cgroup_kill_monotonic_ns: int | None
    populated_zero_monotonic_ns: int
    final_populated: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "initial_populated": self.initial_populated,
            "term_sent": self.term_sent,
            "term_sent_monotonic_ns": self.term_sent_monotonic_ns,
            "term_grace_seconds": self.term_grace_seconds,
            "cgroup_kill_written": self.cgroup_kill_written,
            "cgroup_kill_monotonic_ns": self.cgroup_kill_monotonic_ns,
            "populated_zero_monotonic_ns": self.populated_zero_monotonic_ns,
            "final_populated": self.final_populated,
        }


def build_systemd_run_argv(inner_argv: Sequence[str]) -> list[str]:
    """Build the sole approved outer service wrapper for GPU-capable work."""
    if not inner_argv or any(not isinstance(value, str) or not value for value in inner_argv):
        raise CgroupV2Error("inner application argv is empty or malformed")
    return [
        str(SYSTEMD_RUN),
        "--user",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={APPLICATION_UNIT}",
        "--service-type=exec",
        *[f"--property={value}" for value in SYSTEMD_PROPERTIES],
        "--",
        *inner_argv,
    ]


def _unescape_mount_field(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    return re.sub(r"\\([0-7]{3})", replace, value)


def _cgroup2_mountpoint(mountinfo: str) -> Path:
    matches: list[Path] = []
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        right_fields = right.split()
        left_fields = left.split()
        if right_fields and right_fields[0] == "cgroup2" and len(left_fields) >= 5:
            matches.append(Path(_unescape_mount_field(left_fields[4])))
    if len(matches) != 1 or not matches[0].is_absolute():
        raise CgroupUnavailable("exactly one absolute cgroup2 mount is required")
    return matches[0]


def _unified_cgroup_path(cgroup_text: str) -> str:
    rows = [line for line in cgroup_text.splitlines() if line]
    if len(rows) != 1 or not rows[0].startswith("0::/"):
        raise CgroupUnavailable("process is not in one unified cgroup-v2 hierarchy")
    value = rows[0][3:]
    candidate = PurePosixPath(value)
    if not value.startswith("/") or ".." in candidate.parts:
        raise CgroupUnavailable("process cgroup path is malformed")
    return value


def _parse_systemctl_show(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in result:
            raise CgroupUnavailable("systemd unit property output is malformed")
        result[key] = value
    return result


def _timespan_seconds(value: str) -> float:
    if value.isdecimal():
        return int(value) / 1_000_000.0
    units = {
        "us": 0.000001,
        "ms": 0.001,
        "s": 1.0,
        "min": 60.0,
        "h": 3600.0,
        "d": 86400.0,
    }
    position = 0
    total = 0.0
    for match in re.finditer(r"(?:^|\s)([0-9]+(?:\.[0-9]+)?)(us|ms|s|min|h|d)", value):
        if value[position:match.start()].strip():
            raise CgroupUnavailable("systemd duration property is malformed")
        total += float(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position == 0 or value[position:].strip():
        raise CgroupUnavailable("systemd duration property is malformed")
    return total


def attest_systemd_properties(
    properties: Mapping[str, str], *, expected_cgroup: str
) -> dict[str, str]:
    required = {
        "ActiveState", "SubState", "Delegate", "KillMode", "KillSignal",
        "FinalKillSignal", "SendSIGKILL", "TimeoutStopUSec",
        "RuntimeMaxUSec", "OOMPolicy", "Restart", "TasksMax", "ControlGroup",
    }
    if set(properties) != required:
        raise CgroupUnavailable("systemd unit property set differs")
    exact = {
        "ActiveState": "active",
        "SubState": "running",
        "Delegate": "yes",
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "OOMPolicy": "kill",
        "Restart": "no",
        "TasksMax": "512",
        "ControlGroup": expected_cgroup,
    }
    for key, expected in exact.items():
        if properties.get(key) != expected:
            raise CgroupUnavailable(f"systemd unit property differs: {key}")
    if properties["KillSignal"] not in {"15", "SIGTERM"}:
        raise CgroupUnavailable("systemd KillSignal differs")
    if properties["FinalKillSignal"] not in {"9", "SIGKILL"}:
        raise CgroupUnavailable("systemd FinalKillSignal differs")
    if _timespan_seconds(properties["TimeoutStopUSec"]) != 30.0:
        raise CgroupUnavailable("systemd TimeoutStopSec differs")
    if _timespan_seconds(properties["RuntimeMaxUSec"]) != 7500.0:
        raise CgroupUnavailable("systemd RuntimeMaxSec differs")
    return dict(properties)


def _read_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as error:
        raise CgroupV2Error("cgroup control file is not UTF-8") from error


def _write_fd(fd: int, value: bytes) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        pass
    total = 0
    while total < len(value):
        written = os.write(fd, value[total:])
        if written <= 0:
            raise CgroupV2Error("cgroup control write was incomplete")
        total += written


def _parse_populated(value: str) -> int:
    rows = {}
    for line in value.splitlines():
        key, separator, item = line.partition(" ")
        if not separator or key in rows:
            raise CgroupV2Error("cgroup.events is malformed")
        rows[key] = item
    if rows.get("populated") not in {"0", "1"}:
        raise CgroupV2Error("cgroup.events lacks a valid populated state")
    return int(rows["populated"])


def _parse_pids(value: str) -> list[int]:
    try:
        result = sorted({int(row) for row in value.splitlines() if row})
    except ValueError as error:
        raise CgroupV2Error("cgroup.procs contains a non-PID") from error
    if any(pid <= 0 for pid in result):
        raise CgroupV2Error("cgroup.procs contains a non-positive PID")
    return result


def _open_dir_chain(root: Path, relative: str) -> int:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in PurePosixPath(relative.lstrip("/")).parts:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class CgroupV2Controller:
    """Own per-cell leaves below one attested delegated systemd service."""

    def __init__(
        self,
        *,
        mountpoint: Path,
        relative_path: str,
        unit: str,
        root_fd: int,
        systemd_properties: Mapping[str, str],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        signal_sender: Callable[[int, int], None] = os.kill,
        process_start_ticks: Callable[[int], int] | None = None,
        process_cgroup_reader: Callable[[int], str] | None = None,
        cell_initializer: Callable[[Path], None] | None = None,
        cell_finalizer: Callable[[Path], None] | None = None,
    ) -> None:
        self.mountpoint = mountpoint
        self.relative_path = relative_path.rstrip("/") or "/"
        self.unit = unit
        self.root_fd = root_fd
        self.root_path = mountpoint / self.relative_path.lstrip("/")
        identity = os.fstat(root_fd)
        self.evidence = ApplicationCgroupEvidence(
            mountpoint=str(mountpoint),
            relative_path=self.relative_path,
            unit=unit,
            device=int(identity.st_dev),
            inode=int(identity.st_ino),
            cgroup_type=self._read_control("cgroup.type", os.O_RDONLY).strip(),
            kill_supported=self._control_exists("cgroup.kill"),
            delegated=os.access(self.root_path, os.W_OK | os.X_OK),
            systemd_properties=dict(systemd_properties),
        )
        if (
            self.evidence.cgroup_type not in {"domain", "domain threaded"}
            or not self.evidence.kill_supported
            or not self.evidence.delegated
        ):
            raise CgroupUnavailable("service cgroup is not a delegated kill-capable domain")
        self._monotonic = monotonic
        self._sleep = sleep
        self._signal_sender = signal_sender
        self._process_start_ticks = process_start_ticks or self._default_start_ticks
        self._process_cgroup_reader = process_cgroup_reader or self._default_cgroup_reader
        self._cell_initializer = cell_initializer
        self._cell_finalizer = cell_finalizer
        self._cells: dict[str, CellCgroup] = {}
        self._closed = False

    @classmethod
    def discover_and_preflight(
        cls, *, expected_unit: str = APPLICATION_UNIT
    ) -> "CgroupV2Controller":
        if expected_unit != APPLICATION_UNIT:
            raise CgroupUnavailable("application cgroup unit differs from the fixed contract")
        runtime_root = Path(f"/run/user/{os.getuid()}")
        bus = runtime_root / "bus"
        try:
            mode = bus.stat().st_mode
        except OSError as error:
            raise CgroupUnavailable("systemd user bus is unavailable") from error
        if not stat.S_ISSOCK(mode):
            raise CgroupUnavailable("systemd user bus path is not a socket")
        cgroup_text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        relative = _unified_cgroup_path(cgroup_text)
        if PurePosixPath(relative).name != expected_unit:
            raise CgroupUnavailable("application is outside the fixed systemd service")
        query_properties = (
            "ActiveState", "SubState", "Delegate", "KillMode", "KillSignal",
            "FinalKillSignal", "SendSIGKILL", "TimeoutStopUSec",
            "RuntimeMaxUSec", "OOMPolicy", "Restart", "TasksMax", "ControlGroup",
        )
        environment = {
            "XDG_RUNTIME_DIR": str(runtime_root),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
            "LC_ALL": "C",
            "LANG": "C",
        }
        try:
            result = subprocess.run(
                [
                    str(SYSTEMCTL), "--user", "show", expected_unit,
                    "--no-pager", *[f"--property={key}" for key in query_properties],
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CgroupUnavailable("cannot query the fixed systemd service") from error
        if result.returncode != 0:
            raise CgroupUnavailable("fixed systemd service property query failed")
        properties = attest_systemd_properties(
            _parse_systemctl_show(result.stdout), expected_cgroup=relative
        )
        mountpoint = _cgroup2_mountpoint(
            Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        )
        try:
            root_fd = _open_dir_chain(mountpoint, relative)
        except OSError as error:
            raise CgroupUnavailable("cannot open the delegated service cgroup") from error
        try:
            controller = cls(
                mountpoint=mountpoint,
                relative_path=relative,
                unit=expected_unit,
                root_fd=root_fd,
                systemd_properties=properties,
            )
            controller._probe_delegation()
            return controller
        except BaseException:
            os.close(root_fd)
            raise

    def _control_exists(self, name: str) -> bool:
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CLOEXEC, dir_fd=self.root_fd)
        except OSError:
            return False
        os.close(descriptor)
        return True

    def _read_control(self, name: str, flags: int) -> str:
        descriptor = os.open(name, flags | os.O_CLOEXEC, dir_fd=self.root_fd)
        try:
            return _read_fd(descriptor)
        finally:
            os.close(descriptor)

    def _probe_delegation(self) -> None:
        name = f"g25-preflight-{os.getpid()}"
        if len(name) > 64:
            raise CgroupUnavailable("cgroup delegation probe name is malformed")
        try:
            os.mkdir(name, mode=0o700, dir_fd=self.root_fd)
            probe_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self.root_fd,
            )
            try:
                for control, flags in (
                    ("cgroup.procs", os.O_RDWR),
                    ("cgroup.events", os.O_RDONLY),
                    ("cgroup.kill", os.O_WRONLY),
                ):
                    descriptor = os.open(control, flags | os.O_CLOEXEC, dir_fd=probe_fd)
                    os.close(descriptor)
            finally:
                os.close(probe_fd)
            os.rmdir(name, dir_fd=self.root_fd)
        except OSError as error:
            try:
                os.rmdir(name, dir_fd=self.root_fd)
            except OSError:
                pass
            raise CgroupUnavailable("delegated cgroup create/control/remove probe failed") from error

    @staticmethod
    def _default_start_ticks(pid: int) -> int:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = value.rfind(")")
        fields = value[closing + 2 :].split() if closing >= 0 else []
        if len(fields) <= 19:
            raise CgroupV2Error("process stat is malformed")
        return int(fields[19])

    @staticmethod
    def _default_cgroup_reader(pid: int) -> str:
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")

    def prepare_cell(self, cell_id: str) -> CellCgroup:
        if self._closed or not _CELL_ID.fullmatch(cell_id) or cell_id in self._cells:
            raise CgroupV2Error("cell cgroup identity is invalid or already active")
        name = f"g25-cell-{cell_id}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=self.root_fd)
            path = self.root_path / name
            if self._cell_initializer is not None:
                self._cell_initializer(path)
            directory = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self.root_fd,
            )
            procs = os.open("cgroup.procs", os.O_RDWR | os.O_CLOEXEC, dir_fd=directory)
            events = os.open("cgroup.events", os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
            kill = os.open("cgroup.kill", os.O_WRONLY | os.O_CLOEXEC, dir_fd=directory)
        except OSError as error:
            for descriptor_name in ("kill", "events", "procs", "directory"):
                descriptor = locals().get(descriptor_name)
                if isinstance(descriptor, int):
                    os.close(descriptor)
            try:
                os.rmdir(name, dir_fd=self.root_fd)
            except OSError:
                pass
            raise CgroupUnavailable("cannot create the exact cell cgroup") from error
        identity = os.fstat(directory)
        relative = f"{self.relative_path.rstrip('/')}/{name}"
        cell = CellCgroup(
            controller=self,
            cell_id=cell_id,
            name=name,
            relative_path=relative,
            path=path,
            dir_fd=directory,
            procs_fd=procs,
            events_fd=events,
            kill_fd=kill,
            device=int(identity.st_dev),
            inode=int(identity.st_ino),
        )
        self._cells[cell_id] = cell
        return cell

    def move_and_verify(
        self, cell: CellCgroup, *, pid: int, expected_start_ticks: int
    ) -> dict[str, Any]:
        if cell.closed or self._cells.get(cell.cell_id) is not cell:
            raise CgroupV2Error("cell cgroup is not active")
        if pid <= 0 or expected_start_ticks <= 0:
            raise CgroupV2Error("worker process identity is invalid")
        if self._process_start_ticks(pid) != expected_start_ticks:
            raise CgroupV2Error("worker PID identity changed before cgroup move")
        _write_fd(cell.procs_fd, f"{pid}\n".encode("ascii"))
        if self._process_start_ticks(pid) != expected_start_ticks:
            raise CgroupV2Error("worker PID identity changed during cgroup move")
        observed_path = _unified_cgroup_path(self._process_cgroup_reader(pid))
        if observed_path != cell.relative_path:
            raise CgroupV2Error("worker did not enter the exact cell cgroup")
        if pid not in _parse_pids(_read_fd(cell.procs_fd)):
            raise CgroupV2Error("cell cgroup does not list the moved worker PID")
        cell.moved_pid = pid
        cell.moved_start_ticks = expected_start_ticks
        return {
            "pid": pid,
            "start_ticks": expected_start_ticks,
            "relative_path": cell.relative_path,
            "device": cell.device,
            "inode": cell.inode,
            "move_observed": True,
        }

    def populated(self, cell: CellCgroup) -> int:
        if cell.closed:
            raise CgroupV2Error("cell cgroup is closed")
        return _parse_populated(_read_fd(cell.events_fd))

    def pids(self, cell: CellCgroup) -> list[int]:
        if cell.closed:
            raise CgroupV2Error("cell cgroup is closed")
        return _parse_pids(_read_fd(cell.procs_fd))

    def _wait_until_zero(self, cell: CellCgroup, deadline: float) -> bool:
        while True:
            if self.populated(cell) == 0:
                cell.populated_zero_observed = True
                return True
            now = self._monotonic()
            if now >= deadline:
                return False
            self._sleep(min(0.01, deadline - now))

    def _write_kill(self, cell: CellCgroup) -> int:
        moment = int(self._monotonic() * 1_000_000_000)
        try:
            _write_fd(cell.kill_fd, b"1\n")
        except OSError as error:
            raise CgroupDrainError("cgroup.kill write failed") from error
        return moment

    def terminate_and_drain(
        self,
        cell: CellCgroup,
        *,
        graceful: bool,
        term_grace_seconds: int = TERM_GRACE_SECONDS,
        kill_drain_seconds: int = KILL_DRAIN_SECONDS,
    ) -> DrainEvidence:
        if term_grace_seconds != 30 or kill_drain_seconds != 5:
            raise CgroupDrainError("cgroup drain deadlines differ from the frozen contract")
        initial_populated = self.populated(cell)
        term_sent = False
        term_ns: int | None = None
        if graceful:
            term_ns = int(self._monotonic() * 1_000_000_000)
            for pid in self.pids(cell):
                try:
                    self._signal_sender(pid, int(signal.SIGTERM))
                    term_sent = True
                except ProcessLookupError:
                    continue
            if self._wait_until_zero(cell, self._monotonic() + term_grace_seconds):
                return DrainEvidence(
                    initial_populated=initial_populated,
                    term_sent=term_sent,
                    term_sent_monotonic_ns=term_ns,
                    term_grace_seconds=term_grace_seconds,
                    cgroup_kill_written=False,
                    cgroup_kill_monotonic_ns=None,
                    populated_zero_monotonic_ns=int(
                        self._monotonic() * 1_000_000_000
                    ),
                    final_populated=0,
                )
        kill_ns = self._write_kill(cell)
        if not self._wait_until_zero(cell, self._monotonic() + kill_drain_seconds):
            raise CgroupDrainError("cell cgroup remained populated after cgroup.kill")
        return DrainEvidence(
            initial_populated=initial_populated,
            term_sent=term_sent,
            term_sent_monotonic_ns=term_ns,
            term_grace_seconds=term_grace_seconds,
            cgroup_kill_written=True,
            cgroup_kill_monotonic_ns=kill_ns,
            populated_zero_monotonic_ns=int(self._monotonic() * 1_000_000_000),
            final_populated=0,
        )

    def finalize_normal_exit(self, cell: CellCgroup) -> DrainEvidence:
        """Always issue recursive kill, then prove zero before accepting exit."""
        return self.terminate_and_drain(cell, graceful=False)

    def emergency_kill(self, cell: CellCgroup) -> DrainEvidence:
        return self.terminate_and_drain(cell, graceful=False)

    def close_cell(self, cell: CellCgroup) -> None:
        if cell.closed or self._cells.get(cell.cell_id) is not cell:
            raise CgroupV2Error("cell cgroup is not active")
        if not cell.populated_zero_observed or self.populated(cell) != 0:
            raise CgroupDrainError("cannot remove a populated or unproved cell cgroup")
        for descriptor in (cell.kill_fd, cell.events_fd, cell.procs_fd, cell.dir_fd):
            os.close(descriptor)
        cell.closed = True
        if self._cell_finalizer is not None:
            self._cell_finalizer(cell.path)
        try:
            os.rmdir(cell.name, dir_fd=self.root_fd)
        except OSError as error:
            raise CgroupDrainError("empty cell cgroup could not be removed") from error
        self._cells.pop(cell.cell_id)

    def assert_all_cells_empty(self) -> None:
        failures = [
            cell.relative_path
            for cell in self._cells.values()
            if cell.closed or not cell.populated_zero_observed or self.populated(cell) != 0
        ]
        if failures:
            raise CgroupDrainError(f"cell containment is not empty: {sorted(failures)}")

    def emergency_drain_all(self) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for cell in list(self._cells.values()):
            if cell.closed:
                self._cells.pop(cell.cell_id, None)
                continue
            if not cell.populated_zero_observed:
                drain = self.emergency_kill(cell)
                evidence.append({"cell_id": cell.cell_id, **drain.as_dict()})
            self.close_cell(cell)
        return evidence

    def close(self) -> None:
        if self._cells:
            raise CgroupDrainError("cannot close controller with active cell cgroups")
        if not self._closed:
            os.close(self.root_fd)
            self._closed = True
