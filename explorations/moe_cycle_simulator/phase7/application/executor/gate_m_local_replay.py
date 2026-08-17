#!/usr/bin/env python3
"""Decode and replay one Gate M export inside a hard address-space boundary."""

from __future__ import annotations

import argparse
import os
import resource
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "explorations").is_dir():
            sys.path.insert(0, str(ancestor))
            break

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    SHA256_RE,
    file_sha256,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment import (  # noqa: E402
    parse_gate_m_stdout,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export import (  # noqa: E402
    publish_local_replay,
    verify_export_projection,
)
from explorations.moe_cycle_simulator.phase7.application.executor.process_tree import (  # noqa: E402
    ProcessTreeContainment,
)


RESULT_SCHEMA = "moe-simulator-phase7-gate-m-local-replay-v1"
EXECUTION_SCHEMA = "moe-simulator-phase7-gate-m-local-decode-execution-v1"
MAX_LOCAL_DECODE_LOG_BYTES = 262_144


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise M0Error(f"{label} must be a positive integer")
    return value


def _safe_source(path: Path, expected_sha256: str) -> Path:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.resolve(strict=True) != path
        or SHA256_RE.fullmatch(expected_sha256) is None
        or file_sha256(path) != expected_sha256
    ):
        raise M0Error("local Gate M decoder source/hash identity mismatch")
    return path


def _set_child_limits(address_space_bytes: int) -> None:
    resource.setrlimit(
        resource.RLIMIT_AS,
        (address_space_bytes, address_space_bytes),
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise M0Error("local Gate M decoder log made no write progress")
        offset += written


def run_bounded_decoder_process(
    argv: list[str],
    *,
    decoder_source: Path,
    decoder_source_sha256: str,
    address_space_bytes: int,
    deadline_monotonic_ns: int,
    stdout_path: Path,
    stderr_path: Path,
    execution_record_path: Path,
    max_log_bytes: int = MAX_LOCAL_DECODE_LOG_BYTES,
) -> dict[str, Any]:
    """Execute an exact decoder below RLIMIT_AS and an absolute deadline."""

    if not sys.platform.startswith("linux") or not hasattr(resource, "RLIMIT_AS"):
        raise M0Error("formal local Gate M decode containment requires Linux RLIMIT_AS")
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise M0Error("local Gate M decoder argv is invalid")
    source = _safe_source(decoder_source, decoder_source_sha256)
    address_space_bytes = _positive_int(address_space_bytes, "decoder RLIMIT_AS")
    max_log_bytes = _positive_int(max_log_bytes, "decoder log bound")
    deadline_monotonic_ns = _positive_int(
        deadline_monotonic_ns, "decoder absolute deadline"
    )
    if deadline_monotonic_ns <= time.monotonic_ns():
        raise M0Error("local Gate M decoder deadline already expired")
    for path in (stdout_path, stderr_path, execution_record_path):
        if path.exists() or path.is_symlink():
            raise M0Error("local Gate M decoder evidence path is not fresh")
        parent = path.parent.resolve(strict=True)
        if parent != path.parent or parent.is_symlink():
            raise M0Error("local Gate M decoder evidence parent is unsafe")

    interpreter = Path(argv[0]).resolve(strict=True)
    if interpreter.is_symlink() or not interpreter.is_file():
        raise M0Error("local Gate M decoder interpreter is unsafe")
    started_ns = time.monotonic_ns()
    containment = ProcessTreeContainment()
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    log_limit_exceeded: str | None = None
    stdout_size = 0
    stderr_size = 0
    cleanup: dict[str, object] = {"status": "NOT_STARTED"}
    selector = selectors.DefaultSelector()
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=lambda: _set_child_limits(address_space_bytes),
            )
            containment.attach(process.pid)
            if process.stdout is None or process.stderr is None:
                raise M0Error("local Gate M decoder pipes are unavailable")
            for stream, label in (
                (process.stdout, "stdout"),
                (process.stderr, "stderr"),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            while selector.get_map():
                remaining_ns = deadline_monotonic_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    timed_out = True
                    break
                events = selector.select(
                    timeout=min(remaining_ns / 1_000_000_000, 1.0)
                )
                if not events:
                    continue
                for key, _mask in events:
                    stream = key.fileobj
                    try:
                        chunk = os.read(stream.fileno(), 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    if key.data == "stdout":
                        allowed = max_log_bytes - stdout_size
                        if allowed > 0:
                            _write_all(stdout.fileno(), chunk[:allowed])
                            stdout_size += min(len(chunk), allowed)
                    else:
                        allowed = max_log_bytes - stderr_size
                        if allowed > 0:
                            _write_all(stderr.fileno(), chunk[:allowed])
                            stderr_size += min(len(chunk), allowed)
                    if len(chunk) > allowed:
                        log_limit_exceeded = key.data
                        break
                if log_limit_exceeded is not None:
                    break
            if not timed_out and log_limit_exceeded is None:
                remaining_ns = deadline_monotonic_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    timed_out = True
                else:
                    try:
                        process.wait(timeout=remaining_ns / 1_000_000_000)
                    except subprocess.TimeoutExpired:
                        timed_out = True
            cleanup = containment.assert_clean(
                term_grace_seconds=10,
                kill_grace_seconds=10,
            )
            process.wait(timeout=1)
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
    finally:
        selector.close()
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        if process is not None and process.poll() is None:
            cleanup = containment.assert_clean(
                term_grace_seconds=10,
                kill_grace_seconds=10,
            )
            process.wait(timeout=1)
        for path in (stdout_path, stderr_path):
            if path.exists() and not path.is_symlink():
                path.chmod(0o400)

    returncode = process.returncode if process is not None else None
    status = (
        "INCOMPLETE"
        if timed_out
        else (
            "COMPLETE"
            if returncode == 0 and log_limit_exceeded is None
            else "FAILED"
        )
    )
    record = {
        "schema_version": EXECUTION_SCHEMA,
        "status": status,
        "decoder_source_path": str(source),
        "decoder_source_sha256": decoder_source_sha256,
        "interpreter_path": str(interpreter),
        "interpreter_sha256": file_sha256(interpreter),
        "argv_sha256": semantic_sha256(argv),
        "rlimit_as_bytes": address_space_bytes,
        "max_log_bytes": max_log_bytes,
        "stdout_size_bytes": stdout_size,
        "stderr_size_bytes": stderr_size,
        "log_limit_exceeded": log_limit_exceeded,
        "deadline_monotonic_ns": deadline_monotonic_ns,
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": time.monotonic_ns(),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_sha256": file_sha256(stdout_path),
        "stderr_sha256": file_sha256(stderr_path),
        "process_tree_cleanup": cleanup,
    }
    write_new_json(execution_record_path, record)
    execution_record_path.chmod(0o400)
    if status != "COMPLETE":
        raise M0Error(
            "local Gate M decoder failed within its enforced boundary: "
            f"status={status}, returncode={returncode}, "
            f"log_limit_exceeded={log_limit_exceeded}"
        )
    return record


def validate_local_replay_result(
    result: Mapping[str, Any],
    *,
    input_frame_path: Path,
    address_space_bytes: int,
) -> dict[str, Any]:
    expected = {
        "schema_version",
        "status",
        "input_frame_sha256",
        "input_frame_size_bytes",
        "transport_sha256",
        "transport_size_bytes",
        "remote_summary",
        "remote_summary_sha256",
        "export_manifest_sha256",
        "export_status_sha256",
        "rlimit_as_bytes",
        "deadline_monotonic_ns",
        "started_monotonic_ns",
        "finished_monotonic_ns",
    }
    if (
        set(result) != expected
        or result.get("schema_version") != RESULT_SCHEMA
        or result.get("status") != "COMPLETE_REPLAYED"
        or result.get("rlimit_as_bytes") != address_space_bytes
        or result.get("input_frame_sha256") != file_sha256(input_frame_path)
        or result.get("input_frame_size_bytes") != input_frame_path.stat().st_size
        or not isinstance(result.get("remote_summary"), dict)
        or result.get("remote_summary_sha256")
        != semantic_sha256(result["remote_summary"])
    ):
        raise M0Error("local Gate M replay result identity differs")
    for field in (
        "input_frame_sha256",
        "transport_sha256",
        "remote_summary_sha256",
        "export_manifest_sha256",
        "export_status_sha256",
    ):
        if not isinstance(result.get(field), str) or SHA256_RE.fullmatch(result[field]) is None:
            raise M0Error(f"local Gate M replay result has invalid {field}")
    for field in (
        "input_frame_size_bytes",
        "transport_size_bytes",
        "rlimit_as_bytes",
        "deadline_monotonic_ns",
        "started_monotonic_ns",
        "finished_monotonic_ns",
    ):
        _positive_int(result.get(field), field)
    if not (
        result["started_monotonic_ns"]
        <= result["finished_monotonic_ns"]
        <= result["deadline_monotonic_ns"]
    ):
        raise M0Error("local Gate M replay crossed its absolute deadline")
    return dict(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport-frame", type=Path, required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--application-ledger-sha256", required=True)
    parser.add_argument("--deployment-receipt-sha256", required=True)
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--export-status", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--rlimit-as-bytes", type=int, required=True)
    parser.add_argument("--deadline-monotonic-ns", type=int, required=True)
    args = parser.parse_args()

    started_ns = time.monotonic_ns()
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if soft != args.rlimit_as_bytes or hard != args.rlimit_as_bytes:
        raise M0Error("local Gate M decoder RLIMIT_AS was not installed before exec")
    if started_ns >= args.deadline_monotonic_ns:
        raise M0Error("local Gate M decoder started after its absolute deadline")
    frame = args.transport_frame.resolve(strict=True)
    observed = frame.lstat()
    if not stat.S_ISREG(observed.st_mode) or frame.is_symlink():
        raise M0Error("local Gate M transport frame is not a real regular file")
    payload = frame.read_bytes()
    remote_summary, transport_payload, transport = parse_gate_m_stdout(
        payload,
        bundle_sha256=args.bundle_sha256,
        application_ledger_sha256=args.application_ledger_sha256,
        deployment_receipt_sha256=args.deployment_receipt_sha256,
    )
    replay = publish_local_replay(
        transport_payload,
        export_root=args.export_root,
        status_path=args.export_status,
    )
    projection = verify_export_projection(
        args.export_root,
        status_path=args.export_status,
        remote_summary=remote_summary,
    )
    finished_ns = time.monotonic_ns()
    if finished_ns > args.deadline_monotonic_ns:
        raise M0Error("local Gate M replay crossed its absolute deadline")
    if replay["transport_sha256"] != transport["transport_sha256"]:
        raise M0Error("local Gate M transport replay hash differs")
    result = {
        "schema_version": RESULT_SCHEMA,
        "status": "COMPLETE_REPLAYED",
        "input_frame_sha256": file_sha256(frame),
        "input_frame_size_bytes": observed.st_size,
        "transport_sha256": transport["transport_sha256"],
        "transport_size_bytes": len(transport_payload),
        "remote_summary": remote_summary,
        "remote_summary_sha256": semantic_sha256(remote_summary),
        "export_manifest_sha256": projection["manifest"]["manifest_sha256"],
        "export_status_sha256": file_sha256(args.export_status),
        "rlimit_as_bytes": soft,
        "deadline_monotonic_ns": args.deadline_monotonic_ns,
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": finished_ns,
    }
    write_new_json(args.result, result)
    print(semantic_sha256(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
