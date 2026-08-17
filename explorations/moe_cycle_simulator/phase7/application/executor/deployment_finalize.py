#!/usr/bin/env python3
"""Seal and independently replay one local Gate M deployment evidence tree."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    validate_retained_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    exact_regular_file_set,
    SHA256_RE,
    file_sha256,
    load_json,
    semantic_sha256,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _rename_noreplace,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export import (  # noqa: E402
    verify_export_projection,
)


DeploymentOutcome = Literal["COMPLETE", "FAILED", "INCOMPLETE"]
STATUS_PAYLOAD = {
    "COMPLETE": "GATE_M_DEPLOYMENT_COMPLETE_HARD_STOP\n",
    "FAILED": "GATE_M_DEPLOYMENT_FAILED_IMMUTABLE_NO_RETRY\n",
    "INCOMPLETE": "GATE_M_DEPLOYMENT_INCOMPLETE_IMMUTABLE_NO_RETRY\n",
}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o400,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _hash_field(record: dict[str, Any], field: str) -> None:
    if not isinstance(record.get(field), str) or SHA256_RE.fullmatch(record[field]) is None:
        raise M0Error(f"deployment terminal record has invalid {field}")


def _terminal_record(root: Path, outcome: DeploymentOutcome) -> dict[str, Any]:
    if outcome == "COMPLETE":
        if (root / "deployment_failure.json").exists():
            raise M0Error("deployment COMPLETE cannot coexist with failure")
        record = load_json(root / "deployment_result.json")
        expected = {
            "schema_version",
            "status",
            "application_id",
            "gate_m_session_id",
            "authority_evidence_sha256",
            "deployment_plan_sha256",
            "deployment_approval_sha256",
            "application_ledger_sha256",
            "bundle_sha256",
            "bundle_size_bytes",
            "expected_deployment_receipt_sha256",
            "gate_m_remote_summary_sha256",
            "gate_m_transport_receipt_sha256",
            "gate_m_transport_sha256",
            "gate_m_transport_size_bytes",
            "local_decode_rlimit_as_bytes",
            "local_decode_result_sha256",
            "local_decode_execution_sha256",
            "remote_command_executables",
            "gate_m_remote_status",
            "gate_m_status",
            "remote_materialization_evidence_ledger_sha256",
            "remote_model_ledger_sha256",
            "remote_capacity_prompt_fixture_sha256",
            "remote_runtime_provenance_ledger_sha256",
            "remote_runtime_provenance_record_sha256",
            "remote_export_manifest_sha256",
            "remote_export_commit_marker_sha256",
            "local_export_manifest_sha256",
            "local_export_status_sha256",
            "runtime_provenance_status",
            "export_status",
            "approved_d0_result_sha256",
            "vault_mount_identity_sha256",
            "exact_ssh_argv_sha256",
            "ssh",
            "timing",
            "remote_write_performed",
            "model_downloaded",
            "gpu_workload_performed",
            "next_legal_action",
        }
        if (
            set(record) != expected
            or record.get("schema_version")
            != "moe-simulator-phase7-gate-m-deployment-result-v1"
            or record.get("status") != "COMPLETE"
            or record.get("remote_write_performed") is not True
            or record.get("model_downloaded") is not True
            or record.get("gpu_workload_performed") is not False
            or record.get("gate_m_status")
            not in {"COMPLETE_M0_ELIGIBLE", "COMPLETE_M0_BLOCKED_PROVENANCE"}
        ):
            raise M0Error("deployment result identity or claim boundary differs")
        for field in (
            "authority_evidence_sha256",
            "deployment_plan_sha256",
            "deployment_approval_sha256",
            "application_ledger_sha256",
            "bundle_sha256",
            "expected_deployment_receipt_sha256",
            "gate_m_remote_summary_sha256",
            "gate_m_transport_receipt_sha256",
            "gate_m_transport_sha256",
            "local_decode_result_sha256",
            "local_decode_execution_sha256",
            "remote_materialization_evidence_ledger_sha256",
            "remote_model_ledger_sha256",
            "remote_capacity_prompt_fixture_sha256",
            "remote_runtime_provenance_ledger_sha256",
            "remote_runtime_provenance_record_sha256",
            "remote_export_manifest_sha256",
            "remote_export_commit_marker_sha256",
            "local_export_manifest_sha256",
            "local_export_status_sha256",
            "approved_d0_result_sha256",
            "vault_mount_identity_sha256",
            "exact_ssh_argv_sha256",
        ):
            _hash_field(record, field)
        from explorations.moe_cycle_simulator.phase7.application.executor.deployment import (
            validate_gate_m_remote_summary,
        )
        from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_local_replay import (
            validate_local_replay_result,
        )

        summary_path = root / "gate_m_remote_summary.json"
        summary = load_json(summary_path)
        local_decode_result_path = root / "local_decode_result.json"
        local_decode_execution_path = root / "local_decode_execution.json"
        local_decode_result = validate_local_replay_result(
            load_json(local_decode_result_path),
            input_frame_path=root / "ssh.stdout.log",
            address_space_bytes=record["local_decode_rlimit_as_bytes"],
        )
        parsed_summary = validate_gate_m_remote_summary(
            local_decode_result["remote_summary"],
            bundle_sha256=record["bundle_sha256"],
            application_ledger_sha256=record["application_ledger_sha256"],
            deployment_receipt_sha256=record[
                "expected_deployment_receipt_sha256"
            ],
        )
        projection = verify_export_projection(
            root / "gate_m_export",
            status_path=root / "gate_m_export.status",
            remote_summary=parsed_summary,
        )
        local_manifest = projection["manifest"]
        transport_receipt_path = root / "gate_m_transport_receipt.json"
        transport_receipt = load_json(transport_receipt_path)
        if set(transport_receipt) != {
            "schema_version",
            "transport_sha256",
            "transport_size_bytes",
            "remote_export_manifest_sha256",
            "local_export_manifest_sha256",
            "local_export_status_sha256",
            "local_replay_status",
        } or transport_receipt.get("schema_version") != (
            "moe-simulator-phase7-gate-m-transport-receipt-v1"
        ):
            raise M0Error("Gate M transport receipt identity differs")
        expected_gate_status = (
            "COMPLETE_M0_ELIGIBLE"
            if parsed_summary["status"]
            == "REMOTE_COMPLETE_PROVENANCE_ELIGIBLE"
            else "COMPLETE_M0_BLOCKED_PROVENANCE"
        )
        expected_next_action = (
            "REQUEST_NEW_M0_APPLICATION"
            if expected_gate_status == "COMPLETE_M0_ELIGIBLE"
            else "NO_M0_APPLICATION_PROVIDER_PROVENANCE_REQUIRED"
        )
        if (
            summary != parsed_summary
            or file_sha256(summary_path)
            != record["gate_m_remote_summary_sha256"]
            or record["gate_m_remote_status"] != summary["status"]
            or record["gate_m_status"] != expected_gate_status
            or record["gate_m_transport_receipt_sha256"]
            != file_sha256(transport_receipt_path)
            or record["local_decode_result_sha256"]
            != file_sha256(local_decode_result_path)
            or record["local_decode_execution_sha256"]
            != file_sha256(local_decode_execution_path)
            or record["gate_m_transport_sha256"]
            != local_decode_result["transport_sha256"]
            or record["gate_m_transport_size_bytes"]
            != local_decode_result["transport_size_bytes"]
            or transport_receipt["transport_sha256"]
            != local_decode_result["transport_sha256"]
            or transport_receipt["transport_size_bytes"]
            != local_decode_result["transport_size_bytes"]
            or record["remote_command_executables"]
            != summary["remote_command_executables"]
            or record["remote_materialization_evidence_ledger_sha256"]
            != summary["materialization_evidence_ledger_sha256"]
            or record["remote_model_ledger_sha256"]
            != summary["model_ledger_sha256"]
            or record["remote_capacity_prompt_fixture_sha256"]
            != summary["capacity_prompt_fixture_sha256"]
            or record["remote_runtime_provenance_ledger_sha256"]
            != summary["runtime_provenance_ledger_sha256"]
            or record["remote_runtime_provenance_record_sha256"]
            != summary["runtime_provenance_record_sha256"]
            or record["remote_export_manifest_sha256"]
            != summary["export_manifest_sha256"]
            or record["remote_export_commit_marker_sha256"]
            != summary["export_commit_marker_sha256"]
            or record["local_export_manifest_sha256"]
            != local_manifest["manifest_sha256"]
            or record["local_export_manifest_sha256"]
            != summary["export_manifest_sha256"]
            or record["local_export_status_sha256"]
            != file_sha256(root / "gate_m_export.status")
            or transport_receipt["remote_export_manifest_sha256"]
            != summary["export_manifest_sha256"]
            or transport_receipt["local_export_manifest_sha256"]
            != local_manifest["manifest_sha256"]
            or transport_receipt["local_export_status_sha256"]
            != record["local_export_status_sha256"]
            or transport_receipt["local_replay_status"]
            != "COMPLETE_REPLAYED"
            or record["runtime_provenance_status"]
            != summary["runtime_provenance_status"]
            or record["export_status"] != "COMPLETE_REPLAYED"
            or record["next_legal_action"] != expected_next_action
        ):
            raise M0Error("deployment result and retained Gate M summary differ")
        if isinstance(record.get("bundle_size_bytes"), bool) or not isinstance(
            record.get("bundle_size_bytes"), int
        ) or record["bundle_size_bytes"] <= 0:
            raise M0Error("deployment result bundle size is invalid")
        if (
            isinstance(record.get("gate_m_transport_size_bytes"), bool)
            or not isinstance(record.get("gate_m_transport_size_bytes"), int)
            or record["gate_m_transport_size_bytes"] <= 0
        ):
            raise M0Error("deployment result transport size is invalid")
        if (
            isinstance(record.get("local_decode_rlimit_as_bytes"), bool)
            or not isinstance(record.get("local_decode_rlimit_as_bytes"), int)
            or record["local_decode_rlimit_as_bytes"] != 805306368
        ):
            raise M0Error("deployment result local decoder RLIMIT_AS is invalid")
        execution = load_json(local_decode_execution_path)
        retained_plan = load_json(root / "deployment_inputs/plan.json")
        execution_keys = {
            "schema_version",
            "status",
            "decoder_source_path",
            "decoder_source_sha256",
            "interpreter_path",
            "interpreter_sha256",
            "argv_sha256",
            "rlimit_as_bytes",
            "max_log_bytes",
            "stdout_size_bytes",
            "stderr_size_bytes",
            "log_limit_exceeded",
            "deadline_monotonic_ns",
            "started_monotonic_ns",
            "finished_monotonic_ns",
            "returncode",
            "timed_out",
            "stdout_sha256",
            "stderr_sha256",
            "process_tree_cleanup",
        }
        if (
            set(execution) != execution_keys
            or execution.get("schema_version")
            != "moe-simulator-phase7-gate-m-local-decode-execution-v1"
            or execution.get("status") != "COMPLETE"
            or execution.get("rlimit_as_bytes")
            != record["local_decode_rlimit_as_bytes"]
            or execution.get("decoder_source_sha256")
            != retained_plan.get("bootstrap", {}).get("local_decoder_sha256")
            or execution.get("deadline_monotonic_ns")
            != local_decode_result["deadline_monotonic_ns"]
            or execution.get("stdout_sha256")
            != file_sha256(root / "local_decode.stdout.log")
            or execution.get("stderr_sha256")
            != file_sha256(root / "local_decode.stderr.log")
            or execution.get("timed_out") is not False
            or execution.get("returncode") != 0
            or execution.get("log_limit_exceeded") is not None
            or isinstance(execution.get("max_log_bytes"), bool)
            or not isinstance(execution.get("max_log_bytes"), int)
            or execution["max_log_bytes"] <= 0
            or any(
                isinstance(execution.get(field), bool)
                or not isinstance(execution.get(field), int)
                or execution[field] < 0
                or execution[field] > execution["max_log_bytes"]
                for field in ("stdout_size_bytes", "stderr_size_bytes")
            )
            or execution.get("process_tree_cleanup", {}).get("status") != "CLEAN"
            or retained_plan.get("bootstrap", {}).get(
                "remote_timeout_executable"
            )
            != record["remote_command_executables"]["timeout"]["path"]
            or retained_plan.get("bootstrap", {}).get(
                "remote_timeout_executable_sha256"
            )
            != record["remote_command_executables"]["timeout"]["sha256"]
            or retained_plan.get("bootstrap", {}).get(
                "remote_python_executable"
            )
            != record["remote_command_executables"]["python"]["path"]
            or retained_plan.get("bootstrap", {}).get(
                "remote_python_executable_sha256"
            )
            != record["remote_command_executables"]["python"]["sha256"]
        ):
            raise M0Error("deployment local decoder execution evidence differs")
        for field in (
            "decoder_source_sha256",
            "interpreter_sha256",
            "argv_sha256",
            "stdout_sha256",
            "stderr_sha256",
        ):
            _hash_field(execution, field)
        ssh = record.get("ssh")
        timing = record.get("timing")
        if (
            not isinstance(ssh, dict)
            or set(ssh)
            != {
                "endpoint",
                "host_public_key_blob_sha256",
                "returncode",
                "stdout_sha256",
                "stderr_sha256",
            }
            or not isinstance(ssh.get("endpoint"), dict)
            or set(ssh["endpoint"]) != {"host", "port", "username"}
            or ssh.get("returncode") != 0
            or not isinstance(timing, dict)
            or set(timing)
            != {
                "controller_start_utc",
                "controller_end_utc",
                "controller_origin_monotonic_ns",
                "elapsed_monotonic_ns",
                "ssh_elapsed_monotonic_ns",
                "ssh_eof_deadline_monotonic_ns",
                "local_replay_deadline_monotonic_ns",
                "outer_deadline_monotonic_ns",
                "lease_start_utc",
                "lease_deadline_utc",
            }
            or any(
                isinstance(timing.get(field), bool)
                or not isinstance(timing.get(field), int)
                or timing[field] <= 0
                for field in (
                    "controller_origin_monotonic_ns",
                    "elapsed_monotonic_ns",
                    "ssh_elapsed_monotonic_ns",
                    "ssh_eof_deadline_monotonic_ns",
                    "local_replay_deadline_monotonic_ns",
                    "outer_deadline_monotonic_ns",
                )
            )
            or not (
                timing["controller_origin_monotonic_ns"]
                < timing["ssh_eof_deadline_monotonic_ns"]
                < timing["local_replay_deadline_monotonic_ns"]
                < timing["outer_deadline_monotonic_ns"]
                and timing["ssh_elapsed_monotonic_ns"]
                <= timing["elapsed_monotonic_ns"]
                and timing["controller_origin_monotonic_ns"]
                + timing["elapsed_monotonic_ns"]
                <= timing["local_replay_deadline_monotonic_ns"]
            )
        ):
            raise M0Error("deployment result SSH/timing closure differs")
        for field in ("host_public_key_blob_sha256", "stdout_sha256", "stderr_sha256"):
            _hash_field(ssh, field)
    else:
        if (root / "deployment_result.json").exists():
            raise M0Error("deployment failure cannot coexist with result")
        record = load_json(root / "deployment_failure.json")
        expected = {
            "schema_version",
            "status",
            "failure_type",
            "failure",
            "controller_start_utc",
            "controller_end_utc",
            "elapsed_monotonic_ns",
            "authority_evidence_sha256",
            "remote_write_may_have_occurred",
            "gpu_workload_performed",
            "retry_allowed",
            "resume_allowed",
        }
        if (
            set(record) != expected
            or record.get("schema_version")
            != "moe-simulator-phase7-gate-m-deployment-failure-v1"
            or record.get("status") != outcome
            or record.get("gpu_workload_performed") is not False
            or record.get("retry_allowed") is not False
            or record.get("resume_allowed") is not False
        ):
            raise M0Error("deployment failure record differs")
        authority_hash = record.get("authority_evidence_sha256")
        if authority_hash is not None:
            _hash_field(record, "authority_evidence_sha256")
    authority_dir = root / "authority"
    if outcome == "COMPLETE" and not authority_dir.exists():
        raise M0Error("deployment COMPLETE requires retained authority")
    if authority_dir.exists():
        authority = validate_retained_authority(
            evidence_root=root,
            require_package_match=outcome == "COMPLETE",
        )
        if record.get("authority_evidence_sha256") != semantic_sha256(authority):
            raise M0Error("deployment terminal record does not bind authority")
    elif record.get("authority_evidence_sha256") is not None:
        raise M0Error("deployment terminal record names absent authority")
    return record


def _build_ledger(root: Path, outcome: DeploymentOutcome) -> dict[str, Any]:
    excluded = {
        "evidence_ledger.json",
        "deployment_status.txt",
        ".evidence_ledger.json.staged",
        ".deployment_status.txt.staged",
    }
    members: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise M0Error(f"deployment evidence symlink is forbidden: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"evidence_ledger.json", "deployment_status.txt"}:
            raise M0Error("deployment terminal artifact exists before sealing")
        if relative in excluded:
            raise M0Error(f"stale deployment staging artifact: {relative}")
        if not path.is_file():
            raise M0Error(f"deployment evidence is not regular: {relative}")
        members.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    marker = STATUS_PAYLOAD[outcome].encode("utf-8")
    members.append(
        {
            "path": "deployment_status.txt",
            "size_bytes": len(marker),
            "sha256": hashlib.sha256(marker).hexdigest(),
        }
    )
    members.sort(key=lambda item: item["path"])
    ledger: dict[str, Any] = {
        "schema_version": "moe-simulator-phase7-gate-m-deployment-ledger-v1",
        "terminal_status": outcome,
        "terminal_marker": STATUS_PAYLOAD[outcome].strip(),
        "member_count": len(members),
        "members": members,
    }
    ledger["ledger_sha256"] = semantic_sha256(ledger)
    return ledger


def seal_deployment_terminal(
    root: Path,
    outcome: DeploymentOutcome,
    *,
    publish_deadline_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if outcome not in STATUS_PAYLOAD:
        raise M0Error("invalid deployment outcome")
    _terminal_record(root, outcome)
    ledger = _build_ledger(root, outcome)
    staged_ledger = root / ".evidence_ledger.json.staged"
    staged_status = root / ".deployment_status.txt.staged"
    final_ledger = root / "evidence_ledger.json"
    final_status = root / "deployment_status.txt"
    payload = (
        json.dumps(ledger, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    try:
        _write_new(staged_ledger, payload)
        _write_new(staged_status, STATUS_PAYLOAD[outcome].encode("utf-8"))
        _fsync_directory(root)
        for path in sorted(root.rglob("*"), reverse=True):
            if path in {staged_ledger, staged_status}:
                continue
            if path.is_symlink():
                raise M0Error(f"deployment evidence symlink is forbidden: {path}")
            path.chmod(0o444 if path.is_file() else 0o555)
        _rename_noreplace(staged_ledger, final_ledger)
        _fsync_directory(root)
        if (
            publish_deadline_monotonic_ns is not None
            and time.monotonic_ns() > publish_deadline_monotonic_ns
        ):
            raise M0Error("deployment terminal publication deadline expired")
        _rename_noreplace(staged_status, final_status)
        _fsync_directory(root)
        root.chmod(0o555)
    except Exception:
        root.chmod(0o700)
        for path in (staged_ledger, staged_status, final_ledger, final_status):
            _remove(path)
        _fsync_directory(root)
        raise
    verify_deployment_terminal(root)
    return ledger


def verify_deployment_terminal(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    ledger = load_json(root / "evidence_ledger.json")
    if set(ledger) != {
        "schema_version",
        "terminal_status",
        "terminal_marker",
        "member_count",
        "members",
        "ledger_sha256",
    }:
        raise M0Error("deployment ledger key closure differs")
    digest_input = dict(ledger)
    claimed = digest_input.pop("ledger_sha256", None)
    outcome = ledger.get("terminal_status")
    if (
        ledger.get("schema_version")
        != "moe-simulator-phase7-gate-m-deployment-ledger-v1"
        or outcome not in STATUS_PAYLOAD
        or ledger.get("terminal_marker") != STATUS_PAYLOAD[outcome].strip()
        or claimed != semantic_sha256(digest_input)
    ):
        raise M0Error("deployment ledger identity/root differs")
    members = ledger.get("members")
    if not isinstance(members, list):
        raise M0Error("deployment ledger members are not an array")
    paths = [item.get("path") for item in members if isinstance(item, dict)]
    if (
        len(paths) != len(members)
        or ledger.get("member_count") != len(members)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or "evidence_ledger.json" in paths
        or "deployment_status.txt" not in paths
    ):
        raise M0Error("deployment ledger member closure differs")
    actual = exact_regular_file_set(
        root, excluded_root_files={"evidence_ledger.json"}
    )
    if set(paths) != actual:
        raise M0Error("deployment ledger is not an exact file set")
    for item in members:
        if set(item) != {"path", "size_bytes", "sha256"}:
            raise M0Error("deployment ledger member keys differ")
        path = root / item["path"]
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item["size_bytes"]
            or file_sha256(path) != item["sha256"]
            or path.stat().st_mode & 0o222
        ):
            raise M0Error(f"deployment evidence member differs: {item['path']}")
    for path in (root, *root.rglob("*")):
        if path.is_dir() and path.stat().st_mode & 0o222:
            raise M0Error(f"deployment evidence directory is writable: {path}")
    if (root / "deployment_status.txt").read_text(encoding="utf-8") != STATUS_PAYLOAD[
        outcome
    ]:
        raise M0Error("deployment terminal marker differs")
    _terminal_record(root, outcome)
    return ledger
