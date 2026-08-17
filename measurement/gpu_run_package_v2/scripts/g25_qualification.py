#!/usr/bin/env python3
"""CPU-testable G2.5 termination qualification engine and synthetic replay CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.c1_common import as_mapping, run_generation  # noqa: E402
from collectors.c1_contract import CollectorRequest  # noqa: E402
from adapters.models.contract import GenerationResult  # noqa: E402
from adapters.models.granite_moe.snapshot import validate_exact_snapshot  # noqa: E402
from scheduler.store import atomic_json  # noqa: E402
from scheduler.execution_lock import ExecutionLease  # noqa: E402
from scheduler.g25_cgroup_v2 import (  # noqa: E402
    CellCgroup,
    CgroupDrainError,
)
from scheduler.g25_worker_lifetime import (  # noqa: E402
    ACK_BYTE,
    GO_BYTE,
    GUARD_ENV_KEYS,
    READY_BYTE,
    WorkerLifetimeError,
    assert_process_group_empty,
    kill_and_drain_process_group,
    process_group_members,
    read_process_start_ticks,
)
from scripts.c1_evaluator import evaluate_frozen_sample  # noqa: E402

CONTRACT_PATH = (
    PACKAGE_ROOT
    / "configs/test_suites/granite_c1/g25_workload_qualification_v1.json"
)
PROFILE_MAP_PATH = (
    PACKAGE_ROOT
    / "configs/test_suites/granite_c1/g25_generation_profiles_v1.yaml"
)
PILOT_SESSION_PATH = (
    PACKAGE_ROOT / "configs/test_suites/granite_c1/g25_gpu_pilot_session_v1.json"
)
PILOT_MATRIX_PATH = (
    PACKAGE_ROOT / "configs/test_suites/granite_c1/g25_gpu_pilot_matrix_v1.json"
)
PILOT_ARTIFACTS_PATH = (
    PACKAGE_ROOT / "configs/test_suites/granite_c1/g25_expected_artifacts_v1.json"
)
CELL_SCHEMA = "g25_qualification_cell.schema.json"
LEDGER_SCHEMA = "g25_qualification_ledger.schema.json"
VERDICT_SCHEMA = "g25_qualification_verdict.schema.json"
AUDIT_SCHEMA = "g25_qualification_audit.schema.json"
SESSION_SCHEMA = "g25_qualification_session.schema.json"
EVIDENCE_ROLE = "termination_qualification_non_formal"
PROFILE_NAME = "granite_c1_natural_qualification_v1"
SELECTOR_REVISION = "g25-minimal-common-ceiling-v1"
CLASSIFICATIONS = (
    "INVALID_EVIDENCE",
    "RUNTIME_FAILURE",
    "TIMEOUT",
    "TRUNCATED",
    "INVALID_OUTPUT",
    "QUALIFIED",
)
QUALIFICATION_MODEL_SNAPSHOT_ROOT = (
    PACKAGE_ROOT / "models/snapshots/granite-3.1-1b-a400m-instruct/"
    "0da7a48b0276d500ce5922fd2b33944091fc6c09"
)
REQUIRED_WORKER_FIELDS = {
    "schema_version",
    "prompt_hash",
    "input_token_ids",
    "output_token_ids",
    "input_token_count",
    "output_token_count",
    "text",
    "stop_reason",
    "output_hash",
    "return_code",
    "generation_seconds",
    "wall_time_seconds",
    "timed_out",
    "exception",
    "parser_outcome",
    "routing_capture_enabled",
    "profiler_enabled",
    "execution_identity",
    "effective_generation_config",
    "effective_generation_config_sha256",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def load_profile_map() -> dict[str, Any]:
    return yaml.safe_load(PROFILE_MAP_PATH.read_text(encoding="utf-8"))


def load_pilot_contracts() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    session = json.loads(PILOT_SESSION_PATH.read_text(encoding="utf-8"))
    matrix = json.loads(PILOT_MATRIX_PATH.read_text(encoding="utf-8"))
    artifacts = json.loads(PILOT_ARTIFACTS_PATH.read_text(encoding="utf-8"))
    validate_schema("g25_gpu_pilot_session_contract.schema.json", session)
    validate_schema("g25_gpu_pilot_matrix.schema.json", matrix)
    validate_schema("g25_expected_artifacts.schema.json", artifacts)
    return session, matrix, artifacts


def validate_schema(name: str, value: Mapping[str, Any]) -> None:
    import jsonschema
    from referencing import Registry, Resource

    schema_path = PACKAGE_ROOT / "schemas" / name
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    # Resolve every qualification schema from an explicit local registry.  The
    # schemas carry stable HTTPS identifiers for evidence portability, but
    # validation must never depend on network access.
    resources: list[tuple[str, Resource[Any]]] = []
    for candidate in (PACKAGE_ROOT / "schemas").glob("g25*.schema.json"):
        registered = json.loads(candidate.read_text(encoding="utf-8"))
        if "$id" in registered:
            resources.append((registered["$id"], Resource.from_contents(registered)))
    registry = Registry().with_resources(resources)
    jsonschema.Draft202012Validator(schema, registry=registry).validate(dict(value))


def pilot_plan() -> dict[str, Any]:
    session, matrix, artifacts = load_pilot_contracts()
    return {
        "schema_version": "g25-gpu-pilot-plan-v1",
        "status": session["status"],
        "session_id": session["session_id"],
        "matrix": {
            "instances": matrix["instances"],
            "ceilings": matrix["ceilings"],
            "expected_cells": matrix["expected_cells"],
        },
        "process_strategy": session["process_strategy"],
        "deadlines": session["deadlines"],
        "hashes": {
            "session_contract": sha256_file(PILOT_SESSION_PATH),
            "matrix": sha256_file(PILOT_MATRIX_PATH),
            "generation_profile": sha256_file(PROFILE_MAP_PATH),
            "expected_artifacts": sha256_file(PILOT_ARTIFACTS_PATH),
            "qualification_runner": sha256_file(Path(__file__)),
        },
        "expected_artifacts": artifacts,
        "gpu_used": False,
        "gpu_authorized": False,
        "formal_gate_pass": False,
    }


def _checksums_valid() -> bool:
    ledger = PACKAGE_ROOT / "checksums.txt"
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        path = PACKAGE_ROOT / relative
        if not path.is_file() or sha256_file(path) != expected:
            return False
    return True


def pilot_static_preflight(qualification_root: Path) -> dict[str, Any]:
    session, matrix, _artifacts = load_pilot_contracts()
    checks: dict[str, bool] = {}
    checks["matrix_exact_12_cells"] = (
        len(matrix["instances"]) * len(matrix["ceilings"])
        * matrix["executions_per_cell"] == matrix["expected_cells"] == 12
    )
    identity = session["frozen_identity"]
    for key, path_key, hash_key in (
        ("snapshot_inventory", "snapshot_inventory_path", "snapshot_inventory_sha256"),
        ("sample_manifest", "sample_manifest_path", "sample_manifest_sha256"),
    ):
        checks[f"{key}_hash"] = sha256_file(PACKAGE_ROOT / identity[path_key]) == identity[hash_key]
    checks["generation_profile_hash"] = (
        sha256_file(PROFILE_MAP_PATH) == identity["generation_profile_sha256"]
    )
    checks["package_checksum_ledger"] = _checksums_valid()
    checks["fresh_session"] = not (qualification_root / session["session_id"]).exists()
    from scheduler.g25_historical_evidence import (
        verify_historical_evidence_archive,
    )

    try:
        historical = verify_historical_evidence_archive()
    except Exception:
        historical = {}
    checks["r3_immutable"] = historical.get("r3_session_sha256") == (
        session["preflight"]["r3_session_sha256"]
    )
    r4 = load_contract()["r4_immutability"]
    for name, binding_id, expected_key in (
        ("r4_session", "r4_session_sha256", "session_sha256"),
        ("r4_suite_snapshot", "r4_suite_snapshot_sha256", "suite_snapshot_sha256"),
        ("r4_journal", "r4_journal_sha256", "journal_sha256"),
        ("r4_failed_state", "r4_failed_state_sha256", "failed_state_sha256"),
        ("r4_failure_quality", "r4_failure_quality_sha256", "failure_quality_sha256"),
    ):
        checks[f"{name}_immutable"] = historical.get(binding_id) == r4[expected_key]
    repo_root = PACKAGE_ROOT.parent
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_root,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    checks["clean_worktree"] = status.returncode == 0 and not status.stdout.strip()
    tags = subprocess.run(
        ["git", "tag", "--points-at", "HEAD"], cwd=repo_root,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    annotated = False
    for tag in tags.stdout.splitlines() if tags.returncode == 0 else ():
        kind = subprocess.run(
            ["git", "cat-file", "-t", tag], cwd=repo_root,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        annotated = annotated or (kind.returncode == 0 and kind.stdout.strip() == "tag")
    checks["annotated_review_tag"] = annotated
    blockers = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema_version": "g25-gpu-pilot-static-preflight-v1",
        "status": "static_pass_dynamic_gpu_pending" if not blockers else "blocked",
        "session_id": session["session_id"],
        "checks": checks,
        "blockers": blockers,
        "pending_dynamic_gpu_checks": [
            "exact_RTX_3050_name_uuid_and_pci_bus", "no_other_compute_process",
            "free_vram_at_least_5000000000", "free_disk_at_least_8589934592",
            "offline_runtime_versions", "routing_hook_runtime_invariant",
            "fresh_S4_same_source_review", "5_6sol_GO",
            "owner_approval_record_and_exact_command_hash",
        ],
        "execution_ready": False,
        "gpu_used": False,
        "gpu_authorized": False,
    }


def pilot_status(qualification_root: Path, session_id: str | None = None) -> dict[str, Any]:
    contract, _matrix, _artifacts = load_pilot_contracts()
    requested = session_id or contract["session_id"]
    exists = (qualification_root / _safe_session_id(requested)).exists()
    return {
        "schema_version": "g25-gpu-pilot-status-v1",
        "session_id": requested,
        "session_exists": exists,
        "status": "NOT_RUN" if not exists else "PRESENT_REQUIRES_AUDIT",
        "qualification_cells": 0 if not exists else None,
        "gpu_used": False if not exists else None,
        "gpu_authorized": False,
        "formal_g3_r5_authorized": False,
    }


def qualification_instances(contract: Mapping[str, Any]) -> list[str]:
    return sorted(contract["candidate_scope"]["instances"])


def qualification_ceilings(contract: Mapping[str, Any]) -> list[int]:
    return list(contract["execution_controls"]["token_ceiling_candidates"])


def resolve_task_profile(
    profile_map: Mapping[str, Any], task_id: str, *, ceiling: int | None = None
) -> dict[str, Any]:
    mapping = profile_map.get("mapping_policy") or {}
    if mapping.get("dynamic_output_based_override") != "forbidden":
        raise ValueError("dynamic output-based profile override must be forbidden")
    if mapping.get("per_sample_override") != "forbidden":
        raise ValueError("per-sample generation profile override must be forbidden")
    profiles = profile_map.get("task_profiles") or {}
    if task_id not in profiles:
        raise ValueError(f"unknown task profile: {task_id}")
    profile = dict(profiles[task_id])
    common = dict(profile_map.get("common") or {})
    if task_id == "T1":
        candidates = profile.get("candidate_ceilings")
        if ceiling not in candidates:
            raise ValueError("ceiling is outside the frozen T1 candidate grid")
        profile["max_new_tokens"] = ceiling
    elif ceiling is not None and ceiling != profile.get("max_new_tokens"):
        raise ValueError("task ceiling override differs from frozen profile")
    resolved = {**common, **profile}
    allowed = profile_map.get("executable_generation_keys") or []
    expected = {"max_new_tokens", "do_sample", "num_beams", "use_cache", "seed"}
    if set(allowed) != expected:
        raise ValueError("executable generation key allowlist differs from frozen contract")
    executable = {key: resolved[key] for key in allowed if key in resolved}
    if set(executable) != expected:
        raise ValueError("resolved task profile lacks executable generation controls")
    return executable


def _valid_token_ids(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0
            for item in value
        )
    )


def _valid_actual_parameters(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "parameter_tensors", "total_numel", "dtypes", "device_kinds",
        "device_locations", "model_class", "model_module",
        "config_model_type", "config_architectures",
        "manifest_schema", "parameters", "parameter_manifest_sha256",
    }:
        return False
    manifest = value.get("parameters")
    if not isinstance(manifest, list) or len(manifest) != value.get(
        "parameter_tensors"
    ):
        return False
    expected_keys = {
        "name", "shape", "stride", "numel", "element_size", "dtype",
        "device_kind", "device_location", "requires_grad", "object_id",
        "storage_data_ptr", "storage_nbytes", "mutation_version",
        "content_sha256",
    }
    names: list[str] = []
    total_numel = 0
    for item in manifest:
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            return False
        name = item.get("name")
        shape = item.get("shape")
        stride = item.get("stride")
        numel = item.get("numel")
        element_size = item.get("element_size")
        object_id = item.get("object_id")
        storage_data_ptr = item.get("storage_data_ptr")
        storage_nbytes = item.get("storage_nbytes")
        mutation_version = item.get("mutation_version")
        content_sha256 = item.get("content_sha256")
        if not (
            isinstance(name, str) and name
            and isinstance(shape, list)
            and all(
                isinstance(dimension, int) and not isinstance(dimension, bool)
                and dimension >= 0 for dimension in shape
            )
            and isinstance(stride, list) and len(stride) == len(shape)
            and all(
                isinstance(step, int) and not isinstance(step, bool)
                for step in stride
            )
            and isinstance(numel, int) and not isinstance(numel, bool)
            and numel > 0
            and isinstance(element_size, int)
            and not isinstance(element_size, bool) and element_size > 0
            and item.get("dtype") == "bfloat16"
            and item.get("device_kind") == "cuda"
            and item.get("device_location") == "cuda:0"
            and isinstance(item.get("requires_grad"), bool)
            and isinstance(object_id, int) and not isinstance(object_id, bool)
            and object_id > 0
            and isinstance(storage_data_ptr, int)
            and not isinstance(storage_data_ptr, bool) and storage_data_ptr > 0
            and isinstance(storage_nbytes, int)
            and not isinstance(storage_nbytes, bool) and storage_nbytes > 0
            and isinstance(mutation_version, int)
            and not isinstance(mutation_version, bool) and mutation_version >= 0
            and isinstance(content_sha256, str)
            and len(content_sha256) == 64
            and all(character in "0123456789abcdef" for character in content_sha256)
        ):
            return False
        names.append(name)
        total_numel += numel
    if names != sorted(names) or len(names) != len(set(names)):
        return False
    if value.get("parameter_manifest_sha256") != canonical_hash(manifest):
        return False
    return bool(
        value["manifest_schema"] == "granite-parameter-identity-v1"
        and isinstance(value["parameter_tensors"], int)
        and not isinstance(value["parameter_tensors"], bool)
        and value["parameter_tensors"] > 0
        and isinstance(value["total_numel"], int)
        and not isinstance(value["total_numel"], bool)
        and value["total_numel"] > 0
        and value["total_numel"] == total_numel
        and value["dtypes"] == ["bfloat16"]
        and value["device_kinds"] == ["cuda"]
        and value["device_locations"] == ["cuda:0"]
        and value["model_class"] == "GraniteMoeForCausalLM"
        and isinstance(value["model_module"], str)
        and value["model_module"].endswith("modeling_granitemoe")
        and value["config_model_type"] == "granitemoe"
        and value["config_architectures"] == ["GraniteMoeForCausalLM"]
    )


def load_frozen_qualification_sample(
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a qualification sample from the frozen suite, never worker input."""
    from scripts.projectctl import load_suite

    contract = load_contract()
    suite, _source = load_suite(contract["candidate_scope"]["suite"])
    matches = [
        row for row in suite["samples"]
        if row.get("instance_id") == selection.get("instance_id")
        and row.get("task_id") == "T1"
    ]
    if len(matches) != 1:
        raise ValueError("frozen suite does not contain exactly one qualification instance")
    row = matches[0]
    sample = row.get("source_sample")
    if not isinstance(sample, Mapping):
        raise ValueError("frozen suite qualification row lacks source_sample")
    sample = dict(sample)
    if (
        row.get("source_sample_id") != selection.get("sample_id")
        or sample.get("sample_id") != selection.get("sample_id")
        or row.get("prompt_hash") != selection.get("prompt_hash")
        or sample.get("prompt_hash") != selection.get("prompt_hash")
        or row.get("raw_sample_hash") != selection.get("raw_sample_hash")
        or sample.get("raw_sample_hash") != selection.get("raw_sample_hash")
        or sample.get("task_id") != "T1"
    ):
        raise ValueError("frozen suite sample identity differs from candidate manifest")
    return sample


def _selection_for_prompt(expected_prompt_hash: str) -> dict[str, Any]:
    matches = [
        dict(row) for row in _manifest_selections(load_contract()).values()
        if row.get("prompt_hash") == expected_prompt_hash
    ]
    if len(matches) != 1:
        raise ValueError("prompt hash does not identify exactly one frozen qualification row")
    return matches[0]


def _decode_frozen_output_ids(output_ids: list[int]) -> str:
    """Decode with the exact local tokenizer after revalidating its payload."""
    inventory = validate_exact_snapshot(QUALIFICATION_MODEL_SNAPSHOT_ROOT)
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        inventory["absolute_path"], local_files_only=True
    )
    return str(tokenizer.decode(output_ids, skip_special_tokens=True))


def derive_parent_output_replay(
    evidence: Mapping[str, Any],
    *,
    expected_prompt_hash: str,
    ceiling: int,
    synthetic_session: bool,
    decode_output_ids: Callable[[list[int]], str] | None = None,
) -> dict[str, Any]:
    """Independently derive termination and parser validity from frozen sources."""
    contract = load_contract()
    eos_token_id = contract["frozen_inputs"]["eos_token_id"]
    output_ids_value = evidence.get("output_token_ids")
    output_ids = list(output_ids_value) if _valid_token_ids(output_ids_value) else None
    within_frozen_ceiling = bool(
        output_ids is not None and 0 < len(output_ids) <= ceiling
    )
    worker_text = evidence.get("text") if isinstance(evidence.get("text"), str) else ""
    claimed_stop = evidence.get("stop_reason")
    claimed_parser = evidence.get("parser_outcome")
    terminal_token_id = output_ids[-1] if output_ids else None
    eos_count = output_ids.count(eos_token_id) if output_ids is not None else 0
    return_code = evidence.get("return_code")
    timed_out = evidence.get("timed_out") is True
    if timed_out or return_code != 0:
        derived_stop = "not_run"
        legal_stop = False
    elif not output_ids:
        derived_stop = "illegal_stop"
        legal_stop = False
    elif eos_count == 1 and terminal_token_id == eos_token_id:
        derived_stop = "eos_token"
        legal_stop = len(output_ids) <= ceiling
    elif eos_count == 0 and len(output_ids) == ceiling:
        derived_stop = "max_new_tokens"
        legal_stop = True
    else:
        derived_stop = "illegal_stop"
        legal_stop = False

    parent_text: str | None = None
    replay_error: str | None = None
    sample: dict[str, Any] | None = None
    evaluator_result: dict[str, Any] | None = None
    derived_parser = "not_run"
    parser_validity: bool | None = None
    if synthetic_session:
        parent_text_matches = None
        if derived_stop == "eos_token":
            derived_parser = (
                claimed_parser if claimed_parser in {"parseable", "unparseable"}
                else "unknown"
            )
    elif (
        return_code == 0
        and not timed_out
        and output_ids is not None
        and within_frozen_ceiling
    ):
        try:
            selection = _selection_for_prompt(expected_prompt_hash)
            sample = load_frozen_qualification_sample(selection)
            decoder = decode_output_ids or _decode_frozen_output_ids
            parent_text = decoder(output_ids)
            parent_text_matches = parent_text == worker_text
            if derived_stop == "eos_token":
                evaluator_result = dict(evaluate_frozen_sample(parent_text, sample))
                parser_validity = evaluator_result.get("validity") is True
                derived_parser = "parseable" if parser_validity else "unparseable"
        except Exception as error:
            parent_text_matches = False
            replay_error = f"{type(error).__name__}: {error}"
    else:
        parent_text_matches = None

    stop_claim_matches = (
        claimed_stop == derived_stop
        if derived_stop != "not_run"
        else claimed_stop in {None, "not_run"}
    )
    parser_claim_matches = (
        claimed_parser == derived_parser
        if derived_stop == "eos_token"
        else True
    )
    replay = {
        "schema_version": "g25-parent-output-replay-v2",
        "required": not synthetic_session,
        "frozen_eos_token_id": eos_token_id,
        "frozen_ceiling": ceiling,
        "output_token_ids_sha256": canonical_hash(output_ids)
        if output_ids is not None else None,
        "output_token_count": len(output_ids) if output_ids is not None else None,
        "terminal_token_id": terminal_token_id,
        "eos_occurrence_count": eos_count,
        "eos_is_unique_terminal": bool(
            output_ids and eos_count == 1 and terminal_token_id == eos_token_id
        ),
        "ceiling_reached": bool(output_ids is not None and len(output_ids) == ceiling),
        "within_frozen_ceiling": within_frozen_ceiling,
        "derived_stop_reason": derived_stop,
        "legal_stop": legal_stop,
        "worker_claimed_stop_reason": claimed_stop
        if isinstance(claimed_stop, str) else None,
        "worker_text_sha256": hashlib.sha256(worker_text.encode("utf-8")).hexdigest(),
        "parent_decoded_text_sha256": hashlib.sha256(parent_text.encode("utf-8")).hexdigest()
        if parent_text is not None else None,
        "worker_text_matches_parent_decode": parent_text_matches,
        "source_sample_id": sample.get("sample_id") if sample else None,
        "raw_sample_hash": sample.get("raw_sample_hash") if sample else None,
        "frozen_source_sample_sha256": canonical_hash(sample) if sample else None,
        "evaluator": evaluator_result.get("evaluator") if evaluator_result else None,
        "evaluator_result_sha256": canonical_hash(evaluator_result)
        if evaluator_result is not None else None,
        "parser_validity": parser_validity,
        "derived_parser_outcome": derived_parser,
        "worker_claimed_parser_outcome": claimed_parser
        if claimed_parser in {"parseable", "unparseable", "not_run", "unknown"}
        else "unknown",
        "stop_claim_matches": stop_claim_matches,
        "parser_claim_matches": parser_claim_matches,
        "worker_claims_match": stop_claim_matches and parser_claim_matches,
        "correctness_used_for_eligibility": False,
        "replay_error": replay_error,
    }
    replay["replay_sha256"] = canonical_hash(replay)
    return replay


def classify_worker_evidence(
    evidence: Mapping[str, Any], *, expected_prompt_hash: str, ceiling: int,
    synthetic_session: bool,
    decode_output_ids: Callable[[list[int]], str] | None = None,
) -> tuple[str, str]:
    missing = sorted(REQUIRED_WORKER_FIELDS - set(evidence))
    if missing:
        return "INVALID_EVIDENCE", "missing worker fields: " + ", ".join(missing)
    try:
        input_ids = evidence["input_token_ids"]
        output_ids = evidence["output_token_ids"]
        structural_ok = (
            evidence.get("schema_version") == "g25-worker-evidence-v1"
            and evidence.get("prompt_hash") == expected_prompt_hash
            and _valid_token_ids(input_ids)
            and _valid_token_ids(output_ids)
            and evidence.get("input_token_count") == len(input_ids)
            and evidence.get("output_token_count") == len(output_ids)
            and evidence.get("output_hash") == canonical_hash(output_ids)
            and isinstance(evidence.get("effective_generation_config"), dict)
            and evidence.get("effective_generation_config_sha256")
            == canonical_hash(evidence["effective_generation_config"])
            and isinstance(evidence.get("return_code"), int)
            and not isinstance(evidence.get("return_code"), bool)
            and isinstance(evidence.get("timed_out"), bool)
            and isinstance(evidence.get("generation_seconds"), (int, float))
            and math.isfinite(float(evidence["generation_seconds"]))
            and float(evidence["generation_seconds"]) >= 0
            and isinstance(evidence.get("wall_time_seconds"), (int, float))
            and math.isfinite(float(evidence["wall_time_seconds"]))
            and float(evidence["wall_time_seconds"]) >= 0
            and evidence.get("routing_capture_enabled") is False
            and evidence.get("profiler_enabled") is False
            and isinstance(evidence.get("execution_identity"), dict)
            and isinstance(evidence["execution_identity"].get("mode"), str)
            and isinstance(evidence["execution_identity"].get("runtime"), dict)
            and isinstance(evidence["execution_identity"].get("device"), dict)
            and evidence.get("parser_outcome")
            in {"parseable", "unparseable", "not_run", "unknown"}
        )
    except (TypeError, ValueError):
        structural_ok = False
    if not structural_ok:
        return "INVALID_EVIDENCE", "worker evidence integrity or isolation check failed"
    identity = evidence["execution_identity"]
    expected_generation_config = resolve_task_profile(
        load_profile_map(), "T1", ceiling=ceiling
    )
    if evidence["effective_generation_config"] != expected_generation_config:
        return "INVALID_EVIDENCE", "worker effective generation config differs from frozen profile"
    expected_mode = "synthetic_governance" if synthetic_session else (
        "supervisor_timeout" if evidence["timed_out"] else "qualification"
    )
    if identity["mode"] != expected_mode:
        return "INVALID_EVIDENCE", "worker execution mode differs from parent session context"
    if not synthetic_session:
        parent = evidence.get("parent_process")
        lifetime_guard = parent.get("lifetime_guard") if isinstance(parent, Mapping) else None
        drain = (
            lifetime_guard.get("drain")
            if isinstance(lifetime_guard, Mapping)
            else None
        )
        if (
            not isinstance(parent, Mapping)
            or not isinstance(parent.get("worker_pid"), int)
            or isinstance(parent.get("worker_pid"), bool)
            or parent["worker_pid"] <= 0
            or not isinstance(parent.get("started_unix_ns"), int)
            or not isinstance(parent.get("finished_unix_ns"), int)
            or parent["finished_unix_ns"] < parent["started_unix_ns"]
            or parent.get("timeout_stage")
            not in {"completed", "cell_timeout_sigterm", "cell_timeout_sigkill"}
            or parent.get("term_grace_seconds") != 30
            or not isinstance(parent.get("worker_argv_sha256"), str)
            or len(parent["worker_argv_sha256"]) != 64
            or not isinstance(parent.get("io_manifest_sha256"), str)
            or len(parent["io_manifest_sha256"]) != 64
            or not isinstance(lifetime_guard, Mapping)
            or set(lifetime_guard) != {
                "schema_version", "mechanism", "expected_parent_pid",
                "expected_parent_start_ticks", "lease_fd", "lease_device",
                "lease_inode", "pdeathsig", "pdeathsig_number",
                "ready_observed", "move_observed", "go_sent",
                "membership_ack_observed", "cell_cgroup_path",
                "cell_cgroup_device", "cell_cgroup_inode",
                "cgroup_kill_supported", "populated_zero_observed", "drain",
            }
            or lifetime_guard.get("schema_version")
            != "g25-worker-lifetime-guard-v2"
            or lifetime_guard.get("mechanism")
            != "systemd-delegated-cgroup-v2+pdeathsig-v2"
            or lifetime_guard.get("pdeathsig") != "SIGKILL"
            or lifetime_guard.get("pdeathsig_number") != int(signal.SIGKILL)
            or lifetime_guard.get("ready_observed") is not True
            or lifetime_guard.get("move_observed") is not True
            or lifetime_guard.get("go_sent") is not True
            or lifetime_guard.get("membership_ack_observed") is not True
            or lifetime_guard.get("cgroup_kill_supported") is not True
            or lifetime_guard.get("populated_zero_observed") is not True
            or not isinstance(lifetime_guard.get("cell_cgroup_path"), str)
            or not re.fullmatch(
                r"/.+/g25-cell-[0-9a-f]{64}",
                lifetime_guard.get("cell_cgroup_path", ""),
            )
            or any(
                not isinstance(lifetime_guard.get(key), int)
                or isinstance(lifetime_guard.get(key), bool)
                or lifetime_guard[key] <= 0
                for key in (
                    "expected_parent_pid", "expected_parent_start_ticks",
                    "lease_fd", "lease_device", "lease_inode",
                    "cell_cgroup_device", "cell_cgroup_inode",
                )
            )
            or not isinstance(drain, Mapping)
            or set(drain) != {
                "initial_populated", "term_sent", "term_sent_monotonic_ns",
                "term_grace_seconds", "cgroup_kill_written",
                "cgroup_kill_monotonic_ns", "populated_zero_monotonic_ns",
                "final_populated",
            }
            or drain.get("initial_populated") not in {0, 1}
            or not isinstance(drain.get("term_sent"), bool)
            or drain.get("term_grace_seconds") != 30
            or not isinstance(drain.get("cgroup_kill_written"), bool)
            or drain.get("final_populated") != 0
            or not isinstance(drain.get("populated_zero_monotonic_ns"), int)
            or drain["populated_zero_monotonic_ns"] < 0
        ):
            return "INVALID_EVIDENCE", "parent worker supervision evidence is incomplete"
    if identity["mode"] == "qualification":
        contract = load_contract()
        _pilot_session, pilot_matrix, _pilot_artifacts = load_pilot_contracts()
        model = identity.get("model") or {}
        runtime = identity.get("runtime") or {}
        device = identity.get("device") or {}
        precision = identity.get("precision")
        parameters = identity.get("parameters") or {}
        pre_parameters = parameters.get("pre_generation")
        post_parameters = parameters.get("post_generation")
        if (
            not isinstance(precision, Mapping)
            or set(precision) != {
                "required", "pre_generation", "post_generation"
            }
            or precision.get("required") != "bf16"
            or precision.get("pre_generation") != "bf16"
            or precision.get("post_generation") != "bf16"
        ):
            return (
                "INVALID_EVIDENCE",
                "actual BF16 precision is missing, non-BF16, or changed during generation",
            )
        if (
            set(parameters) != {"required", "pre_generation", "post_generation"}
            or parameters.get("required") != {
                "dtype": "bfloat16",
                "device_kind": "cuda",
                "model_class": "GraniteMoeForCausalLM",
                "config_model_type": "granitemoe",
            }
            or not _valid_actual_parameters(pre_parameters)
            or pre_parameters != post_parameters
        ):
            return (
                "INVALID_EVIDENCE",
                "actual model parameter dtype/device/config evidence is missing or drifted",
            )
        prompt_instances = [
            item["instance_id"] for item in _manifest_selections(contract).values()
            if item["prompt_hash"] == expected_prompt_hash
        ]
        rendered_input = (
            pilot_matrix["frozen_rendered_inputs"].get(prompt_instances[0])
            if len(prompt_instances) == 1 else None
        )
        if (
            model.get("model_id") != contract["frozen_inputs"]["model_id"]
            or model.get("model_revision")
            != contract["frozen_inputs"]["model_revision"]
            or model.get("tokenizer_revision")
            != contract["frozen_inputs"]["tokenizer_revision"]
            or identity.get("chat_template_sha256")
            != contract["frozen_inputs"]["chat_template_sha256"]
            or identity.get("prompt_construction_revision")
            != contract["frozen_inputs"]["prompt_construction_revision"]
            or identity.get("system_message_sha256")
            != contract["frozen_inputs"]["system_message_sha256"]
            or identity.get("tokenizer_config_sha256")
            != contract["frozen_inputs"]["tokenizer_config_sha256"]
            or identity.get("generation_config_file_sha256")
            != contract["frozen_inputs"]["generation_config_file_sha256"]
            or identity.get("special_tokens_map_sha256")
            != contract["frozen_inputs"]["special_tokens_map_sha256"]
            or identity.get("eos_token_id")
            != contract["frozen_inputs"]["eos_token_id"]
            or identity.get("pad_token_id")
            != contract["frozen_inputs"]["pad_token_id"]
            or not isinstance(identity.get("rendered_chat_sha256"), str)
            or len(identity["rendered_chat_sha256"]) != 64
            or rendered_input is None
            or identity["rendered_chat_sha256"]
            != rendered_input["rendered_chat_sha256"]
            or evidence["input_token_count"] != rendered_input["input_token_count"]
            or canonical_hash(evidence["input_token_ids"])
            != rendered_input["input_token_ids_sha256"]
            or identity.get("seed") != contract["frozen_inputs"]["seed"]
            or runtime.get("torch") != contract["frozen_inputs"]["torch_version"]
            or runtime.get("transformers")
            != contract["frozen_inputs"]["transformers_version"]
            or device.get("kind") != "cuda"
            or device.get("locations") != ["cuda:0"]
            or not device.get("name")
            or not device.get("uuid")
        ):
            return "INVALID_EVIDENCE", "actual execution identity differs from frozen contract"
    if evidence["return_code"] != 0 and not evidence["timed_out"]:
        return "RUNTIME_FAILURE", evidence.get("exception") or "worker returned nonzero"
    if evidence["timed_out"]:
        return "TIMEOUT", "per-cell wall-time ceiling reached"
    replay = derive_parent_output_replay(
        evidence,
        expected_prompt_hash=expected_prompt_hash,
        ceiling=ceiling,
        synthetic_session=synthetic_session,
        decode_output_ids=decode_output_ids,
    )
    if not synthetic_session:
        if replay["within_frozen_ceiling"] is not True:
            return "INVALID_OUTPUT", "parent replay found output beyond the frozen ceiling"
        if replay["replay_error"] is not None:
            return "INVALID_EVIDENCE", "parent output replay failed: " + replay["replay_error"]
        if replay["worker_text_matches_parent_decode"] is not True:
            return "INVALID_EVIDENCE", "worker text differs from parent tokenizer replay"
        if replay["worker_claims_match"] is not True:
            return "INVALID_EVIDENCE", "worker stop/parser claims contradict parent replay"
        if replay["derived_stop_reason"] == "max_new_tokens":
            return "TRUNCATED", "generation reached the frozen token ceiling"
        if replay["derived_stop_reason"] != "eos_token" or not replay["legal_stop"]:
            return "INVALID_OUTPUT", "parent replay found an illegal generation stop"
        if replay["parser_validity"] is not True:
            return "INVALID_OUTPUT", "parent frozen evaluator found an unparsable output"
        return "QUALIFIED", "parent replay proved legal EOS and parseable output"
    if evidence.get("stop_reason") == "max_new_tokens":
        return "TRUNCATED", "generation reached the frozen token ceiling"
    if (
        evidence.get("stop_reason") != "eos_token"
        or not evidence.get("text")
        or evidence.get("parser_outcome") != "parseable"
        or evidence.get("output_token_count", 0) <= 0
        or evidence.get("output_token_count", 0) > ceiling
    ):
        return "INVALID_OUTPUT", "output is empty, unparsable, over ceiling, or illegally stopped"
    return "QUALIFIED", "complete parseable execution with legal eos_token stop"


def _safe_session_id(value: str) -> str:
    candidate = Path(value)
    if (
        not value
        or candidate.is_absolute()
        or candidate.name != value
        or value in {".", ".."}
    ):
        raise ValueError("qualification session must be one relative path component")
    return value


def cell_identity(
    session_id: str,
    instance_id: str,
    sample_id: str,
    ceiling: int,
    profile_sha256: str,
    generation_config_sha256: str,
) -> str:
    return canonical_hash({
        "session_id": session_id,
        "instance_id": instance_id,
        "sample_id": sample_id,
        "ceiling": ceiling,
        "profile_sha256": profile_sha256,
        "generation_config_sha256": generation_config_sha256,
    })


def build_cell_row(
    *,
    session_id: str,
    selection: Mapping[str, Any],
    ceiling: int,
    profile_sha256: str,
    generation_config_sha256: str,
    evidence: Mapping[str, Any],
    evidence_descriptor: Mapping[str, Any],
    synthetic_session: bool = True,
    decode_output_ids: Callable[[list[int]], str] | None = None,
) -> dict[str, Any]:
    classification, reason = classify_worker_evidence(
        evidence,
        expected_prompt_hash=selection["prompt_hash"],
        ceiling=ceiling,
        synthetic_session=synthetic_session,
        decode_output_ids=decode_output_ids,
    )
    replay = derive_parent_output_replay(
        evidence,
        expected_prompt_hash=selection["prompt_hash"],
        ceiling=ceiling,
        synthetic_session=synthetic_session,
        decode_output_ids=decode_output_ids,
    )
    input_ids = evidence.get("input_token_ids")
    output_ids = evidence.get("output_token_ids")
    row = {
        "schema_version": "g25-qualification-cell-v2",
        "session_id": session_id,
        "cell_id": cell_identity(
            session_id,
            selection["instance_id"],
            selection["sample_id"],
            ceiling,
            profile_sha256,
            generation_config_sha256,
        ),
        "evidence_role": EVIDENCE_ROLE,
        "formal_c1_evidence": False,
        "synthetic": synthetic_session,
        "instance_id": selection["instance_id"],
        "sample_id": selection["sample_id"],
        "task_id": "T1",
        "ceiling": ceiling,
        "generation_profile": PROFILE_NAME,
        "generation_profile_sha256": profile_sha256,
        "generation_config_sha256": generation_config_sha256,
        "prompt_hash": selection["prompt_hash"],
        "input_hash": canonical_hash(input_ids) if _valid_token_ids(input_ids) else None,
        "input_token_count": evidence.get("input_token_count")
        if isinstance(evidence.get("input_token_count"), int) else None,
        "output_token_count": evidence.get("output_token_count")
        if isinstance(evidence.get("output_token_count"), int) else None,
        "stop_reason": replay["derived_stop_reason"],
        "worker_claimed_stop_reason": evidence.get("stop_reason")
        if isinstance(evidence.get("stop_reason"), str) else None,
        "wall_time_seconds": evidence.get("wall_time_seconds")
        if isinstance(evidence.get("wall_time_seconds"), (int, float)) else None,
        "generation_seconds": evidence.get("generation_seconds")
        if isinstance(evidence.get("generation_seconds"), (int, float)) else None,
        "process_return_code": evidence.get("return_code")
        if isinstance(evidence.get("return_code"), int) else None,
        "timed_out": evidence.get("timed_out")
        if isinstance(evidence.get("timed_out"), bool) else None,
        "execution_status": {
            "QUALIFIED": "complete",
            "TRUNCATED": "complete",
            "INVALID_OUTPUT": "complete",
            "RUNTIME_FAILURE": "failed",
            "TIMEOUT": "timeout",
            "INVALID_EVIDENCE": "invalid_evidence",
        }[classification],
        "qualification_class": classification,
        "classification_reason": reason,
        "output_hash": evidence.get("output_hash")
        if isinstance(evidence.get("output_hash"), str) else None,
        "output_token_ids_sha256": canonical_hash(output_ids)
        if _valid_token_ids(output_ids) else None,
        "parser_outcome": replay["derived_parser_outcome"],
        "worker_claimed_parser_outcome": evidence.get("parser_outcome")
        if evidence.get("parser_outcome")
        in {"parseable", "unparseable", "not_run", "unknown"}
        else "unknown",
        "parent_output_replay": replay,
        "parent_output_replay_sha256": canonical_hash(replay),
        "routing_capture_enabled": False,
        "profiler_enabled": False,
        "execution_identity": evidence.get("execution_identity")
        if isinstance(evidence.get("execution_identity"), dict) else {},
        "execution_identity_sha256": canonical_hash(
            evidence.get("execution_identity")
        ) if isinstance(evidence.get("execution_identity"), dict) else None,
        "worker_evidence_sha256": canonical_hash(evidence),
        "evidence_paths": [dict(evidence_descriptor)],
    }
    validate_schema(CELL_SCHEMA, row)
    return row


class _SingleLoadRuntimeObserver:
    """Delegate the formal generation core while observing its one model load."""

    def __init__(
        self,
        adapter: Any,
        runtime_closure_verifier: Callable[[str], dict[str, Any]],
    ) -> None:
        self.adapter = adapter
        self.runtime_closure_verifier = runtime_closure_verifier
        self.load_calls = 0
        self.pre_generation_runtime: dict[str, Any] | None = None
        self.pre_generation_closure: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    def load_model(self, *args: Any, **kwargs: Any) -> None:
        if self.load_calls:
            raise RuntimeError("qualification generation attempted a second model load")
        self.load_calls += 1
        self.adapter.load_model(*args, **kwargs)
        self.pre_generation_closure = self.runtime_closure_verifier(
            "worker_pre_generation"
        )
        runtime = as_mapping(self.adapter.collect_runtime_metadata())
        if runtime.get("routing_capture_enabled") is not False:
            raise RuntimeError("routing capture must be disabled before qualification")
        self.pre_generation_runtime = runtime


def execute_generation_core(
    adapter: Any,
    *,
    execution: Mapping[str, Any],
    prompt: str,
    sample: Mapping[str, Any],
    generation_config: Mapping[str, Any],
    request_id: str,
    runtime_closure_verifier: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the same generation core as formal P0 without collectors or routing hooks."""
    if runtime_closure_verifier is None:
        from scheduler.g25_runtime_closure import verify_live_loaded_closure

        runtime_closure_verifier = verify_live_loaded_closure
    started = time.perf_counter()
    observed_runner = _SingleLoadRuntimeObserver(adapter, runtime_closure_verifier)
    request = CollectorRequest(
        execution=dict(execution),
        prompt=prompt,
        generation_config=dict(generation_config),
        request_id=request_id,
        sample=dict(sample),
    )
    output, _timing = run_generation(observed_runner, request)
    wall = time.perf_counter() - started
    if (
        observed_runner.load_calls != 1
        or observed_runner.pre_generation_runtime is None
        or observed_runner.pre_generation_closure is None
    ):
        raise RuntimeError("qualification generation did not perform exactly one model load")
    pre_runtime = observed_runner.pre_generation_runtime
    post_runtime = as_mapping(adapter.collect_runtime_metadata())
    post_generation_closure = runtime_closure_verifier("worker_post_generation")
    if post_runtime.get("routing_capture_enabled") is not False:
        raise RuntimeError("routing capture became enabled during qualification")
    if output.get("routing"):
        raise RuntimeError("qualification generation emitted routing evidence")
    generation_result = GenerationResult(
        text=str(output.get("text") or ""),
        input_token_ids=list(output.get("input_token_ids") or []),
        output_token_ids=list(output.get("output_token_ids") or []),
        input_token_count=int(output.get("input_token_count") or 0),
        output_token_count=int(output.get("output_token_count") or 0),
        stop_reason=str(output.get("stop_reason") or ""),
        output_hash=str(output.get("output_hash") or ""),
        return_code=int(output.get("return_code") or 0),
        generation_seconds=float(output.get("generation_seconds") or 0.0),
        tokenization_metadata=dict(as_mapping(
            output.get("tokenization_metadata") or {}
        )),
        score_diagnostics=output.get("score_diagnostics")
        if isinstance(output.get("score_diagnostics"), Mapping) else None,
        routing=[],
        exception=output.get("exception")
        if isinstance(output.get("exception"), str) else None,
    )
    try:
        quality = as_mapping(adapter.collect_quality_result(generation_result, sample))
        parser_outcome = "parseable" if quality.get("validity") is True else "unparseable"
    except Exception:
        parser_outcome = "unparseable"
    pre_parameters = dict(as_mapping(pre_runtime.get("parameter_evidence") or {}))
    post_parameters = dict(as_mapping(post_runtime.get("parameter_evidence") or {}))
    post_device_kinds = post_parameters.get("device_kinds")
    normalized_device_kind = (
        post_device_kinds[0]
        if isinstance(post_device_kinds, list) and len(post_device_kinds) == 1
        else None
    )
    pre_dtypes = pre_parameters.get("dtypes")
    post_dtypes = post_parameters.get("dtypes")
    return {
        "schema_version": "g25-worker-evidence-v1",
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "input_token_ids": list(output.get("input_token_ids") or []),
        "output_token_ids": list(output.get("output_token_ids") or []),
        "input_token_count": output.get("input_token_count"),
        "output_token_count": output.get("output_token_count"),
        "text": output.get("text"),
        "stop_reason": output.get("stop_reason"),
        "output_hash": output.get("output_hash"),
        "return_code": output.get("return_code"),
        "generation_seconds": output.get("generation_seconds"),
        "wall_time_seconds": wall,
        "timed_out": False,
        "exception": output.get("exception"),
        "parser_outcome": parser_outcome,
        "routing_capture_enabled": False,
        "profiler_enabled": False,
        "effective_generation_config": dict(generation_config),
        "effective_generation_config_sha256": canonical_hash(generation_config),
        "execution_identity": {
            "mode": "qualification",
            "model": post_runtime.get("model"),
            "precision": {
                "required": "bf16",
                "pre_generation": (
                    "bf16" if pre_dtypes == ["bfloat16"] else None
                ),
                "post_generation": (
                    "bf16" if post_dtypes == ["bfloat16"] else None
                ),
            },
            "parameters": {
                "required": {
                    "dtype": "bfloat16",
                    "device_kind": "cuda",
                    "model_class": "GraniteMoeForCausalLM",
                    "config_model_type": "granitemoe",
                },
                "pre_generation": pre_parameters,
                "post_generation": post_parameters,
            },
            "chat_template_sha256": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("chat_template_sha256"),
            "prompt_construction_revision": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("prompt_construction_revision"),
            "system_message_sha256": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("system_message_sha256"),
            "tokenizer_config_sha256": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("tokenizer_config_sha256"),
            "generation_config_file_sha256": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("generation_config_file_sha256"),
            "special_tokens_map_sha256": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("special_tokens_map_sha256"),
            "eos_token_id": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("eos_token_id"),
            "pad_token_id": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("pad_token_id"),
            "rendered_chat_sha256": as_mapping(
                output.get("tokenization_metadata") or {}
            ).get("rendered_chat_sha256"),
            "seed": generation_config.get("seed"),
            "runtime": {
                "torch": post_runtime.get("torch_version"),
                "transformers": post_runtime.get("transformers_version"),
            },
            "runtime_closure": {
                "pre_generation": observed_runner.pre_generation_closure,
                "post_generation": post_generation_closure,
            },
            "device": {
                "kind": normalized_device_kind,
                "locations": post_parameters.get("device_locations"),
                "name": as_mapping(execution.get("device_identity") or {}).get("name"),
                "uuid": as_mapping(execution.get("device_identity") or {}).get("uuid"),
                "pci_bus_id": as_mapping(execution.get("device_identity") or {}).get("pci_bus_id"),
            },
        },
    }


class WorkerSupervisorInterrupted(RuntimeError):
    """The application supervisor received an outer termination signal."""


def _await_worker_guard_ready(
    ready_fd: int,
    go_fd: int,
    ack_fd: int,
    process: subprocess.Popen[str],
    lifetime_guard: Mapping[str, Any],
    containment: CellCgroup,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    readable, _writable, _exceptional = select.select(
        [ready_fd], [], [], timeout_seconds
    )
    received = os.read(ready_fd, 2) if readable else b""
    if received != READY_BYTE:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        drain = containment.controller.emergency_kill(containment)
        containment.controller.close_cell(containment)
        raise WorkerLifetimeError("worker did not prove its parent-death guard")
    worker_start_ticks = read_process_start_ticks(process.pid)
    move = containment.controller.move_and_verify(
        containment,
        pid=process.pid,
        expected_start_ticks=worker_start_ticks,
    )
    if os.write(go_fd, GO_BYTE) != len(GO_BYTE):
        raise WorkerLifetimeError("worker cgroup execution grant was incomplete")
    readable, _writable, _exceptional = select.select(
        [ack_fd], [], [], timeout_seconds
    )
    received_ack = os.read(ack_fd, 2) if readable else b""
    if received_ack != ACK_BYTE:
        containment.controller.emergency_kill(containment)
        containment.controller.close_cell(containment)
        raise WorkerLifetimeError("worker did not acknowledge exact cgroup membership")
    return {
        "schema_version": "g25-worker-lifetime-guard-v2",
        "mechanism": "systemd-delegated-cgroup-v2+pdeathsig-v2",
        "expected_parent_pid": lifetime_guard["owner_pid"],
        "expected_parent_start_ticks": lifetime_guard["owner_start_ticks"],
        "lease_fd": lifetime_guard["fd"],
        "lease_device": lifetime_guard["device"],
        "lease_inode": lifetime_guard["inode"],
        "pdeathsig": "SIGKILL",
        "pdeathsig_number": int(signal.SIGKILL),
        "ready_observed": True,
        "move_observed": move["move_observed"],
        "go_sent": True,
        "membership_ack_observed": True,
        "cell_cgroup_path": containment.relative_path,
        "cell_cgroup_device": containment.device,
        "cell_cgroup_inode": containment.inode,
        "cgroup_kill_supported": True,
        "populated_zero_observed": False,
        "drain": None,
    }


def _bind_cgroup_drain(
    containment: CellCgroup,
    lifetime_evidence: dict[str, Any],
    drain: Mapping[str, Any],
) -> None:
    if drain.get("final_populated") != 0 or not containment.populated_zero_observed:
        raise CgroupDrainError("cell cgroup lacks authoritative populated=0 evidence")
    lifetime_evidence["populated_zero_observed"] = True
    lifetime_evidence["drain"] = dict(drain)


def invoke_worker_process(
    argv: Sequence[str], *, lease: ExecutionLease, containment: CellCgroup,
    timeout_seconds: int = 480
) -> dict[str, Any]:
    """Invoke one worker and retain parent-authoritative process evidence."""
    if timeout_seconds != 480:
        raise ValueError("G2.5 worker timeout must equal the frozen 480 seconds")
    if containment.controller._cells.get(containment.cell_id) is not containment:
        raise WorkerLifetimeError("worker containment is not an active exact cell cgroup")
    lease.assert_active()
    lifetime_guard = lease.inheritance_descriptor()
    if (
        lifetime_guard["owner_pid"] != os.getpid()
        or lifetime_guard["owner_start_ticks"] != read_process_start_ticks(os.getpid())
    ):
        raise WorkerLifetimeError("execution lease owner differs from worker supervisor")
    injected = sorted(key for key in GUARD_ENV_KEYS if key in os.environ)
    if injected:
        raise WorkerLifetimeError(
            f"reserved worker lifetime environment already exists: {injected}"
        )
    ready_read_fd, ready_write_fd = os.pipe2(os.O_CLOEXEC)
    go_read_fd, go_write_fd = os.pipe2(os.O_CLOEXEC)
    ack_read_fd, ack_write_fd = os.pipe2(os.O_CLOEXEC)
    environment = dict(os.environ)
    environment.update({
        "G25_EXPECTED_PARENT_PID": str(lifetime_guard["owner_pid"]),
        "G25_EXPECTED_PARENT_START_TICKS": str(lifetime_guard["owner_start_ticks"]),
        "G25_INHERITED_LEASE_FD": str(lifetime_guard["fd"]),
        "G25_INHERITED_LEASE_DEVICE": str(lifetime_guard["device"]),
        "G25_INHERITED_LEASE_INODE": str(lifetime_guard["inode"]),
        "G25_WORKER_READY_FD": str(ready_write_fd),
        "G25_WORKER_GO_FD": str(go_read_fd),
        "G25_WORKER_ACK_FD": str(ack_write_fd),
        "G25_EXPECTED_CGROUP_PATH": containment.relative_path,
        "G25_EXPECTED_CGROUP_MOUNTPOINT": str(containment.controller.mountpoint),
        "G25_EXPECTED_CGROUP_DEVICE": str(containment.device),
        "G25_EXPECTED_CGROUP_INODE": str(containment.inode),
    })
    started_unix_ns = time.time_ns()
    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            list(argv),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(
                lifetime_guard["fd"], ready_write_fd, go_read_fd, ack_write_fd,
            ),
            env=environment,
        )
    except BaseException:
        os.close(ready_read_fd)
        os.close(ready_write_fd)
        os.close(go_read_fd)
        os.close(go_write_fd)
        os.close(ack_read_fd)
        os.close(ack_write_fd)
        raise
    os.close(ready_write_fd)
    os.close(go_read_fd)
    os.close(ack_write_fd)
    try:
        lifetime_evidence = _await_worker_guard_ready(
            ready_read_fd, go_write_fd, ack_read_fd,
            process, lifetime_guard, containment,
        )
    finally:
        os.close(ready_read_fd)
        os.close(go_write_fd)
        os.close(ack_read_fd)
    previous_handlers: dict[int, Any] = {}

    def terminate_active_group(signum: int, _frame: Any) -> None:
        # Outer timeout is an emergency boundary.  Kill the detached worker
        # immediately so parent finalization can never release the lease while
        # GPU work remains alive.
        try:
            drain = containment.controller.emergency_kill(containment)
            _bind_cgroup_drain(containment, lifetime_evidence, drain.as_dict())
        except CgroupDrainError:
            raise
        raise WorkerSupervisorInterrupted(
            f"parent received {signal.Signals(signum).name}; active worker group killed"
        )

    for forwarded in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_handlers[int(forwarded)] = signal.getsignal(forwarded)
        signal.signal(forwarded, terminate_active_group)
    try:
        remaining = max(0.0, timeout_seconds - (time.perf_counter() - started))
        stdout, stderr = process.communicate(timeout=remaining)
        drain = containment.controller.finalize_normal_exit(containment)
        _bind_cgroup_drain(containment, lifetime_evidence, drain.as_dict())
        containment.controller.close_cell(containment)
        if drain.initial_populated != 0:
            raise WorkerLifetimeError(
                "worker leader exited while its cell cgroup retained descendants"
            )
        return {
            "supervisor_result": True,
            "worker_pid": process.pid,
            "argv": list(argv),
            "stdout": stdout,
            "stderr": stderr,
            "return_code": process.returncode,
            "timed_out": False,
            "wall_time_seconds": time.perf_counter() - started,
            "parent_started_unix_ns": started_unix_ns,
            "parent_finished_unix_ns": time.time_ns(),
            "termination_signal": None,
            "timeout_stage": "completed",
            "term_grace_seconds": 30,
            "lifetime_guard": lifetime_evidence,
        }
    except subprocess.TimeoutExpired as exc:
        drain = containment.controller.terminate_and_drain(
            containment, graceful=True
        )
        _bind_cgroup_drain(containment, lifetime_evidence, drain.as_dict())
        termination_signal = (
            "SIGKILL" if drain.cgroup_kill_written else "SIGTERM"
        )
        stdout, stderr = process.communicate()
        containment.controller.close_cell(containment)
        return {
            "supervisor_result": True,
            "worker_pid": process.pid,
            "argv": list(argv),
            "stdout": stdout,
            "stderr": stderr,
            "return_code": process.returncode,
            "timed_out": True,
            "wall_time_seconds": time.perf_counter() - started,
            "parent_started_unix_ns": started_unix_ns,
            "parent_finished_unix_ns": time.time_ns(),
            "termination_signal": termination_signal,
            "timeout_stage": (
                "cell_timeout_sigkill"
                if termination_signal == "SIGKILL"
                else "cell_timeout_sigterm"
            ),
            "term_grace_seconds": 30,
            "exception": f"TimeoutExpired: {exc}",
            "lifetime_guard": lifetime_evidence,
        }
    except WorkerSupervisorInterrupted:
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                containment.controller.emergency_kill(containment)
            except CgroupDrainError:
                raise
            process.communicate()
        if not containment.closed:
            if not containment.populated_zero_observed:
                drain = containment.controller.emergency_kill(containment)
                _bind_cgroup_drain(
                    containment, lifetime_evidence, drain.as_dict()
                )
            containment.controller.close_cell(containment)
        raise
    finally:
        for forwarded, previous in previous_handlers.items():
            signal.signal(forwarded, previous)


def invoke_worker_evidence_process(
    argv: Sequence[str], evidence_path: Path, *, lease: ExecutionLease,
    containment: CellCgroup, timeout_seconds: int = 480,
) -> dict[str, Any]:
    """Supervise a worker whose protocol is an atomic evidence file, not stdout."""
    if evidence_path.exists() or evidence_path.is_symlink():
        raise FileExistsError("worker evidence path must be fresh")
    result = invoke_worker_process(
        argv, lease=lease, containment=containment,
        timeout_seconds=timeout_seconds,
    )
    payload: dict[str, Any] | None = None
    evidence_sha256: str | None = None
    if evidence_path.is_file() and not evidence_path.is_symlink():
        evidence_sha256 = sha256_file(evidence_path)
        try:
            candidate = json.loads(evidence_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict):
            payload = candidate
    result["evidence_payload"] = payload
    result["evidence_file_sha256"] = evidence_sha256
    result["stdout_sha256"] = hashlib.sha256(
        str(result.get("stdout") or "").encode("utf-8")
    ).hexdigest()
    result["stderr_sha256"] = hashlib.sha256(
        str(result.get("stderr") or "").encode("utf-8")
    ).hexdigest()
    return result


def normalize_worker_process_result(
    result: subprocess.CompletedProcess[str] | Mapping[str, Any],
    *,
    selection: Mapping[str, Any],
    ceiling: int,
) -> dict[str, Any]:
    """Convert subprocess output or supervisor timeout into classifiable evidence."""
    if isinstance(result, subprocess.CompletedProcess):
        try:
            value = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        value.setdefault("return_code", result.returncode)
        value.setdefault("timed_out", False)
        value.setdefault("exception", result.stderr or None)
        return value
    if result.get("supervisor_result") is True:
        parsed = result.get("evidence_payload")
        if parsed is None and "evidence_payload" not in result:
            # Compatibility for CPU-only S2 tests. The real S4 path always
            # supplies evidence_payload and never treats stdout as protocol.
            try:
                parsed = json.loads(result.get("stdout") or "{}")
            except (TypeError, json.JSONDecodeError):
                parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        # These fields describe the OS process and therefore must never be
        # accepted from worker-controlled JSON.
        parsed["return_code"] = int(result["return_code"])
        parsed["timed_out"] = bool(result["timed_out"])
        parsed["wall_time_seconds"] = float(result["wall_time_seconds"])
        if not parsed.get("exception") and result.get("stderr"):
            parsed["exception"] = result["stderr"]
        parsed["parent_process"] = {
            "worker_pid": int(result["worker_pid"]),
            "started_unix_ns": int(result["parent_started_unix_ns"]),
            "finished_unix_ns": int(result["parent_finished_unix_ns"]),
            "termination_signal": result.get("termination_signal"),
            "timeout_stage": result.get("timeout_stage"),
            "term_grace_seconds": int(result["term_grace_seconds"]),
            "worker_argv_sha256": canonical_hash(result["argv"]),
            "io_manifest_sha256": result.get("io_manifest_sha256"),
            "evidence_file_sha256": result.get("evidence_file_sha256"),
            "stdout_sha256": result.get("stdout_sha256"),
            "stderr_sha256": result.get("stderr_sha256"),
            "lifetime_guard": result.get("lifetime_guard"),
        }
        if not parsed["timed_out"]:
            return parsed
        result = {**result, "exception": result.get("exception")}
    output_ids: list[int] = []
    return {
        "schema_version": "g25-worker-evidence-v1",
        "prompt_hash": selection["prompt_hash"],
        "input_token_ids": [],
        "output_token_ids": output_ids,
        "input_token_count": 0,
        "output_token_count": 0,
        "text": "",
        "stop_reason": None,
        "output_hash": canonical_hash(output_ids),
        "return_code": int(result.get("return_code", -int(signal.SIGTERM))),
        "generation_seconds": 0.0,
        "wall_time_seconds": float(result.get("wall_time_seconds", 480.0)),
        "timed_out": bool(result.get("timed_out")),
        "exception": result.get("exception"),
        "parser_outcome": "not_run",
        "routing_capture_enabled": False,
        "profiler_enabled": False,
        "effective_generation_config": resolve_task_profile(
            load_profile_map(), "T1", ceiling=ceiling
        ),
        "effective_generation_config_sha256": canonical_hash(resolve_task_profile(
            load_profile_map(), "T1", ceiling=ceiling
        )),
        "execution_identity": {
            "mode": "supervisor_timeout",
            "runtime": {},
            "device": {},
        },
        "parent_process": {
            "worker_pid": result.get("worker_pid"),
            "started_unix_ns": result.get("parent_started_unix_ns"),
            "finished_unix_ns": result.get("parent_finished_unix_ns"),
            "termination_signal": result.get("termination_signal", "SIGTERM"),
            "timeout_stage": result.get("timeout_stage"),
            "term_grace_seconds": int(result.get("term_grace_seconds", 30)),
            "worker_argv_sha256": canonical_hash(result.get("argv", [])),
            "io_manifest_sha256": result.get("io_manifest_sha256"),
            "lifetime_guard": result.get("lifetime_guard"),
        },
    }


def run_qualification_matrix(
    contract: Mapping[str, Any],
    evidence_provider: Callable[[Mapping[str, Any], int], Mapping[str, Any]],
) -> list[tuple[dict[str, Any], int, dict[str, Any]]]:
    """Execute the frozen exact 12-cell matrix without early dispatch stops."""
    selections = _manifest_selections(contract)
    results = []
    for ceiling in qualification_ceilings(contract):
        for instance in qualification_instances(contract):
            selection = selections[instance]
            evidence = dict(evidence_provider(selection, ceiling))
            results.append((selection, ceiling, evidence))
    if len(results) != contract["common_ceiling_rule"]["expected_cells"]:
        raise RuntimeError("qualification runner did not produce the exact matrix")
    return results


def subprocess_evidence_provider(
    argv_factory: Callable[[Mapping[str, Any], int], Sequence[str]],
    lease: ExecutionLease,
    containment_provider: Callable[[Mapping[str, Any], int], CellCgroup],
) -> Callable[[Mapping[str, Any], int], dict[str, Any]]:
    """Bind every real worker cell to the frozen parent-process timeout."""
    def provide(selection: Mapping[str, Any], ceiling: int) -> dict[str, Any]:
        result = invoke_worker_process(
            argv_factory(selection, ceiling), lease=lease,
            containment=containment_provider(selection, ceiling),
            timeout_seconds=480,
        )
        return normalize_worker_process_result(
            result, selection=selection, ceiling=ceiling
        )
    return provide


def _manifest_selections(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    path = PACKAGE_ROOT / contract["frozen_inputs"]["candidate_manifest_path"]
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = {
        row["instance_id"]: row
        for row in rows
        if row.get("suite_id") == contract["candidate_scope"]["suite"]
        and row.get("task_id") == "T1"
    }
    if set(selected) != set(qualification_instances(contract)):
        raise ValueError("candidate manifest differs from frozen G2.5 scope")
    return selected


def build_worker_descriptor(
    *,
    session_id: str,
    instance_id: str,
    ceiling: int,
    model_snapshot_inventory_sha256: str,
    device_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one immutable, schema-checked worker input from frozen sources."""
    from scripts.projectctl import load_suite

    session_id = _safe_session_id(session_id)
    contract = load_contract()
    selections = _manifest_selections(contract)
    if instance_id not in selections:
        raise ValueError("worker instance is outside the frozen qualification matrix")
    suite, _source = load_suite(contract["candidate_scope"]["suite"])
    suite_rows = {
        row["sample_id"]: row for row in suite["samples"]
        if row.get("task_id") == "T1"
    }
    if instance_id not in suite_rows:
        raise ValueError("frozen suite does not embed the requested worker sample")
    selection = dict(selections[instance_id])
    suite_row = suite_rows[instance_id]
    sample = dict(suite_row["source_sample"])
    config = resolve_task_profile(load_profile_map(), "T1", ceiling=ceiling)
    profile_sha256 = sha256_file(PROFILE_MAP_PATH)
    config_sha256 = canonical_hash(config)
    descriptor = {
        "schema_version": "g25-worker-descriptor-v1",
        "session_id": session_id,
        "cell_id": cell_identity(
            session_id, instance_id, selection["sample_id"], ceiling,
            profile_sha256, config_sha256,
        ),
        "instance_id": instance_id,
        "sample_id": selection["sample_id"],
        "logical_pass": "P0",
        "ceiling": ceiling,
        "selection": selection,
        "sample": sample,
        "generation_config": config,
        "generation_profile_sha256": profile_sha256,
        "generation_config_sha256": config_sha256,
        "prompt_hash": selection["prompt_hash"],
        "model_snapshot_inventory_sha256": model_snapshot_inventory_sha256,
        "device_identity": {
            key: device_identity[key]
            for key in ("kind", "name", "uuid", "pci_bus_id")
        },
    }
    validate_schema("g25_worker_descriptor.schema.json", descriptor)
    return descriptor


def build_worker_argv(
    descriptor_path: Path, evidence_path: Path, model_snapshot: Path,
    *, package_snapshot_root: Path | None = None,
    python_executable: Path | None = None,
) -> list[str]:
    """Return the only allowed per-cell worker argv shape."""
    paths = [descriptor_path, evidence_path, model_snapshot]
    if any(not path.is_absolute() for path in paths):
        raise ValueError("worker argv paths must be absolute")
    worker_root = package_snapshot_root or PACKAGE_ROOT
    worker_script = worker_root / "scripts/g25_worker.py"
    if package_snapshot_root is not None and (
        not package_snapshot_root.is_absolute() or not worker_script.is_file()
    ):
        raise ValueError("session package snapshot lacks the frozen G2.5 worker")
    from scheduler.g25_runtime_closure import build_attested_python_argv

    return build_attested_python_argv(
        "worker",
        [
            "--cell-descriptor", str(descriptor_path),
            "--evidence-out", str(evidence_path),
            "--model-snapshot", str(model_snapshot),
        ],
        package_root=worker_root,
        python_executable=python_executable or Path(sys.executable),
    )


def _assert_cell_ceiling_invariants(row: Mapping[str, Any]) -> None:
    """Reject a cell whose replay or QUALIFIED claim escapes its frozen ceiling."""
    replay = row.get("parent_output_replay")
    ceiling = row.get("ceiling")
    count = row.get("output_token_count")
    if not isinstance(replay, Mapping) or ceiling not in {256, 384, 512}:
        raise ValueError("qualification cell lacks a bounded parent replay")
    within = (
        isinstance(count, int)
        and not isinstance(count, bool)
        and 0 < count <= ceiling
    )
    if (
        row.get("parent_output_replay_sha256") != canonical_hash(replay)
        or replay.get("replay_sha256")
        != canonical_hash({
            key: value for key, value in replay.items() if key != "replay_sha256"
        })
        or replay.get("frozen_ceiling") != ceiling
        or replay.get("output_token_count") != count
        or replay.get("within_frozen_ceiling") is not within
    ):
        raise ValueError("qualification cell ceiling replay is inconsistent")
    if row.get("qualification_class") == "QUALIFIED":
        common_invalid = (
            not within
            or replay.get("derived_stop_reason") != "eos_token"
            or replay.get("legal_stop") is not True
            or replay.get("eos_is_unique_terminal") is not True
            or replay.get("eos_occurrence_count") != 1
            or replay.get("terminal_token_id") != replay.get("frozen_eos_token_id")
        )
        real_invalid = row.get("synthetic") is False and (
            replay.get("worker_text_matches_parent_decode") is not True
            or replay.get("parser_validity") is not True
            or replay.get("worker_claims_match") is not True
            or replay.get("replay_error") is not None
        )
        if common_invalid or real_invalid:
            raise ValueError("QUALIFIED cell contradicts parent-authoritative replay")


def build_ledger(
    session_id: str,
    cells: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any] | None = None,
    profile_sha256: str | None = None,
    synthetic_session: bool = True,
) -> dict[str, Any]:
    contract = dict(contract or load_contract())
    profile_sha256 = profile_sha256 or sha256_file(PROFILE_MAP_PATH)
    instances = qualification_instances(contract)
    ceilings = qualification_ceilings(contract)
    expected_pairs = {(instance, ceiling) for ceiling in ceilings for instance in instances}
    seen: set[tuple[str, int]] = set()
    normalized: list[dict[str, Any]] = []
    for value in cells:
        row = dict(value)
        validate_schema(CELL_SCHEMA, row)
        _assert_cell_ceiling_invariants(row)
        pair = (row["instance_id"], row["ceiling"])
        if pair in seen:
            raise ValueError(f"duplicate qualification cell: {pair}")
        seen.add(pair)
        selection = _manifest_selections(contract).get(row["instance_id"])
        if (
            selection is None
            or row["sample_id"] != selection["sample_id"]
            or row["prompt_hash"] != selection["prompt_hash"]
            or row["task_id"] != selection["task_id"]
        ):
            raise ValueError("qualification cell differs from frozen sample identity")
        expected_config_sha256 = canonical_hash(resolve_task_profile(
            load_profile_map(), "T1", ceiling=row["ceiling"]
        ))
        expected_cell_id = cell_identity(
            session_id,
            row["instance_id"],
            selection["sample_id"],
            row["ceiling"],
            profile_sha256,
            expected_config_sha256,
        )
        if (
            row["session_id"] != session_id
            or row["generation_profile_sha256"] != profile_sha256
            or row["generation_config_sha256"] != expected_config_sha256
            or row["cell_id"] != expected_cell_id
            or row["evidence_role"] != EVIDENCE_ROLE
            or row["formal_c1_evidence"] is not False
            or row["synthetic"] is not synthetic_session
            or row["routing_capture_enabled"] is not False
            or row["profiler_enabled"] is not False
        ):
            raise ValueError("qualification cell identity or isolation drift")
        normalized.append(row)
    if seen != expected_pairs:
        missing = sorted(expected_pairs - seen)
        extra = sorted(seen - expected_pairs)
        raise ValueError(f"qualification ledger cell set mismatch; missing={missing}, extra={extra}")
    normalized.sort(key=lambda row: (row["ceiling"], row["instance_id"]))
    ledger = {
        "schema_version": "g25-qualification-ledger-v1",
        "session_id": session_id,
        "evidence_role": EVIDENCE_ROLE,
        "formal_c1_evidence": False,
        "synthetic": synthetic_session,
        "contract_sha256": sha256_file(CONTRACT_PATH),
        "profile_map_sha256": profile_sha256,
        "candidate_manifest_sha256": contract["frozen_inputs"][
            "candidate_manifest_sha256"
        ],
        "execution_identity_set_sha256": canonical_hash([
            row["execution_identity_sha256"] for row in normalized
        ]),
        "expected_cell_count": 12,
        "complete_cell_count": len(normalized),
        "ceilings": ceilings,
        "instances": instances,
        "cells": normalized,
        "cell_set_sha256": canonical_hash(normalized),
    }
    validate_schema(LEDGER_SCHEMA, ledger)
    return ledger


def select_common_ceiling(ledger: Mapping[str, Any]) -> dict[str, Any]:
    validate_schema(LEDGER_SCHEMA, ledger)
    if ledger.get("cell_set_sha256") != canonical_hash(ledger["cells"]):
        raise ValueError("qualification ledger cell-set hash mismatch")
    rows: dict[str, dict[str, int]] = {}
    selected: int | None = None
    for ceiling in ledger["ceilings"]:
        relevant = [row for row in ledger["cells"] if row["ceiling"] == ceiling]
        counts = {name: 0 for name in CLASSIFICATIONS}
        for row in relevant:
            _assert_cell_ceiling_invariants(row)
            counts[row["qualification_class"]] += 1
        rows[str(ceiling)] = counts
        if selected is None and len(relevant) == len(ledger["instances"]) and all(
            row["qualification_class"] == "QUALIFIED" for row in relevant
        ):
            selected = ceiling
    verdict = {
        "schema_version": "g25-qualification-verdict-v1",
        "session_id": ledger["session_id"],
        "status": "QUALIFIED" if selected is not None
        else "NO_COMMON_CEILING_WITHIN_CONTRACT",
        "selected_common_ceiling": selected,
        "selector_revision": SELECTOR_REVISION,
        "selector_input_sha256": canonical_hash(ledger),
        "row_summaries": rows,
        "formal_gate_pass": False,
        "gpu_pilot_authorized": False,
    }
    validate_schema(VERDICT_SCHEMA, verdict)
    return verdict


def _synthetic_class(scenario: str, instance: str, ceiling: int) -> str:
    index = int(instance.rsplit("-", 1)[1])
    if scenario == "all-256":
        return "QUALIFIED"
    if scenario == "common-384":
        return "TRUNCATED" if ceiling == 256 and index == 1 else "QUALIFIED"
    if scenario == "common-512":
        return "TRUNCATED" if ceiling < 512 and index == 1 else "QUALIFIED"
    if scenario == "no-common":
        return "TRUNCATED" if index == 1 else "QUALIFIED"
    if scenario == "timeout":
        return "TIMEOUT" if index == 2 else "QUALIFIED"
    if scenario == "runtime-failure":
        return "RUNTIME_FAILURE" if index == 2 else "QUALIFIED"
    if scenario == "invalid-evidence":
        return "INVALID_EVIDENCE" if index == 2 else "QUALIFIED"
    raise ValueError(f"unknown synthetic scenario: {scenario}")


def synthetic_worker_evidence(
    selection: Mapping[str, Any], ceiling: int, classification: str
) -> dict[str, Any]:
    output_ids = [ceiling % 97 + 1, 0]
    value: dict[str, Any] = {
        "schema_version": "g25-worker-evidence-v1",
        "prompt_hash": selection["prompt_hash"],
        "input_token_ids": [11, 12, 13],
        "output_token_ids": output_ids,
        "input_token_count": 3,
        "output_token_count": len(output_ids),
        "text": "#### 42",
        "stop_reason": "eos_token",
        "output_hash": canonical_hash(output_ids),
        "return_code": 0,
        "generation_seconds": 0.01,
        "wall_time_seconds": 0.02,
        "timed_out": False,
        "exception": None,
        "parser_outcome": "parseable",
        "routing_capture_enabled": False,
        "profiler_enabled": False,
        "effective_generation_config": resolve_task_profile(
            load_profile_map(), "T1", ceiling=ceiling
        ),
        "effective_generation_config_sha256": canonical_hash(resolve_task_profile(
            load_profile_map(), "T1", ceiling=ceiling
        )),
        "execution_identity": {
            "mode": "synthetic_governance",
            "model_loaded": False,
            "runtime": {
                "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            },
            "device": {"kind": "synthetic_cpu", "name": None, "uuid": None},
        },
    }
    if classification == "TRUNCATED":
        value["stop_reason"] = "max_new_tokens"
        value["output_token_ids"] = list(range(1, ceiling + 1))
        value["output_token_count"] = ceiling
        value["output_hash"] = canonical_hash(value["output_token_ids"])
        value["text"] = "synthetic truncated output"
    elif classification == "TIMEOUT":
        value["timed_out"] = True
        value["return_code"] = -15
        value["parser_outcome"] = "not_run"
    elif classification == "RUNTIME_FAILURE":
        value["return_code"] = 1
        value["exception"] = "SyntheticRuntimeError"
        value["parser_outcome"] = "not_run"
    elif classification == "INVALID_EVIDENCE":
        value.pop("output_hash")
    return value


def _write_json(path: Path, value: Any) -> None:
    atomic_json(path, value)


def write_qualification_session(
    output_root: Path,
    session_id: str,
    evidence_provider: Callable[[Mapping[str, Any], int], Mapping[str, Any]],
    *,
    synthetic: bool,
    gpu_used: bool,
    decode_output_ids: Callable[[list[int]], str] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    session_id = _safe_session_id(session_id)
    root = output_root.resolve() / session_id
    if root.exists():
        raise FileExistsError("qualification session path already exists")
    (root / "raw").mkdir(parents=True)
    (root / "cells").mkdir()
    contract = load_contract()
    profile_sha256 = sha256_file(PROFILE_MAP_PATH)
    cells: list[dict[str, Any]] = []
    selections = _manifest_selections(contract)
    for ceiling in qualification_ceilings(contract):
        for instance in qualification_instances(contract):
            selection = selections[instance]
            evidence = dict(evidence_provider(selection, ceiling))
            generation_config = resolve_task_profile(
                load_profile_map(), "T1", ceiling=ceiling
            )
            raw_name = f"{selection['instance_id']}__{ceiling}.json"
            raw_path = root / "raw" / raw_name
            _write_json(raw_path, evidence)
            descriptor = {
                "path": f"raw/{raw_name}",
                "bytes": raw_path.stat().st_size,
                "sha256": sha256_file(raw_path),
            }
            row = build_cell_row(
                session_id=session_id,
                selection=selection,
                ceiling=ceiling,
                profile_sha256=profile_sha256,
                generation_config_sha256=canonical_hash(generation_config),
                evidence=evidence,
                evidence_descriptor=descriptor,
                synthetic_session=synthetic,
                decode_output_ids=decode_output_ids,
            )
            _write_json(root / "cells" / f"{row['cell_id']}.json", row)
            cells.append(row)
    ledger = build_ledger(
        session_id,
        cells,
        contract=contract,
        profile_sha256=profile_sha256,
        synthetic_session=synthetic,
    )
    verdict = select_common_ceiling(ledger)
    _write_json(root / "ledger.json", ledger)
    _write_json(root / "verdict.json", verdict)
    session = {
        "schema_version": "g25-qualification-session-v1",
        "session_id": session_id,
        "status": "synthetic_governance_complete" if synthetic
        else "qualification_execution_complete",
        "evidence_role": EVIDENCE_ROLE,
        "synthetic": synthetic,
        "gpu_used": gpu_used,
        "formal_c1_evidence": False,
        "contract_sha256": sha256_file(CONTRACT_PATH),
        "profile_map_sha256": profile_sha256,
        "candidate_manifest_sha256": contract["frozen_inputs"][
            "candidate_manifest_sha256"
        ],
        "execution_identity_set_sha256": ledger["execution_identity_set_sha256"],
        "expected_cell_count": 12,
        "completed_cell_count": 12,
        "artifacts": {
            "ledger": sha256_file(root / "ledger.json"),
            "verdict": sha256_file(root / "verdict.json"),
        },
        "selected_common_ceiling": verdict["selected_common_ceiling"],
        "gpu_pilot_authorized": False,
    }
    validate_schema(SESSION_SCHEMA, session)
    _write_json(root / "session.json", session)
    audit = audit_session(root, decode_output_ids=decode_output_ids)
    _write_json(root / "audit.json", audit)
    return root, verdict, audit


def write_synthetic_session(
    output_root: Path, session_id: str, scenario: str
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    def provider(selection: Mapping[str, Any], ceiling: int) -> dict[str, Any]:
        return synthetic_worker_evidence(
            selection,
            ceiling,
            _synthetic_class(scenario, selection["instance_id"], ceiling),
        )
    return write_qualification_session(
        output_root, session_id, provider, synthetic=True, gpu_used=False
    )


def write_worker_qualification_session(
    output_root: Path,
    session_id: str,
    argv_factory: Callable[[Mapping[str, Any], int], Sequence[str]],
    lease: ExecutionLease,
    containment_provider: Callable[[Mapping[str, Any], int], CellCgroup],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Persist a real 12-cell session; intentionally not exposed by the CLI."""
    return write_qualification_session(
        output_root,
        session_id,
        subprocess_evidence_provider(argv_factory, lease, containment_provider),
        synthetic=False,
        gpu_used=True,
    )


def _safe_artifact(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("qualification artifact path is unsafe")
    resolved = (root / candidate).resolve(strict=True)
    resolved.relative_to(root.resolve())
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError("qualification artifact is not a regular file")
    return resolved


def audit_session(
    root: Path,
    *,
    decode_output_ids: Callable[[list[int]], str] | None = None,
) -> dict[str, Any]:
    findings: list[str] = []
    try:
        ledger_path = root / "ledger.json"
        verdict_path = root / "verdict.json"
        session_path = root / "session.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        session = json.loads(session_path.read_text(encoding="utf-8"))
        validate_schema(SESSION_SCHEMA, session)
        if (
            session["session_id"] != root.name
            or session["contract_sha256"] != sha256_file(CONTRACT_PATH)
            or session["profile_map_sha256"] != sha256_file(PROFILE_MAP_PATH)
            or session["execution_identity_set_sha256"]
            != ledger["execution_identity_set_sha256"]
            or session["artifacts"]["ledger"] != sha256_file(ledger_path)
            or session["artifacts"]["verdict"] != sha256_file(verdict_path)
            or session["selected_common_ceiling"] != verdict["selected_common_ceiling"]
        ):
            findings.append("session identity or artifact binding drift")
        rebuilt = build_ledger(
            ledger["session_id"],
            ledger["cells"],
            contract=load_contract(),
            profile_sha256=sha256_file(PROFILE_MAP_PATH),
            synthetic_session=session["synthetic"],
        )
        if rebuilt != ledger:
            findings.append("ledger is not canonical or reproducible")
        replayed = select_common_ceiling(ledger)
        if replayed != verdict:
            findings.append("selector replay differs from stored verdict")
        expected_cell_files: set[str] = set()
        expected_raw_files: set[str] = set()
        selections = _manifest_selections(load_contract())
        for row in ledger["cells"]:
            cell_name = f"{row['cell_id']}.json"
            expected_cell_files.add(cell_name)
            cell_path = _safe_artifact(root, f"cells/{cell_name}")
            stored_cell = json.loads(cell_path.read_text(encoding="utf-8"))
            if stored_cell != row:
                findings.append(f"cell artifact differs from ledger: {row['cell_id']}")
            for descriptor in row["evidence_paths"]:
                expected_raw_files.add(Path(descriptor["path"]).name)
                path = _safe_artifact(root, descriptor["path"])
                if path.stat().st_size != descriptor["bytes"]:
                    findings.append(f"artifact byte count drift: {descriptor['path']}")
                if sha256_file(path) != descriptor["sha256"]:
                    findings.append(f"artifact checksum drift: {descriptor['path']}")
                evidence = json.loads(path.read_text(encoding="utf-8"))
                classification, _reason = classify_worker_evidence(
                    evidence,
                    expected_prompt_hash=row["prompt_hash"],
                    ceiling=row["ceiling"],
                    synthetic_session=session["synthetic"],
                    decode_output_ids=decode_output_ids,
                )
                if classification != row["qualification_class"]:
                    findings.append(f"classification replay drift: {row['cell_id']}")
                if canonical_hash(evidence) != row["worker_evidence_sha256"]:
                    findings.append(f"worker evidence binding drift: {row['cell_id']}")
                regenerated = build_cell_row(
                    session_id=ledger["session_id"],
                    selection=selections[row["instance_id"]],
                    ceiling=row["ceiling"],
                    profile_sha256=sha256_file(PROFILE_MAP_PATH),
                    generation_config_sha256=canonical_hash(resolve_task_profile(
                        load_profile_map(), "T1", ceiling=row["ceiling"]
                    )),
                    evidence=evidence,
                    evidence_descriptor=descriptor,
                    synthetic_session=session["synthetic"],
                    decode_output_ids=decode_output_ids,
                )
                if regenerated != row:
                    findings.append(f"canonical cell replay drift: {row['cell_id']}")
        actual_cell_files = {
            path.name for path in (root / "cells").iterdir()
            if path.is_file() and not path.is_symlink()
        }
        if actual_cell_files != expected_cell_files:
            findings.append("cell artifact inventory differs from exact ledger matrix")
        actual_raw_files = {
            path.name for path in (root / "raw").iterdir()
            if path.is_file() and not path.is_symlink()
        }
        if actual_raw_files != expected_raw_files:
            findings.append("raw artifact inventory differs from exact ledger matrix")
        ledger_hash = sha256_file(ledger_path)
        verdict_hash = canonical_hash(verdict)
        replayed_hash = canonical_hash(replayed)
    except Exception as exc:
        findings.append(f"audit exception: {type(exc).__name__}: {exc}")
        ledger_hash = "0" * 64
        verdict_hash = "0" * 64
        replayed_hash = "0" * 64
    audit = {
        "schema_version": "g25-qualification-audit-v1",
        "session_id": root.name,
        "status": "complete" if not findings else "failed",
        "finding_count": len(findings),
        "findings": findings,
        "ledger_sha256": ledger_hash,
        "verdict_sha256": verdict_hash,
        "replayed_verdict_sha256": replayed_hash,
        "selector_deterministic": not findings,
        "formal_c1_loader_eligible": False,
        "gpu_used": session.get("gpu_used", False) if "session" in locals() else False,
    }
    validate_schema(AUDIT_SCHEMA, audit)
    return audit


def replay_session(root: Path) -> dict[str, Any]:
    audit = audit_session(root)
    if audit["status"] != "complete":
        raise ValueError("qualification session audit failed before replay")
    ledger = json.loads((root / "ledger.json").read_text(encoding="utf-8"))
    return select_common_ceiling(ledger)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    synthetic = sub.add_parser("synthetic-run")
    synthetic.add_argument("--output-root", required=True, type=Path)
    synthetic.add_argument("--session", required=True)
    synthetic.add_argument(
        "--scenario",
        required=True,
        choices=(
            "all-256", "common-384", "common-512", "no-common",
            "timeout", "runtime-failure", "invalid-evidence",
        ),
    )
    for action in ("replay", "audit"):
        command = sub.add_parser(action)
        command.add_argument("--session-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "synthetic-run":
        root, verdict, audit = write_synthetic_session(
            args.output_root, args.session, args.scenario
        )
        print(json.dumps({
            "status": "complete",
            "session_root": str(root),
            "verdict": verdict,
            "audit": audit,
        }, indent=2, sort_keys=True))
        return 0
    if args.action == "replay":
        print(json.dumps(replay_session(args.session_root), indent=2, sort_keys=True))
        return 0
    audit = audit_session(args.session_root)
    print(json.dumps(audit, indent=2, sort_keys=True))
    return 0 if audit["status"] == "complete" else 20


if __name__ == "__main__":
    raise SystemExit(main())
