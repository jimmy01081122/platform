#!/usr/bin/env python3
"""Pure C1 Quality Contract v2 classification, binding, and comparison helpers."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from scripts.c1_evaluator import TASK_EVALUATORS, evaluate_frozen_sample

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
KNOWN_TASKS = frozenset({"T0", "T1", "T2"})
FORMAL_GENERATION_FIELDS = (
    "input_token_ids",
    "output_token_ids",
    "stop_reason",
    "output_hash",
    "execution_alignment_key",
)
QUALITY_COMPARISON_FIELDS = (
    "evaluator",
    "parser_outcome",
    "task_outcome",
    "quality_binding_sha256",
)

EXPECTED_QUALITY_GATES = {
    "QG-1": {"scope": "execution_integrity", "disposition": "blocking"},
    "QG-2": {
        "scope": "cross_pass_exact_generation_and_classification",
        "disposition": "blocking",
        "baseline_pass": "P0",
    },
    "QG-3": {"scope": "T0_task_correctness", "disposition": "blocking"},
    "QG-4": {
        "scope": "T1_T2_task_correctness",
        "disposition": "recorded_nonblocking",
    },
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


def reference_hash(sample: Mapping[str, Any]) -> str:
    """Bind the task identity and exact frozen reference, including null/missing."""
    return canonical_hash({
        "task_id": sample.get("task_id"),
        "reference": sample.get("reference"),
    })


def reference_bundle_hash(samples: Sequence[Mapping[str, Any]]) -> str:
    rows = []
    for sample in samples:
        if isinstance(sample, Mapping):
            source = sample.get("source_sample") or sample
            source = source if isinstance(source, Mapping) else {}
            sample_id = sample.get("source_sample_id", sample.get("sample_id"))
        else:
            source = {}
            sample_id = sample
        rows.append({
            "sample_id": sample_id,
            "task_id": source.get("task_id"),
            "reference_sha256": reference_hash(source),
        })
    return canonical_hash(sorted(
        rows,
        key=lambda row: (
            str(row["sample_id"]), str(row["task_id"]), row["reference_sha256"]
        ),
    ))


def validate_contract_definition(contract: Mapping[str, Any]) -> None:
    """Enforce the prospective governance boundary before any source is bound."""
    required_strings = ("contract_revision", "evaluator_revision")
    if contract.get("schema_version") != "c1-quality-contract-v2":
        raise ValueError("unsupported quality contract schema")
    if any(not isinstance(contract.get(name), str) or not contract[name]
           for name in required_strings):
        raise ValueError("quality contract revisions must be non-empty strings")
    if contract.get("evaluator_source_path") != "scripts/c1_evaluator.py":
        raise ValueError("quality contract evaluator source is not the replayable evaluator")
    if contract.get("quality_engine_path") != "scripts/c1_quality.py":
        raise ValueError("quality contract engine source is invalid")
    if contract.get("prospective_only") is not True:
        raise ValueError("quality contract must be prospective-only")
    if (contract.get("historical_decisions") or {}).get("G3-R3") != "PERMANENT_FAIL":
        raise ValueError("G3-R3 historical failure boundary is not preserved")
    if contract.get("legal_stop_reasons") != ["eos_token"]:
        raise ValueError("quality contract legal stop reasons drifted")
    if contract.get("unknown_stop_reason_policy") != "blocking":
        raise ValueError("unknown stop reasons must remain blocking")
    if contract.get("unknown_task_policy") != "blocking":
        raise ValueError("unknown tasks must remain blocking")
    if contract.get("quality_gates") != EXPECTED_QUALITY_GATES:
        raise ValueError("quality gate definitions drifted")
    task_contracts = contract.get("task_contracts")
    if not isinstance(task_contracts, Mapping) or set(task_contracts) != set(KNOWN_TASKS):
        raise ValueError("quality task contracts must exactly cover T0/T1/T2")
    expected_gate = {"T0": "QG-3", "T1": "QG-4", "T2": "QG-4"}
    for task_id, evaluator in TASK_EVALUATORS.items():
        if task_contracts.get(task_id) != {
            "evaluator": evaluator,
            "correctness_gate": expected_gate[task_id],
        }:
            raise ValueError(f"quality task contract drifted: {task_id}")


def build_contract_binding(
    contract: Mapping[str, Any],
    *,
    path: str,
    source_sha256: str,
    evaluator_source_sha256: str,
    quality_engine_sha256: str,
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    validate_contract_definition(contract)
    hashes = {
        "quality contract": source_sha256,
        "evaluator source": evaluator_source_sha256,
        "quality engine": quality_engine_sha256,
    }
    if any(not HASH_RE.fullmatch(value) for value in hashes.values()):
        raise ValueError("quality contract source hash is invalid")
    evaluator_path = contract.get("evaluator_source_path")
    quality_engine_path = contract.get("quality_engine_path")
    if not isinstance(evaluator_path, str) or not evaluator_path:
        raise ValueError("quality contract evaluator source path is invalid")
    if not isinstance(quality_engine_path, str) or not quality_engine_path:
        raise ValueError("quality contract quality engine path is invalid")
    return {
        "path": path,
        "sha256": source_sha256,
        "contract_revision": str(contract["contract_revision"]),
        "evaluator_revision": str(contract["evaluator_revision"]),
        "evaluator_source_path": evaluator_path,
        "evaluator_sha256": evaluator_source_sha256,
        "quality_engine_path": quality_engine_path,
        "quality_engine_sha256": quality_engine_sha256,
        "reference_bundle_sha256": reference_bundle_hash(samples),
    }


def validate_contract_binding(
    binding: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    path: str,
    source_sha256: str,
    evaluator_source_sha256: str,
    quality_engine_sha256: str,
    samples: Sequence[Mapping[str, Any]],
) -> None:
    expected = build_contract_binding(
        contract,
        path=path,
        source_sha256=source_sha256,
        evaluator_source_sha256=evaluator_source_sha256,
        quality_engine_sha256=quality_engine_sha256,
        samples=samples,
    )
    if dict(binding) != expected:
        raise ValueError("quality contract binding differs from verified source")


def task_classification(
    sample: Mapping[str, Any],
    quality: Mapping[str, Any],
    contract: Mapping[str, Any],
    text: Any,
) -> dict[str, str]:
    task_id = sample.get("task_id")
    if task_id not in KNOWN_TASKS:
        return {
            "task_class": "unknown",
            "evaluator": "unknown",
            "parser_outcome": "unknown",
            "task_outcome": "unknown",
            "classification_error": "unknown_task",
        }
    validate_contract_definition(contract)
    task_contract = (contract.get("task_contracts") or {}).get(task_id)
    expected_evaluator = (
        task_contract.get("evaluator")
        if isinstance(task_contract, Mapping)
        else None
    )
    authoritative = evaluate_frozen_sample(text, sample)
    evaluator = authoritative["evaluator"]
    validity = authoritative["validity"]
    correctness = authoritative["correctness"]
    native_validity = quality.get("validity", quality.get("status") == "pass")
    native_correctness = quality.get("correctness")
    if (
        not isinstance(expected_evaluator, str)
        or evaluator != expected_evaluator
        or quality.get("evaluator") != evaluator
        or native_validity is not validity
        or native_correctness is not correctness
        or not isinstance(validity, bool)
        or (validity and not isinstance(correctness, bool))
    ):
        return {
            "task_class": task_id,
            "evaluator": evaluator,
            "parser_outcome": "evaluator_error",
            "task_outcome": "unknown",
            "classification_error": "evaluator_error",
        }
    if not validity:
        return {
            "task_class": task_id,
            "evaluator": evaluator,
            "parser_outcome": "unparseable",
            "task_outcome": "unknown",
            "classification_error": "no_parse",
        }
    return {
        "task_class": task_id,
        "evaluator": evaluator,
        "parser_outcome": "parseable",
        "task_outcome": "correct" if correctness else "incorrect",
        "classification_error": "none",
    }


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(item) for item in value)
    return True


def _expert_ids_legal(records: Sequence[Mapping[str, Any]]) -> bool | None:
    expert_lists = [
        row["selected_experts"] for row in records if "selected_experts" in row
    ]
    if not expert_lists:
        return None
    return all(
        isinstance(experts, list)
        and len(experts) == 8
        and len(set(experts)) == 8
        and all(
            isinstance(expert, int)
            and not isinstance(expert, bool)
            and 0 <= expert < 32
            for expert in experts
        )
        for experts in expert_lists
    )


def quality_binding_document(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": artifact.get("schema_version"),
        "contract_path": artifact.get("contract_path"),
        "contract_sha256": artifact.get("contract_sha256"),
        "contract_revision": artifact.get("contract_revision"),
        "evaluator_revision": artifact.get("evaluator_revision"),
        "evaluator_source_path": artifact.get("evaluator_source_path"),
        "evaluator_sha256": artifact.get("evaluator_sha256"),
        "quality_engine_path": artifact.get("quality_engine_path"),
        "quality_engine_sha256": artifact.get("quality_engine_sha256"),
        "reference_bundle_sha256": artifact.get("reference_bundle_sha256"),
        "reference_sha256": artifact.get("reference_sha256"),
        "execution_alignment_key": artifact.get("execution_alignment_key"),
        "task_class": artifact.get("task_class"),
        "evaluator": artifact.get("evaluator"),
        "parser_outcome": artifact.get("parser_outcome"),
        "task_outcome": artifact.get("task_outcome"),
        "quality_role": artifact.get("quality_role"),
        "task_capability": artifact.get("task_capability"),
        "output_hash": artifact.get("output_hash"),
        "stop_reason": artifact.get("stop_reason"),
        "legal_stop": artifact.get("legal_stop"),
        "qg1": artifact.get("qg1"),
        "qg2": artifact.get("qg2"),
        "qg3": artifact.get("qg3"),
        "blocking_status": artifact.get("blocking_status"),
        "qg4": artifact.get("qg4"),
    }


def build_quality_artifact(
    *,
    contract: Mapping[str, Any],
    contract_binding: Mapping[str, Any],
    sample: Mapping[str, Any],
    quality: Mapping[str, Any],
    generation: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    alignment: str,
    pass_id: str,
) -> dict[str, Any]:
    classification = task_classification(
        sample, quality, contract, generation.get("text")
    )
    output_ids = generation.get("output_token_ids")
    input_ids = generation.get("input_token_ids")
    input_tokens_legal = (
        isinstance(input_ids, list)
        and bool(input_ids)
        and all(
            isinstance(token, int) and not isinstance(token, bool) and token >= 0
            for token in input_ids
        )
        and generation.get("input_token_count") == len(input_ids)
    )
    output_tokens_legal = (
        isinstance(output_ids, list)
        and bool(output_ids)
        and all(
            isinstance(token, int) and not isinstance(token, bool) and token >= 0
            for token in output_ids
        )
    )
    output_nonempty = isinstance(output_ids, list) and bool(output_ids)
    token_count_consistent = (
        isinstance(output_ids, list)
        and generation.get("output_token_count") == len(output_ids)
    )
    finite_values = _finite(records)
    expert_ids_legal = _expert_ids_legal(records)
    legal_stops = contract.get("legal_stop_reasons")
    stop_reason = generation.get("stop_reason")
    legal_stop = (
        isinstance(legal_stops, list)
        and stop_reason in legal_stops
    )
    qg1_reasons: list[str] = []
    if not input_tokens_legal:
        qg1_reasons.append("input token IDs/count are invalid")
    if not output_nonempty:
        qg1_reasons.append("generation output is empty")
    if not output_tokens_legal:
        qg1_reasons.append("output token IDs are invalid")
    if not token_count_consistent:
        qg1_reasons.append("output token count differs from token IDs")
    if generation.get("output_hash") != (
        canonical_hash(output_ids) if output_tokens_legal else None
    ):
        qg1_reasons.append("output hash differs from canonical token IDs")
    if not legal_stop:
        qg1_reasons.append(f"illegal or unknown stop reason: {stop_reason!r}")
    if not finite_values:
        qg1_reasons.append("collector records contain NaN or infinity")
    if expert_ids_legal is False:
        qg1_reasons.append("collector records contain illegal expert IDs")
    if classification["parser_outcome"] != "parseable":
        qg1_reasons.append(
            f"parser/evaluator classification is {classification['parser_outcome']}"
        )
    if classification["task_class"] == "unknown":
        qg1_reasons.append("unknown task is blocked fail-closed")

    qg3 = "not_applicable"
    if classification["task_class"] == "T0":
        qg3 = (
            "pass"
            if classification["task_outcome"] == "correct"
            else "fail"
        )
    qg4 = "not_applicable"
    if classification["task_class"] in {"T1", "T2"}:
        qg4 = "recorded_nonblocking"
    blocking_reasons = list(qg1_reasons)
    if qg3 == "fail":
        blocking_reasons.append("QG-3 T0 task correctness failed")
    artifact: dict[str, Any] = {
        "schema_version": "c1-quality-v2",
        "contract_path": contract_binding.get("path"),
        "contract_sha256": contract_binding.get("sha256"),
        "contract_revision": contract_binding.get("contract_revision"),
        "evaluator_revision": contract_binding.get("evaluator_revision"),
        "evaluator_source_path": contract_binding.get("evaluator_source_path"),
        "evaluator_sha256": contract_binding.get("evaluator_sha256"),
        "quality_engine_path": contract_binding.get("quality_engine_path"),
        "quality_engine_sha256": contract_binding.get("quality_engine_sha256"),
        "reference_bundle_sha256": contract_binding.get(
            "reference_bundle_sha256"
        ),
        "reference_sha256": reference_hash(sample),
        "execution_alignment_key": alignment,
        "pass_id": pass_id,
        "output_hash": generation.get("output_hash"),
        "output_nonempty": output_nonempty,
        "finite_values": finite_values,
        "token_count_consistent": token_count_consistent,
        "expert_ids_legal": expert_ids_legal,
        "stop_reason": stop_reason,
        "legal_stop": legal_stop,
        "task_class": classification["task_class"],
        "evaluator": classification.get("evaluator", "unknown"),
        "parser_outcome": classification["parser_outcome"],
        "task_outcome": classification["task_outcome"],
        "quality_role": "observational",
        "task_capability": "not_inferred",
        "qg1": "pass" if not qg1_reasons else "fail",
        "qg2": "pending_cross_pass",
        "qg3": qg3,
        "qg4": qg4,
        "blocking_status": "pass" if not blocking_reasons else "fail",
        "blocking_reasons": blocking_reasons,
    }
    artifact["quality_binding_sha256"] = canonical_hash(
        quality_binding_document(artifact)
    )
    return artifact


def validate_quality_artifact(
    artifact: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    contract_binding: Mapping[str, Any],
    sample: Mapping[str, Any],
    generation: Mapping[str, Any],
) -> None:
    validate_contract_definition(contract)
    if artifact.get("schema_version") != "c1-quality-v2":
        raise ValueError("quality artifact is not c1-quality-v2")
    expected_contract = {
        "contract_path": contract_binding.get("path"),
        "contract_sha256": contract_binding.get("sha256"),
        "contract_revision": contract.get("contract_revision"),
        "evaluator_revision": contract.get("evaluator_revision"),
        "evaluator_source_path": contract_binding.get("evaluator_source_path"),
        "evaluator_sha256": contract_binding.get("evaluator_sha256"),
        "quality_engine_path": contract_binding.get("quality_engine_path"),
        "quality_engine_sha256": contract_binding.get("quality_engine_sha256"),
        "reference_bundle_sha256": contract_binding.get(
            "reference_bundle_sha256"
        ),
    }
    if any(artifact.get(name) != value for name, value in expected_contract.items()):
        raise ValueError("quality artifact contract binding mismatch")
    if artifact.get("reference_sha256") != reference_hash(sample):
        raise ValueError("quality artifact reference hash mismatch")
    if artifact.get("output_hash") != generation.get("output_hash"):
        raise ValueError("quality artifact generation binding mismatch")
    if artifact.get("execution_alignment_key") != generation.get(
        "execution_alignment_key"
    ):
        raise ValueError("quality artifact execution alignment mismatch")
    authoritative = evaluate_frozen_sample(generation.get("text"), sample)
    task_contract = (contract.get("task_contracts") or {}).get(sample.get("task_id"))
    expected_evaluator = (
        task_contract.get("evaluator") if isinstance(task_contract, Mapping) else None
    )
    if sample.get("task_id") in KNOWN_TASKS and (
        authoritative["evaluator"] != expected_evaluator
    ):
        raise ValueError("quality artifact evaluator contract mismatch")
    expected_parser = (
        "unknown" if sample.get("task_id") not in KNOWN_TASKS else
        "parseable" if authoritative["validity"] else "unparseable"
    )
    expected_outcome = (
        "correct" if authoritative["correctness"] is True else
        "incorrect" if authoritative["correctness"] is False else "unknown"
    )
    if (
        artifact.get("evaluator") != authoritative["evaluator"]
        or artifact.get("parser_outcome") != expected_parser
        or artifact.get("task_outcome") != expected_outcome
    ):
        raise ValueError("quality artifact differs from replayed evaluator result")
    expected_binding = canonical_hash(quality_binding_document(artifact))
    if artifact.get("quality_binding_sha256") != expected_binding:
        raise ValueError("quality artifact binding hash mismatch")
    task_class = artifact.get("task_class")
    expected_task = sample.get("task_id")
    if task_class != (expected_task if expected_task in KNOWN_TASKS else "unknown"):
        raise ValueError("quality artifact task class mismatch")
    if (
        artifact.get("quality_role") != "observational"
        or artifact.get("task_capability") != "not_inferred"
    ):
        raise ValueError("quality artifact must not infer task capability")
    if task_class not in KNOWN_TASKS:
        if artifact.get("blocking_status") != "fail":
            raise ValueError("unknown task must be blocking")
    if artifact.get("task_outcome") == "incorrect":
        expected_qg4 = (
            "recorded_nonblocking" if task_class in {"T1", "T2"}
            else "not_applicable"
        )
        if artifact.get("qg4") != expected_qg4:
            raise ValueError("incorrect task outcome has invalid QG-4 disposition")
    if artifact.get("stop_reason") not in contract.get("legal_stop_reasons", []):
        if artifact.get("blocking_status") != "fail":
            raise ValueError("illegal stop reason must be blocking")
    if artifact.get("stop_reason") != generation.get("stop_reason"):
        raise ValueError("quality artifact stop reason mismatch")
    expected_legal_stop = generation.get("stop_reason") in contract.get(
        "legal_stop_reasons", []
    )
    if artifact.get("legal_stop") is not expected_legal_stop:
        raise ValueError("quality artifact legal stop classification mismatch")
    if artifact.get("blocking_status") == "pass":
        if (
            artifact.get("parser_outcome") != "parseable"
            or artifact.get("task_outcome") not in {"correct", "incorrect"}
            or artifact.get("qg1") != "pass"
            or artifact.get("qg2") != "pending_cross_pass"
            or artifact.get("output_nonempty") is not True
            or artifact.get("finite_values") is not True
            or artifact.get("token_count_consistent") is not True
            or artifact.get("expert_ids_legal") is False
            or artifact.get("legal_stop") is not True
        ):
            raise ValueError("passing quality artifact violates blocking gates")
        expected_qg3 = (
            "pass"
            if task_class == "T0" and artifact.get("task_outcome") == "correct"
            else "not_applicable"
        )
        if artifact.get("qg3") != expected_qg3:
            raise ValueError("passing quality artifact QG-3 mismatch")
        expected_qg4 = (
            "recorded_nonblocking"
            if task_class in {"T1", "T2"}
            else "not_applicable"
        )
        if artifact.get("qg4") != expected_qg4:
            raise ValueError("passing quality artifact QG-4 mismatch")


def compare_cross_pass_evidence(
    evidence_by_pass: Mapping[str, Mapping[str, Any]],
    *,
    expected_passes: Sequence[str],
    baseline_pass: str = "P0",
) -> list[dict[str, Any]]:
    """Compare every declared generation pass to P0, including classification."""
    findings: list[dict[str, Any]] = []
    expected = list(expected_passes)
    missing = [pass_id for pass_id in expected if pass_id not in evidence_by_pass]
    for pass_id in missing:
        findings.append({"kind": "missing_pass_evidence", "pass_id": pass_id})
    baseline = evidence_by_pass.get(baseline_pass)
    if baseline is None:
        findings.append({"kind": "missing_baseline", "pass_id": baseline_pass})
        return findings
    baseline_generation = baseline.get("generation")
    baseline_quality = baseline.get("quality")
    if not isinstance(baseline_generation, Mapping) or not isinstance(
        baseline_quality, Mapping
    ):
        findings.append({"kind": "invalid_baseline_evidence"})
        return findings
    for pass_id in expected:
        if pass_id == baseline_pass or pass_id not in evidence_by_pass:
            continue
        candidate = evidence_by_pass[pass_id]
        generation = candidate.get("generation")
        quality = candidate.get("quality")
        if not isinstance(generation, Mapping) or not isinstance(quality, Mapping):
            findings.append({"kind": "invalid_pass_evidence", "pass_id": pass_id})
            continue
        for field in FORMAL_GENERATION_FIELDS:
            if baseline_generation.get(field) != generation.get(field):
                findings.append({
                    "kind": "generation_drift",
                    "pass_id": pass_id,
                    "field": field,
                })
        for field in QUALITY_COMPARISON_FIELDS:
            if baseline_quality.get(field) != quality.get(field):
                findings.append({
                    "kind": "classification_drift",
                    "pass_id": pass_id,
                    "field": field,
                })
    return findings
