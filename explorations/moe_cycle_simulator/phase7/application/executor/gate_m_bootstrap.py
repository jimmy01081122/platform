#!/usr/bin/env python3
"""Standalone one-connection Gate M bootstrap and remote-stage launcher.

This source is transported by the exact SSH command before the application
exists remotely.  It imports only the Python standard library, executes the
separately hash-bound deployment bootstrap as a non-main module, and then
launches the newly installed Gate M remote controller under absolute monotonic
phase deadlines.  It never imports vLLM, Torch, CUDA, or model code.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import hashlib
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SHA256_LENGTH = 64
DEPLOYMENT_DEADLINE_SECONDS = 300
MATERIALIZATION_DEADLINE_SECONDS = 4200
RUNTIME_PROVENANCE_DEADLINE_SECONDS = 4500
EXPORT_REPLAY_DEADLINE_SECONDS = 4620
REMOTE_CLEANUP_RESERVE_SECONDS = 120
REMOTE_OUTER_SECONDS = 4800
MAX_GATE_TRANSPORT_BYTES = 96 * 1024 * 1024
MAX_GATE_HEADER_BYTES = 128
MAX_GATE_STDOUT_BYTES = MAX_GATE_HEADER_BYTES + MAX_GATE_TRANSPORT_BYTES
MAX_GATE_STDERR_BYTES = 1024 * 1024
TRANSPORT_MAGIC = b"MOE_GATE_M_EXPORT_V1"
MATERIALIZATION_UNLOCK = "OWNER_APPROVED_EXACT_MATERIALIZATION_COMMAND"
PR_SET_CHILD_SUBREAPER = 36
ACTIVE_REMOTE_PROCESS: subprocess.Popen[bytes] | None = None


class GateMBootstrapError(RuntimeError):
    """A blocking remote Gate M bootstrap or orchestration failure."""


def _sha256(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise GateMBootstrapError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validated_remote_executable(
    value: Path,
    expected_sha256: str,
    label: str,
) -> Path:
    """Resolve no names: accept only one canonical, executable file identity."""

    digest = _sha256(expected_sha256, f"{label} hash")
    text = str(value)
    if (
        not value.is_absolute()
        or "\\" in text
        or ".." in value.parts
        or value.is_symlink()
        or not value.is_file()
        or value.resolve(strict=True) != value
        or not os.access(value, os.X_OK)
    ):
        raise GateMBootstrapError(f"{label} must be one canonical absolute executable")
    observed = hashlib.sha256()
    with value.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            observed.update(block)
    if observed.hexdigest() != digest:
        raise GateMBootstrapError(f"{label} file/hash identity differs")
    return value


def _decode_source(encoded: str, expected_sha256: str) -> bytes:
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise GateMBootstrapError("deployment bootstrap source is not valid base64") from exc
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise GateMBootstrapError("deployment bootstrap source base64 is noncanonical")
    if hashlib.sha256(payload).hexdigest() != _sha256(
        expected_sha256, "deployment bootstrap source hash"
    ):
        raise GateMBootstrapError("deployment bootstrap source hash differs")
    return payload


def _load_bootstrap(source: bytes) -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "__name__": "_phase7_deployment_bootstrap",
        "__file__": "deployment_bootstrap.py",
    }
    exec(compile(source, "deployment_bootstrap.py", "exec"), namespace)
    for name in ("initialize_project_root", "bootstrap", "BootstrapError"):
        if name not in namespace:
            raise GateMBootstrapError(f"deployment bootstrap omitted {name}")
    return namespace


def _remaining_seconds(deadline_ns: int, label: str) -> float:
    remaining_ns = deadline_ns - time.monotonic_ns()
    if remaining_ns <= 0:
        raise GateMBootstrapError(f"{label} absolute deadline expired")
    return remaining_ns / 1_000_000_000


def _enable_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise GateMBootstrapError(f"cannot enable Gate M child subreaper: errno={error}")


def _descendants(parent_pid: int) -> set[int]:
    relations: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            suffix = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1]
            fields = suffix.split()
            relations[int(entry.name)] = int(fields[1])
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    result: set[int] = set()
    frontier = {parent_pid}
    while frontier:
        children = {
            pid
            for pid, observed_parent in relations.items()
            if observed_parent in frontier and pid not in result
        }
        result.update(children)
        frontier = children
    return result


def _signal_processes(pids: set[int], signum: int) -> None:
    for pid in sorted(pids, reverse=True):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            continue


def _reap_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    targets = _descendants(os.getpid()) | _descendants(process.pid) | {process.pid}
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _signal_processes(targets, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass
        _reap_children()
        survivors = {pid for pid in targets if Path(f"/proc/{pid}").exists()}
        survivors.update(_descendants(os.getpid()))
        if not survivors:
            return
        time.sleep(0.05)
        targets.update(survivors)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _signal_processes(targets | _descendants(os.getpid()), signal.SIGKILL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass
        _reap_children()
        survivors = {
            pid
            for pid in targets | _descendants(os.getpid())
            if Path(f"/proc/{pid}").exists()
        }
        if not survivors:
            return
        time.sleep(0.05)
    raise GateMBootstrapError("Gate M remote process tree survived cleanup")


def _signal_handler(signum: int, _frame: Any) -> None:
    if ACTIVE_REMOTE_PROCESS is not None:
        _terminate_group(ACTIVE_REMOTE_PROCESS)
    raise GateMBootstrapError(f"Gate M bootstrap received signal {signum}")


def _parse_transport_header(header: bytes) -> tuple[int, str]:
    if not header.endswith(b"\n") or len(header) > MAX_GATE_HEADER_BYTES:
        raise GateMBootstrapError("Gate M transport header is absent or oversized")
    parts = header[:-1].split(b" ")
    if len(parts) != 3 or parts[0] != TRANSPORT_MAGIC:
        raise GateMBootstrapError("Gate M transport header magic differs")
    size_text, digest = parts[1], parts[2]
    if (
        not size_text
        or size_text.startswith(b"0")
        or not size_text.isdigit()
        or len(digest) != 64
        or any(character not in b"0123456789abcdef" for character in digest)
    ):
        raise GateMBootstrapError("Gate M transport header fields are noncanonical")
    size = int(size_text)
    if size <= 0 or size > MAX_GATE_TRANSPORT_BYTES:
        raise GateMBootstrapError("Gate M transport payload size is outside its bound")
    return size, digest.decode("ascii")


def _relay_output(output_stream: Any, payload: bytes, *, deadline_ns: int) -> None:
    descriptor = output_stream.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    offset = 0
    try:
        selector.register(descriptor, selectors.EVENT_WRITE)
        while offset < len(payload):
            events = selector.select(
                timeout=min(_remaining_seconds(deadline_ns, "Gate M relay"), 1.0)
            )
            if not events:
                continue
            try:
                written = os.write(descriptor, payload[offset:])
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise GateMBootstrapError("Gate M SSH consumer closed early") from exc
            if written <= 0:
                raise GateMBootstrapError("Gate M SSH relay made no write progress")
            offset += written
    finally:
        selector.close()


def _run_remote_controller(
    *,
    application: Path,
    evidence_root: Path,
    materialization_deadline_ns: int,
    provenance_deadline_ns: int,
    export_deadline_ns: int,
    output_stream: Any,
    remote_timeout_executable: Path,
    remote_timeout_executable_sha256: str,
    remote_python_executable: Path,
    remote_python_executable_sha256: str,
) -> None:
    global ACTIVE_REMOTE_PROCESS
    executable = application / "executor/gate_m_remote.py"
    if executable.is_symlink() or not executable.is_file():
        raise GateMBootstrapError("installed Gate M remote controller is unsafe or absent")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "PYTHONPATH",
            "LD_LIBRARY_PATH",
            "CUDA_HOME",
            "VIRTUAL_ENV",
            "LANG",
            "LC_ALL",
            "TZ",
            "MOE_PHASE7_CONTAINER_DIGEST",
        }
    }
    environment["MOE_PHASE7_MATERIALIZATION_UNLOCK"] = MATERIALIZATION_UNLOCK
    environment["MOE_PHASE7_REMOTE_TIMEOUT_EXECUTABLE"] = str(
        remote_timeout_executable
    )
    environment["MOE_PHASE7_REMOTE_TIMEOUT_EXECUTABLE_SHA256"] = (
        remote_timeout_executable_sha256
    )
    environment["MOE_PHASE7_REMOTE_PYTHON_EXECUTABLE"] = str(
        remote_python_executable
    )
    environment["MOE_PHASE7_REMOTE_PYTHON_EXECUTABLE_SHA256"] = (
        remote_python_executable_sha256
    )
    command = [
        str(remote_python_executable),
        "-I",
        "-B",
        str(executable),
        "--application-dir",
        str(application),
        "--evidence-root",
        str(evidence_root),
        "--materialization-deadline-monotonic-ns",
        str(materialization_deadline_ns),
        "--runtime-provenance-deadline-monotonic-ns",
        str(provenance_deadline_ns),
        "--export-deadline-monotonic-ns",
        str(export_deadline_ns),
    ]
    process = subprocess.Popen(
        command,
        cwd=application,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    ACTIVE_REMOTE_PROCESS = process
    if process.stdout is None or process.stderr is None:
        _terminate_group(process)
        raise GateMBootstrapError("installed Gate M controller pipes are unavailable")
    selector = selectors.DefaultSelector()
    stdout_count = 0
    stderr_payload = bytearray()
    header_buffer = bytearray()
    expected_payload_bytes: int | None = None
    expected_payload_sha256: str | None = None
    payload_bytes = 0
    payload_hash = hashlib.sha256()
    try:
        for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)
        while selector.get_map():
            remaining = _remaining_seconds(export_deadline_ns, "Gate M export")
            events = selector.select(timeout=min(remaining, 1.0))
            if not events:
                if process.poll() is not None:
                    continue
                continue
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 1024 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if key.data == "stdout":
                    stdout_count += len(chunk)
                    if stdout_count > MAX_GATE_STDOUT_BYTES:
                        _terminate_group(process)
                        raise GateMBootstrapError(
                            "installed Gate M controller stdout exceeded its bound"
                        )
                    if expected_payload_bytes is None:
                        header_buffer.extend(chunk)
                        newline = header_buffer.find(b"\n")
                        if newline < 0:
                            if len(header_buffer) >= MAX_GATE_HEADER_BYTES:
                                _terminate_group(process)
                                raise GateMBootstrapError(
                                    "installed Gate M controller header exceeded its bound"
                                )
                            continue
                        header = bytes(header_buffer[: newline + 1])
                        remainder = bytes(header_buffer[newline + 1 :])
                        expected_payload_bytes, expected_payload_sha256 = (
                            _parse_transport_header(header)
                        )
                        _relay_output(
                            output_stream,
                            header,
                            deadline_ns=export_deadline_ns,
                        )
                        header_buffer.clear()
                        chunk = remainder
                    if chunk:
                        payload_bytes += len(chunk)
                        if payload_bytes > expected_payload_bytes:
                            _terminate_group(process)
                            raise GateMBootstrapError(
                                "installed Gate M controller emitted extra payload bytes"
                            )
                        payload_hash.update(chunk)
                        _relay_output(
                            output_stream,
                            chunk,
                            deadline_ns=export_deadline_ns,
                        )
                else:
                    if len(stderr_payload) + len(chunk) > MAX_GATE_STDERR_BYTES:
                        _terminate_group(process)
                        raise GateMBootstrapError(
                            "installed Gate M controller stderr exceeded its bound"
                        )
                    stderr_payload.extend(chunk)
        process.wait(timeout=_remaining_seconds(export_deadline_ns, "Gate M export"))
    except subprocess.TimeoutExpired as exc:
        _terminate_group(process)
        raise GateMBootstrapError("installed Gate M controller exceeded its deadline") from exc
    except GateMBootstrapError:
        _terminate_group(process)
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        ACTIVE_REMOTE_PROCESS = None
    if process.returncode != 0:
        raise GateMBootstrapError(
            "installed Gate M controller failed: "
            f"returncode={process.returncode}, "
            f"stderr_sha256={hashlib.sha256(stderr_payload).hexdigest()}"
        )
    if (
        expected_payload_bytes is None
        or expected_payload_sha256 is None
        or payload_bytes != expected_payload_bytes
        or payload_hash.hexdigest() != expected_payload_sha256
    ):
        raise GateMBootstrapError("installed Gate M controller frame is short or differs")
    leaked = _descendants(os.getpid())
    if leaked:
        _terminate_group(process)
        raise GateMBootstrapError("installed Gate M controller left descendant processes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowed-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--expected-mount-identity-sha256", required=True)
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-size", type=int, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--deployment-bootstrap-source-base64", required=True)
    parser.add_argument("--deployment-bootstrap-source-sha256", required=True)
    parser.add_argument("--remote-timeout-executable", type=Path, required=True)
    parser.add_argument("--remote-timeout-executable-sha256", required=True)
    parser.add_argument("--remote-python-executable", type=Path, required=True)
    parser.add_argument("--remote-python-executable-sha256", required=True)
    parser.add_argument("--materialization-evidence-root", type=Path, required=True)
    parser.add_argument("--prepare-relative-dir", action="append", default=[])
    args = parser.parse_args()

    remote_timeout_executable = _validated_remote_executable(
        args.remote_timeout_executable,
        args.remote_timeout_executable_sha256,
        "remote timeout executable",
    )
    remote_python_executable = _validated_remote_executable(
        args.remote_python_executable,
        args.remote_python_executable_sha256,
        "remote Python executable",
    )
    if Path(sys.executable).resolve(strict=True) != remote_python_executable:
        raise GateMBootstrapError(
            "running Python identity differs from the approved remote executable"
        )

    stage_start_ns = time.monotonic_ns()
    if not (
        DEPLOYMENT_DEADLINE_SECONDS
        < MATERIALIZATION_DEADLINE_SECONDS
        < RUNTIME_PROVENANCE_DEADLINE_SECONDS
        < EXPORT_REPLAY_DEADLINE_SECONDS
        and EXPORT_REPLAY_DEADLINE_SECONDS + REMOTE_CLEANUP_RESERVE_SECONDS
        <= REMOTE_OUTER_SECONDS
    ):
        raise GateMBootstrapError("Gate M frozen deadline ordering is invalid")
    _enable_subreaper()
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGHUP, _signal_handler)
    deployment_deadline_ns = (
        stage_start_ns + DEPLOYMENT_DEADLINE_SECONDS * 1_000_000_000
    )
    materialization_deadline_ns = (
        stage_start_ns + MATERIALIZATION_DEADLINE_SECONDS * 1_000_000_000
    )
    provenance_deadline_ns = (
        stage_start_ns + RUNTIME_PROVENANCE_DEADLINE_SECONDS * 1_000_000_000
    )
    export_deadline_ns = stage_start_ns + EXPORT_REPLAY_DEADLINE_SECONDS * 1_000_000_000
    source = _decode_source(
        args.deployment_bootstrap_source_base64,
        args.deployment_bootstrap_source_sha256,
    )
    namespace = _load_bootstrap(source)
    try:
        namespace["initialize_project_root"](
            allowed_root=args.allowed_root,
            project_root=args.project_root,
            relative_directories=args.prepare_relative_dir,
            expected_mount_identity_sha256=args.expected_mount_identity_sha256,
        )
        receipt = namespace["bootstrap"](
            sys.stdin.buffer,
            allowed_root=args.allowed_root,
            incoming=args.incoming,
            target=args.target,
            receipt=args.receipt,
            expected_size=args.expected_size,
            expected_sha256=args.expected_sha256,
        )
    except namespace["BootstrapError"] as exc:
        raise GateMBootstrapError(str(exc)) from exc
    if time.monotonic_ns() > deployment_deadline_ns:
        raise GateMBootstrapError("deployment crossed its absolute phase deadline")
    if receipt.get("bundle_sha256") != args.expected_sha256:
        raise GateMBootstrapError("deployment receipt differs from the approved bundle")
    _run_remote_controller(
        application=args.target,
        evidence_root=args.materialization_evidence_root,
        materialization_deadline_ns=materialization_deadline_ns,
        provenance_deadline_ns=provenance_deadline_ns,
        export_deadline_ns=export_deadline_ns,
        output_stream=sys.stdout.buffer,
        remote_timeout_executable=remote_timeout_executable,
        remote_timeout_executable_sha256=args.remote_timeout_executable_sha256,
        remote_python_executable=remote_python_executable,
        remote_python_executable_sha256=args.remote_python_executable_sha256,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateMBootstrapError as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
