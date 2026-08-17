#!/usr/bin/env python3
"""Verify a deployed package, run materialization once, and replay both terminals."""

from __future__ import annotations

import argparse
import hashlib
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    canonical_bytes,
    load_json,
    semantic_sha256,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    verify_install,
)
from explorations.moe_cycle_simulator.phase7.application.executor.materialization_driver import (  # noqa: E402
    verify_materialization_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.storage_identity import (  # noqa: E402
    validate_mount_identity,
)
from explorations.moe_cycle_simulator.phase7.application.executor.process_tree import (  # noqa: E402
    ProcessTreeContainment,
    enable_child_subreaper,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_provenance import (  # noqa: E402
    verify_runtime_provenance,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export import (  # noqa: E402
    build_and_publish_export,
    build_transport_envelope,
    frame_transport_envelope,
    verify_export,
)


REMOTE_MATERIALIZATION_TERMINAL_RESERVE_SECONDS = 300
MAX_DRIVER_STDOUT_BYTES = 4096
MAX_DRIVER_STDERR_BYTES = 262144
ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
ACTIVE_CONTAINMENT: ProcessTreeContainment | None = None


class GateMRemoteInterrupted(M0Error):
    """The installed Gate M controller received an outer signal."""


def _validated_remote_executable(path_text: str, digest: str, label: str) -> Path:
    path = Path(path_text)
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not path.is_absolute()
        or "\\" in path_text
        or ".." in path.parts
        or path.is_symlink()
        or not path.is_file()
        or path.resolve(strict=True) != path
        or not os.access(path, os.X_OK)
    ):
        raise M0Error(f"{label} identity is invalid")
    observed = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            observed.update(block)
    if observed.hexdigest() != digest:
        raise M0Error(f"{label} file/hash identity changed")
    return path


def _deadline(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise M0Error(f"{label} must be a positive monotonic-ns integer") from exc
    if parsed <= 0:
        raise M0Error(f"{label} must be a positive monotonic-ns integer")
    return parsed


def _remaining_seconds(deadline_ns: int, label: str) -> float:
    remaining = deadline_ns - time.monotonic_ns()
    if remaining <= 0:
        raise M0Error(f"{label} absolute deadline expired")
    return remaining / 1_000_000_000


def terminate_active() -> dict[str, object]:
    global ACTIVE_CONTAINMENT, ACTIVE_PROCESS
    if ACTIVE_CONTAINMENT is None:
        return {"status": "NOT_STARTED"}
    cleanup = ACTIVE_CONTAINMENT.terminate(
        term_grace_seconds=30,
        kill_grace_seconds=30,
    )
    if ACTIVE_PROCESS is not None:
        try:
            ACTIVE_PROCESS.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            raise M0Error("Gate M materialization child survived containment") from exc
    ACTIVE_PROCESS = None
    ACTIVE_CONTAINMENT = None
    return cleanup


def signal_handler(signum: int, _frame: Any) -> None:
    terminate_active()
    raise GateMRemoteInterrupted(f"Gate M remote controller received signal {signum}")


def _run_contained(
    command: list[str],
    *,
    application: Path,
    environment: dict[str, str],
    deadline_ns: int,
    label: str,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object]]:
    global ACTIVE_CONTAINMENT, ACTIVE_PROCESS
    containment = ProcessTreeContainment()
    ACTIVE_CONTAINMENT = containment
    process = subprocess.Popen(
        command,
        cwd=application,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    ACTIVE_PROCESS = process
    containment.attach(process.pid)
    stdout_payload = bytearray()
    stderr_payload = bytearray()
    selector = selectors.DefaultSelector()
    try:
        if process.stdout is None or process.stderr is None:
            raise M0Error(f"{label} pipes are unavailable")
        for stream, stream_label in (
            (process.stdout, "stdout"),
            (process.stderr, "stderr"),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, stream_label)
        while selector.get_map():
            events = selector.select(
                timeout=min(_remaining_seconds(deadline_ns, label), 1.0)
            )
            if not events:
                continue
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                target = stdout_payload if key.data == "stdout" else stderr_payload
                limit = (
                    MAX_DRIVER_STDOUT_BYTES
                    if key.data == "stdout"
                    else MAX_DRIVER_STDERR_BYTES
                )
                if len(target) + len(chunk) > limit:
                    terminate_active()
                    raise M0Error(f"{label} {key.data} exceeded its bound")
                target.extend(chunk)
        process.wait(timeout=_remaining_seconds(deadline_ns, label))
        cleanup = containment.assert_clean(
            term_grace_seconds=30,
            kill_grace_seconds=30,
        )
        ACTIVE_PROCESS = None
        ACTIVE_CONTAINMENT = None
    except subprocess.TimeoutExpired as exc:
        terminate_active()
        raise M0Error(f"{label} exceeded its absolute phase deadline") from exc
    except Exception:
        if ACTIVE_CONTAINMENT is not None:
            terminate_active()
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if ACTIVE_CONTAINMENT is not None:
            ACTIVE_CONTAINMENT.assert_clean(
                term_grace_seconds=30,
                kill_grace_seconds=30,
            )
            ACTIVE_PROCESS = None
            ACTIVE_CONTAINMENT = None
    return (
        subprocess.CompletedProcess(
            command,
            process.returncode,
            bytes(stdout_payload),
            bytes(stderr_payload),
        ),
        cleanup,
    )


def _write_transport(payload: bytes, *, deadline_ns: int) -> None:
    frame = frame_transport_envelope(payload)
    descriptor = sys.stdout.buffer.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    offset = 0
    try:
        selector.register(descriptor, selectors.EVENT_WRITE)
        while offset < len(frame):
            events = selector.select(
                timeout=min(_remaining_seconds(deadline_ns, "Gate M transport"), 1.0)
            )
            if not events:
                continue
            try:
                written = os.write(descriptor, frame[offset : offset + 1024 * 1024])
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise M0Error("Gate M transport consumer closed early") from exc
            if written <= 0:
                raise M0Error("Gate M transport made no write progress")
            offset += written
        if time.monotonic_ns() > deadline_ns:
            raise M0Error("Gate M transport crossed its absolute deadline")
    finally:
        selector.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--materialization-deadline-monotonic-ns", required=True)
    parser.add_argument("--runtime-provenance-deadline-monotonic-ns", required=True)
    parser.add_argument("--export-deadline-monotonic-ns", required=True)
    args = parser.parse_args()
    if (
        os.environ.get("MOE_PHASE7_MATERIALIZATION_UNLOCK")
        != "OWNER_APPROVED_EXACT_MATERIALIZATION_COMMAND"
    ):
        raise M0Error("missing exact materialization unlock")
    timeout_path_text = os.environ.get("MOE_PHASE7_REMOTE_TIMEOUT_EXECUTABLE", "")
    timeout_sha256 = os.environ.get(
        "MOE_PHASE7_REMOTE_TIMEOUT_EXECUTABLE_SHA256", ""
    )
    python_path_text = os.environ.get("MOE_PHASE7_REMOTE_PYTHON_EXECUTABLE", "")
    python_sha256 = os.environ.get(
        "MOE_PHASE7_REMOTE_PYTHON_EXECUTABLE_SHA256", ""
    )
    application = args.application_dir.resolve(strict=True)
    materialization_deadline_ns = _deadline(
        args.materialization_deadline_monotonic_ns,
        "materialization deadline",
    )
    provenance_deadline_ns = _deadline(
        args.runtime_provenance_deadline_monotonic_ns,
        "runtime provenance deadline",
    )
    export_deadline_ns = _deadline(
        args.export_deadline_monotonic_ns,
        "export deadline",
    )
    if not (
        time.monotonic_ns()
        < materialization_deadline_ns
        < provenance_deadline_ns
        < export_deadline_ns
    ):
        raise M0Error("Gate M absolute phase deadlines are expired or unordered")
    # Revalidate immediately on entry, before package traversal or any stage work.
    remote_timeout_executable = _validated_remote_executable(
        timeout_path_text, timeout_sha256, "remote timeout executable"
    )
    remote_python_executable = _validated_remote_executable(
        python_path_text, python_sha256, "remote Python executable"
    )
    if Path(sys.executable).resolve(strict=True) != remote_python_executable:
        raise M0Error("running Python identity changed before materialization")
    plan = load_json(application / "materialization_plan.template.json")
    approval = load_json(application / "materialization_approval.template.json")
    project_root = Path(plan["storage_contract"]["persistent_project_root"])
    target = Path(plan["deployment"]["application_target"])
    receipt_path = Path(plan["deployment"]["deployment_receipt"])
    if (
        application != target
        or approval["approved_deployment_project_root"] != str(project_root)
        or approval["approved_application_target"] != str(target)
        or approval["approved_deployment_receipt_path"] != str(receipt_path)
    ):
        raise M0Error("Gate M deployed path/approval binding differs")
    validate_mount_identity(
        plan["storage_contract"]["persistent_mount"],
        approval["approved_vault_mount_identity_sha256"],
    )
    receipt = verify_install(
        allowed_root=Path(plan["storage_contract"]["persistent_mount"]),
        target=target,
        receipt=receipt_path,
    )
    package = build_application_ledger(application)
    if (
        receipt["package_ledger"] != package
        or package["ledger_sha256"] != approval["application_ledger_sha256"]
    ):
        raise M0Error("Gate M receipt/application/approval ledger differs")
    evidence_root = args.evidence_root
    if (
        not evidence_root.is_absolute()
        or evidence_root.parent != project_root / "evidence"
        or evidence_root.exists()
        or evidence_root.is_symlink()
    ):
        raise M0Error("Gate M evidence root must be one fresh project evidence child")

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
            "MOE_PHASE7_MATERIALIZATION_UNLOCK",
            "MOE_PHASE7_CONTAINER_DIGEST",
        }
    }
    environment["CUDA_VISIBLE_DEVICES"] = ""
    remaining_materialization = int(
        _remaining_seconds(materialization_deadline_ns, "materialization")
    )
    work_seconds = (
        remaining_materialization
        - REMOTE_MATERIALIZATION_TERMINAL_RESERVE_SECONDS
    )
    if work_seconds <= 0:
        raise M0Error("materialization terminal reserve no longer fits")
    command = [
        str(remote_python_executable),
        str(application / "executor/materialization_driver.py"),
        "--application-dir",
        str(application),
        "--evidence-root",
        str(evidence_root),
        "--work-seconds",
        str(work_seconds),
    ]
    enable_child_subreaper()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    materialization_started_ns = time.monotonic_ns()
    completed, materialization_cleanup = _run_contained(
        command,
        application=application,
        environment=environment,
        deadline_ns=materialization_deadline_ns,
        label="materialization",
    )
    if (
        len(completed.stdout) > MAX_DRIVER_STDOUT_BYTES
        or len(completed.stderr) > MAX_DRIVER_STDERR_BYTES
    ):
        raise M0Error("Gate M materialization driver output exceeded its bound")
    if completed.returncode != 0:
        raise M0Error(
            "Gate M materialization driver failed: "
            f"returncode={completed.returncode}, "
            f"stderr_sha256={hashlib.sha256(completed.stderr).hexdigest()}"
        )
    if time.monotonic_ns() > materialization_deadline_ns:
        raise M0Error("materialization completion crossed its absolute deadline")
    ledger = verify_materialization_terminal(evidence_root)
    receipt_after = verify_install(
        allowed_root=Path(plan["storage_contract"]["persistent_mount"]),
        target=target,
        receipt=receipt_path,
    )
    if receipt_after != receipt:
        raise M0Error("Gate M deployment receipt changed during materialization")
    stage_result = load_json(evidence_root / "stage_result.json")
    materialization_ended_ns = time.monotonic_ns()
    provenance_root = Path(plan["runtime_provenance"]["output_root"])
    if (
        provenance_root != project_root / "evidence/runtime-provenance"
        or provenance_root.exists()
        or provenance_root.is_symlink()
    ):
        raise M0Error("runtime provenance root differs or is not fresh")
    provenance_started_ns = time.monotonic_ns()
    provenance_completed, provenance_cleanup = _run_contained(
        plan["runtime_provenance"]["command_argv"],
        application=application,
        environment=environment,
        deadline_ns=provenance_deadline_ns,
        label="runtime provenance",
    )
    if (
        len(provenance_completed.stdout) > MAX_DRIVER_STDOUT_BYTES
        or len(provenance_completed.stderr) > MAX_DRIVER_STDERR_BYTES
        or provenance_completed.returncode != 0
    ):
        raise M0Error(
            "Gate M runtime provenance failed: "
            f"returncode={provenance_completed.returncode}, "
            f"stderr_sha256={hashlib.sha256(provenance_completed.stderr).hexdigest()}"
        )
    provenance_ledger = verify_runtime_provenance(provenance_root)
    provenance_status = provenance_ledger["terminal_status"]
    provenance_record = (
        provenance_root / "runtime_provenance.json"
        if provenance_status == "COMPLETE"
        else provenance_root / "runtime_provenance_failure.json"
    )
    if time.monotonic_ns() > provenance_deadline_ns:
        raise M0Error("runtime provenance completion crossed its absolute deadline")
    provenance_ended_ns = time.monotonic_ns()
    export_started_ns = time.monotonic_ns()
    export_root = project_root / "export/gate-m"
    export_status_path = project_root / "export/gate-m.status"
    export_manifest = build_and_publish_export(
        application=application,
        receipt=receipt_path,
        materialization_root=evidence_root,
        runtime_provenance_root=provenance_root,
        export_root=export_root,
        status_path=export_status_path,
    )
    if time.monotonic_ns() > export_deadline_ns:
        raise M0Error("Gate M export crossed its absolute deadline")
    if verify_export(export_root, status_path=export_status_path) != export_manifest:
        raise M0Error("Gate M export replay differs")
    export_ended_ns = time.monotonic_ns()
    provenance_eligible = provenance_status == "COMPLETE"
    summary = {
        "schema_version": "moe-simulator-phase7-gate-m-remote-summary-v1",
        "status": (
            "REMOTE_COMPLETE_PROVENANCE_ELIGIBLE"
            if provenance_eligible
            else "REMOTE_COMPLETE_BLOCKED_PROVENANCE"
        ),
        "application_ledger_sha256": package["ledger_sha256"],
        "deployment_bundle_sha256": receipt["bundle_sha256"],
        "deployment_receipt_sha256": semantic_sha256(receipt),
        "materialization_evidence_ledger_sha256": ledger["ledger_sha256"],
        "model_ledger_sha256": stage_result["model_ledger_sha256"],
        "capacity_prompt_fixture_sha256": stage_result[
            "capacity_prompt_fixture_sha256"
        ],
        "runtime_provenance_ledger_sha256": provenance_ledger["ledger_sha256"],
        "runtime_provenance_record_sha256": hashlib.sha256(
            provenance_record.read_bytes()
        ).hexdigest(),
        "export_manifest_sha256": export_manifest["manifest_sha256"],
        "export_commit_marker_sha256": hashlib.sha256(
            export_status_path.read_bytes()
        ).hexdigest(),
        "driver_stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "driver_stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        "remote_command_executables": {
            "timeout": {
                "path": str(remote_timeout_executable),
                "sha256": timeout_sha256,
            },
            "python": {
                "path": str(remote_python_executable),
                "sha256": python_sha256,
            },
        },
        "process_cleanup": {
            "status": "CLEAN",
            "materialization": materialization_cleanup,
            "runtime_provenance": provenance_cleanup,
            "surviving_pids": [],
        },
        "phase_timing": {
            "materialization_start_monotonic_ns": materialization_started_ns,
            "materialization_end_monotonic_ns": materialization_ended_ns,
            "materialization_deadline_monotonic_ns": materialization_deadline_ns,
            "runtime_provenance_start_monotonic_ns": provenance_started_ns,
            "runtime_provenance_end_monotonic_ns": provenance_ended_ns,
            "runtime_provenance_deadline_monotonic_ns": provenance_deadline_ns,
            "export_start_monotonic_ns": export_started_ns,
            "export_end_monotonic_ns": export_ended_ns,
            "export_deadline_monotonic_ns": export_deadline_ns,
        },
        "runtime_provenance_status": provenance_status,
        "export_status": "REMOTE_COMPLETE_LOCAL_REPLAY_REQUIRED",
        "gpu_workload_performed": False,
        "next_legal_action": (
            "LOCAL_EXPORT_REPLAY_REQUIRED_BEFORE_M0_ELIGIBILITY"
            if provenance_eligible
            else "NO_M0_APPLICATION_PROVIDER_PROVENANCE_REQUIRED"
        ),
    }
    transport = build_transport_envelope(
        export_root=export_root,
        status_path=export_status_path,
        remote_summary=summary,
    )
    if time.monotonic_ns() > export_deadline_ns:
        raise M0Error("Gate M transport build crossed its absolute deadline")
    _write_transport(transport, deadline_ns=export_deadline_ns)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
