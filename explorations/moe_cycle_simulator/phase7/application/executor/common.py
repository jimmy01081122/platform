#!/usr/bin/env python3
"""Strict, dependency-free M0 execution and evidence primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
EXPECTED_SCHEMA = "moe-simulator-phase7-m0-execution-contract-v1"


class M0Error(RuntimeError):
    """A blocking M0 contract or evidence failure."""


def exact_regular_file_set(
    root: Path,
    *,
    excluded_root_files: set[str] | frozenset[str] = frozenset(),
) -> set[str]:
    """Return an exact relative regular-file set and reject unsafe members.

    Exclusions apply only to files directly below ``root``.  A nested file with
    the same basename remains evidence and cannot escape exact-set replay.
    """

    resolved = root.resolve(strict=True)
    result: set[str] = set()
    for path in resolved.rglob("*"):
        relative = path.relative_to(resolved).as_posix()
        observed = path.lstat()
        if stat.S_ISLNK(observed.st_mode):
            raise M0Error(f"evidence symlink is forbidden: {relative}")
        if stat.S_ISDIR(observed.st_mode):
            continue
        if not stat.S_ISREG(observed.st_mode):
            raise M0Error(f"evidence special file is forbidden: {relative}")
        if relative not in excluded_root_files:
            result.add(relative)
    return result


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise M0Error(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise M0Error(f"JSON floating-point values are forbidden: {value}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise M0Error(f"cannot load strict JSON {path}: {exc}") from exc
    return load_json_bytes(payload, str(path))


def load_json_bytes(payload: bytes, label: str = "<bytes>") -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M0Error(f"cannot load strict JSON {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise M0Error(f"JSON root must be an object: {label}")
    return value


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise M0Error(f"value is not canonical JSON: {exc}") from exc


def semantic_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_new_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2
    ) + "\n"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise M0Error(f"refusing to overwrite evidence: {path}") from exc


def require_unlock(contract: Mapping[str, Any]) -> None:
    expected = contract["authority"]["required_unlock"]
    if os.environ.get("MOE_PHASE7_EXECUTION_UNLOCK") != expected:
        raise M0Error("missing owner-approved exact M0 execution unlock")


def require_materialization_unlock(contract: Mapping[str, Any]) -> None:
    expected = contract["authority"]["required_materialization_unlock"]
    if os.environ.get("MOE_PHASE7_MATERIALIZATION_UNLOCK") != expected:
        raise M0Error("missing owner-approved exact materialization unlock")


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise M0Error(
            f"{label} key closure mismatch; "
            f"missing={sorted(expected-actual)}, extra={sorted(actual-expected)}"
        )


def validate_contract(value: Mapping[str, Any]) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "target",
            "model",
            "engine",
            "probe",
            "timeouts",
            "allocation",
            "authority",
        },
        "contract",
    )
    if value["schema_version"] != EXPECTED_SCHEMA:
        raise M0Error("unsupported M0 execution contract")
    if value["target"] != {
        "gpu_count": 1,
        "exact_product_name": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
        "minimum_memory_bytes": 96_000_000_000,
    }:
        raise M0Error("M0 target contract changed")
    if value["model"] != {
        "model_id": "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "repository_commit": "eba92302a2861cdc0098cc54bc9f17cb2c47eb61",
        "precision": "BF16",
        "quantization": None,
    }:
        raise M0Error("M0 model contract changed")
    engine = value["engine"]
    expected_engine = {
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "max_model_length": 32768,
        "max_batched_tokens": 32768,
        "max_sequences": 1,
        "cpu_offload_gb": 0,
        "swap_space_gb": 0,
        "kv_cache_dtype": "bfloat16",
        "enforce_eager": True,
        "generation_config": "vllm",
    }
    if engine != expected_engine:
        raise M0Error("M0 engine contract changed")
    probe = value["probe"]
    if probe != {
        "input_tokens": 28672,
        "output_tokens": 4096,
        "repetitions": 3,
        "seed": 0,
        "temperature": "0",
        "top_p": "1",
        "ignore_eos": True,
        "required_finish_reason": "length",
        "required_stop_reason": None,
        "cleanup_memory_tolerance_bytes": 268435456,
    }:
        raise M0Error("M0 probe contract changed")
    if value["timeouts"] != {
        "materialization_seconds": 4200,
        "materialization_stage_seconds": 5400,
        "materialization_work_seconds": 4800,
        "single_launch_seconds": 3600,
        "audit_seconds": 600,
        "m0_work_seconds": 13200,
        "terminal_reserve_seconds": 1200,
        "outer_seconds": 14400,
    }:
        raise M0Error("M0 timeout contract changed")
    if value["allocation"] != {
        "total_seconds": 21600,
        "start_trigger": "OWNER_RELEASES_FRESH_SSH_HANDOFF",
        "billing_mode": "PREPAID_FIXED_WINDOW",
        "extension_allowed": False,
        "additional_cost_allowed": False,
        "maximum_additional_spend_amount": "0",
        "maximum_additional_spend_currency": "TWD",
        "release_reserve_seconds": 900,
    }:
        raise M0Error("M0 allocation contract changed")
    if value["authority"] != {
        "required_materialization_unlock": "OWNER_APPROVED_EXACT_MATERIALIZATION_COMMAND",
        "required_unlock": "OWNER_APPROVED_EXACT_M0_COMMAND",
        "fresh_session": True,
        "resume": False,
        "retry_failed": False,
        "m1_through_m4": False,
    }:
        raise M0Error("M0 authority contract changed")


def validate_runtime(runtime: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    validate_contract(contract)
    if runtime.get("schema_version") != "moe-simulator-phase7-m0-runtime-variant-v1":
        raise M0Error("runtime variant schema mismatch")
    canonical_path_value = runtime.get("canonical_runtime_identity_path")
    canonical_hash = runtime.get("canonical_runtime_identity_sha256")
    if (
        not isinstance(canonical_path_value, str)
        or not canonical_path_value
        or not isinstance(canonical_hash, str)
        or not SHA256_RE.fullmatch(canonical_hash)
    ):
        raise M0Error("canonical runtime identity binding is invalid")
    canonical_path = Path(canonical_path_value)
    if (
        not canonical_path.is_absolute()
        or not canonical_path.is_file()
        or canonical_path.is_symlink()
        or file_sha256(canonical_path) != canonical_hash
    ):
        raise M0Error("canonical runtime identity file/hash mismatch")
    canonical = load_json(canonical_path)
    if (
        canonical.get("schema_version") != "runtime-variant-v1"
        or canonical.get("variant_id") != runtime.get("variant_id")
    ):
        raise M0Error("canonical/application runtime variant IDs differ")
    model = runtime.get("model", {})
    if (
        model.get("model_id") != contract["model"]["model_id"]
        or model.get("repository_commit") != contract["model"]["repository_commit"]
        or model.get("precision") != "BF16"
        or model.get("quantization") is not None
        or not isinstance(model.get("model_file_ledger_sha256"), str)
        or not SHA256_RE.fullmatch(model["model_file_ledger_sha256"])
    ):
        raise M0Error("runtime model identity or ledger binding mismatch")
    ledger_path_value = model.get("model_file_ledger_path")
    ledger_file_hash = model.get("model_file_ledger_file_sha256")
    if (
        not isinstance(ledger_path_value, str)
        or not Path(ledger_path_value).is_absolute()
        or not isinstance(ledger_file_hash, str)
        or not SHA256_RE.fullmatch(ledger_file_hash)
    ):
        raise M0Error("runtime model-ledger file binding is invalid")
    ledger_path = Path(ledger_path_value)
    if (
        not ledger_path.is_file()
        or ledger_path.is_symlink()
        or file_sha256(ledger_path) != ledger_file_hash
        or load_json(ledger_path).get("ledger_sha256")
        != model["model_file_ledger_sha256"]
    ):
        raise M0Error("runtime model-ledger file/hash mismatch")
    fixture_path_value = model.get("capacity_prompt_fixture_path")
    fixture_hash = model.get("capacity_prompt_fixture_sha256")
    if (
        not isinstance(fixture_path_value, str)
        or not Path(fixture_path_value).is_absolute()
        or not isinstance(fixture_hash, str)
        or not SHA256_RE.fullmatch(fixture_hash)
    ):
        raise M0Error("runtime capacity-prompt fixture binding is invalid")
    fixture_path = Path(fixture_path_value)
    if (
        not fixture_path.is_file()
        or fixture_path.is_symlink()
        or file_sha256(fixture_path) != fixture_hash
    ):
        raise M0Error("capacity-prompt fixture file/hash mismatch")
    engine = runtime.get("runtime", {})
    checks = {
        "name": "vLLM",
        "tensor_parallel_size": 1,
        "expert_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "max_model_length": 32768,
        "max_batched_tokens": 32768,
        "max_sequences": 1,
        "cpu_offload_gb": 0,
        "swap_space_gb": 0,
        "execution_mode": "EAGER",
        "kv_cache_dtype": "BF16",
    }
    for key, expected in checks.items():
        if engine.get(key) != expected:
            raise M0Error(f"runtime engine field mismatch: {key}")
    if not isinstance(engine.get("gpu_memory_utilization"), str):
        raise M0Error("gpu_memory_utilization must be a frozen decimal string")
    generation = runtime.get("generation", {})
    expected_generation = {
        "seed": 0,
        "do_sample": False,
        "temperature": 0,
        "top_p": 1,
        "input_tokens": 28672,
        "max_new_tokens": 4096,
        "ignore_eos": True,
        "required_stop": "FORCED_LENGTH_4096",
    }
    if generation != expected_generation:
        raise M0Error("runtime generation identity mismatch")
    if runtime.get("command_environment") != {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "CUDA_VISIBLE_DEVICES": "0",
        "PYTHONNOUSERSITE": "1",
    }:
        raise M0Error("runtime isolated command environment changed")
    backend_contract = runtime.get("backend_evidence_contract")
    if (
        not isinstance(backend_contract, dict)
        or backend_contract.get("source")
        != "VLLM_STARTUP_LOG_OR_FROZEN_VERSION_ADAPTER"
        or set(backend_contract.get("required_utf8_markers", {}))
        != {"attention_backend", "fused_moe_backend", "kernel_backend"}
        or any(
            not isinstance(value, str)
            or not value
            or value.startswith("[BLOCKING:")
            for value in backend_contract["required_utf8_markers"].values()
        )
    ):
        raise M0Error("runtime backend evidence contract is unresolved or invalid")
    for key, marker in backend_contract["required_utf8_markers"].items():
        if str(engine[key]) not in marker:
            raise M0Error(f"backend marker does not name frozen backend: {key}")
    from explorations.moe_cycle_simulator.phase7.application.executor.runtime_attestation import (
        validate_runtime_attestation,
    )
    from explorations.moe_cycle_simulator.phase7.application.executor.vllm_runtime_adapter import (
        load_adapter_contract,
    )

    load_adapter_contract(runtime)
    validate_runtime_attestation(runtime)


def validate_materialization_plan(
    plan: Mapping[str, Any], contract: Mapping[str, Any]
) -> None:
    validate_contract(contract)
    _exact_keys(
        plan,
        {
            "schema_version",
            "status",
            "model",
            "materializer",
            "tokenizer_builder",
            "paths",
            "storage_contract",
            "deployment",
            "runtime_provenance",
            "command_argv",
            "prompt_fixture_command_argv",
        },
        "materialization plan",
    )
    if plan["schema_version"] != "moe-simulator-phase7-m0-materialization-plan-v1":
        raise M0Error("materialization plan schema mismatch")
    model = plan["model"]
    if model != {
        "model_id": contract["model"]["model_id"],
        "repository_commit": contract["model"]["repository_commit"],
    }:
        raise M0Error("materialization model identity mismatch")
    materializer = plan["materializer"]
    expected_allow_patterns = [
        "config.json",
        "generation_config.json",
        "model-*.safetensors",
        "model.safetensors.index.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer.model",
        "tokenizer_config.json",
    ]
    expected_ignore_patterns = ["*.bin", "*.gguf", "*.pt", "consolidated.*"]
    if (
        materializer.get("library") != "huggingface_hub"
        or materializer.get("method") != "snapshot_download_local_dir"
        or materializer.get("max_workers") != 4
        or materializer.get("allow_patterns") != expected_allow_patterns
        or materializer.get("ignore_patterns") != expected_ignore_patterns
        or not isinstance(materializer.get("version"), str)
        or not materializer["version"]
    ):
        raise M0Error("materializer identity mismatch")
    builder = plan["tokenizer_builder"]
    if (
        builder.get("library") != "transformers"
        or builder.get("method") != "AutoTokenizer.from_pretrained_local_only"
        or not isinstance(builder.get("version"), str)
        or not builder["version"]
    ):
        raise M0Error("tokenizer builder identity mismatch")
    paths = plan["paths"]
    if (
        set(paths)
        != {
            "snapshot",
            "model_ledger",
            "materialization_result",
            "capacity_prompt_fixture",
        }
        or any(not isinstance(value, str) or not value for value in paths.values())
    ):
        raise M0Error("materialization paths are invalid")
    resolved_paths: list[Path] = []
    for label, value in paths.items():
        candidate = Path(value)
        if not candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise M0Error(f"materialization path must be absolute: {label}")
        resolved_paths.append(candidate)
    if len({str(path) for path in resolved_paths}) != len(resolved_paths):
        raise M0Error("materialization paths must be distinct")
    storage = plan["storage_contract"]
    if (
        set(storage)
        != {
            "persistent_mount",
            "persistent_project_root",
            "ephemeral_workspace",
            "model_snapshot_location",
            "ledger_fixture_and_evidence_location",
            "credentials_in_persistent_or_workspace_storage",
        }
        or storage["persistent_mount"] != "/vault"
        or storage["ephemeral_workspace"] != "/workspace"
        or storage["model_snapshot_location"] != "PERSISTENT"
        or storage["ledger_fixture_and_evidence_location"] != "PERSISTENT"
        or storage["credentials_in_persistent_or_workspace_storage"] != "FORBIDDEN"
    ):
        raise M0Error("materialization storage contract changed")
    persistent_root = Path(storage["persistent_project_root"])
    if (
        not persistent_root.is_absolute()
        or persistent_root.parent != Path("/vault")
        or not persistent_root.name.startswith("flow-mixtral-rtxpro6000-r12-")
    ):
        raise M0Error("materialization project root is not a fresh /vault child")
    for label, path in zip(paths, resolved_paths):
        try:
            path.relative_to(persistent_root)
        except ValueError as exc:
            raise M0Error(
                f"materialization path must remain below the persistent project root: {label}"
            ) from exc
    for field in ("command_argv", "prompt_fixture_command_argv"):
        if (
            not isinstance(plan[field], list)
            or not plan[field]
            or any(not isinstance(item, str) or not item for item in plan[field])
        ):
            raise M0Error(f"{field} is invalid")
    deployment = plan["deployment"]
    if set(deployment) != {"application_target", "deployment_receipt"}:
        raise M0Error("materialization deployment binding closure changed")
    application_target = Path(deployment["application_target"])
    receipt = Path(deployment["deployment_receipt"])
    if (
        application_target
        != persistent_root
        / "packages/materialization/repo/explorations/moe_cycle_simulator/phase7/application"
        or receipt
        != persistent_root / "packages/materialization/deployment_receipt.json"
    ):
        raise M0Error("materialization deployment paths differ from fixed layout")
    provenance = plan["runtime_provenance"]
    if (
        set(provenance)
        != {
            "output_root",
            "command_argv",
            "inspection_mode",
            "selected_backend_observation",
            "missing_evidence_disposition",
            "gpu_compute",
            "vllm_model_load",
        }
        or Path(provenance["output_root"])
        != persistent_root / "evidence/runtime-provenance"
        or not isinstance(provenance["command_argv"], list)
        or not provenance["command_argv"]
        or any(
            not isinstance(item, str) or not item
            for item in provenance["command_argv"]
        )
        or provenance["inspection_mode"]
        != "STATIC_FILES_AND_DISTRIBUTION_METADATA_ONLY"
        or provenance["selected_backend_observation"]
        != "PENDING_M0_R1_STARTUP"
        or provenance["missing_evidence_disposition"]
        != "COMPLETE_M0_BLOCKED_PROVENANCE"
        or provenance["gpu_compute"] is not False
        or provenance["vllm_model_load"] is not False
    ):
        raise M0Error("materialization runtime-provenance contract changed")


def validate_session_id(value: str) -> None:
    if not SESSION_RE.fullmatch(value):
        raise M0Error("invalid fresh session ID")


def validate_fresh_target(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise M0Error(f"{label} must be an absolute fresh path")
    if path.exists() or path.is_symlink():
        raise M0Error(f"{label} already exists or is a symlink")
    parent = path.parent
    resolved_parent = parent.resolve(strict=True)
    if (
        resolved_parent != parent
        or not resolved_parent.is_dir()
        or resolved_parent.is_symlink()
    ):
        raise M0Error(f"{label} parent must be an existing real directory")
    return resolved_parent / path.name


def load_capacity_fixture(
    runtime: Mapping[str, Any],
    contract: Mapping[str, Any],
    model_ledger: Mapping[str, Any],
) -> tuple[dict[str, Any], list[int]]:
    path = Path(runtime["model"]["capacity_prompt_fixture_path"])
    fixture = load_json(path)
    _exact_keys(
        fixture,
        {
            "schema_version",
            "model_id",
            "repository_commit",
            "model_ledger_sha256",
            "tokenizer_builder",
            "seed_text",
            "add_special_tokens",
            "token_count",
            "token_ids",
            "token_ids_sha256",
            "tokenizer_config_sha256",
            "tokenizer_sha256",
        },
        "capacity prompt fixture",
    )
    token_ids = fixture["token_ids"]
    if (
        fixture["schema_version"] != "moe-simulator-phase7-capacity-prompt-v1"
        or fixture["model_id"] != contract["model"]["model_id"]
        or fixture["repository_commit"] != contract["model"]["repository_commit"]
        or fixture["model_ledger_sha256"] != model_ledger["ledger_sha256"]
        or fixture["add_special_tokens"] is not False
        or fixture["token_count"] != contract["probe"]["input_tokens"]
        or not isinstance(token_ids, list)
        or len(token_ids) != contract["probe"]["input_tokens"]
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in token_ids
        )
        or fixture["token_ids_sha256"] != semantic_sha256(token_ids)
        or fixture["tokenizer_config_sha256"]
        != model_ledger["snapshot_structure"]["tokenizer_config_sha256"]
        or fixture["tokenizer_sha256"]
        != model_ledger["snapshot_structure"]["tokenizer_sha256"]
    ):
        raise M0Error("capacity prompt fixture binding or token contract mismatch")
    return fixture, token_ids


def build_model_ledger(
    root: Path, *, model_id: str, repository_commit: str
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise M0Error("model snapshot root is not a directory")
    members: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise M0Error(f"model snapshot symlink is forbidden: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise M0Error(f"non-regular model artifact is forbidden: {path}")
        relative = path.relative_to(root).as_posix()
        members.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    if not members:
        raise M0Error("model snapshot contains no files")
    ledger: dict[str, Any] = {
        "schema_version": "moe-simulator-phase7-model-ledger-v1",
        "model_id": model_id,
        "repository_commit": repository_commit,
        "member_count": len(members),
        "total_size_bytes": sum(item["size_bytes"] for item in members),
        "snapshot_structure": validate_snapshot_structure(root),
        "members": members,
    }
    ledger["ledger_sha256"] = semantic_sha256(ledger)
    return ledger


def validate_snapshot_structure(root: Path) -> dict[str, Any]:
    """Validate the pinned Mixtral config and exact sharded-weight closure."""
    root = root.resolve(strict=True)
    required = {
        "config.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "model.safetensors.index.json",
    }
    present = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    missing = required - present
    if missing:
        raise M0Error(f"required model files are missing: {sorted(missing)}")
    config = load_json(root / "config.json")
    if (
        config.get("model_type") != "mixtral"
        or config.get("num_local_experts") != 8
        or config.get("num_experts_per_tok") != 2
        or config.get("max_position_embeddings") != 32768
        or config.get("torch_dtype") not in {"bfloat16", "bf16"}
        or config.get("quantization_config") is not None
    ):
        raise M0Error("pinned Mixtral architecture/dtype invariants do not hold")
    index = load_json(root / "model.safetensors.index.json")
    if set(index) != {"metadata", "weight_map"} or not isinstance(
        index["weight_map"], dict
    ):
        raise M0Error("invalid safetensors index closure")
    shards = sorted(set(index["weight_map"].values()))
    if (
        not shards
        or any(
            not isinstance(item, str)
            or Path(item).is_absolute()
            or ".." in Path(item).parts
            or not item.endswith(".safetensors")
            for item in shards
        )
    ):
        raise M0Error("invalid safetensors shard path")
    missing_shards = [item for item in shards if item not in present]
    actual_shards = sorted(
        item for item in present if item.endswith(".safetensors")
    )
    if missing_shards or actual_shards != shards:
        raise M0Error(
            "safetensors shard set mismatch; "
            f"missing={missing_shards}, extra={sorted(set(actual_shards)-set(shards))}"
        )
    if any(
        item.endswith((".bin", ".gguf", ".pt"))
        or "awq" in item.lower()
        or "gptq" in item.lower()
        for item in present
    ):
        raise M0Error("alternate or quantized weight artifacts are forbidden")
    return {
        "model_type": config["model_type"],
        "num_local_experts": config["num_local_experts"],
        "num_experts_per_tok": config["num_experts_per_tok"],
        "max_position_embeddings": config["max_position_embeddings"],
        "torch_dtype": config["torch_dtype"],
        "weight_shard_count": len(shards),
        "weight_shards": shards,
        "config_sha256": file_sha256(root / "config.json"),
        "tokenizer_config_sha256": file_sha256(root / "tokenizer_config.json"),
        "tokenizer_sha256": file_sha256(root / "tokenizer.json"),
        "weight_index_sha256": file_sha256(
            root / "model.safetensors.index.json"
        ),
    }


def verify_model_ledger(
    root: Path,
    ledger: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
) -> None:
    validate_contract(contract)
    _exact_keys(
        ledger,
        {
            "schema_version",
            "model_id",
            "repository_commit",
            "member_count",
            "total_size_bytes",
            "snapshot_structure",
            "members",
            "ledger_sha256",
        },
        "model ledger",
    )
    if (
        ledger["model_id"] != contract["model"]["model_id"]
        or ledger["repository_commit"]
        != contract["model"]["repository_commit"]
    ):
        raise M0Error("model ledger does not identify the exact M0 model/revision")
    base = dict(ledger)
    claimed = base.pop("ledger_sha256")
    if not isinstance(claimed, str) or not SHA256_RE.fullmatch(claimed):
        raise M0Error("invalid model ledger SHA-256")
    if semantic_sha256(base) != claimed:
        raise M0Error("model ledger semantic hash mismatch")
    rebuilt = build_model_ledger(
        root,
        model_id=ledger["model_id"],
        repository_commit=ledger["repository_commit"],
    )
    if rebuilt != ledger:
        raise M0Error("model snapshot differs from its complete ledger")


def validate_probe_record(
    record: Mapping[str, Any],
    *,
    contract_sha256: str,
    runtime_sha256: str,
    model_ledger_sha256: str,
    launch_index: int,
    session_id: str,
    contract: Mapping[str, Any],
) -> None:
    if record.get("schema_version") != "moe-simulator-phase7-m0-probe-record-v1":
        raise M0Error("probe record schema mismatch")
    expected = {
        "contract_sha256": contract_sha256,
        "runtime_variant_sha256": runtime_sha256,
        "model_ledger_sha256": model_ledger_sha256,
        "launch_index": launch_index,
        "session_id": session_id,
        "status": "COMPLETE",
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise M0Error(f"probe record binding mismatch: {key}")
    process = record.get("process_identity")
    if (
        not isinstance(process, dict)
        or not isinstance(process.get("pid"), int)
        or process["pid"] <= 0
        or not isinstance(process.get("boot_id"), str)
        or not process["boot_id"]
        or not isinstance(process.get("start_ticks"), int)
        or process["start_ticks"] <= 0
        or not isinstance(process.get("nonce"), str)
        or not process["nonce"]
    ):
        raise M0Error("probe process identity is incomplete")
    gpu = record.get("gpu")
    if (
        not isinstance(gpu, dict)
        or gpu.get("name") != contract["target"]["exact_product_name"]
        or gpu.get("count") != 1
        or gpu.get("total_memory_bytes", 0)
        < contract["target"]["minimum_memory_bytes"]
    ):
        raise M0Error("probe GPU evidence does not match target")
    probe = record.get("probe")
    if (
        not isinstance(probe, dict)
        or probe.get("input_token_count") != contract["probe"]["input_tokens"]
        or probe.get("output_token_count") != contract["probe"]["output_tokens"]
        or probe.get("finish_reason")
        != contract["probe"]["required_finish_reason"]
        or probe.get("stop_reason") is not contract["probe"]["required_stop_reason"]
        or not isinstance(probe.get("prompt_token_ids_sha256"), str)
        or not SHA256_RE.fullmatch(probe["prompt_token_ids_sha256"])
        or not isinstance(probe.get("capacity_prompt_fixture_sha256"), str)
        or not SHA256_RE.fullmatch(probe["capacity_prompt_fixture_sha256"])
        or not isinstance(probe.get("output_token_ids"), list)
        or len(probe["output_token_ids"]) != contract["probe"]["output_tokens"]
        or semantic_sha256(probe["output_token_ids"])
        != probe.get("output_token_ids_sha256")
    ):
        raise M0Error("probe token or termination evidence is invalid")
    memory = record.get("memory")
    if (
        not isinstance(memory, dict)
        or set(memory) != {"before_load", "after_load", "after_generation"}
        or any(
            not isinstance(item, dict)
            or item.get("used_memory_bytes", -1) < 0
            or item.get("free_memory_bytes", -1) < 0
            for item in memory.values()
        )
    ):
        raise M0Error("probe device-memory evidence is invalid")
    engine = record.get("engine")
    if not isinstance(engine, dict):
        raise M0Error("probe engine evidence is missing")
    arguments = engine.get("constructor_arguments")
    if not isinstance(arguments, dict):
        raise M0Error("probe engine constructor evidence is invalid")
    expected_arguments = {
        "dtype": "bfloat16",
        "quantization": None,
        "tensor_parallel_size": contract["engine"]["tensor_parallel_size"],
        "pipeline_parallel_size": contract["engine"]["pipeline_parallel_size"],
        "max_model_len": contract["engine"]["max_model_length"],
        "max_num_batched_tokens": contract["engine"]["max_batched_tokens"],
        "max_num_seqs": contract["engine"]["max_sequences"],
        "swap_space": contract["engine"]["swap_space_gb"],
        "cpu_offload_gb": contract["engine"]["cpu_offload_gb"],
        "enforce_eager": contract["engine"]["enforce_eager"],
        "trust_remote_code": False,
        "skip_tokenizer_init": True,
        "generation_config": contract["engine"]["generation_config"],
        "seed": contract["probe"]["seed"],
        "kv_cache_dtype": contract["engine"]["kv_cache_dtype"],
    }
    for key, expected_value in expected_arguments.items():
        if arguments.get(key) != expected_value:
            raise M0Error(f"probe engine constructor differs from contract: {key}")
    for key in ("model", "tokenizer"):
        value = arguments.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise M0Error(f"probe engine {key} path must be absolute")
    if arguments["model"] != arguments["tokenizer"]:
        raise M0Error("probe model/tokenizer snapshot paths differ")
    utilization = arguments.get("gpu_memory_utilization")
    try:
        utilization_decimal = Decimal(utilization) if isinstance(utilization, str) else Decimal(-1)
    except InvalidOperation:
        utilization_decimal = Decimal(-1)
    if (
        not isinstance(utilization, str)
        or not re.fullmatch(r"0\.[0-9]+|1(?:\.0+)?", utilization)
        or not Decimal(0) < utilization_decimal <= Decimal(1)
    ):
        raise M0Error("probe engine GPU-memory utilization is invalid")
    if engine.get("constructor_arguments_sha256") != semantic_sha256(arguments):
        raise M0Error("probe engine constructor evidence hash mismatch")
    resolved_kv = engine.get("resolved_kv_cache_dtype")
    if (
        not isinstance(resolved_kv, dict)
        or set(resolved_kv)
        != {"method", "attribute_path", "raw_value", "normalized_value"}
        or resolved_kv["method"] != "FROZEN_VERSION_ATTRIBUTE_PATH"
        or not isinstance(resolved_kv["attribute_path"], list)
        or not resolved_kv["attribute_path"]
        or any(
            not isinstance(item, str) or not item
            for item in resolved_kv["attribute_path"]
        )
        or not isinstance(resolved_kv["raw_value"], str)
        or resolved_kv["normalized_value"] != "bfloat16"
    ):
        raise M0Error("probe resolved KV-cache dtype evidence is not exact BF16")
    software = record.get("software")
    if (
        not isinstance(software, dict)
        or software.get("vllm_version") != record.get("runtime_qualified_version")
        or software.get("vllm_source_git_commit")
        != record.get("runtime_qualified_git_commit")
        or not isinstance(software.get("installed_distribution_ledger_sha256"), str)
        or not SHA256_RE.fullmatch(
            software["installed_distribution_ledger_sha256"]
        )
        or not isinstance(software.get("build_attestation_file_sha256"), str)
        or not SHA256_RE.fullmatch(software["build_attestation_file_sha256"])
        or not isinstance(software.get("runtime_adapter_contract_sha256"), str)
        or not SHA256_RE.fullmatch(software["runtime_adapter_contract_sha256"])
    ):
        raise M0Error("probe runtime attestation evidence is incomplete")
    loaded_evidence = software.get("vllm_import_evidence")
    if not isinstance(loaded_evidence, dict):
        raise M0Error("loaded vLLM import evidence is missing")
    loaded_base = dict(loaded_evidence)
    loaded_claim = loaded_base.pop("evidence_sha256", None)
    loaded_modules = loaded_evidence.get("loaded_modules")
    binary_modules = loaded_evidence.get("binary_modules")
    if (
        set(loaded_evidence)
        != {
            "schema_version",
            "distribution_name",
            "distribution_version",
            "distribution_ledger_sha256",
            "module_prefix",
            "loaded_module_count",
            "loaded_modules",
            "binary_module_count",
            "binary_modules",
            "evidence_sha256",
        }
        or loaded_evidence["schema_version"]
        != "moe-simulator-phase7-loaded-vllm-modules-v1"
        or loaded_evidence["distribution_name"] != "vllm"
        or loaded_evidence["module_prefix"] != "vllm"
        or loaded_evidence["distribution_version"] != software["vllm_version"]
        or loaded_evidence["distribution_ledger_sha256"]
        != software["installed_distribution_ledger_sha256"]
        or not isinstance(loaded_claim, str)
        or not SHA256_RE.fullmatch(loaded_claim)
        or semantic_sha256(loaded_base) != loaded_claim
        or not isinstance(loaded_modules, list)
        or not loaded_modules
        or loaded_evidence["loaded_module_count"] != len(loaded_modules)
        or not isinstance(binary_modules, list)
        or loaded_evidence["binary_module_count"] != len(binary_modules)
    ):
        raise M0Error("loaded vLLM import evidence binding is invalid")
    module_names: list[str] = []
    observed_binary: list[str] = []
    root_hash: str | None = None
    for item in loaded_modules:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "module_name",
                "declared_path",
                "resolved_path",
                "size_bytes",
                "sha256",
                "binary",
            }
            or not isinstance(item["module_name"], str)
            or (
                item["module_name"] != "vllm"
                and not item["module_name"].startswith("vllm.")
            )
            or not isinstance(item["declared_path"], str)
            or not item["declared_path"]
            or not isinstance(item["resolved_path"], str)
            or not Path(item["resolved_path"]).is_absolute()
            or not isinstance(item["size_bytes"], int)
            or item["size_bytes"] < 0
            or not isinstance(item["sha256"], str)
            or not SHA256_RE.fullmatch(item["sha256"])
            or not isinstance(item["binary"], bool)
        ):
            raise M0Error("loaded vLLM module evidence member is invalid")
        module_names.append(item["module_name"])
        if item["binary"]:
            observed_binary.append(item["module_name"])
        if item["module_name"] == "vllm":
            root_hash = item["sha256"]
    if (
        module_names != sorted(module_names)
        or len(module_names) != len(set(module_names))
        or root_hash is None
        or root_hash != software.get("vllm_init_sha256")
        or binary_modules != observed_binary
    ):
        raise M0Error("loaded vLLM module inventory is inconsistent")


def process_identity(nonce: str) -> dict[str, Any]:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
        fields = Path("/proc/self/stat").read_text(encoding="utf-8").split()
        start_ticks = int(fields[21])
    except (OSError, ValueError, IndexError) as exc:
        raise M0Error(f"cannot capture process identity: {exc}") from exc
    return {
        "pid": os.getpid(),
        "boot_id": boot_id,
        "start_ticks": start_ticks,
        "nonce": nonce,
    }
