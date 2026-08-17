#!/usr/bin/env python3
"""CPU-only validator for draft, materialization, and M0 execution gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))


EXPECTED_GPU = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"
EXPECTED_MODEL = "mistralai/Mixtral-8x7B-Instruct-v0.1"
EXPECTED_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
BLOCKING_PREFIX = "[BLOCKING:"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
APPROVAL_EXCLUSIONS = {
    "approval.template.json",
    "environment_disclosure_approval.template.json",
    "materialization_approval.template.json",
}


class ValidationError(RuntimeError):
    pass


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_float(value: str) -> None:
    raise ValidationError(f"JSON floating-point value is forbidden: {value}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                object_pairs_hook=reject_duplicates,
                parse_float=reject_float,
                parse_constant=reject_float,
            )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{path}: {exc}") from exc
    require(isinstance(value, dict), f"{path}: JSON root must be an object")
    return value


def walk_blockers(value: Any, location: str = "$") -> list[str]:
    blockers: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            blockers.extend(walk_blockers(child, f"{location}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            blockers.extend(walk_blockers(child, f"{location}[{index}]"))
    elif isinstance(value, str) and value.startswith(BLOCKING_PREFIX):
        blockers.append(location)
    return blockers


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def decimal_value(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"{field} is not a decimal amount") from exc
    require(result >= 0, f"{field} must be nonnegative")
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def application_ledger_sha256(directory: Path) -> str:
    rows = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValidationError(f"application symlink is forbidden: {path}")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in APPROVAL_EXCLUSIONS or relative.endswith(".pyc"):
            continue
        rows.append(f"{file_sha256(path)}  {relative}\n".encode("utf-8"))
    require(rows, "recursive application ledger is empty")
    return hashlib.sha256(b"".join(rows)).hexdigest()


def unresolved(documents: dict[str, dict[str, Any]], names: tuple[str, ...]) -> list[str]:
    blockers = []
    for name in names:
        blockers.extend(
            f"{name}:{location}" for location in walk_blockers(documents[name])
        )
    return blockers


def validate_common(
    app: dict[str, Any],
    runtime: dict[str, Any],
    plan: dict[str, Any],
    materialization: dict[str, Any],
) -> None:
    require(app["requested_promotion_stage"] == "M0_ONLY", "application must be M0_ONLY")
    allocation = app["allocation_window_contract"]
    require(
        allocation
        == {
            "total_seconds": 21600,
            "start_trigger": "OWNER_RELEASES_FRESH_SSH_HANDOFF",
            "billing_mode": "PREPAID_FIXED_WINDOW",
            "extension_allowed": False,
            "additional_cost_allowed": False,
            "maximum_additional_spend_amount": "0",
            "maximum_additional_spend_currency": "TWD",
            "release_reserve_seconds": 900,
        },
        "application allocation contract changed",
    )
    require(app["estimated_resources"]["reservation_hours"] == 6, "reservation must be six hours")
    require(app["target"]["gpu_count"] == 1, "exactly one GPU is permitted")
    require(app["target"]["exact_product_name"] == EXPECTED_GPU, "wrong target GPU")
    require(app["target"]["minimum_memory_bytes"] == 96_000_000_000, "wrong memory floor")
    require(app["model"]["model_id"] == EXPECTED_MODEL, "wrong application model")
    require(app["model"]["repository_commit"] == EXPECTED_REVISION, "wrong model revision")
    require(app["model"]["precision"] == "BF16", "precision must remain BF16")
    require(app["model"]["quantization"] is None, "quantization is forbidden")
    require(app["model"]["cpu_offload_gb"] == 0, "CPU offload is forbidden")
    require(app["model"]["swap_space_gb"] == 0, "swap-backed execution is forbidden")
    require(materialization["model"] == {
        "model_id": EXPECTED_MODEL,
        "repository_commit": EXPECTED_REVISION,
    }, "materialization model identity mismatch")
    require(
        materialization["materializer"]["method"] == "snapshot_download_local_dir",
        "materialization method mismatch",
    )
    require(materialization["materializer"]["max_workers"] == 4, "materializer worker count changed")
    require(runtime["model"]["model_id"] == EXPECTED_MODEL, "runtime model mismatch")
    require(runtime["model"]["repository_commit"] == EXPECTED_REVISION, "runtime revision mismatch")
    require(runtime["model"]["precision"] == "BF16", "runtime precision must be BF16")
    require(runtime["model"]["quantization"] is None, "runtime quantization is forbidden")
    rt = runtime["runtime"]
    require(rt["name"] == "vLLM", "runtime must be vLLM")
    require(rt["tensor_parallel_size"] == 1, "tensor parallelism is outside M0")
    require(rt["expert_parallel_size"] == 1, "expert parallelism is outside M0")
    require(rt["pipeline_parallel_size"] == 1, "pipeline parallelism is outside M0")
    require(rt["max_model_length"] == 32768, "wrong maximum model length")
    require(rt["max_batched_tokens"] == 32768, "wrong maximum batched tokens")
    require(rt["max_sequences"] == 1, "M0 must use one active sequence")
    require(rt["cpu_offload_gb"] == 0, "runtime CPU offload is forbidden")
    require(rt["swap_space_gb"] == 0, "runtime swap is forbidden")
    require(rt["execution_mode"] == "EAGER", "M0 must use frozen eager mode")
    require(rt["kv_cache_dtype"] == "BF16", "runtime KV cache identity must be BF16")
    require(
        set(runtime["runtime_adapter_contract"]) == {"path", "file_sha256"},
        "runtime-adapter binding closure changed",
    )
    require(
        set(runtime["runtime_attestation"])
        == {"build_attestation_path", "build_attestation_file_sha256"},
        "runtime-attestation binding closure changed",
    )
    backend_evidence = runtime["backend_evidence_contract"]
    require(
        backend_evidence["source"]
        == "VLLM_STARTUP_LOG_OR_FROZEN_VERSION_ADAPTER",
        "backend evidence source changed",
    )
    require(
        set(backend_evidence["required_utf8_markers"])
        == {"attention_backend", "fused_moe_backend", "kernel_backend"},
        "backend evidence marker closure changed",
    )
    for key, marker in backend_evidence["required_utf8_markers"].items():
        require(isinstance(marker, str), f"backend evidence marker is not text: {key}")
        if not marker.startswith(BLOCKING_PREFIX) and not rt[key].startswith(
            BLOCKING_PREFIX
        ):
            require(rt[key] in marker, f"backend evidence marker does not bind {key}")
    generation = runtime["generation"]
    require(generation["input_tokens"] == 28672, "wrong input-token envelope")
    require(generation["max_new_tokens"] == 4096, "wrong output-token envelope")
    require(generation["ignore_eos"] is True, "capacity probe must force output length")
    require(generation["required_stop"] == "FORCED_LENGTH_4096", "wrong stop contract")
    require(plan["stage"] == "M0", "plan must remain M0")
    require(plan["fresh_session_required"] is True, "fresh session must be required")
    require(plan["resume_allowed"] is False, "resume must be forbidden")
    require(plan["retry_failed_allowed"] is False, "retry-failed must be forbidden")
    require(plan["global_outer_timeout_seconds"] == 14400, "wrong outer timeout")
    require(plan["allocation_window_seconds"] == 21600, "wrong allocation window")
    require(plan["release_reserve_seconds"] == 900, "wrong release reserve")
    require(
        plan.get("prerequisites")
        == [
            "GATE_M_COMPLETE_M0_ELIGIBLE_LOCAL_EXPORT_REPLAYED",
            "MATERIALIZATION_COMPLETE_HARD_STOP",
            "MODEL_LEDGER_AND_CAPACITY_FIXTURE_HASH_BOUND",
            "FROZEN_RUNTIME_AND_BUILD_ATTESTATION",
            "SEPARATE_EXACT_M0_OWNER_APPROVAL",
        ],
        "M0 prerequisites changed",
    )
    commands = runtime["commands"]
    require(
        set(commands) == {"qualification_command_argv", "audit_command_argv"},
        "execution runtime must define exactly qualification and audit commands",
    )
    for name, argv in commands.items():
        require(isinstance(argv, list) and argv, f"{name} must be nonempty argv")
        require(all(isinstance(item, str) and item for item in argv), f"{name} has invalid argv")
    require(
        [cell["cell_id"] for cell in plan["cells"]]
        == [
            "M0-PREFLIGHT",
            "M0-BF16-CAPACITY-R1",
            "M0-BF16-CAPACITY-R2",
            "M0-BF16-CAPACITY-R3",
            "M0-AUDIT",
        ],
        "M0 cell order or scope changed",
    )


def validate_environment(environment: dict[str, Any]) -> None:
    require(environment["manifest_status"] == "FROZEN", "environment manifest not frozen")
    require(environment["gpu"]["exact_product_name"] == EXPECTED_GPU, "environment GPU mismatch")
    require(int(environment["gpu"]["total_memory_bytes"]) >= 96_000_000_000, "insufficient GPU memory")
    require(environment["gpu"]["exclusive_allocation_confirmed"] is True, "exclusive GPU not confirmed")
    require(environment["host"]["working_directory"].startswith("/vault/flow/"), "working directory must be persistent")
    require(environment["host"]["persistent_mount"] == "/vault", "persistent mount changed")
    require(environment["host"]["ephemeral_workspace"] == "/workspace", "ephemeral workspace changed")
    require(environment["host"]["vault_mount_confirmed"] is True, "Vault mount not confirmed")
    require(
        isinstance(environment["host"]["vault_mount_identity_sha256"], str)
        and SHA256_RE.fullmatch(environment["host"]["vault_mount_identity_sha256"]) is not None,
        "Vault mount identity is invalid",
    )
    require(
        isinstance(environment["host"]["d0_result_file_sha256"], str)
        and SHA256_RE.fullmatch(environment["host"]["d0_result_file_sha256"]) is not None,
        "D0 result binding is invalid",
    )
    require(
        int(environment["host"]["vault_free_bytes_at_capture"])
        >= environment["host"]["minimum_free_bytes_before_materialization"],
        "Vault capacity is below materialization floor",
    )
    require(
        environment["network"]["other_workload_network_access"] == "FORBIDDEN",
        "workload network policy changed",
    )


def validate_cost_and_host(
    app: dict[str, Any],
    environment: dict[str, Any],
    approval: dict[str, Any],
) -> None:
    app_currency = app["estimated_resources"]["maximum_additional_spend_currency"]
    env_currency = environment["provider"]["maximum_additional_spend_currency"]
    cap_currency = approval["approved_maximum_spend_currency"]
    require(app_currency == env_currency == cap_currency, "price/cap currencies differ")
    app_spend = decimal_value(app["estimated_resources"]["maximum_additional_spend_amount"], "application additional spend")
    env_spend = decimal_value(environment["provider"]["maximum_additional_spend_amount"], "environment additional spend")
    cap = decimal_value(approval["approved_maximum_spend_amount"], "approved maximum spend")
    require(app_spend == env_spend == cap == Decimal(0), "additional spend must remain zero")
    require(environment["provider"]["billing_mode"] == "PREPAID_FIXED_WINDOW", "billing mode changed")
    require(environment["provider"]["allocation_seconds"] == 21600, "allocation is not six hours")
    require(environment["provider"]["extension_allowed"] is False, "allocation extension is forbidden")
    require(environment["provider"]["additional_cost_allowed"] is False, "additional cost is forbidden")
    require(
        approval["approved_ssh_host_key_sha256"] == environment["ssh"]["host_key_sha256"],
        "approved SSH host-key hash mismatch",
    )


def validate_materialization_ready(
    directory: Path, documents: dict[str, dict[str, Any]]
) -> None:
    blockers = unresolved(
        documents,
        ("application", "environment", "materialization", "materialization_approval"),
    )
    require(not blockers, "unresolved materialization fields: " + ", ".join(blockers))
    app = documents["application"]
    environment = documents["environment"]
    materialization = documents["materialization"]
    approval = documents["materialization_approval"]
    require(app["status"] == "FROZEN_MATERIALIZATION_APPROVED", "application not materialization-ready")
    require(app["required_approvals"]["application"] == "APPROVE", "application not approved")
    require(app["required_approvals"]["materialization"] == "APPROVE", "materialization not approved")
    require(app["required_approvals"]["maximum_spend"] == "APPROVE", "spend not approved")
    require(app["required_approvals"]["exact_command"] == "PENDING", "GPU command must remain pending")
    require(app["authorized"] == [
        "approved SSH connection",
        "approved environment and GPU identity preflight",
        "one pinned model materialization",
    ], "materialization authority set mismatch")
    require("M0 execution" in app["not_authorized"], "M0 workload must remain unauthorized")
    validate_environment(environment)
    validate_cost_and_host(app, environment, approval)
    require(
        approval["approved_d0_result_sha256"]
        == environment["host"]["d0_result_file_sha256"],
        "materialization approval D0 result binding mismatch",
    )
    require(
        approval["approved_vault_mount_identity_sha256"]
        == environment["host"]["vault_mount_identity_sha256"],
        "materialization approval Vault identity mismatch",
    )
    require(
        approval["approved_deployment_project_root"]
        == materialization["storage_contract"]["persistent_project_root"]
        and approval["approved_application_target"]
        == materialization["deployment"]["application_target"]
        and approval["approved_deployment_receipt_path"]
        == materialization["deployment"]["deployment_receipt"],
        "materialization approval deployment paths mismatch",
    )
    require(materialization["status"] == "FROZEN", "materialization plan is not frozen")
    require(approval["application_decision"] == "APPROVE", "application decision missing")
    require(approval["materialization_decision"] == "APPROVE", "materialization decision missing")
    require(
        approval["materialization_plan_sha256"]
        == file_sha256(directory / "materialization_plan.template.json"),
        "materialization plan hash mismatch",
    )
    require(
        approval["exact_materialization_commands_sha256"]
        == canonical_json_sha256(
            {
                "materialize": materialization["command_argv"],
                "prompt_fixture": materialization["prompt_fixture_command_argv"],
                "runtime_provenance": materialization["runtime_provenance"][
                    "command_argv"
                ],
            }
        ),
        "materialization command-set hash mismatch",
    )
    require(
        approval["application_ledger_sha256"] == application_ledger_sha256(directory),
        "recursive application ledger mismatch",
    )
    require(
        approval["owner_authority_record_sha256"]
        == file_sha256(directory / "owner_environment_decision_20260729.json"),
        "owner authority record hash mismatch",
    )
    from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (
        validate_allocation_window,
    )
    try:
        validate_allocation_window(approval["allocation_window"])
    except Exception as exc:
        raise ValidationError(str(exc)) from exc


def validate_execution_ready(
    directory: Path, documents: dict[str, dict[str, Any]]
) -> None:
    blockers = unresolved(
        documents,
        (
            "application",
            "environment",
            "materialization",
            "materialization_approval",
            "runtime",
            "approval",
            "gate_m_parent",
        ),
    )
    require(not blockers, "unresolved blocking fields: " + ", ".join(blockers))
    app = documents["application"]
    environment = documents["environment"]
    runtime = documents["runtime"]
    approval = documents["approval"]
    gate_m_parent = documents["gate_m_parent"]
    validate_environment(environment)
    require(app["status"] == "FROZEN_PENDING_EXECUTION", "application is not frozen for execution")
    for gate in ("application", "materialization", "exact_command", "maximum_spend"):
        require(app["required_approvals"][gate] == "APPROVE", f"{gate} not approved")
    require(app["authorized"] == [
        "approved SSH connection",
        "approved GPU preflight",
        "approved pinned model snapshot",
        "one fresh M0 execution",
    ], "execution authority set mismatch")
    for forbidden in ("M1", "M2", "M3", "M4"):
        require(forbidden in app["not_authorized"], f"{forbidden} must remain unauthorized")
    require(runtime["variant_status"] == "FROZEN", "runtime variant not frozen")
    require(
        runtime["runtime"]["version"] == environment["software"]["vllm_version"],
        "runtime/environment vLLM version mismatch",
    )
    require(
        runtime["runtime"]["container_image"]
        == environment["software"]["container_image"]
        and runtime["runtime"]["container_digest"]
        == environment["software"]["container_digest"],
        "runtime/environment container identity mismatch",
    )
    require(approval["application_decision"] == "APPROVE", "owner application decision missing")
    require(approval["exact_command_decision"] == "APPROVE", "owner exact-command decision missing")
    gate_m_parent_path = directory / "gate_m_parent_evidence.template.json"
    require(
        approval["gate_m_parent_evidence_file_sha256"]
        == file_sha256(gate_m_parent_path),
        "approved Gate M parent hash mismatch",
    )
    try:
        from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_parent import (
            validate_gate_m_parent,
            validate_m0_model_binding,
        )

        validate_gate_m_parent(gate_m_parent, verify_live=True)
        validate_m0_model_binding(gate_m_parent, runtime)
    except Exception as exc:
        from explorations.moe_cycle_simulator.phase7.application.executor.common import (
            M0Error,
        )

        if isinstance(exc, M0Error):
            raise ValidationError(str(exc)) from exc
        raise
    validate_cost_and_host(app, environment, approval)
    require(
        approval["approved_d0_result_sha256"]
        == environment["host"]["d0_result_file_sha256"],
        "execution approval D0 result binding mismatch",
    )
    require(
        approval["approved_vault_mount_identity_sha256"]
        == environment["host"]["vault_mount_identity_sha256"],
        "execution approval Vault identity mismatch",
    )
    require(
        approval["runtime_variant_sha256"]
        == file_sha256(directory / "runtime_variant.template.json"),
        "approved runtime hash mismatch",
    )
    require(
        approval["approved_model_ledger_sha256"]
        == runtime["model"]["model_file_ledger_sha256"],
        "approved model ledger is not bound to runtime",
    )
    canonical_path = Path(runtime["canonical_runtime_identity_path"])
    require(
        canonical_path.is_absolute()
        and canonical_path.is_file()
        and not canonical_path.is_symlink()
        and file_sha256(canonical_path)
        == runtime["canonical_runtime_identity_sha256"],
        "canonical runtime identity file/hash mismatch",
    )
    canonical_runtime = load_json(canonical_path)
    require(
        canonical_runtime.get("schema_version") == "runtime-variant-v1"
        and canonical_runtime.get("variant_id") == runtime["variant_id"],
        "canonical/application runtime identity mismatch",
    )
    model_ledger_path = Path(runtime["model"]["model_file_ledger_path"])
    prompt_path = Path(runtime["model"]["capacity_prompt_fixture_path"])
    for label, path, expected_hash in (
        (
            "model ledger",
            model_ledger_path,
            runtime["model"]["model_file_ledger_file_sha256"],
        ),
        (
            "capacity prompt",
            prompt_path,
            runtime["model"]["capacity_prompt_fixture_sha256"],
        ),
    ):
        require(
            path.is_absolute()
            and path.is_file()
            and not path.is_symlink()
            and file_sha256(path) == expected_hash,
            f"{label} file/hash mismatch",
        )
    model_ledger = load_json(model_ledger_path)
    require(
        model_ledger.get("ledger_sha256")
        == runtime["model"]["model_file_ledger_sha256"],
        "model ledger semantic identity mismatch",
    )
    require(
        approval["exact_command_sha256"]
        == canonical_json_sha256(runtime["commands"]),
        "approved execution command hash mismatch",
    )
    require(
        approval["application_ledger_sha256"] == application_ledger_sha256(directory),
        "recursive application ledger mismatch",
    )
    require(
        approval["owner_authority_record_sha256"]
        == file_sha256(directory / "owner_environment_decision_20260729.json"),
        "owner authority record hash mismatch",
    )
    from explorations.moe_cycle_simulator.phase7.application.executor.allocation import (
        validate_allocation_window,
    )
    try:
        validate_allocation_window(approval["allocation_window"])
    except Exception as exc:
        raise ValidationError(str(exc)) from exc
    try:
        from explorations.moe_cycle_simulator.phase7.application.executor.runtime_attestation import (
            validate_runtime_attestation,
        )
        from explorations.moe_cycle_simulator.phase7.application.executor.vllm_runtime_adapter import (
            load_adapter_contract,
        )

        load_adapter_contract(runtime)
        validate_runtime_attestation(runtime)
    except Exception as exc:
        from explorations.moe_cycle_simulator.phase7.application.executor.common import (
            M0Error,
        )

        if isinstance(exc, M0Error):
            raise ValidationError(str(exc)) from exc
        raise
    for field in (
        "approval_token_sha256",
        "approved_model_ledger_sha256",
        "gate_m_parent_evidence_file_sha256",
    ):
        require(
            isinstance(approval[field], str) and SHA256_RE.fullmatch(approval[field]) is not None,
            f"invalid {field}",
        )


def validate_disclosure_ready(
    directory: Path, documents: dict[str, dict[str, Any]]
) -> None:
    blockers = unresolved(
        documents,
        ("disclosure", "disclosure_approval"),
    )
    require(not blockers, "unresolved disclosure fields: " + ", ".join(blockers))
    try:
        from explorations.moe_cycle_simulator.phase7.application.executor.disclosure import (
            validate_disclosure_approval,
            validate_disclosure_plan,
            validate_known_hosts,
        )
        from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (
            build,
        )

        plan = documents["disclosure"]
        approval = documents["disclosure_approval"]
        require(plan["status"] == "FROZEN", "D0 plan is not frozen")
        plan_path = directory / "environment_disclosure_plan.template.json"
        owner_path = directory / "owner_environment_decision_20260729.json"
        validate_disclosure_plan(plan, application_dir=directory, verify_files=True)
        validate_known_hosts(plan)
        validate_disclosure_approval(
            approval,
            plan=plan,
            plan_path=plan_path,
            application_ledger_sha256=build(directory)["ledger_sha256"],
            owner_authority_record_sha256=file_sha256(owner_path),
        )
    except Exception as exc:
        from explorations.moe_cycle_simulator.phase7.application.executor.common import M0Error

        if isinstance(exc, M0Error):
            raise ValidationError(str(exc)) from exc
        raise


def validate_gate_m_ready(
    directory: Path,
    documents: dict[str, dict[str, Any]],
    external_approval_path: Path | None,
) -> None:
    require(
        external_approval_path is not None,
        "gate-m-ready requires --external-approval",
    )
    approval_path = external_approval_path.resolve(strict=True)
    approval = load_json(approval_path)
    blockers = unresolved(
        documents,
        (
            "application",
            "environment",
            "materialization",
            "materialization_approval",
            "deployment_plan",
        ),
    )
    blockers.extend(
        f"external_approval:{location}"
        for location in walk_blockers(approval)
    )
    require(not blockers, "unresolved Gate M fields: " + ", ".join(blockers))
    validate_materialization_ready(directory, documents)
    try:
        from explorations.moe_cycle_simulator.phase7.application.executor.deployment import (
            validate_deployment_approval,
            validate_deployment_plan,
        )

        plan_path = directory / "deployment_plan.template.json"
        plan = documents["deployment_plan"]
        validate_deployment_plan(
            plan,
            application_dir=directory,
            verify_files=True,
        )
        validate_deployment_approval(
            approval,
            approval_path=approval_path,
            plan=plan,
            plan_path=plan_path,
            application_dir=directory,
            owner_authority_record_sha256=file_sha256(
                directory / "owner_environment_decision_20260729.json"
            ),
        )
    except Exception as exc:
        from explorations.moe_cycle_simulator.phase7.application.executor.common import (
            M0Error,
        )

        if isinstance(exc, M0Error):
            raise ValidationError(str(exc)) from exc
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "draft",
            "disclosure-ready",
            "materialization-ready",
            "gate-m-ready",
            "execution-ready",
        ),
        required=True,
    )
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--external-approval", type=Path)
    args = parser.parse_args()
    directory = args.application_dir.resolve()
    required = {
        "application": directory / "application_manifest.json",
        "environment": directory / "environment_manifest.template.json",
        "materialization": directory / "materialization_plan.template.json",
        "materialization_approval": directory / "materialization_approval.template.json",
        "runtime": directory / "runtime_variant.template.json",
        "approval": directory / "approval.template.json",
        "plan": directory / "m0_plan.json",
        "disclosure": directory / "environment_disclosure_plan.template.json",
        "disclosure_approval": directory / "environment_disclosure_approval.template.json",
        "owner_decision": directory / "owner_environment_decision_20260729.json",
        "deployment_plan": directory / "deployment_plan.template.json",
        "gate_m_parent": directory / "gate_m_parent_evidence.template.json",
    }
    documents = {name: load_json(path) for name, path in required.items()}
    validate_common(
        documents["application"],
        documents["runtime"],
        documents["plan"],
        documents["materialization"],
    )
    if args.mode == "draft":
        blockers = unresolved(
            documents,
            (
                "application",
                "disclosure",
                "disclosure_approval",
                "environment",
                "materialization",
                "materialization_approval",
                "runtime",
                "approval",
                "deployment_plan",
                "gate_m_parent",
            ),
        )
        require(blockers, "draft unexpectedly contains no blockers")
        require(documents["application"]["status"] == "DRAFT_NOT_AUTHORIZED", "draft status mismatch")
        require(not documents["application"]["authorized"], "draft must authorize nothing")
        print(f"PASS: draft is fail-closed with {len(blockers)} blocking fields")
    elif args.mode == "disclosure-ready":
        validate_disclosure_ready(directory, documents)
        print("PASS: package is ready for one read-only D0 disclosure; remote writes and GPU work remain forbidden")
    elif args.mode == "materialization-ready":
        validate_materialization_ready(directory, documents)
        print("PASS: package is materialization-ready; GPU workload remains unauthorized")
    elif args.mode == "gate-m-ready":
        validate_gate_m_ready(directory, documents, args.external_approval)
        print(
            "PASS: package is ready for one exact CPU/I/O Gate M deployment; "
            "GPU workload remains unauthorized"
        )
    else:
        validate_execution_ready(directory, documents)
        print("PASS: package is execution-ready for one exact M0 session")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as exc:
        raise SystemExit(f"FAIL: {exc}") from exc
