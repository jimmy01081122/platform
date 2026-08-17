#!/usr/bin/env python3
"""Verify native T0 or M0 traces and convert them to canonical MoE IR."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
ADAPTER = "canonical-moe-converter-v2"
M0_CONVERTER = "m0-qwen2moe-native-p2-to-canonical-v1"
COMMON_KEYS = {
    "schema_version", "fixture_id", "seed", "prompt_sha256",
    "token_sequence", "pass_id", "sequence", "event_id", "event_type",
    "timestamp_ns", "evidence",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_verified(capture_manifest: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    manifest = json.loads(capture_manifest.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "t0-capture-manifest-v1":
        raise ValueError("unsupported capture manifest schema")
    identity = manifest["identity"]
    events: list[dict[str, Any]] = []
    content_ids: list[str] = []
    for artifact in manifest.get("artifacts", []):
        path = capture_manifest.parent / artifact["path"]
        actual = sha256_file(path)
        if actual != artifact["sha256"]:
            raise ValueError(f"checksum mismatch: {artifact['path']}")
        content_ids.append(actual)
        previous = -1
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            event = json.loads(line)
            missing = COMMON_KEYS - event.keys()
            if missing:
                raise ValueError(f"{artifact['path']}:{line_number} missing keys {sorted(missing)}")
            if event["schema_version"] != "t0-fixture-native-v1":
                raise ValueError(f"{artifact['path']}:{line_number} unsupported native schema")
            if event["pass_id"] != artifact["pass_id"]:
                raise ValueError(f"{artifact['path']}:{line_number} pass mismatch")
            for key in ("fixture_id", "seed", "prompt_sha256", "token_sequence"):
                if event[key] != identity[key]:
                    raise ValueError(f"{artifact['path']}:{line_number} identity mismatch for {key}")
            if event["sequence"] <= previous:
                raise ValueError(f"{artifact['path']} native sequence is not strictly increasing")
            previous = event["sequence"]
            events.append(event)
    return identity, events, sorted(content_ids)


def canonicalize(capture_manifest: Path, routing_output: Path, system_output: Path) -> tuple[dict, dict]:
    identity, native, content_ids = load_verified(capture_manifest)
    provenance = {
        "adapter": ADAPTER,
        "source_format": "t0-fixture-native-v1",
        "source_content_ids": content_ids,
    }
    routing_events = [
        event for event in native
        if event["pass_id"] == "P2" and event["event_type"] == "routing_decision"
    ]
    system_events = [
        event for event in native
        if event["event_type"] in {
            "timeline_anchor", "dma_marker", "queue_marker", "residency_marker",
            "counter_unavailable_marker", "telemetry_sample", "rtl_reference_event",
        }
    ]
    routing = {
        "schema_version": "canonical-moe-ir-v1",
        "ir_kind": "moe-routing",
        "identity": identity,
        "provenance": provenance,
        "events": routing_events,
    }
    system = {
        "schema_version": "canonical-moe-ir-v1",
        "ir_kind": "system-events",
        "identity": identity,
        "provenance": provenance,
        "events": system_events,
    }
    write_json(routing_output, routing)
    write_json(system_output, system)
    return routing, system


def _jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: record must be an object")
        records.append(value)
    return records


def _m0_checksums(root: Path) -> dict[str, str]:
    document = json.loads((root / "checksums.json").read_text(encoding="utf-8"))
    if document.get("schema_version") != "m0-artifact-checksums-v1":
        raise ValueError("unsupported M0 checksum manifest")
    checksums = {item["path"]: item["sha256"] for item in document["files"]}
    for relative in ("p0/native.jsonl", "p2/native.jsonl", "p3/native.jsonl"):
        if sha256_file(root / relative) != checksums.get(relative):
            raise ValueError(f"checksum mismatch: {relative}")
    return checksums


def _measured_samples(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    samples = {
        item["request_id"]: item for item in records
        if item.get("event") == "sample" and item.get("run_kind") == "measured"
    }
    if len(samples) != 8:
        raise ValueError(f"expected GSM8K 4 + MMLU 4 measured samples, got {len(samples)}")
    if {item["benchmark"] for item in samples.values()} != {"gsm8k", "mmlu"}:
        raise ValueError("measured benchmark set must be GSM8K and MMLU")
    return samples


def _validate_schema(instance: dict[str, Any], schema_path: Path) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise ValueError("jsonschema is required for M0 canonicalization") from exc
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator_type = getattr(jsonschema, "Draft202012Validator", None)
    if validator_type is None:
        validator_type = getattr(jsonschema, "Draft7Validator", None)
    if validator_type is None:
        raise ValueError("installed jsonschema exposes no supported validator")
    errors = sorted(
        validator_type(schema).iter_errors(instance),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        rendered = "; ".join(
            f"{'/'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"schema validation failed: {rendered}")


def canonicalize_m0(
    artifact_root: Path,
    routing_output: Path,
    benchmark_records_output: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convert verified M0 measured P2 routes; never mutates native artifacts."""
    root = artifact_root.resolve()
    checksums = _m0_checksums(root)
    run_manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    consistency = json.loads(
        (root / "cross_pass_consistency.json").read_text(encoding="utf-8")
    )
    if consistency.get("status") != "pass" or consistency.get("mismatches"):
        raise ValueError("cross-pass consistency report is not clean")
    inventory = json.loads(
        (root / "model_snapshot_inventory.json").read_text(encoding="utf-8")
    )
    if run_manifest.get("model_snapshot_aggregate_sha256") != inventory.get("aggregate_sha256"):
        raise ValueError("model snapshot aggregate hash mismatch")
    if sha256_file(root / "model_snapshot_inventory.json") != checksums.get(
        "model_snapshot_inventory.json"
    ):
        raise ValueError("model snapshot inventory checksum mismatch")

    by_pass = {
        pass_id: _jsonl(root / pass_id.lower() / "native.jsonl")
        for pass_id in ("P0", "P2", "P3")
    }
    starts = {pass_id: records[0] for pass_id, records in by_pass.items()}
    for pass_id, start in starts.items():
        if start.get("event") != "pass_start" or start.get("pass") != pass_id:
            raise ValueError(f"{pass_id} does not start with a matching pass_start")
    model = starts["P2"]["provenance"]["model"]
    hardware = starts["P2"]["provenance"]["hardware"]
    generation = starts["P2"]["generation"]
    generation_hash = canonical_hash(generation)
    for pass_id, start in starts.items():
        provenance = start["provenance"]
        if provenance["model"] != model:
            raise ValueError(f"model provenance differs in {pass_id}")
        if start["generation"] != generation:
            raise ValueError(f"generation configuration differs in {pass_id}")
        if provenance["model"]["snapshot_aggregate_sha256"] != inventory["aggregate_sha256"]:
            raise ValueError(f"model hash differs in {pass_id}")

    samples_by_pass = {
        pass_id: _measured_samples(records) for pass_id, records in by_pass.items()
    }
    request_ids = set(samples_by_pass["P2"])
    if any(set(samples) != request_ids for samples in samples_by_pass.values()):
        raise ValueError("cross-pass measured request set mismatch")
    compare_fields = (
        "benchmark", "sample_id", "raw_sample_hash", "prompt_hash",
        "input_token_ids", "output_token_ids", "output_hash",
    )
    for request_id in sorted(request_ids):
        baseline = samples_by_pass["P0"][request_id]
        for pass_id in ("P2", "P3"):
            for field in compare_fields:
                if samples_by_pass[pass_id][request_id].get(field) != baseline.get(field):
                    raise ValueError(
                        f"cross-pass {field} mismatch for {request_id} in {pass_id}"
                    )
        if canonical_hash(baseline["output_token_ids"]) != baseline["output_hash"]:
            raise ValueError(f"output hash mismatch for {request_id}")

    mapping_document = json.loads(
        (root / "suite_class_mapping_v1.2.0.json").read_text(encoding="utf-8")
    )
    if (
        mapping_document.get("target_suite_revision") != "v1.2.0"
        or mapping_document.get("native_artifacts_unchanged") is not True
    ):
        raise ValueError("suite mapping is not the immutable v1.2.0 correction")
    mapping = {item["native_sample_id"]: item for item in mapping_document["samples"]}
    frozen_path = PACKAGE_ROOT / "configs/test_suites/frozen/v1.2.0/sample_manifest.jsonl"
    frozen = {item["raw_sample_hash"]: item for item in _jsonl(frozen_path)}
    for sample in samples_by_pass["P2"].values():
        corrected = mapping.get(sample["sample_id"])
        if not corrected or corrected["raw_sample_hash"] != sample["raw_sample_hash"]:
            raise ValueError(f"suite mapping mismatch for {sample['sample_id']}")
        suite_row = frozen.get(sample["raw_sample_hash"])
        if (
            not suite_row
            or suite_row["sample_id"] != corrected["v1_2_sample_id"]
            or suite_row["prompt_hash"] != sample["prompt_hash"]
            or suite_row["task_id"] != corrected["suite_class"]
        ):
            raise ValueError(f"dataset/sample/prompt hash mismatch for {sample['sample_id']}")
        expected_revision = starts["P2"]["provenance"]["dataset"]["selected"][
            sample["benchmark"]
        ][0]["dataset_revision"]
        if suite_row["source"]["dataset_revision"] != expected_revision:
            raise ValueError(f"dataset revision mismatch for {sample['sample_id']}")

    environment_hash = canonical_hash(hardware)
    model_revision = model["revision"]
    session_id = f"m0-{checksums['p2/native.jsonl'][:16]}"
    template_literals = {
        "gsm8k": "Solve the problem. Give the final numeric answer after ####.\\n\\n{question}",
        "mmlu": (
            "{question}\\nA. {choice_a}\\nB. {choice_b}\\nC. {choice_c}\\n"
            "D. {choice_d}\\nAnswer with one letter only."
        ),
    }
    benchmark_records: list[dict[str, Any]] = []
    sample_by_request = samples_by_pass["P2"]
    from collectors.trace_contract import (  # local import keeps standalone CLI simple
        build_alignment_key, validate_benchmark_trace_record,
    )
    for request_index, request_id in enumerate(sorted(request_ids)):
        sample = sample_by_request[request_id]
        corrected = mapping[sample["sample_id"]]
        tokens = {
            "prompt": len(sample["input_token_ids"]),
            "generated": len(sample["output_token_ids"]),
            "total": len(sample["input_token_ids"]) + len(sample["output_token_ids"]),
            "prompt_token_ids": sample["input_token_ids"],
            "generated_token_ids": sample["output_token_ids"],
            "token_ids_hash": canonical_hash({
                "prompt_token_ids": sample["input_token_ids"],
                "generated_token_ids": sample["output_token_ids"],
            }),
        }
        alignment = {
            "suite_id": "moe-trace-suite-v1.2.0",
            "sample_id": corrected["v1_2_sample_id"],
            "model_revision": model_revision,
            "generation_config_hash": generation_hash,
            "seed": generation["seed"],
            "request_id": request_id,
            "token_index": 0,
            "layer_index": 0,
            "repetition_index": sample["repetition"],
            "session_id": session_id,
        }
        alignment["alignment_key"] = build_alignment_key(alignment)
        native_path = "p2/native.jsonl"
        record = {
            "schema_version": "benchmark-trace-record-v1",
            "record_id": canonical_hash({"request_id": request_id, "pass": "P2"}),
            "model_id": model["repo_id"],
            "model_revision": model_revision,
            "weights_revision": model_revision,
            "tokenizer_revision": model_revision,
            "suite_id": "moe-trace-suite-v1.2.0",
            "benchmark_id": sample["benchmark"],
            "sample_id": corrected["v1_2_sample_id"],
            "template_id": f"{sample['benchmark']}-v1.2.0",
            "template_hash": canonical_hash({
                "literal": template_literals[sample["benchmark"]],
                "revision": "v1.2.0",
            }),
            "prompt_hash": sample["prompt_hash"],
            "generation_config": generation,
            "generation_config_hash": generation_hash,
            "actual_tokens": tokens,
            "output_hash": sample["output_hash"],
            "quality": {
                "status": "pass" if sample["quality"].get(
                    "validity", sample["quality"].get("valid")
                ) else "fail",
                "score": 1.0 if sample["quality"].get(
                    "correctness", sample["quality"].get("correct")
                ) else 0.0,
                "reason": "native M0 evaluator result",
            },
            "serving_runtime": "transformers-generate",
            "serving": starts["P2"]["provenance"]["runtime"],
            "hardware_id": hardware["cuda"]["name"],
            "hardware": hardware,
            "repetition_index": sample["repetition"],
            "request_index": request_index,
            "profiler_pass": "P2",
            "native_format": "m0-qwen2moe-routing-jsonl-v1",
            "native_paths": [native_path],
            "native_sha256": checksums[native_path],
            "native_checksums": {native_path: checksums[native_path]},
            "environment_hash": environment_hash,
            "alignment": alignment,
            "completeness": {"complete": True, "missing_fields": [], "truncated": False},
        }
        errors = validate_benchmark_trace_record(record)
        if errors:
            raise ValueError(f"benchmark record semantic validation failed: {errors}")
        _validate_schema(
            record, PACKAGE_ROOT / "schemas/benchmark_trace_record.schema.json"
        )
        benchmark_records.append(record)

    route_calls = [
        item for item in by_pass["P2"]
        if item.get("event") == "routing" and item.get("request_id") in request_ids
    ]
    for call in route_calls:
        row_count = len(call.get("token_positions", []))
        if (
            call.get("shape_sanity", {}).get("valid") is not True
            or call.get("router_shape", [None, None])[1] != model["expected_num_experts"]
            or len(call.get("top_k_experts", [])) != row_count
            or len(call.get("top_k_scores", [])) != row_count
            or any(len(row) != model["expected_top_k"] for row in call["top_k_experts"])
            or any(len(row) != model["expected_top_k"] for row in call["top_k_scores"])
        ):
            raise ValueError(f"invalid P2 routing shape for {call.get('request_id')}")
    tensors = starts["P2"]["loading"]["compatibility_remap"]["tensors"]
    gate = next(item for item in tensors if item["projection"] == "gate_proj")
    down = next(item for item in tensors if item["projection"] == "down_proj")
    expert_intermediate_size, hidden_size = gate["shape"]
    if down["shape"] != [hidden_size, expert_intermediate_size]:
        raise ValueError("expert projection dimensions are inconsistent")
    events: list[dict[str, Any]] = []
    for call in route_calls:
        sample = sample_by_request[call["request_id"]]
        corrected = mapping[sample["sample_id"]]
        all_token_ids = sample["input_token_ids"] + sample["output_token_ids"]
        rows = zip(
            call["token_positions"], call["top_k_experts"], call["top_k_scores"]
        )
        for token_position, experts, scores in rows:
            events.append({
                "event_id": f"m0-route-{len(events):06d}",
                "event_type": "routing_decision",
                "sequence": len(events),
                "request_id": call["request_id"],
                "benchmark_id": sample["benchmark"],
                "suite_class": corrected["suite_class"],
                "sample_id": corrected["v1_2_sample_id"],
                "raw_sample_hash": sample["raw_sample_hash"],
                "prompt_hash": sample["prompt_hash"],
                "generation_config_hash": generation_hash,
                "output_hash": sample["output_hash"],
                "phase": call["phase"],
                "call_index": call["call_index"],
                "layer_index": call["layer"],
                "token_index": token_position,
                "token_id": (
                    all_token_ids[token_position]
                    if token_position < len(all_token_ids) else None
                ),
                "selected_experts": experts,
                "selected_scores": scores,
                "routing_semantics": "reconstructed_topk_from_gate_logits",
                "actual_dispatch_verified": False,
                "drop_overflow_unavailable": True,
                "evidence": {
                    "class": "measured_gpu_gate_logits_reconstructed_topk",
                    "source_pass": "P2",
                    "synthetic": False,
                    "hardware": hardware["cuda"]["name"],
                },
            })
    identity = {
        "session_id": session_id,
        "suite_id": "moe-trace-suite",
        "suite_revision": "v1.2.0",
        "model_id": model["repo_id"],
        "model_revision": model_revision,
        "generation_config_hash": generation_hash,
        "seed": generation["seed"],
        "hardware_id": hardware["cuda"]["name"],
        "request_count": len(request_ids),
    }
    routing = {
        "schema_version": "canonical-moe-ir-v1",
        "ir_kind": "moe-routing",
        "identity": identity,
        "provenance": {
            "converter": M0_CONVERTER,
            "source_format": "m0-executable-p2-native-jsonl",
            "source_content_ids": [checksums["p2/native.jsonl"]],
            "benchmark_record_schema": "benchmark-trace-record-v1",
            "cross_pass_consistency_sha256": checksums["cross_pass_consistency.json"],
            "suite_mapping_sha256": checksums["suite_class_mapping_v1.2.0.json"],
            "model_snapshot_aggregate_sha256": inventory["aggregate_sha256"],
            "hardware": hardware,
            "model_dimensions": {
                "num_layers": model["expected_num_layers"],
                "num_experts": model["expected_num_experts"],
                "top_k": model["expected_top_k"],
                "hidden_size": hidden_size,
                "expert_intermediate_size": expert_intermediate_size,
                "dtype": model["dtype"],
            },
        },
        "events": events,
    }
    _validate_schema(routing, PACKAGE_ROOT / "schemas/canonical_moe_ir.schema.json")
    write_json(routing_output, routing)
    benchmark_records_output.parent.mkdir(parents=True, exist_ok=True)
    benchmark_records_output.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in benchmark_records),
        encoding="utf-8",
    )
    return routing, benchmark_records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--capture-manifest", type=Path)
    source.add_argument("--m0-root", type=Path)
    parser.add_argument("--routing-output", required=True, type=Path)
    parser.add_argument("--system-output", type=Path)
    parser.add_argument("--benchmark-records-output", type=Path)
    args = parser.parse_args()
    if args.m0_root:
        if not args.benchmark_records_output:
            parser.error("--m0-root requires --benchmark-records-output")
        canonicalize_m0(
            args.m0_root, args.routing_output, args.benchmark_records_output
        )
    else:
        if not args.system_output:
            parser.error("--capture-manifest requires --system-output")
        canonicalize(args.capture_manifest, args.routing_output, args.system_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
