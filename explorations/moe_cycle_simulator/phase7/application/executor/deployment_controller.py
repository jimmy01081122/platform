#!/usr/bin/env python3
"""Consume one exact approval and perform one bounded Gate M package deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (  # noqa: E402
    M0_STAGE_SECONDS,
    MATERIALIZATION_STAGE_SECONDS,
    require_remaining_budget,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    retain_authority,
    validate_retained_authority,
    write_exact_new,
)
from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    load_json,
    load_json_bytes,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment import (  # noqa: E402
    build_deployment_ssh_argv,
    validate_deployment_approval,
    validate_deployment_plan,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_finalize import (  # noqa: E402
    seal_deployment_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _rename_noreplace,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_local_replay import (  # noqa: E402
    run_bounded_decoder_process,
    validate_local_replay_result,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.process_tree import (  # noqa: E402
    ProcessTreeContainment,
    enable_child_subreaper,
)


ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
ACTIVE_CONTAINMENT: ProcessTreeContainment | None = None
REMOTE_WRITE_STARTED = False


class DeploymentInterrupted(M0Error):
    """The local deployment controller received an outer signal."""


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise M0Error("deployment evidence spool made no write progress")
        offset += written


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def terminate_active() -> dict[str, object]:
    global ACTIVE_CONTAINMENT, ACTIVE_PROCESS
    if ACTIVE_CONTAINMENT is None:
        return {"status": "NOT_STARTED"}
    cleanup = ACTIVE_CONTAINMENT.terminate(
        term_grace_seconds=10,
        kill_grace_seconds=10,
    )
    if ACTIVE_PROCESS is not None:
        try:
            ACTIVE_PROCESS.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            raise M0Error("deployment SSH child survived containment") from exc
    ACTIVE_PROCESS = None
    ACTIVE_CONTAINMENT = None
    return cleanup


def signal_handler(signum: int, _frame: Any) -> None:
    terminate_active()
    raise DeploymentInterrupted(f"deployment controller received signal {signum}")


def consume_approval(approval: dict[str, Any], approval_bytes: bytes) -> Path:
    registry = Path(approval["used_once_registry_path"])
    if not registry.is_absolute() or registry.name in {"", ".", ".."}:
        raise M0Error("deployment one-shot registry path must be absolute")
    parent = registry.parent.resolve(strict=True)
    if parent != registry.parent or parent.is_symlink():
        raise M0Error("deployment registry parent must be a real directory")
    payload = {
        "schema_version": "moe-simulator-phase7-used-deployment-approval-v1",
        "approval_id": approval["approval_id"],
        "approval_token_sha256": approval["approval_token_sha256"],
        "gate_m_session_id": approval["gate_m_session_id"],
        "approval_file_sha256": hashlib.sha256(approval_bytes).hexdigest(),
    }
    try:
        descriptor = os.open(
            registry,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
        )
    except FileExistsError as exc:
        raise M0Error("deployment approval was already consumed") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return registry


def run_ssh(
    argv: list[str],
    *,
    bundle: bytes,
    deadline_monotonic_ns: int,
    stdout_path: Path,
    stderr_path: Path,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> tuple[int, bool, int, int, int]:
    global ACTIVE_CONTAINMENT, ACTIVE_PROCESS, REMOTE_WRITE_STARTED
    if (
        not bundle
        or isinstance(deadline_monotonic_ns, bool)
        or not isinstance(deadline_monotonic_ns, int)
        or deadline_monotonic_ns <= time.monotonic_ns()
        or isinstance(max_stdout_bytes, bool)
        or max_stdout_bytes <= 0
        or isinstance(max_stderr_bytes, bool)
        or max_stderr_bytes <= 0
    ):
        raise M0Error("deployment SSH streaming bounds are invalid")
    for path in (stdout_path, stderr_path):
        if path.exists() or path.is_symlink():
            raise M0Error("deployment SSH spool path is not fresh")
        parent = path.parent.resolve(strict=True)
        if parent != path.parent or parent.is_symlink():
            raise M0Error("deployment SSH spool parent is unsafe")
    stdout_descriptor = os.open(
        stdout_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        stderr_descriptor = os.open(
            stderr_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except Exception:
        os.close(stdout_descriptor)
        stdout_path.unlink()
        raise
    containment = ProcessTreeContainment()
    ACTIVE_CONTAINMENT = containment
    started = time.monotonic_ns()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception:
        os.close(stdout_descriptor)
        os.close(stderr_descriptor)
        stdout_path.unlink()
        stderr_path.unlink()
        ACTIVE_CONTAINMENT = None
        raise
    REMOTE_WRITE_STARTED = True
    ACTIVE_PROCESS = process
    containment.attach(process.pid)
    timed_out = False
    stdout_count = 0
    stderr_count = 0
    bundle_offset = 0
    deadline_ns = deadline_monotonic_ns
    selector = selectors.DefaultSelector()
    if process.stdin is None or process.stdout is None or process.stderr is None:
        terminate_active()
        raise M0Error("deployment SSH pipes are unavailable")
    try:
        for stream, events, label in (
            (process.stdin, selectors.EVENT_WRITE, "stdin"),
            (process.stdout, selectors.EVENT_READ, "stdout"),
            (process.stderr, selectors.EVENT_READ, "stderr"),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events, label)
        while selector.get_map():
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                timed_out = True
                terminate_active()
                break
            events = selector.select(timeout=min(remaining_ns / 1_000_000_000, 1.0))
            if not events:
                continue
            for key, _mask in events:
                stream = key.fileobj
                if key.data == "stdin":
                    try:
                        written = os.write(stream.fileno(), bundle[bundle_offset : bundle_offset + 1024 * 1024])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    if written:
                        bundle_offset += written
                    if bundle_offset == len(bundle) or process.poll() is not None:
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 1024 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if key.data == "stdout":
                    stdout_count += len(chunk)
                    if stdout_count > max_stdout_bytes:
                        terminate_active()
                        raise M0Error("deployment SSH stdout exceeded its frozen bound")
                    _write_all(stdout_descriptor, chunk)
                else:
                    stderr_count += len(chunk)
                    if stderr_count > max_stderr_bytes:
                        terminate_active()
                        raise M0Error("deployment SSH stderr exceeded its frozen bound")
                    _write_all(stderr_descriptor, chunk)
        if not timed_out:
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                timed_out = True
                terminate_active()
            else:
                try:
                    process.wait(timeout=remaining_ns / 1_000_000_000)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_active()
    finally:
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        os.fsync(stdout_descriptor)
        os.fsync(stderr_descriptor)
        os.close(stdout_descriptor)
        os.close(stderr_descriptor)
        stdout_path.chmod(0o400)
        stderr_path.chmod(0o400)
        if ACTIVE_CONTAINMENT is not None:
            ACTIVE_CONTAINMENT.assert_clean(
                term_grace_seconds=10,
                kill_grace_seconds=10,
            )
        ACTIVE_PROCESS = None
        ACTIVE_CONTAINMENT = None
    if not timed_out and bundle_offset != len(bundle):
        raise M0Error("deployment SSH closed before the complete bundle was written")
    return (
        process.returncode if process.returncode is not None else -signal.SIGKILL,
        timed_out,
        time.monotonic_ns() - started,
        stdout_count,
        stderr_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    controller_origin_ns = time.monotonic_ns()
    local_result_deadline_ns = controller_origin_ns + 4770 * 1_000_000_000
    outer_deadline_ns = controller_origin_ns + 4800 * 1_000_000_000
    if (
        os.environ.get("MOE_PHASE7_DEPLOYMENT_UNLOCK")
        != "OWNER_APPROVED_EXACT_GATE_M_DEPLOYMENT_COMMAND"
    ):
        raise M0Error("missing exact Gate M deployment second-factor unlock")
    application = args.application_dir.resolve(strict=True)
    plan_path = application / "deployment_plan.template.json"
    owner_path = application / "owner_environment_decision_20260729.json"
    approval_path = args.approval.resolve(strict=True)
    plan_bytes = plan_path.read_bytes()
    approval_bytes = approval_path.read_bytes()
    owner_bytes = owner_path.read_bytes()
    plan = load_json_bytes(plan_bytes, str(plan_path))
    approval = load_json_bytes(approval_bytes, str(approval_path))
    validate_deployment_plan(plan, application_dir=application, verify_files=True)
    validation = validate_deployment_approval(
        approval,
        approval_path=approval_path,
        plan=plan,
        plan_path=plan_path,
        application_dir=application,
        owner_authority_record_sha256=hashlib.sha256(owner_bytes).hexdigest(),
    )
    require_remaining_budget(
        approval["allocation_window"],
        stage_outer_seconds=MATERIALIZATION_STAGE_SECONDS,
        downstream_reserve_seconds=M0_STAGE_SECONDS,
    )
    root = args.evidence_root
    if str(root) != approval["approved_local_evidence_root"]:
        raise M0Error("deployment evidence root differs from approval")
    if not root.is_absolute() or root.name in {"", ".", ".."}:
        raise M0Error("deployment evidence root must be an absolute fresh path")
    parent = root.parent.resolve(strict=True)
    if parent != root.parent or parent.is_symlink():
        raise M0Error("deployment evidence parent must be a real directory")
    root.mkdir(mode=0o700, exist_ok=False)
    started_utc = utc_now()
    started_monotonic = controller_origin_ns
    authority: dict[str, Any] | None = None
    enable_child_subreaper()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    try:
        registry = consume_approval(approval, approval_bytes)
        authority = retain_authority(
            application=application,
            approval_path=approval_path,
            registry_path=registry,
            evidence_root=root,
            expected_application_ledger_sha256=approval[
                "application_ledger_sha256"
            ],
            approval_bytes=approval_bytes,
            package_ledger=validation["application_ledger"],
        )
        inputs = root / "deployment_inputs"
        inputs.mkdir(mode=0o700)
        known_hosts_bytes = Path(plan["ssh"]["known_hosts_file"]).read_bytes()
        retained = {
            "plan.json": plan_bytes,
            "known_hosts": known_hosts_bytes,
            "gate_m_bootstrap.py": (
                application / plan["bootstrap"]["source_relative_path"]
            ).read_bytes(),
            "deployment_bootstrap.py": (
                application
                / plan["bootstrap"]["deployment_bootstrap_relative_path"]
            ).read_bytes(),
            "gate_m_remote.py": (
                application / plan["bootstrap"]["remote_controller_relative_path"]
            ).read_bytes(),
            "runtime_provenance.py": (
                application / plan["bootstrap"]["runtime_provenance_relative_path"]
            ).read_bytes(),
            "gate_m_export.py": (
                application / plan["bootstrap"]["exporter_relative_path"]
            ).read_bytes(),
            "gate_m_local_replay.py": (
                application / plan["bootstrap"]["local_decoder_relative_path"]
            ).read_bytes(),
            "d0_result.json": Path(approval["approved_d0_result_path"]).read_bytes(),
            "owner_environment_decision.json": owner_bytes,
            "application.bundle.json": validation["bundle_bytes"],
        }
        for name, payload in retained.items():
            write_exact_new(inputs / name, payload)
        if file_sha256(inputs / "known_hosts") != plan["ssh"][
            "known_hosts_file_sha256"
        ]:
            raise M0Error("retained deployment known_hosts bytes differ")
        argv = build_deployment_ssh_argv(
            plan,
            application,
            bundle_size=approval["bundle"]["size_bytes"],
            bundle_sha256=approval["bundle"]["sha256"],
        )
        stdout_path = root / "ssh.stdout.log"
        stderr_path = root / "ssh.stderr.log"
        returncode, timed_out, ssh_elapsed, stdout_size, stderr_size = run_ssh(
            argv,
            bundle=validation["bundle_bytes"],
            deadline_monotonic_ns=(
                controller_origin_ns
                + plan["bootstrap"]["ssh_timeout_seconds"] * 1_000_000_000
            ),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            max_stdout_bytes=plan["bootstrap"]["max_stdout_bytes"],
            max_stderr_bytes=plan["bootstrap"]["max_stderr_bytes"],
        )
        if (
            stdout_path.stat().st_size != stdout_size
            or stderr_path.stat().st_size != stderr_size
        ):
            raise M0Error("deployment SSH bounded spool size differs")
        if timed_out:
            raise TimeoutError("deployment SSH exceeded its frozen timeout")
        if returncode != 0:
            raise M0Error(f"deployment SSH failed with return code {returncode}")
        local_export_root = root / "gate_m_export"
        local_export_status = root / "gate_m_export.status"
        local_decode_result_path = root / "local_decode_result.json"
        local_decode_execution_path = root / "local_decode_execution.json"
        decoder_source = (
            application / plan["bootstrap"]["local_decoder_relative_path"]
        )
        local_module_root = Path(__file__).resolve(strict=True).parents[5]
        decoder_launcher = (
            "import runpy,sys;sys.path.insert(0,"
            + repr(str(local_module_root))
            + ");runpy.run_path("
            + repr(str(decoder_source))
            + ",run_name='__main__')"
        )
        decoder_argv = [
            str(Path(sys.executable).resolve(strict=True)),
            "-I",
            "-B",
            "-c",
            decoder_launcher,
            "--transport-frame",
            str(stdout_path),
            "--bundle-sha256",
            approval["bundle"]["sha256"],
            "--application-ledger-sha256",
            approval["application_ledger_sha256"],
            "--deployment-receipt-sha256",
            approval["bundle"]["expected_deployment_receipt_sha256"],
            "--export-root",
            str(local_export_root),
            "--export-status",
            str(local_export_status),
            "--result",
            str(local_decode_result_path),
            "--rlimit-as-bytes",
            str(plan["bootstrap"]["local_decode_rlimit_as_bytes"]),
            "--deadline-monotonic-ns",
            str(local_result_deadline_ns),
        ]
        run_bounded_decoder_process(
            decoder_argv,
            decoder_source=decoder_source,
            decoder_source_sha256=plan["bootstrap"]["local_decoder_sha256"],
            address_space_bytes=plan["bootstrap"]["local_decode_rlimit_as_bytes"],
            deadline_monotonic_ns=local_result_deadline_ns,
            stdout_path=root / "local_decode.stdout.log",
            stderr_path=root / "local_decode.stderr.log",
            execution_record_path=local_decode_execution_path,
        )
        local_decode_result = validate_local_replay_result(
            load_json(local_decode_result_path),
            input_frame_path=stdout_path,
            address_space_bytes=plan["bootstrap"]["local_decode_rlimit_as_bytes"],
        )
        remote_summary = local_decode_result["remote_summary"]
        expected_remote_executables = {
            "timeout": {
                "path": plan["bootstrap"]["remote_timeout_executable"],
                "sha256": plan["bootstrap"]["remote_timeout_executable_sha256"],
            },
            "python": {
                "path": plan["bootstrap"]["remote_python_executable"],
                "sha256": plan["bootstrap"]["remote_python_executable_sha256"],
            },
        }
        if remote_summary["remote_command_executables"] != expected_remote_executables:
            raise M0Error("remote executable evidence differs from approved identities")
        write_new_json(root / "gate_m_remote_summary.json", remote_summary)
        gate_m_status = (
            "COMPLETE_M0_ELIGIBLE"
            if remote_summary["status"]
            == "REMOTE_COMPLETE_PROVENANCE_ELIGIBLE"
            else "COMPLETE_M0_BLOCKED_PROVENANCE"
        )
        next_legal_action = (
            "REQUEST_NEW_M0_APPLICATION"
            if gate_m_status == "COMPLETE_M0_ELIGIBLE"
            else "NO_M0_APPLICATION_PROVIDER_PROVENANCE_REQUIRED"
        )
        write_new_json(
            root / "gate_m_transport_receipt.json",
            {
                "schema_version": "moe-simulator-phase7-gate-m-transport-receipt-v1",
                "transport_sha256": local_decode_result["transport_sha256"],
                "transport_size_bytes": local_decode_result["transport_size_bytes"],
                "remote_export_manifest_sha256": remote_summary[
                    "export_manifest_sha256"
                ],
                "local_export_manifest_sha256": local_decode_result[
                    "export_manifest_sha256"
                ],
                "local_export_status_sha256": file_sha256(local_export_status),
                "local_replay_status": "COMPLETE_REPLAYED",
            },
        )
        authority = validate_retained_authority(
            evidence_root=root,
            require_package_match=True,
        )
        result = {
            "schema_version": "moe-simulator-phase7-gate-m-deployment-result-v1",
            "status": "COMPLETE",
            "application_id": approval["application_id"],
            "gate_m_session_id": approval["gate_m_session_id"],
            "authority_evidence_sha256": semantic_sha256(authority),
            "deployment_plan_sha256": file_sha256(plan_path),
            "deployment_approval_sha256": file_sha256(approval_path),
            "application_ledger_sha256": approval["application_ledger_sha256"],
            "bundle_sha256": approval["bundle"]["sha256"],
            "bundle_size_bytes": approval["bundle"]["size_bytes"],
            "expected_deployment_receipt_sha256": approval["bundle"][
                "expected_deployment_receipt_sha256"
            ],
            "gate_m_remote_summary_sha256": file_sha256(
                root / "gate_m_remote_summary.json"
            ),
            "gate_m_transport_receipt_sha256": file_sha256(
                root / "gate_m_transport_receipt.json"
            ),
            "gate_m_transport_sha256": local_decode_result["transport_sha256"],
            "gate_m_transport_size_bytes": local_decode_result["transport_size_bytes"],
            "local_decode_rlimit_as_bytes": plan["bootstrap"][
                "local_decode_rlimit_as_bytes"
            ],
            "local_decode_result_sha256": file_sha256(local_decode_result_path),
            "local_decode_execution_sha256": file_sha256(
                local_decode_execution_path
            ),
            "remote_command_executables": expected_remote_executables,
            "gate_m_remote_status": remote_summary["status"],
            "gate_m_status": gate_m_status,
            "remote_materialization_evidence_ledger_sha256": remote_summary[
                "materialization_evidence_ledger_sha256"
            ],
            "remote_model_ledger_sha256": remote_summary["model_ledger_sha256"],
            "remote_capacity_prompt_fixture_sha256": remote_summary[
                "capacity_prompt_fixture_sha256"
            ],
            "remote_runtime_provenance_ledger_sha256": remote_summary[
                "runtime_provenance_ledger_sha256"
            ],
            "remote_runtime_provenance_record_sha256": remote_summary[
                "runtime_provenance_record_sha256"
            ],
            "remote_export_manifest_sha256": remote_summary[
                "export_manifest_sha256"
            ],
            "remote_export_commit_marker_sha256": remote_summary[
                "export_commit_marker_sha256"
            ],
            "local_export_manifest_sha256": local_decode_result[
                "export_manifest_sha256"
            ],
            "local_export_status_sha256": file_sha256(local_export_status),
            "runtime_provenance_status": remote_summary[
                "runtime_provenance_status"
            ],
            "export_status": "COMPLETE_REPLAYED",
            "approved_d0_result_sha256": approval["approved_d0_result_sha256"],
            "vault_mount_identity_sha256": approval[
                "approved_vault_mount_identity_sha256"
            ],
            "exact_ssh_argv_sha256": semantic_sha256(argv),
            "ssh": {
                "endpoint": {
                    "host": plan["ssh"]["host"],
                    "port": plan["ssh"]["port"],
                    "username": plan["ssh"]["username"],
                },
                "host_public_key_blob_sha256": plan["ssh"][
                    "host_public_key_blob_sha256"
                ],
                "returncode": returncode,
                "stdout_sha256": file_sha256(root / "ssh.stdout.log"),
                "stderr_sha256": file_sha256(root / "ssh.stderr.log"),
            },
            "timing": {
                "controller_start_utc": started_utc,
                "controller_end_utc": utc_now(),
                "controller_origin_monotonic_ns": controller_origin_ns,
                "elapsed_monotonic_ns": time.monotonic_ns() - started_monotonic,
                "ssh_elapsed_monotonic_ns": ssh_elapsed,
                "ssh_eof_deadline_monotonic_ns": (
                    controller_origin_ns
                    + plan["bootstrap"]["ssh_timeout_seconds"] * 1_000_000_000
                ),
                "local_replay_deadline_monotonic_ns": (
                    controller_origin_ns
                    + plan["bootstrap"]["local_replay_deadline_seconds"]
                    * 1_000_000_000
                ),
                "outer_deadline_monotonic_ns": (
                    outer_deadline_ns
                ),
                "lease_start_utc": approval["allocation_window"]["lease_start_utc"],
                "lease_deadline_utc": approval["allocation_window"][
                    "lease_deadline_utc"
                ],
            },
            "remote_write_performed": True,
            "model_downloaded": True,
            "gpu_workload_performed": False,
            "next_legal_action": next_legal_action,
        }
        write_new_json(root / "deployment_result.json", result)
        if time.monotonic_ns() > local_result_deadline_ns:
            raise TimeoutError("local Gate M result crossed its absolute deadline")
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        seal_deployment_terminal(
            root,
            "COMPLETE",
            publish_deadline_monotonic_ns=outer_deadline_ns,
        )
        print(semantic_sha256(result))
        return 0
    except Exception as exc:
        provisional_result = root / "deployment_result.json"
        if provisional_result.exists():
            _rename_noreplace(
                provisional_result,
                root / "deployment_partial_result.json",
            )
        if authority is None and (root / "authority").exists():
            try:
                authority = validate_retained_authority(
                    evidence_root=root,
                    require_package_match=False,
                )
            except Exception:
                pass
        failure = {
            "schema_version": "moe-simulator-phase7-gate-m-deployment-failure-v1",
            "status": (
                "INCOMPLETE"
                if isinstance(
                    exc,
                    (TimeoutError, KeyboardInterrupt, DeploymentInterrupted),
                )
                else "FAILED"
            ),
            "failure_type": type(exc).__name__,
            "failure": str(exc),
            "controller_start_utc": started_utc,
            "controller_end_utc": utc_now(),
            "elapsed_monotonic_ns": time.monotonic_ns() - started_monotonic,
            "authority_evidence_sha256": (
                semantic_sha256(authority) if authority is not None else None
            ),
            "remote_write_may_have_occurred": REMOTE_WRITE_STARTED,
            "gpu_workload_performed": False,
            "retry_allowed": False,
            "resume_allowed": False,
        }
        write_new_json(root / "deployment_failure.json", failure)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        seal_deployment_terminal(root, failure["status"])
        if isinstance(exc, M0Error):
            raise
        raise M0Error(str(exc)) from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
