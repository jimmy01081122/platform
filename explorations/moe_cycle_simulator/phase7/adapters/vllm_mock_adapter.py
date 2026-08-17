#!/usr/bin/env python3
"""Fail-closed CPU mock adapter for the prospective Phase 7 vLLM collector.

This module validates shape and identity contracts only.  It does not import vLLM,
query a GPU, download a model, or claim that synthetic observations are measured.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable

PHASE7_ROOT = Path(__file__).resolve().parents[1]
SIM_ROOT = PHASE7_ROOT.parent
sys.path.insert(0, str(SIM_ROOT / "tools"))

from contract_runtime import (  # noqa: E402
    Clock,
    ContractError,
    alignment_grade,
    canonical_bytes,
    dataset_semantic_hash,
    runtime_variant_hash,
    schema_fingerprint,
    transform_alignment,
    validate_alignment,
    validate_events,
    validate_observability,
    validate_routing,
    validate_runtime_variant,
)


class AdapterContractError(ValueError):
    """Raised when a Phase 7 input is not contract-valid."""


HASH_CHARS = frozenset("0123456789abcdef")
PASS_IDS = ("P0", "P1", "P2", "P3", "P4", "P5", "V0")
PASS_DEFINITIONS = {
    "P0": ("SAMPLE", "CLEAN_CORRECTNESS_AUTHORITATIVE_TIMING", True, True),
    "P1": ("SAMPLE", "OPERATOR_KERNEL_SERVICE_TIMING", True, True),
    "P2": ("SAMPLE", "ROUTING_TOPK_TOKEN_EXPERT_MAPPING", True, True),
    "P3": ("SAMPLE", "ALLOCATION_RESIDENCY_COPY_ENGINE", True, True),
    "P4": ("SAMPLE", "P2P_NVLINK", True, True),
    "P5": ("SESSION", "STEADY_STATE_TELEMETRY", True, True),
    "V0": ("OFFLINE", "IR_CONVERSION_REPLAY_VALIDATION", False, False),
}
EVENT_SHAPES = {
    "request": "REQUEST_ARRIVAL",
    "operator": "COMPUTE_START",
    "kernel": "COMPUTE_START",
    "kernel_complete": "COMPUTE_COMPLETE",
    "router": "DEPENDENCY_READY",
    "allocation": "RESOURCE_ACQUIRE",
    "copy": "TRANSFER_START",
    "collective": "TRANSFER_START",
    "p2p": "TRANSFER_START",
    "telemetry": "TELEMETRY_SAMPLE",
}
ALLOWED_USES = {
    "CROSS_DOMAIN_CALIBRATION",
    "EVENT_ORDERING",
    "SAME_SOURCE_DURATION",
    "AGGREGATE_STATISTICS",
}
SINGLE_BLOCKING = {
    "input_token_ids",
    "generation_parameters",
    "output_token_ids",
    "stop_reason",
    "execution_validity",
}
SERVING_BLOCKING = {
    "request_set",
    "arrival_trace",
    "generation_parameters",
    "execution_validity",
    "completion_set",
}
SERVING_OBSERVATIONAL = {
    "batch_formation",
    "schedule",
    "stream_ordering",
    "event_timing",
}
RUNTIME_KEYS = {
    "schema_version",
    "variant_id",
    "runtime",
    "container",
    "cuda",
    "driver",
    "attention_backend",
    "fused_moe_backend",
    "tensor_parallel_size",
    "expert_parallel_size",
    "pipeline_parallel_size",
    "distributed_executor",
    "execution_mode",
    "max_model_length",
    "max_batched_tokens",
    "max_sequences",
    "scheduler_policy",
    "kv_cache_dtype",
    "nccl_environment",
    "placement",
    "offload",
    "kernel_backend",
    "seed",
    "generation",
    "collector_hash",
    "adapter_hash",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for raw_key, item in pairs:
        key = unicodedata.normalize("NFC", raw_key)
        if key in value:
            raise AdapterContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise AdapterContractError(f"non-finite JSON number: {value}")


def _reject_floats(value: Any, location: str = "$") -> None:
    if isinstance(value, float):
        raise AdapterContractError(f"{location}: JSON floats are forbidden")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f"{location}[{index}]")


def load_strict_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterContractError(f"invalid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise AdapterContractError("fixture root must be an object")
    _reject_floats(value)
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        raise AdapterContractError(
            f"{location}: key closure mismatch "
            f"missing={sorted(expected-actual)} extra={sorted(actual-expected)}"
        )


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise AdapterContractError(f"{location}: non-empty string required")
    return unicodedata.normalize("NFC", value)


def _uint(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterContractError(f"{location}: unsigned integer required")
    return value


def _uint_string(value: Any, location: str) -> int:
    if (
        not isinstance(value, str)
        or not value
        or any(character not in "0123456789" for character in value)
        or (value != "0" and value.startswith("0"))
    ):
        raise AdapterContractError(f"{location}: canonical uint string required")
    return int(value)


def _hash(value: Any, location: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in HASH_CHARS for char in value)
    ):
        raise AdapterContractError(f"{location}: lowercase SHA-256 required")
    return value


def _record_hash(record: dict[str, Any], domain: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + canonical_bytes(record)).hexdigest()


def validate_runtime_manifest(value: dict[str, Any]) -> None:
    _exact_keys(value, RUNTIME_KEYS, "runtime_variant")
    if value["schema_version"] != "runtime-variant-v1":
        raise AdapterContractError("runtime variant schema version mismatch")
    for field in ("runtime", "container", "cuda", "driver"):
        item = value[field]
        if not isinstance(item, dict):
            raise AdapterContractError(f"runtime_variant.{field}: object required")
        _exact_keys(item, {"name", "revision"}, f"runtime_variant.{field}")
        _text(item["name"], f"runtime_variant.{field}.name")
        _text(item["revision"], f"runtime_variant.{field}.revision")
    for field in (
        "attention_backend",
        "fused_moe_backend",
        "distributed_executor",
        "execution_mode",
        "scheduler_policy",
        "kv_cache_dtype",
        "kernel_backend",
    ):
        _text(value[field], f"runtime_variant.{field}")
    for field in (
        "tensor_parallel_size",
        "expert_parallel_size",
        "pipeline_parallel_size",
        "max_model_length",
        "max_batched_tokens",
        "max_sequences",
    ):
        if _uint(value[field], f"runtime_variant.{field}") < 1:
            raise AdapterContractError(f"runtime_variant.{field}: must be positive")
    _uint(value["seed"], "runtime_variant.seed")
    if value["execution_mode"] not in {"EAGER", "CUDA_GRAPH"}:
        raise AdapterContractError("runtime variant execution mode is invalid")
    for field in ("nccl_environment", "placement", "offload", "generation"):
        if not isinstance(value[field], dict):
            raise AdapterContractError(f"runtime_variant.{field}: object required")
        _reject_floats(value[field], f"runtime_variant.{field}")
    _hash(value["collector_hash"], "runtime_variant.collector_hash")
    _hash(value["adapter_hash"], "runtime_variant.adapter_hash")
    try:
        validate_runtime_variant(value)
    except (ContractError, KeyError) as exc:
        raise AdapterContractError(str(exc)) from exc


def validate_pass_contracts(values: Any) -> None:
    if not isinstance(values, list) or len(values) != len(PASS_IDS):
        raise AdapterContractError("pass_contracts must contain P0-P5 and V0 once")
    observed: dict[str, dict[str, Any]] = {}
    keys = {
        "pass_id",
        "scope",
        "purpose",
        "execution_role",
        "gpu_execution",
        "runtime_pass",
        "minimum_steady_state_seconds",
    }
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise AdapterContractError(f"pass_contracts[{index}]: object required")
        _exact_keys(value, keys, f"pass_contracts[{index}]")
        pass_id = _text(value["pass_id"], f"pass_contracts[{index}].pass_id")
        if pass_id in observed or pass_id not in PASS_DEFINITIONS:
            raise AdapterContractError("duplicate or unknown pass ID")
        scope, purpose, gpu_execution, runtime_pass = PASS_DEFINITIONS[pass_id]
        expected_role = "OFFLINE_VALIDATION" if pass_id == "V0" else "RUNTIME_PASS"
        if (
            value["scope"] != scope
            or value["purpose"] != purpose
            or value["execution_role"] != expected_role
            or value["gpu_execution"] is not gpu_execution
            or value["runtime_pass"] is not runtime_pass
        ):
            raise AdapterContractError(f"{pass_id}: pass role contract mismatch")
        duration = value["minimum_steady_state_seconds"]
        if pass_id == "P5":
            if duration != 30:
                raise AdapterContractError("P5 requires exactly the frozen 30-second minimum")
        elif duration is not None:
            raise AdapterContractError(f"{pass_id}: steady-state duration is inapplicable")
        observed[pass_id] = value
    if tuple(sorted(observed, key=PASS_IDS.index)) != PASS_IDS:
        raise AdapterContractError("pass contract closure mismatch")


def _validate_observability(value: dict[str, Any], location: str) -> None:
    allowed = {"availability", "evidence_mode", "expected_evidence_modes"}
    if set(value) not in (allowed, allowed - {"expected_evidence_modes"}):
        raise AdapterContractError(f"{location}: observability key closure mismatch")
    if value.get("availability") not in {
        "CONFIRMED",
        "CONDITIONAL",
        "UNAVAILABLE",
        "NOT_APPLICABLE",
    }:
        raise AdapterContractError(f"{location}: availability enum mismatch")
    if value.get("evidence_mode") not in {
        "MEASURED",
        "DERIVED",
        "INSTRUMENTED",
        "NONE",
    }:
        raise AdapterContractError(f"{location}: evidence mode enum mismatch")
    if "expected_evidence_modes" in value:
        expected = value["expected_evidence_modes"]
        if (
            not isinstance(expected, list)
            or not expected
            or len(set(expected)) != len(expected)
            or any(item not in {"MEASURED", "DERIVED", "INSTRUMENTED"} for item in expected)
        ):
            raise AdapterContractError(f"{location}: invalid expected evidence modes")
    try:
        validate_observability(value)
    except (ContractError, KeyError) as exc:
        raise AdapterContractError(str(exc)) from exc


def _validate_alignment_binding(
    alignment: dict[str, Any],
    global_clock_record: dict[str, Any],
    shortest_component: dict[str, Any],
) -> str:
    expected_clock_hash = hashlib.sha256(canonical_bytes(global_clock_record)).hexdigest()
    expected_component_hash = hashlib.sha256(
        canonical_bytes(shortest_component)
    ).hexdigest()
    inputs = alignment.get("grading_inputs", {})
    if inputs.get("target_clock_profile_hash") != expected_clock_hash:
        raise AdapterContractError("alignment target-clock profile binding mismatch")
    if (
        inputs.get("shortest_component_record_hash") != expected_component_hash
        or inputs.get("shortest_component_duration_fs")
        != shortest_component["duration_fs"]
    ):
        raise AdapterContractError("alignment shortest-component binding mismatch")
    try:
        validate_alignment(alignment)
        return alignment_grade(alignment)
    except (ContractError, KeyError, TypeError) as exc:
        raise AdapterContractError(str(exc)) from exc


def _validate_identity(value: dict[str, Any], runtime_hash: str) -> None:
    keys = {
        "contract_id",
        "mode",
        "runtime_variant_hash",
        "request_set",
        "arrival_trace",
        "generation_parameters",
        "execution_validity",
        "completion_set",
        "blocking_fields",
        "routing_comparison",
        "aggregate_routing_demand",
        "observational_fields",
    }
    _exact_keys(value, keys, "identity_contract")
    _text(value["contract_id"], "identity_contract.contract_id")
    if value["runtime_variant_hash"] != runtime_hash:
        raise AdapterContractError("identity runtime variant mismatch")
    if not isinstance(value["request_set"], list) or not value["request_set"]:
        raise AdapterContractError("identity request set must be non-empty")
    request_ids: list[str] = []
    for request in value["request_set"]:
        if not isinstance(request, dict):
            raise AdapterContractError("identity request must be an object")
        _exact_keys(
            request,
            {"request_id", "input_token_ids", "output_token_ids", "stop_reason"},
            "identity request",
        )
        request_ids.append(_text(request["request_id"], "identity request ID"))
        for field in ("input_token_ids", "output_token_ids"):
            if (
                not isinstance(request[field], list)
                or any(
                    isinstance(token, bool) or not isinstance(token, int) or token < 0
                    for token in request[field]
                )
            ):
                raise AdapterContractError(f"identity {field} is invalid")
        _text(request["stop_reason"], "identity stop reason")
    if len(set(request_ids)) != len(request_ids):
        raise AdapterContractError("identity request IDs must be unique")
    if not isinstance(value["generation_parameters"], dict):
        raise AdapterContractError("identity generation parameters must be an object")
    if value["execution_validity"] not in {"VALID", "INVALID"}:
        raise AdapterContractError("identity execution validity is invalid")
    if not isinstance(value["blocking_fields"], list):
        raise AdapterContractError("identity blocking_fields must be a list")
    mode = value["mode"]
    if mode == "SINGLE_REQUEST":
        if len(request_ids) != 1:
            raise AdapterContractError("single-request identity requires one request")
        if (
            set(value["blocking_fields"]) != SINGLE_BLOCKING
            or value["arrival_trace"] is not None
            or value["completion_set"] is not None
            or value["aggregate_routing_demand"] is not None
            or value["observational_fields"] != []
            or value["routing_comparison"] != "TOPK_AMBIGUITY_RULE_WEIGHTS_TOLERANCE_BOUND"
        ):
            raise AdapterContractError("single-request identity contract mismatch")
    elif mode == "SERVING_REPLAY":
        arrivals = value["arrival_trace"]
        completions = value["completion_set"]
        if (
            set(value["blocking_fields"]) != SERVING_BLOCKING
            or set(value["observational_fields"]) != SERVING_OBSERVATIONAL
            or value["routing_comparison"] != "AGGREGATE_CONFIDENCE_BOUND"
            or value["aggregate_routing_demand"] != "CONFIDENCE_BOUND"
            or not isinstance(arrivals, list)
            or not isinstance(completions, list)
            or set(completions) != set(request_ids)
        ):
            raise AdapterContractError("serving identity contract mismatch")
        arrival_ids: list[str] = []
        previous = -1
        for item in arrivals:
            if not isinstance(item, dict):
                raise AdapterContractError("serving arrival must be an object")
            _exact_keys(item, {"request_id", "arrival_time_fs"}, "serving arrival")
            arrival_ids.append(item["request_id"])
            timestamp = _uint_string(item["arrival_time_fs"], "arrival_time_fs")
            if timestamp < previous:
                raise AdapterContractError("serving arrival trace must be ordered")
            previous = timestamp
        if set(arrival_ids) != set(request_ids) or len(arrival_ids) != len(set(arrival_ids)):
            raise AdapterContractError("serving arrival/request set mismatch")
    else:
        raise AdapterContractError("unknown identity mode")


def _load_priorities_and_descriptors() -> tuple[dict[str, int], dict[str, Any]]:
    priorities_value = load_strict_json(SIM_ROOT / "contracts" / "event_priorities.json")
    priorities = {item["name"]: item["value"] for item in priorities_value["priorities"]}
    descriptors = load_strict_json(
        SIM_ROOT / "contracts" / "semantic_descriptors.json"
    )["descriptors"]
    return priorities, descriptors


def _validate_fixture_root(fixture: dict[str, Any]) -> None:
    _exact_keys(
        fixture,
        {
            "schema_version",
            "fixture_id",
            "evidence_class",
            "producer",
            "runtime_variant",
            "pass_contracts",
            "global_clock",
            "shortest_component",
            "clock_alignments",
            "raw_events",
            "routing_records",
            "identity_contracts",
        },
        "fixture",
    )
    if fixture["schema_version"] != "moe-phase7-mock-vllm-trace-v1":
        raise AdapterContractError("fixture schema version mismatch")
    if fixture["evidence_class"] != "SYNTHETIC_CPU_MOCK":
        raise AdapterContractError("fixture may only contain synthetic CPU mock evidence")
    _text(fixture["fixture_id"], "fixture.fixture_id")
    _text(fixture["producer"], "fixture.producer")


def adapt_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    """Validate and convert one synthetic fixture into canonical observations."""
    _validate_fixture_root(fixture)
    runtime = fixture["runtime_variant"]
    validate_runtime_manifest(runtime)
    validate_pass_contracts(fixture["pass_contracts"])
    runtime_hash = runtime["variant_id"]

    global_clock_record = fixture["global_clock"]
    global_clock = Clock.from_record(global_clock_record)
    if global_clock.clock_id != "global":
        raise AdapterContractError("Phase 7 fixture target clock must be global")
    shortest = fixture["shortest_component"]
    _exact_keys(shortest, {"record_id", "duration_fs"}, "shortest_component")
    if _uint_string(shortest["duration_fs"], "shortest component duration") == 0:
        raise AdapterContractError("shortest component duration must be positive")

    alignments: dict[str, tuple[dict[str, Any], str]] = {}
    if not isinstance(fixture["clock_alignments"], list):
        raise AdapterContractError("clock_alignments must be a list")
    for alignment in fixture["clock_alignments"]:
        source_id = _text(alignment.get("source_clock_id"), "alignment source clock")
        if source_id in alignments:
            raise AdapterContractError("duplicate source clock alignment")
        if alignment.get("target_clock_id") != "global":
            raise AdapterContractError("alignment target must be global")
        grade = _validate_alignment_binding(alignment, global_clock_record, shortest)
        alignments[source_id] = (alignment, grade)

    priorities, descriptors = _load_priorities_and_descriptors()
    event_descriptor = descriptors["event-ir-v1"]
    event_descriptor_hash = schema_fingerprint(event_descriptor)
    observation_descriptor = load_strict_json(
        PHASE7_ROOT / "schemas" / "canonical_observation_descriptor.json"
    )
    source_hash = hashlib.sha256(canonical_bytes(fixture)).hexdigest()
    observations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    seen_raw_ids: set[str] = set()
    raw_events = fixture["raw_events"]
    if not isinstance(raw_events, list) or not raw_events:
        raise AdapterContractError("raw_events must be non-empty")
    raw_keys = {
        "raw_event_id",
        "pass_id",
        "shape",
        "source_clock_id",
        "source_timestamp",
        "request_id",
        "token_index",
        "layer_index",
        "rank",
        "stream_id",
        "correlation_id",
        "dependencies",
        "alignment_use",
        "observability",
        "attributes",
    }
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            raise AdapterContractError(f"raw_events[{index}]: object required")
        _exact_keys(raw, raw_keys, f"raw_events[{index}]")
        raw_id = _text(raw["raw_event_id"], "raw event ID")
        if raw_id in seen_raw_ids:
            raise AdapterContractError("duplicate raw event ID")
        seen_raw_ids.add(raw_id)
        pass_id = raw["pass_id"]
        if pass_id not in PASS_IDS or pass_id == "V0":
            raise AdapterContractError("raw runtime events require P0-P5")
        shape = raw["shape"]
        if shape not in EVENT_SHAPES:
            raise AdapterContractError("unknown raw event shape")
        source_id = raw["source_clock_id"]
        if source_id not in alignments:
            raise AdapterContractError("raw event references unaligned source clock")
        source_time = _uint_string(raw["source_timestamp"], "source timestamp")
        alignment, grade = alignments[source_id]
        try:
            aligned_time = transform_alignment(alignment, source_time)
        except ContractError as exc:
            raise AdapterContractError(str(exc)) from exc
        alignment_use = raw["alignment_use"]
        if alignment_use not in ALLOWED_USES:
            raise AdapterContractError("unknown alignment use")
        if alignment_use == "CROSS_DOMAIN_CALIBRATION" and grade != "CYCLE_GRADE":
            raise AdapterContractError(
                "cross-domain calibration requires CYCLE_GRADE alignment"
            )
        if grade == "AGGREGATE_ONLY" and alignment_use not in {
            "SAME_SOURCE_DURATION",
            "AGGREGATE_STATISTICS",
        }:
            raise AdapterContractError(
                "aggregate-only alignment cannot support event ordering/timing"
            )
        observability = raw["observability"]
        if not isinstance(observability, dict):
            raise AdapterContractError("raw observability must be an object")
        _validate_observability(observability, f"raw_events[{index}].observability")
        if (
            fixture["evidence_class"] == "SYNTHETIC_CPU_MOCK"
            and observability["evidence_mode"] == "MEASURED"
        ):
            raise AdapterContractError("synthetic mock evidence cannot be MEASURED")
        if (
            alignment_use == "CROSS_DOMAIN_CALIBRATION"
            and observability["availability"] != "CONFIRMED"
        ):
            raise AdapterContractError(
                "conditional/unavailable observation cannot enter calibration"
            )
        if not isinstance(raw["dependencies"], list) or len(
            set(raw["dependencies"])
        ) != len(raw["dependencies"]):
            raise AdapterContractError("event dependencies must be a unique list")
        if not isinstance(raw["attributes"], dict):
            raise AdapterContractError("event attributes must be an object")
        _reject_floats(raw["attributes"], "raw event attributes")
        observation = {
            "schema_version": "phase7-canonical-observation-v1",
            "observation_id": raw_id,
            "pass_id": pass_id,
            "source_clock_id": source_id,
            "source_timestamp": raw["source_timestamp"],
            "aligned_time_fs": str(aligned_time),
            "alignment_id": alignment["alignment_id"],
            "alignment_grade": grade,
            "alignment_use": alignment_use,
            "request_id": raw["request_id"],
            "rank": raw["rank"],
            "stream_id": raw["stream_id"],
            "correlation_id": raw["correlation_id"],
            "observability": observability,
            "evidence_class": fixture["evidence_class"],
            "runtime_variant_hash": runtime_hash,
            "raw_source_sha256": source_hash,
        }
        observations.append(observation)
        event_type = EVENT_SHAPES[shape]
        events.append(
            {
                "schema_version": "event-ir-v1",
                "trace_id": fixture["fixture_id"],
                "event_id": raw_id,
                "event_type": event_type,
                "time_fs": str(aligned_time),
                "event_priority": priorities[event_type],
                "request_id": raw["request_id"],
                "token_index": raw["token_index"],
                "layer_index": raw["layer_index"],
                "component_id": source_id,
                "clock_id": "global",
                "rank": raw["rank"],
                "stream_id": raw["stream_id"],
                "correlation_id": raw["correlation_id"],
                "semantic_descriptor_hash": event_descriptor_hash,
                "duration_fs": None,
                "dependencies": raw["dependencies"],
                "observability": {
                    "schema_version": "observability-v1",
                    **observability,
                },
                "provenance": {
                    "producer": fixture["producer"],
                    "producer_version": "phase7-cpu-mock-v1",
                    "raw_content_ids": [source_hash],
                },
                "attributes": {
                    **raw["attributes"],
                    "mock_shape": shape,
                    "pass_id": pass_id,
                    "alignment_grade": grade,
                    "alignment_use": alignment_use,
                    "evidence_class": fixture["evidence_class"],
                    "runtime_variant_hash": runtime_hash,
                },
            }
        )
    if any(dependency not in seen_raw_ids for raw in raw_events for dependency in raw["dependencies"]):
        raise AdapterContractError("event dependency references unknown raw event")
    try:
        events = validate_events(events, {"global": global_clock}, priorities)
    except (ContractError, KeyError) as exc:
        raise AdapterContractError(str(exc)) from exc

    routing_records: list[dict[str, Any]] = []
    for index, routing in enumerate(fixture["routing_records"]):
        if not isinstance(routing, dict):
            raise AdapterContractError(f"routing_records[{index}]: object required")
        value = {
            "schema_version": "routing-ir-v1",
            **routing,
            "model_profile_hash": runtime_hash,
            "semantic_descriptor_hash": schema_fingerprint(
                descriptors["routing-ir-v1"]
            ),
        }
        if value["observability"].get("evidence_mode") == "MEASURED":
            raise AdapterContractError("synthetic routing cannot be MEASURED")
        try:
            validate_routing(value)
        except (ContractError, KeyError, TypeError) as exc:
            raise AdapterContractError(str(exc)) from exc
        routing_records.append(value)

    identities = fixture["identity_contracts"]
    if not isinstance(identities, list) or {item.get("mode") for item in identities} != {
        "SINGLE_REQUEST",
        "SERVING_REPLAY",
    }:
        raise AdapterContractError("identity contracts require single and serving modes")
    identity_ids: set[str] = set()
    for identity in identities:
        _validate_identity(identity, runtime_hash)
        if identity["contract_id"] in identity_ids:
            raise AdapterContractError("duplicate identity contract ID")
        identity_ids.add(identity["contract_id"])

    observation_rows, observation_root = dataset_semantic_hash(
        observations, observation_descriptor
    )
    event_rows, event_root = dataset_semantic_hash(events, event_descriptor)
    routing_rows, routing_root = dataset_semantic_hash(
        routing_records, descriptors["routing-ir-v1"]
    )
    identity_hashes = [
        _record_hash(item, b"moe-phase7-identity-v1")
        for item in sorted(identities, key=lambda item: item["contract_id"])
    ]
    aggregate = hashlib.sha256(
        b"moe-phase7-mock-adapter-v1\0"
        + bytes.fromhex(runtime_hash)
        + bytes.fromhex(observation_root)
        + bytes.fromhex(event_root)
        + bytes.fromhex(routing_root)
        + b"".join(bytes.fromhex(item) for item in identity_hashes)
    ).hexdigest()
    return {
        "schema_version": "moe-phase7-adapter-output-v1",
        "evidence_class": "SYNTHETIC_CPU_MOCK",
        "formal_runtime_evidence": False,
        "gpu_used": False,
        "model_downloaded": False,
        "runtime_variant": runtime,
        "pass_contracts": fixture["pass_contracts"],
        "clock_alignments": fixture["clock_alignments"],
        "observations": observations,
        "events": events,
        "routing_records": routing_records,
        "identity_contracts": identities,
        "v0": {
            "execution_role": "OFFLINE_VALIDATION",
            "gpu_execution": False,
            "runtime_pass": False,
            "status": "PASS",
            "findings": [],
        },
        "semantic_hashes": {
            "observation_rows": observation_rows,
            "observation_root": observation_root,
            "event_rows": event_rows,
            "event_root": event_root,
            "routing_rows": routing_rows,
            "routing_root": routing_root,
            "identity_records": identity_hashes,
            "adapter_output_root": aggregate,
        },
    }


def validate_output(output: dict[str, Any]) -> None:
    """Replay conversion and require exact canonical output identity."""
    expected_keys = {
        "schema_version",
        "evidence_class",
        "formal_runtime_evidence",
        "gpu_used",
        "model_downloaded",
        "runtime_variant",
        "pass_contracts",
        "clock_alignments",
        "observations",
        "events",
        "routing_records",
        "identity_contracts",
        "v0",
        "semantic_hashes",
    }
    _exact_keys(output, expected_keys, "adapter_output")
    if (
        output["schema_version"] != "moe-phase7-adapter-output-v1"
        or output["evidence_class"] != "SYNTHETIC_CPU_MOCK"
        or output["formal_runtime_evidence"] is not False
        or output["gpu_used"] is not False
        or output["model_downloaded"] is not False
    ):
        raise AdapterContractError("adapter output authority boundary mismatch")
    validate_runtime_manifest(output["runtime_variant"])
    validate_pass_contracts(output["pass_contracts"])
    if output["v0"] != {
        "execution_role": "OFFLINE_VALIDATION",
        "gpu_execution": False,
        "runtime_pass": False,
        "status": "PASS",
        "findings": [],
    }:
        raise AdapterContractError("V0 role contract mismatch")
    # Canonical round-trip is deliberately strict; output is not accepted merely
    # because its embedded hash strings are syntactically valid.
    fixture = load_strict_json(PHASE7_ROOT / "fixtures" / "mock_vllm_trace.json")
    expected = adapt_fixture(fixture)
    if output != expected:
        raise AdapterContractError("adapter output replay mismatch")


def write_output(path: Path, value: dict[str, Any]) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-output", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.validate_output is not None:
        validate_output(load_strict_json(args.validate_output))
        print("PASS phase7 adapter output validation")
        return 0
    output = adapt_fixture(load_strict_json(args.input))
    if args.output is None:
        raise AdapterContractError("--output is required for conversion")
    if args.output.exists():
        raise AdapterContractError("output already exists")
    write_output(args.output, output)
    print(output["semantic_hashes"]["adapter_output_root"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AdapterContractError, ContractError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
