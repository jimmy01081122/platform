#!/usr/bin/env python3
"""Strict Gate M deployment plan, authority, and SSH command primitives."""

from __future__ import annotations

import base64
import hashlib
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (  # noqa: E402
    validate_allocation_window,
)
from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    SHA256_RE,
    canonical_bytes,
    file_sha256,
    load_json,
    load_json_bytes,
    semantic_sha256,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _receipt_object,
    _validated_bundle,
)
from explorations.moe_cycle_simulator.phase7.application.executor.disclosure import (  # noqa: E402
    SAFE_HOST_RE,
    SAFE_USER_RE,
    validate_known_hosts,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export import (  # noqa: E402
    parse_transport_frame,
    parse_transport_envelope,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)


PLAN_SCHEMA = "moe-simulator-phase7-gate-m-plan-v2"
APPROVAL_SCHEMA = "moe-simulator-phase7-gate-m-approval-v2"
APPLICATION_ID = "mixtral-rtxpro6000-bf16-m0-r12-20260729"
SAFE_DEPLOYMENT_ID_RE = re.compile(
    r"^phase7-gate-m-deploy-[a-z0-9][a-z0-9._-]{7,80}$"
)
SAFE_PROJECT_NAME_RE = re.compile(
    r"^flow-mixtral-rtxpro6000-r12-[a-z0-9][a-z0-9._-]{7,80}$"
)
PREPARED_DIRECTORIES = [
    "authority/registries",
    "evidence",
    "export",
    "fixtures",
    "incoming",
    "model/ledger",
    "packages/m0",
    "packages/materialization/repo/explorations/moe_cycle_simulator/phase7",
]
MATERIALIZATION_APPROVAL = "materialization_approval.template.json"


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise M0Error(
            f"{label} key closure mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise M0Error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _absolute(value: Any, label: str) -> Path:
    if not isinstance(value, str):
        raise M0Error(f"{label} must be an absolute path string")
    path = Path(value)
    if (
        not path.is_absolute()
        or "\\" in value
        or ".." in path.parts
        or str(path) != value
    ):
        raise M0Error(f"{label} must be a canonical absolute path")
    return path


def _resolved_file(path: Path, expected_sha256: str, label: str) -> Path:
    if (
        not path.is_file()
        or path.is_symlink()
        or path.resolve(strict=True) != path
        or file_sha256(path) != expected_sha256
    ):
        raise M0Error(f"{label} file/hash identity mismatch")
    return path


def _frozen_storage(plan: Mapping[str, Any]) -> tuple[Path, Path, Path, Path]:
    storage = plan["storage"]
    _exact_keys(
        storage,
        {
            "allowed_root",
            "expected_mount_identity_sha256",
            "project_root",
            "prepare_relative_directories",
            "incoming_bundle",
            "application_target",
            "deployment_receipt",
        },
        "deployment storage",
    )
    if storage["allowed_root"] != "/vault":
        raise M0Error("deployment allowed root must remain /vault")
    _hash(
        storage["expected_mount_identity_sha256"],
        "expected_mount_identity_sha256",
    )
    if storage["prepare_relative_directories"] != PREPARED_DIRECTORIES:
        raise M0Error("deployment prepared-directory layout changed")
    root = Path("/vault")
    project = _absolute(storage["project_root"], "project_root")
    if project.parent != root or SAFE_PROJECT_NAME_RE.fullmatch(project.name) is None:
        raise M0Error("deployment project root must be a fresh named child of /vault")
    incoming = _absolute(storage["incoming_bundle"], "incoming_bundle")
    target = _absolute(storage["application_target"], "application_target")
    receipt = _absolute(storage["deployment_receipt"], "deployment_receipt")
    expected = {
        "incoming": project / "incoming/application.bundle.json",
        "target": project
        / "packages/materialization/repo/explorations/moe_cycle_simulator/phase7/application",
        "receipt": project / "packages/materialization/deployment_receipt.json",
    }
    if incoming != expected["incoming"] or target != expected["target"] or receipt != expected["receipt"]:
        raise M0Error("deployment storage paths differ from the fixed fresh layout")
    return project, incoming, target, receipt


def validate_deployment_plan(
    plan: Mapping[str, Any],
    *,
    application_dir: Path,
    verify_files: bool,
) -> None:
    _exact_keys(
        plan,
        {
            "schema_version",
            "application_id",
            "status",
            "fresh_session_required",
            "retry_allowed",
            "resume_allowed",
            "ssh",
            "bootstrap",
            "storage",
            "allocation_window",
            "output",
            "authority",
        },
        "deployment plan",
    )
    if (
        plan["schema_version"] != PLAN_SCHEMA
        or plan["application_id"] != APPLICATION_ID
        or plan["status"] not in {"DRAFT_WITH_BLOCKING_FIELDS", "FROZEN"}
        or plan["fresh_session_required"] is not True
        or plan["retry_allowed"] is not False
        or plan["resume_allowed"] is not False
    ):
        raise M0Error("deployment plan identity or lifecycle changed")
    ssh = plan["ssh"]
    _exact_keys(
        ssh,
        {
            "executable",
            "executable_sha256",
            "host",
            "port",
            "username",
            "known_hosts_file",
            "known_hosts_file_sha256",
            "runtime_known_hosts_relative_path",
            "host_key_algorithm",
            "host_public_key_blob_sha256",
            "openssh_fingerprint",
            "credential_storage",
        },
        "deployment SSH",
    )
    bootstrap = plan["bootstrap"]
    _exact_keys(
        bootstrap,
        {
            "source_relative_path",
            "source_sha256",
            "deployment_bootstrap_relative_path",
            "deployment_bootstrap_sha256",
            "controller_relative_path",
            "controller_sha256",
            "remote_controller_relative_path",
            "remote_controller_sha256",
            "runtime_provenance_relative_path",
            "runtime_provenance_sha256",
            "exporter_relative_path",
            "exporter_sha256",
            "local_decoder_relative_path",
            "local_decoder_sha256",
            "remote_timeout_executable",
            "remote_timeout_executable_sha256",
            "remote_python_executable",
            "remote_python_executable_sha256",
            "transport",
            "ssh_timeout_seconds",
            "stage_work_seconds",
            "stage_kill_grace_seconds",
            "receipt_deadline_seconds",
            "materialization_deadline_seconds",
            "runtime_provenance_deadline_seconds",
            "remote_export_deadline_seconds",
            "local_replay_deadline_seconds",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "local_decode_rlimit_as_bytes",
            "ssh_argv_template_sha256",
        },
        "deployment bootstrap",
    )
    if (
        bootstrap["source_relative_path"] != "executor/gate_m_bootstrap.py"
        or bootstrap["deployment_bootstrap_relative_path"]
        != "executor/deployment_bootstrap.py"
        or bootstrap["controller_relative_path"] != "executor/deployment_controller.py"
        or bootstrap["remote_controller_relative_path"]
        != "executor/gate_m_remote.py"
        or bootstrap["runtime_provenance_relative_path"]
        != "executor/runtime_provenance.py"
        or bootstrap["exporter_relative_path"] != "executor/gate_m_export.py"
        or bootstrap["local_decoder_relative_path"]
        != "executor/gate_m_local_replay.py"
        or bootstrap["transport"]
        != "BASE64_BOOTSTRAPS_BUNDLE_STDIN_CANONICAL_BOUNDED_EXPORT_STDOUT_V1"
        or bootstrap["ssh_timeout_seconds"] != 4680
        or bootstrap["stage_work_seconds"] != 4800
        or bootstrap["stage_kill_grace_seconds"] != 600
        or bootstrap["receipt_deadline_seconds"] != 300
        or bootstrap["materialization_deadline_seconds"] != 4200
        or bootstrap["runtime_provenance_deadline_seconds"] != 4500
        or bootstrap["remote_export_deadline_seconds"] != 4620
        or bootstrap["local_replay_deadline_seconds"] != 4770
        or bootstrap["max_stdout_bytes"] != 100663424
        or bootstrap["max_stderr_bytes"] != 1048576
        or bootstrap["local_decode_rlimit_as_bytes"] != 805306368
    ):
        raise M0Error("deployment bootstrap scope changed")
    if plan["authority"] != {
        "required_unlock": "OWNER_APPROVED_EXACT_GATE_M_DEPLOYMENT_COMMAND",
        "remote_connections": 1,
        "remote_write_scope": "FRESH_PROJECT_ROOT_ONLY",
        "model_download": "ONE_PINNED_MIXTRAL_REVISION_ONLY",
        "network_scope": "PINNED_HUGGING_FACE_MODEL_MATERIALIZATION_ONLY",
        "package_install": False,
        "vllm_model_load": False,
        "gpu_compute": False,
    }:
        raise M0Error("deployment authority expanded")
    output = plan["output"]
    if set(output) != {"local_evidence_root", "terminal_sealing"} or output["terminal_sealing"] != "REQUIRED":
        raise M0Error("deployment output contract changed")
    allocation = plan["allocation_window"]
    _exact_keys(
        allocation,
        {
            "start_trigger",
            "lease_start_utc",
            "lease_deadline_utc",
            "total_seconds",
            "billing_mode",
            "extension_allowed",
            "additional_cost_allowed",
            "maximum_additional_spend_amount",
            "maximum_additional_spend_currency",
            "release_reserve_seconds",
        },
        "deployment allocation window",
    )
    if (
        allocation["start_trigger"] != "OWNER_RELEASES_FRESH_SSH_HANDOFF"
        or allocation["total_seconds"] != 21600
        or allocation["billing_mode"] != "PREPAID_FIXED_WINDOW"
        or allocation["extension_allowed"] is not False
        or allocation["additional_cost_allowed"] is not False
        or allocation["maximum_additional_spend_amount"] != "0"
        or allocation["maximum_additional_spend_currency"] != "TWD"
        or allocation["release_reserve_seconds"] != 900
    ):
        raise M0Error("deployment allocation authority changed")
    if plan["status"] != "FROZEN":
        return
    validate_allocation_window(allocation)
    if (
        SAFE_HOST_RE.fullmatch(ssh["host"]) is None
        or isinstance(ssh["port"], bool)
        or not isinstance(ssh["port"], int)
        or not 1 <= ssh["port"] <= 65535
        or SAFE_USER_RE.fullmatch(ssh["username"]) is None
        or ssh["runtime_known_hosts_relative_path"] != "deployment_inputs/known_hosts"
        or ssh["host_key_algorithm"] != "ssh-ed25519"
        or ssh["credential_storage"] != "EXTERNAL_NOT_RECORDED"
    ):
        raise M0Error("deployment SSH endpoint is invalid")
    for field in (
        "executable_sha256",
        "known_hosts_file_sha256",
        "host_public_key_blob_sha256",
    ):
        _hash(ssh[field], field)
    for field in (
        "source_sha256",
        "deployment_bootstrap_sha256",
        "controller_sha256",
        "remote_controller_sha256",
        "runtime_provenance_sha256",
        "exporter_sha256",
        "local_decoder_sha256",
        "remote_timeout_executable_sha256",
        "remote_python_executable_sha256",
        "ssh_argv_template_sha256",
    ):
        _hash(bootstrap[field], field)
    _frozen_storage(plan)
    _absolute(
        bootstrap["remote_timeout_executable"],
        "remote timeout executable",
    )
    _absolute(
        bootstrap["remote_python_executable"],
        "remote Python executable",
    )
    _absolute(output["local_evidence_root"], "local_evidence_root")
    if not verify_files:
        return
    application = application_dir.resolve(strict=True)
    _resolved_file(
        _absolute(ssh["executable"], "ssh executable"),
        ssh["executable_sha256"],
        "SSH executable",
    )
    known_hosts = _resolved_file(
        _absolute(ssh["known_hosts_file"], "known_hosts_file"),
        ssh["known_hosts_file_sha256"],
        "known_hosts",
    )
    validate_known_hosts(plan)
    for relative, digest, label in (
        (
            bootstrap["source_relative_path"],
            bootstrap["source_sha256"],
            "Gate M bootstrap",
        ),
        (
            bootstrap["deployment_bootstrap_relative_path"],
            bootstrap["deployment_bootstrap_sha256"],
            "deployment bootstrap",
        ),
        (
            bootstrap["controller_relative_path"],
            bootstrap["controller_sha256"],
            "controller",
        ),
        (
            bootstrap["remote_controller_relative_path"],
            bootstrap["remote_controller_sha256"],
            "remote controller",
        ),
        (
            bootstrap["runtime_provenance_relative_path"],
            bootstrap["runtime_provenance_sha256"],
            "runtime provenance collector",
        ),
        (
            bootstrap["exporter_relative_path"],
            bootstrap["exporter_sha256"],
            "Gate M exporter",
        ),
        (
            bootstrap["local_decoder_relative_path"],
            bootstrap["local_decoder_sha256"],
            "Gate M local decoder",
        ),
    ):
        _resolved_file(application / relative, digest, label)
    if file_sha256(known_hosts) != ssh["known_hosts_file_sha256"]:
        raise M0Error("known_hosts bytes changed")
    if semantic_sha256(build_deployment_ssh_argv(plan, application)) != bootstrap[
        "ssh_argv_template_sha256"
    ]:
        raise M0Error("deployment plan SSH argv hash differs")


def _bootstrap_remote_command(plan: Mapping[str, Any], application_dir: Path) -> str:
    bootstrap = plan["bootstrap"]
    storage = plan["storage"]
    source = (application_dir / bootstrap["source_relative_path"]).read_bytes()
    if hashlib.sha256(source).hexdigest() != bootstrap["source_sha256"]:
        raise M0Error("deployment bootstrap source differs from plan")
    encoded = base64.b64encode(source).decode("ascii")
    deployment_source = (
        application_dir / bootstrap["deployment_bootstrap_relative_path"]
    ).read_bytes()
    if hashlib.sha256(deployment_source).hexdigest() != bootstrap[
        "deployment_bootstrap_sha256"
    ]:
        raise M0Error("deployment bootstrap source differs from plan")
    deployment_encoded = base64.b64encode(deployment_source).decode("ascii")
    launcher = (
        "import base64;exec(compile(base64.b64decode(\""
        + encoded
        + "\"),\"deployment_bootstrap.py\",\"exec\"))"
    )
    argv = [
        bootstrap["remote_timeout_executable"],
        "--signal=TERM",
        f"--kill-after={bootstrap['stage_kill_grace_seconds']}s",
        str(bootstrap["stage_work_seconds"]),
        bootstrap["remote_python_executable"],
        "-I",
        "-B",
        "-c",
        launcher,
        "--allowed-root",
        storage["allowed_root"],
        "--project-root",
        storage["project_root"],
        "--expected-mount-identity-sha256",
        storage["expected_mount_identity_sha256"],
        "--incoming",
        storage["incoming_bundle"],
        "--target",
        storage["application_target"],
        "--receipt",
        storage["deployment_receipt"],
        "--expected-size",
        "__BUNDLE_SIZE_FROM_APPROVAL__",
        "--expected-sha256",
        "__BUNDLE_SHA256_FROM_APPROVAL__",
        "--deployment-bootstrap-source-base64",
        deployment_encoded,
        "--deployment-bootstrap-source-sha256",
        bootstrap["deployment_bootstrap_sha256"],
        "--remote-timeout-executable",
        bootstrap["remote_timeout_executable"],
        "--remote-timeout-executable-sha256",
        bootstrap["remote_timeout_executable_sha256"],
        "--remote-python-executable",
        bootstrap["remote_python_executable"],
        "--remote-python-executable-sha256",
        bootstrap["remote_python_executable_sha256"],
        "--materialization-evidence-root",
        str(Path(storage["project_root"]) / "evidence/materialization"),
    ]
    for relative in storage["prepare_relative_directories"]:
        argv.extend(("--prepare-relative-dir", relative))
    return shlex.join(argv)


def build_deployment_ssh_argv(
    plan: Mapping[str, Any],
    application_dir: Path,
    *,
    bundle_size: int | None = None,
    bundle_sha256: str | None = None,
) -> list[str]:
    ssh = plan["ssh"]
    remote = _bootstrap_remote_command(plan, application_dir)
    if bundle_size is not None or bundle_sha256 is not None:
        if (
            isinstance(bundle_size, bool)
            or not isinstance(bundle_size, int)
            or bundle_size <= 0
            or bundle_sha256 is None
        ):
            raise M0Error("bundle size/hash substitution is incomplete")
        _hash(bundle_sha256, "bundle_sha256")
        remote = remote.replace("__BUNDLE_SIZE_FROM_APPROVAL__", str(bundle_size))
        remote = remote.replace("__BUNDLE_SHA256_FROM_APPROVAL__", bundle_sha256)
    runtime_known_hosts = (
        Path(plan["output"]["local_evidence_root"])
        / ssh["runtime_known_hosts_relative_path"]
    )
    return [
        ssh["executable"],
        "-F",
        "/dev/null",
        "-T",
        "-p",
        str(ssh["port"]),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={runtime_known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "RequestTTY=no",
        "-o",
        "LogLevel=ERROR",
        "--",
        f"{ssh['username']}@{ssh['host']}",
        remote,
    ]


def validate_gate_m_remote_summary(
    summary: Mapping[str, Any],
    *,
    bundle_sha256: str,
    application_ledger_sha256: str,
    deployment_receipt_sha256: str,
) -> dict[str, Any]:
    """Validate the exact remote Gate M summary and its authority bindings."""

    _exact_keys(
        summary,
        {
            "schema_version",
            "status",
            "application_ledger_sha256",
            "deployment_bundle_sha256",
            "deployment_receipt_sha256",
            "materialization_evidence_ledger_sha256",
            "model_ledger_sha256",
            "capacity_prompt_fixture_sha256",
            "runtime_provenance_ledger_sha256",
            "runtime_provenance_record_sha256",
            "export_manifest_sha256",
            "export_commit_marker_sha256",
            "driver_stdout_sha256",
            "driver_stderr_sha256",
            "remote_command_executables",
            "process_cleanup",
            "phase_timing",
            "runtime_provenance_status",
            "export_status",
            "gpu_workload_performed",
            "next_legal_action",
        },
        "Gate M remote summary",
    )
    if (
        summary["schema_version"]
        != "moe-simulator-phase7-gate-m-remote-summary-v1"
        or summary["status"]
        not in {
            "REMOTE_COMPLETE_PROVENANCE_ELIGIBLE",
            "REMOTE_COMPLETE_BLOCKED_PROVENANCE",
        }
        or summary["application_ledger_sha256"]
        != _hash(application_ledger_sha256, "application ledger")
        or summary["deployment_bundle_sha256"] != bundle_sha256
        or summary["deployment_receipt_sha256"]
        != _hash(deployment_receipt_sha256, "deployment receipt")
        or summary["gpu_workload_performed"] is not False
    ):
        raise M0Error("Gate M remote summary identity or claim boundary differs")
    for field in (
        "materialization_evidence_ledger_sha256",
        "model_ledger_sha256",
        "capacity_prompt_fixture_sha256",
        "runtime_provenance_ledger_sha256",
        "runtime_provenance_record_sha256",
        "export_manifest_sha256",
        "export_commit_marker_sha256",
        "driver_stdout_sha256",
        "driver_stderr_sha256",
    ):
        _hash(summary[field], field)
    executables = summary["remote_command_executables"]
    if not isinstance(executables, dict) or set(executables) != {
        "timeout",
        "python",
    }:
        raise M0Error("Gate M remote executable identity closure differs")
    for name in ("timeout", "python"):
        identity = executables[name]
        if not isinstance(identity, dict) or set(identity) != {"path", "sha256"}:
            raise M0Error(f"Gate M remote {name} executable identity differs")
        _absolute(identity["path"], f"remote {name} executable")
        _hash(identity["sha256"], f"remote {name} executable")
    cleanup = summary["process_cleanup"]
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("status") != "CLEAN"
        or cleanup.get("surviving_pids") != []
    ):
        raise M0Error("Gate M remote process cleanup is incomplete")
    timing = summary["phase_timing"]
    timing_keys = {
        "materialization_start_monotonic_ns",
        "materialization_end_monotonic_ns",
        "materialization_deadline_monotonic_ns",
        "runtime_provenance_start_monotonic_ns",
        "runtime_provenance_end_monotonic_ns",
        "runtime_provenance_deadline_monotonic_ns",
        "export_start_monotonic_ns",
        "export_end_monotonic_ns",
        "export_deadline_monotonic_ns",
    }
    if (
        not isinstance(timing, dict)
        or set(timing) != timing_keys
        or any(
            isinstance(timing[key], bool)
            or not isinstance(timing[key], int)
            or timing[key] <= 0
            for key in timing_keys
        )
        or not (
            timing["materialization_start_monotonic_ns"]
            <= timing["materialization_end_monotonic_ns"]
            <= timing["materialization_deadline_monotonic_ns"]
            and timing["materialization_end_monotonic_ns"]
            <= timing["runtime_provenance_start_monotonic_ns"]
            <= timing["runtime_provenance_end_monotonic_ns"]
            <= timing["runtime_provenance_deadline_monotonic_ns"]
            and timing["runtime_provenance_end_monotonic_ns"]
            <= timing["export_start_monotonic_ns"]
            <= timing["export_end_monotonic_ns"]
            <= timing["export_deadline_monotonic_ns"]
            and timing["materialization_deadline_monotonic_ns"]
            < timing["runtime_provenance_deadline_monotonic_ns"]
            < timing["export_deadline_monotonic_ns"]
        )
    ):
        raise M0Error("Gate M remote phase timing is invalid")
    if summary["status"] == "REMOTE_COMPLETE_PROVENANCE_ELIGIBLE":
        if (
            summary["runtime_provenance_status"] != "COMPLETE"
            or summary["export_status"]
            != "REMOTE_COMPLETE_LOCAL_REPLAY_REQUIRED"
            or summary["next_legal_action"]
            != "LOCAL_EXPORT_REPLAY_REQUIRED_BEFORE_M0_ELIGIBILITY"
        ):
            raise M0Error("provenance-eligible remote Gate M claim differs")
    else:
        if (
            summary["runtime_provenance_status"] != "BLOCKED"
            or summary["export_status"]
            != "REMOTE_COMPLETE_LOCAL_REPLAY_REQUIRED"
            or summary["next_legal_action"]
            != "NO_M0_APPLICATION_PROVIDER_PROVENANCE_REQUIRED"
        ):
            raise M0Error("provenance-blocked remote Gate M claim differs")
    return dict(summary)


def parse_gate_m_stdout(
    payload: bytes,
    *,
    bundle_sha256: str,
    application_ledger_sha256: str,
    deployment_receipt_sha256: str,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Validate one length/hash framed canonical export transport and EOF."""

    transport_payload = parse_transport_frame(payload)
    transport = parse_transport_envelope(transport_payload)
    summary = validate_gate_m_remote_summary(
        transport["remote_summary"],
        bundle_sha256=bundle_sha256,
        application_ledger_sha256=application_ledger_sha256,
        deployment_receipt_sha256=deployment_receipt_sha256,
    )
    return summary, transport_payload, transport


def validate_deployment_approval(
    approval: Mapping[str, Any],
    *,
    approval_path: Path,
    plan: Mapping[str, Any],
    plan_path: Path,
    application_dir: Path,
    owner_authority_record_sha256: str,
) -> dict[str, Any]:
    _exact_keys(
        approval,
        {
            "schema_version",
            "application_id",
            "gate_m_session_id",
            "approval_id",
            "approval_token_sha256",
            "used_once_registry_path",
            "application_ledger_sha256",
            "deployment_plan_sha256",
            "bootstrap_source_sha256",
            "deployment_bootstrap_source_sha256",
            "controller_sha256",
            "remote_controller_sha256",
            "runtime_provenance_sha256",
            "exporter_sha256",
            "local_decoder_sha256",
            "approved_remote_timeout_executable",
            "approved_remote_timeout_executable_sha256",
            "approved_remote_python_executable",
            "approved_remote_python_executable_sha256",
            "bundle",
            "approved_d0_result_path",
            "approved_d0_result_sha256",
            "approved_vault_mount_identity_sha256",
            "approved_ssh_host_key_sha256",
            "exact_deployment_ssh_argv_sha256",
            "allocation_window",
            "approved_local_evidence_root",
            "owner_authority_record_sha256",
            "decision",
            "approved_at_utc",
            "owner_identity",
            "authority",
            "not_authorized",
        },
        "deployment approval",
    )
    if (
        approval["schema_version"] != APPROVAL_SCHEMA
        or approval["application_id"] != APPLICATION_ID
        or SAFE_DEPLOYMENT_ID_RE.fullmatch(approval["gate_m_session_id"])
        is None
        or approval["decision"] != "APPROVE"
    ):
        raise M0Error("deployment approval identity or decision is invalid")
    for field in (
        "approval_token_sha256",
        "application_ledger_sha256",
        "deployment_plan_sha256",
        "bootstrap_source_sha256",
        "deployment_bootstrap_source_sha256",
        "controller_sha256",
        "remote_controller_sha256",
        "runtime_provenance_sha256",
        "exporter_sha256",
        "local_decoder_sha256",
        "approved_remote_timeout_executable_sha256",
        "approved_remote_python_executable_sha256",
        "approved_d0_result_sha256",
        "approved_vault_mount_identity_sha256",
        "approved_ssh_host_key_sha256",
        "exact_deployment_ssh_argv_sha256",
        "owner_authority_record_sha256",
    ):
        _hash(approval[field], field)
    registry = _absolute(approval["used_once_registry_path"], "used_once_registry_path")
    evidence_root = _absolute(
        approval["approved_local_evidence_root"], "approved_local_evidence_root"
    )
    if evidence_root != _absolute(
        plan["output"]["local_evidence_root"], "plan local_evidence_root"
    ):
        raise M0Error("deployment approval evidence root differs from plan")
    if registry.parent == evidence_root or registry.is_relative_to(evidence_root):
        raise M0Error("deployment registry must remain outside fresh evidence")
    if approval["allocation_window"] != plan["allocation_window"]:
        raise M0Error("deployment approval allocation window differs")
    validate_allocation_window(approval["allocation_window"])
    if approval["authority"] != [
        "one exact host-key-pinned SSH Gate M connection",
        "fresh Vault project envelope creation",
        "exact bundle receive install seal and receipt verification",
        "one exact pinned Mixtral revision materialization",
        "CPU-only static runtime provenance capture",
        "remote export and local terminal evidence replay",
    ]:
        raise M0Error("deployment approval authority changed")
    if set(approval["not_authorized"]) != {
        "unapproved or non-pinned model download",
        "package manager installation",
        "CUDA workload",
        "vLLM model load",
        "M0",
        "M1",
        "M2",
        "M3",
        "M4",
        "retry",
        "resume",
        "allocation extension",
        "additional cost",
    }:
        raise M0Error("deployment approval prohibition set changed")
    application = application_dir.resolve(strict=True)
    plan_path = plan_path.resolve(strict=True)
    approval_path = approval_path.resolve(strict=True)
    owner_path = application / "owner_environment_decision_20260729.json"
    if (
        file_sha256(plan_path) != approval["deployment_plan_sha256"]
        or file_sha256(application / plan["bootstrap"]["source_relative_path"])
        != approval["bootstrap_source_sha256"]
        or file_sha256(
            application
            / plan["bootstrap"]["deployment_bootstrap_relative_path"]
        )
        != approval["deployment_bootstrap_source_sha256"]
        or file_sha256(application / plan["bootstrap"]["controller_relative_path"])
        != approval["controller_sha256"]
        or file_sha256(
            application / plan["bootstrap"]["remote_controller_relative_path"]
        )
        != approval["remote_controller_sha256"]
        or file_sha256(
            application / plan["bootstrap"]["runtime_provenance_relative_path"]
        )
        != approval["runtime_provenance_sha256"]
        or file_sha256(application / plan["bootstrap"]["exporter_relative_path"])
        != approval["exporter_sha256"]
        or file_sha256(
            application / plan["bootstrap"]["local_decoder_relative_path"]
        )
        != approval["local_decoder_sha256"]
        or approval["approved_remote_timeout_executable"]
        != plan["bootstrap"]["remote_timeout_executable"]
        or approval["approved_remote_timeout_executable_sha256"]
        != plan["bootstrap"]["remote_timeout_executable_sha256"]
        or approval["approved_remote_python_executable"]
        != plan["bootstrap"]["remote_python_executable"]
        or approval["approved_remote_python_executable_sha256"]
        != plan["bootstrap"]["remote_python_executable_sha256"]
        or file_sha256(owner_path) != owner_authority_record_sha256
        or approval["owner_authority_record_sha256"]
        != owner_authority_record_sha256
    ):
        raise M0Error("deployment approval source/owner binding differs")
    _absolute(
        approval["approved_remote_timeout_executable"],
        "approved remote timeout executable",
    )
    _absolute(
        approval["approved_remote_python_executable"],
        "approved remote Python executable",
    )
    application_ledger = build_application_ledger(application)
    if application_ledger["ledger_sha256"] != approval["application_ledger_sha256"]:
        raise M0Error("deployment approval application ledger differs")

    bundle_binding = approval["bundle"]
    _exact_keys(
        bundle_binding,
        {
            "local_path",
            "size_bytes",
            "sha256",
            "package_ledger_sha256",
            "included_materialization_approval_sha256",
            "expected_deployment_receipt_sha256",
        },
        "deployment approval bundle",
    )
    bundle_path = _absolute(bundle_binding["local_path"], "bundle local_path")
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise M0Error("deployment bundle must be a real regular file")
    payload = bundle_path.read_bytes()
    if (
        isinstance(bundle_binding["size_bytes"], bool)
        or not isinstance(bundle_binding["size_bytes"], int)
        or bundle_binding["size_bytes"] != len(payload)
        or file_sha256(bundle_path) != _hash(bundle_binding["sha256"], "bundle sha256")
    ):
        raise M0Error("deployment bundle size/hash differs")
    bundle, decoded = _validated_bundle(payload)
    if (
        bundle["package_ledger"]["ledger_sha256"]
        != _hash(bundle_binding["package_ledger_sha256"], "bundle package ledger")
        or bundle["package_ledger"] != application_ledger
    ):
        raise M0Error("deployment bundle/application package ledger differs")
    members = {item["path"]: item for item in bundle["members"]}
    materialization_hash = _hash(
        bundle_binding["included_materialization_approval_sha256"],
        "included materialization approval sha256",
    )
    if members.get(MATERIALIZATION_APPROVAL, {}).get("sha256") != materialization_hash:
        raise M0Error("bundle materialization approval bytes differ")
    project, _, target, _ = _frozen_storage(plan)
    materialization_approval = load_json_bytes(
        decoded[MATERIALIZATION_APPROVAL], "bundled Gate M authority projection"
    )
    materialization_plan = load_json_bytes(
        decoded["materialization_plan.template.json"],
        "bundled materialization plan",
    )
    remote_registry = project / "authority/registries/gate-m-consumption.json"
    if (
        materialization_approval.get("application_id") != approval["application_id"]
        or materialization_approval.get("approval_id") != approval["approval_id"]
        or materialization_approval.get("approval_token_sha256")
        != approval["approval_token_sha256"]
        or materialization_approval.get("used_once_registry_path")
        != str(remote_registry)
        or materialization_approval.get("application_ledger_sha256")
        != approval["application_ledger_sha256"]
        or materialization_approval.get("materialization_plan_sha256")
        != hashlib.sha256(decoded["materialization_plan.template.json"]).hexdigest()
        or materialization_approval.get("exact_materialization_commands_sha256")
        != semantic_sha256(
            {
                "materialize": materialization_plan.get("command_argv"),
                "prompt_fixture": materialization_plan.get(
                    "prompt_fixture_command_argv"
                ),
                "runtime_provenance": materialization_plan.get(
                    "runtime_provenance", {}
                ).get("command_argv"),
            }
        )
        or materialization_approval.get("approved_ssh_host_key_sha256")
        != approval["approved_ssh_host_key_sha256"]
        or materialization_approval.get("approved_d0_result_sha256")
        != approval["approved_d0_result_sha256"]
        or materialization_approval.get("approved_vault_mount_identity_sha256")
        != approval["approved_vault_mount_identity_sha256"]
        or materialization_approval.get("approved_deployment_project_root")
        != str(project)
        or materialization_approval.get("approved_application_target") != str(target)
        or materialization_approval.get("approved_deployment_receipt_path")
        != plan["storage"]["deployment_receipt"]
        or materialization_approval.get("allocation_window")
        != approval["allocation_window"]
        or materialization_approval.get("owner_authority_record_sha256")
        != approval["owner_authority_record_sha256"]
        or materialization_approval.get("application_decision") != "APPROVE"
        or materialization_approval.get("materialization_decision") != "APPROVE"
        or materialization_approval.get("approved_at_utc")
        != approval["approved_at_utc"]
        or materialization_approval.get("owner_identity") != approval["owner_identity"]
    ):
        raise M0Error("bundled materialization authority is not the Gate M projection")
    receipt = _receipt_object(
        allowed_root=Path("/vault"),
        target=target,
        bundle=bundle,
        bundle_sha256=bundle_binding["sha256"],
    )
    expected_receipt_hash = hashlib.sha256(canonical_bytes(receipt)).hexdigest()
    if expected_receipt_hash != _hash(
        bundle_binding["expected_deployment_receipt_sha256"],
        "expected deployment receipt sha256",
    ):
        raise M0Error("expected deployment receipt hash differs")

    d0_path = _absolute(approval["approved_d0_result_path"], "approved_d0_result_path")
    _resolved_file(d0_path, approval["approved_d0_result_sha256"], "D0 result")
    d0 = load_json(d0_path)
    if (
        d0.get("disclosure_status") != "COMPLETE"
        or d0.get("environment_eligibility")
        != "READY_FOR_MATERIALIZATION_APPLICATION"
        or d0.get("vault_mount_identity_sha256")
        != approval["approved_vault_mount_identity_sha256"]
        or plan["storage"]["expected_mount_identity_sha256"]
        != approval["approved_vault_mount_identity_sha256"]
    ):
        raise M0Error("deployment approval D0/Vault binding differs")
    if (
        approval["approved_ssh_host_key_sha256"]
        != plan["ssh"]["host_public_key_blob_sha256"]
    ):
        raise M0Error("deployment approval host key differs")
    argv = build_deployment_ssh_argv(
        plan,
        application,
        bundle_size=bundle_binding["size_bytes"],
        bundle_sha256=bundle_binding["sha256"],
    )
    if semantic_sha256(argv) != approval["exact_deployment_ssh_argv_sha256"]:
        raise M0Error("deployment approval exact SSH argv differs")
    return {
        "application_ledger": application_ledger,
        "bundle": bundle,
        "bundle_path": bundle_path,
        "bundle_bytes": payload,
        "expected_receipt": receipt,
        "expected_receipt_sha256": expected_receipt_hash,
        "project_root": project,
        "ssh_argv": argv,
        "approval_file_sha256": file_sha256(approval_path),
    }
