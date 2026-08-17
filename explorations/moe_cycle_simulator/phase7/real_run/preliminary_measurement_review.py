#!/usr/bin/env python3
"""Preliminary reasonableness/correctness review for Phase 7 GPU raw evidence.

The reviewer is intentionally read-only: it emits a report and never edits raw
artifacts.  A PASS here is a prerequisite for adopting a batch; it is not a
replacement for the family-specific audit or held-out validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any


REVIEW_REVISION = "preliminary-review-v6"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0


def finite_median(values: list[Any]) -> float | None:
    usable = [float(value) for value in values if finite_positive(value)]
    return statistics.median(usable) if usable else None


def token_digest(values: list[Any]) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate percentile of empty values")
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return float(ordered[index])


class Review:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []

    def check(self, name: str, condition: bool, observed: Any, expected: Any, *, critical: bool = True) -> None:
        row = {"name": name, "status": "PASS" if condition else "FAIL", "observed": observed, "expected": expected, "critical": critical}
        self.checks.append(row)
        if not condition and not critical:
            self.warnings.append(row)

    def warn(self, name: str, observed: Any, expected: Any) -> None:
        row = {"name": name, "status": "WARNING", "observed": observed, "expected": expected, "critical": False}
        self.checks.append(row)
        self.warnings.append(row)

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [row for row in self.checks if row["status"] == "FAIL" and row["critical"]]


def expected_component_ids() -> set[str]:
    ids = {f"CMP-A0-{value}" for value in (128, 2048, 8192, 16384)}
    ids |= {f"CMP-A1-{value}" for value in (512, 4096, 12288, 28672)}
    ids |= {f"CMP-A2-{context}x{batch}" for context in (512, 4096, 16384) for batch in (1, 4)}
    ids |= {f"CMP-A3-{context}x{batch}" for context in (2048, 8192, 28672) for batch in (2, 8)}
    ids |= {f"CMP-M0-{value}" for value in (1, 4, 16, 64, 256)}
    ids |= {f"CMP-M1-{value}" for value in (2, 8, 32, 128)}
    ids |= {f"CMP-M2-{value}" for value in (1, 4, 16, 64, 256)}
    ids |= {f"CMP-M3-{value}" for value in ("p10", "p50", "p90", "max")}
    ids |= {f"CMP-L0-{value}" for value in (1, 2, 4, 8, 16)}
    ids |= {f"CMP-L1-{value}" for value in (1, 2, 4)}
    ids |= {f"CMP-L2-{value}" for value in (1, 2, 4, 8)}
    ids |= {f"CMP-L3-{value}" for value in (0, 1000000, 5000000, 20000000)}
    return ids


def expected_ids_for_families(families: set[str]) -> set[str]:
    expected = expected_component_ids()
    prefixes = tuple(f"CMP-{family.removeprefix('CMP-')}" for family in families)
    return {point_id for point_id in expected if point_id.startswith(prefixes)}


def expected_transfer_ids(groups: set[str]) -> set[str]:
    expected: set[str] = set()
    if "L" in groups:
        expected |= ({f"XFER-L0-{label}-{direction}" for label in ("4KiB", "1MiB", "16MiB", "E", "4E") for direction in ("H2D", "D2H")} |
                     {f"XFER-L1-{label}-{direction}" for label in ("64KiB", "4MiB", "0.5E", "2E") for direction in ("H2D", "D2H")} |
                     {f"XFER-L2-{label}-{direction}" for label in ("4KiB", "1MiB", "16MiB", "E", "4E") for direction in ("H2D", "D2H")} |
                     {f"XFER-L3-{label}-{direction}" for label in ("E", "2E") for direction in ("H2D", "D2H")})
    if "E" in groups:
        expected |= ({f"XFER-E1-{count}-{direction}" for count in (2, 4) for direction in ("H2D", "D2H")} |
                     {f"XFER-E2-{repeat}-{direction}" for repeat in (1, 2, 4) for direction in ("H2D", "D2H")} |
                     {f"XFER-E3-{direction}" for direction in ("H2D", "D2H")})
    if "Q" in groups:
        expected |= {f"XFER-Q0-{depth}-H2D" for depth in (1, 2, 4, 8)}
        expected |= {f"XFER-Q1-{concurrency}-H2D" for concurrency in (1, 2, 4)}
    if "O" in groups:
        expected |= {f"XFER-O0-{label}-H2D" for label in ("E", "2E")}
        expected |= {f"XFER-O1-{occupancy}" for occupancy in ("low", "high")}
        expected |= {f"XFER-O2-{occupancy}" for occupancy in ("low", "high")}
        expected |= {f"XFER-O3-{label}" for label in ("symmetric-E", "asymmetric-2E")}
    return expected


def review_catalog(path: Path, review: Review, *, require_runtime_semantics: bool = False) -> None:
    catalog = read_json(path)
    objects = catalog.get("objects", {})
    schema = catalog.get("schema_version")
    allowed_schemas = {"phase7-expert-catalog-v1", "phase7-expert-catalog-v2"}
    review.check("catalog.schema", schema in allowed_schemas, schema, sorted(allowed_schemas))
    if require_runtime_semantics:
        review.check("catalog.runtime_schema", schema == "phase7-expert-catalog-v2", schema, "phase7-expert-catalog-v2")
    review.check("catalog.object_count", len(objects) == 256, len(objects), 256)
    bad_objects = []
    bad_runtime_objects = []
    for object_id, object_entry in objects.items():
        if isinstance(object_entry, list):
            object_meta: dict[str, Any] = {}
            tensors = object_entry
        elif isinstance(object_entry, dict):
            object_meta = object_entry
            tensors = object_entry.get("tensors") or []
        else:
            object_meta = {}
            tensors = []
        names = {tensor.get("tensor_name") for tensor in tensors}
        if len(tensors) != 3 or names != {"w1.weight", "w2.weight", "w3.weight"}:
            bad_objects.append(object_id)
        for tensor in tensors:
            if not finite_positive(tensor.get("bytes")) or not tensor.get("shard") or not tensor.get("shape"):
                bad_objects.append(object_id)
        if require_runtime_semantics:
            tensor_materialized = [tensor.get("materialized_bytes") for tensor in tensors]
            tensor_packed = [tensor.get("packed_bytes") for tensor in tensors]
            tensor_aligned = [tensor.get("aligned_bytes") for tensor in tensors]
            ownership = object_meta.get("ownership") or {}
            valid_runtime = (
                object_meta.get("object_granularity") == "layer_expert_three_tensor_bundle"
                and isinstance(ownership, dict)
                and all(ownership.get(key) for key in ("checkpoint_owner", "host_owner", "device_owner"))
                and object_meta.get("mutable") is False
                and object_meta.get("writeback_allowed") is False
                and object_meta.get("eviction_semantics") == "RELEASE_DISCARD_AND_RELOAD"
                and len(tensors) == 3
                and all(finite_positive(value) for value in tensor_materialized + tensor_packed + tensor_aligned)
                and all(int(aligned) >= int(materialized) for aligned, materialized in zip(tensor_aligned, tensor_materialized))
                and object_meta.get("materialized_bytes") == sum(int(value) for value in tensor_materialized if finite_positive(value))
                and object_meta.get("packed_bytes") == sum(int(value) for value in tensor_packed if finite_positive(value))
                and object_meta.get("aligned_bytes") == sum(int(value) for value in tensor_aligned if finite_positive(value))
            )
            if not valid_runtime:
                bad_runtime_objects.append(object_id)
    review.check("catalog.each_object_has_w1_w2_w3_and_bytes", not bad_objects, bad_objects[:20], "empty")
    if require_runtime_semantics:
        review.check("catalog.runtime_object_semantics", not bad_runtime_objects, bad_runtime_objects[:20], "all objects have materialized/packed/aligned bytes, granularity, ownership, immutable no-writeback semantics")
        review.check("catalog.immutable_eviction_not_d2h", catalog.get("immutable_weight_eviction_semantics") == "release/discard device allocation and reload from immutable checkpoint-backed host source; no D2H writeback", catalog.get("immutable_weight_eviction_semantics"), "release/discard and reload; no D2H writeback")


def review_component(component_dir: Path, review: Review) -> None:
    manifest = read_json(component_dir / "manifest.json")
    rows = read_jsonl(component_dir / "measurements.jsonl")
    review.check("component.manifest_status", manifest.get("status") == "PASS", manifest.get("status"), "PASS")
    review.check("component.measurement_class", manifest.get("measurement_class") == "CHECKPOINT_BACKED_COMPONENT_PROBE", manifest.get("measurement_class"), "CHECKPOINT_BACKED_COMPONENT_PROBE")
    review.check("component.not_end_to_end_flag", manifest.get("not_end_to_end_model_generation") is True, manifest.get("not_end_to_end_model_generation"), True)
    dims = manifest.get("config_dimensions", {})
    review.check("component.model_dimensions", (dims.get("num_hidden_layers"), dims.get("num_local_experts"), dims.get("num_experts_per_tok")) == (32, 8, 2), dims, "32/8/2")
    families = {str(row.get("family")) for row in rows}
    expected_ids = expected_ids_for_families(families - {"CMP-A"})
    if "CMP-A" in families and manifest.get("schema_version") == "phase7-component-probe-v2":
        attention_ids = manifest.get("attention_point_ids") or []
        review.check("component.attention_coordinate_schema", manifest.get("attention_coordinate_schema") == "query/chunk length x past-KV length x active sequences/batch x phase x KV fragmentation", manifest.get("attention_coordinate_schema"), "five-dimensional attention coordinate")
        expected_ids |= {str(point_id) for point_id in attention_ids}
    elif "CMP-A" in families:
        expected_ids |= expected_ids_for_families({"CMP-A"})
    review.check("component.expected_point_set", {row.get("id") for row in rows} == expected_ids, sorted({row.get("id") for row in rows} ^ expected_ids), f"exact matrix for families {sorted(families)}")
    review.check("component.no_duplicate_ids", len({row.get("id") for row in rows}) == len(rows), len(rows), "unique")
    for row in rows:
        point_id = row.get("id")
        samples = row.get("samples") or []
        duration = row.get("gpu_duration_ns") or {}
        review.check(f"{point_id}.repetition_count", 10 <= len(samples) <= 30, len(samples), "10..30")
        review.check(f"{point_id}.duration_summary", finite_positive(duration.get("median")) and finite_positive(duration.get("min")) and finite_positive(duration.get("max")), duration, "finite positive")
        review.check(f"{point_id}.profiler_canary", (row.get("profiler_canary") or {}).get("status") == "PASS", (row.get("profiler_canary") or {}).get("status"), "PASS")
        review.check(f"{point_id}.known_bytes", finite_positive(row.get("input_bytes", row.get("query_input_bytes", 1))) and finite_positive(row.get("weight_bytes", 1)), {key: row.get(key) for key in ("input_bytes", "query_input_bytes", "kv_cache_bytes", "weight_bytes", "expert_weight_bytes", "router_weight_bytes")}, "positive known bytes")
        bad_nvml = [sample.get("nvml") for sample in samples if not isinstance(sample.get("nvml"), dict) or "error" in sample.get("nvml", {})]
        review.check(f"{point_id}.nvml_completeness", not bad_nvml, len(bad_nvml), 0)
        bad_samples = [sample.get("repetition") for sample in samples if not finite_positive(sample.get("gpu_duration_ns")) or not finite_positive(sample.get("cpu_after_sync_ns"))]
        review.check(f"{point_id}.sample_finiteness", not bad_samples, bad_samples[:10], "all finite")
        if row.get("family") == "CMP-M":
            details = row.get("routing_details_last_repetition") or {}
            expected_active = 2 if point_id.startswith("CMP-M2-") else 8
            review.check(f"{point_id}.controlled_active_experts", details.get("controlled_active_expert_count") == expected_active, details.get("controlled_active_expert_count"), expected_active)
            counts = details.get("controlled_route_counts") or []
            review.check(f"{point_id}.route_count_shape", len(counts) == 8 and all(isinstance(value, int) and value > 0 for value in counts[:expected_active]) and all(value == 0 for value in counts[expected_active:]) if point_id.startswith("CMP-M2-") else len(counts) == 8 and all(isinstance(value, int) and value > 0 for value in counts), counts, "8 expert route-count entries")
            review.check(f"{point_id}.router_entropy_finite", finite_positive(details.get("router_entropy_mean")), details.get("router_entropy_mean"), "positive finite")
        if row.get("family") == "CMP-A" and manifest.get("schema_version") == "phase7-component-probe-v2":
            coordinate = row.get("attention_coordinate") or {}
            fragmentation = coordinate.get("kv_fragmentation") or {}
            coordinate_valid = (
                finite_positive(coordinate.get("query_length"))
                and coordinate.get("chunk_length") == coordinate.get("query_length")
                and isinstance(coordinate.get("past_kv_length"), int)
                and coordinate.get("past_kv_length") >= 0
                and finite_positive(coordinate.get("active_sequences"))
                and coordinate.get("batch_size") == coordinate.get("active_sequences")
                and coordinate.get("phase") in {"prefill", "chunked-prefill", "decode"}
                and fragmentation.get("mode") in {"contiguous", "segmented-16"}
                and finite_positive(fragmentation.get("fragment_count"))
            )
            review.check(f"{point_id}.attention_coordinate", coordinate_valid, coordinate, "query/chunk x past-KV x active sequences/batch x phase x KV fragmentation")
            review.check(f"{point_id}.isolated_claim_boundary", row.get("not_vllm_fused_kernel") is True and row.get("model_bound_claim_allowed") is False, {"not_vllm_fused_kernel": row.get("not_vllm_fused_kernel"), "model_bound_claim_allowed": row.get("model_bound_claim_allowed")}, "isolated shape evidence only until correlation gate")
            if row.get("fused_path_correlation_status") != "PASS":
                review.warn(f"{point_id}.fused_path_correlation", row.get("fused_path_correlation_status"), "PENDING is allowed only for isolated shape evidence; fused-path calibration remains blocked")
    medians = {row["id"]: float(row["gpu_duration_ns"]["median"]) for row in rows if finite_positive((row.get("gpu_duration_ns") or {}).get("median"))}
    if "CMP-A" in families and manifest.get("schema_version") == "phase7-component-probe-v2":
        fit_rows = [row for row in rows if row.get("family") == "CMP-A" and "fit" in str(row.get("role")) and "held-out" not in str(row.get("role"))]
        held_out_rows = [row for row in rows if row.get("family") == "CMP-A" and "held-out" in str(row.get("role"))]
        outside_envelope = []
        for held_out in held_out_rows:
            coordinate = held_out.get("attention_coordinate") or {}
            fragmentation = (coordinate.get("kv_fragmentation") or {}).get("mode")
            candidates = [
                row for row in fit_rows
                if (row.get("attention_coordinate") or {}).get("phase") == coordinate.get("phase")
                and ((row.get("attention_coordinate") or {}).get("kv_fragmentation") or {}).get("mode") == fragmentation
            ]
            bounded = bool(candidates)
            for key in ("query_length", "past_kv_length", "active_sequences"):
                values = [int((row.get("attention_coordinate") or {}).get(key)) for row in candidates if isinstance((row.get("attention_coordinate") or {}).get(key), int)]
                target = coordinate.get(key)
                if not values or not isinstance(target, int) or not (min(values) <= target <= max(values)):
                    bounded = False
            if not bounded:
                outside_envelope.append({"id": held_out.get("id"), "coordinate": coordinate, "candidate_fit_ids": [row.get("id") for row in candidates]})
        review.check("component.attention_held_out_envelope", not outside_envelope, outside_envelope, "every held-out coordinate is bounded by same-phase/same-fragmentation fit anchors on query, past-KV and active-sequence axes")
    if medians:
        review.warn("component.cross_point_duration_spread", {"min": min(medians.values()), "max": max(medians.values())}, "spread is reported for review; no monotonicity assumption forced")
    review_catalog(component_dir / "expert_catalog.json", review)


def review_k(k_batch: Path, runs_root: Path, review: Review) -> None:
    status = read_json(k_batch / "status.json")
    progress = __import__("csv").DictReader((k_batch / "progress.tsv").open("r", encoding="utf-8"), delimiter="\t")
    rows = list(progress)
    review.check("k.batch_status", status.get("status") == "PASS", status.get("status"), "PASS")
    review.check("k.batch_count", len(rows) == 12 and status.get("completed_runs") == 12, {"progress": len(rows), "completed": status.get("completed_runs")}, "12")
    for row in rows:
        run = runs_root / Path(row["run_dir"]).name
        review.check(f"{row['experiment_id']}.run_exists", run.is_dir(), str(run), "directory")
        if not run.is_dir():
            continue
        run_status = read_json(run / "status.json")
        requests = read_jsonl(run / "requests.jsonl")
        review.check(f"{row['experiment_id']}.status", run_status.get("status") == "PASS", run_status.get("status"), "PASS")
        review.check(f"{row['experiment_id']}.one_request", len(requests) == 1, len(requests), 1)
        if len(requests) != 1:
            continue
        request = requests[0]
        profiler = request.get("profiler") or {}
        checks = {
            "kernel_event_count": profiler.get("kernel_event_count"),
            "model_kernel_event_count": profiler.get("model_kernel_event_count"),
            "prefill_marker_count": profiler.get("prefill_marker_count"),
            "decode_marker_count": profiler.get("decode_marker_count"),
            "trace_event_count": profiler.get("trace_event_count"),
            "correlation_event_count": profiler.get("correlation_event_count"),
        }
        review.check(f"{row['experiment_id']}.profiler_counts", all(finite_positive(value) for value in checks.values()), checks, "all positive")
        review.check(f"{row['experiment_id']}.stream_ids", bool(profiler.get("stream_ids")), profiler.get("stream_ids"), "non-empty")
        review.check(f"{row['experiment_id']}.sampling_mode", request.get("sampling_mode") == "FORCED_LENGTH_CONTROLLED", request.get("sampling_mode"), "FORCED_LENGTH_CONTROLLED")
        review.check(f"{row['experiment_id']}.finish_reason", request.get("finish_reason") == "length", request.get("finish_reason"), "length")


def review_model_run(run_dir: Path, review: Review) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    status = read_json(run_dir / "status.json")
    model = read_json(run_dir / "model_identity.json")
    requests = read_jsonl(run_dir / "requests.jsonl")
    memory = read_jsonl(run_dir / "memory.jsonl")
    experiment_id = str(manifest.get("experiment_id") or run_dir.name)
    input_tokens = manifest.get("input_tokens_requested")
    output_tokens = manifest.get("output_tokens_requested")
    review.check(f"{experiment_id}.status", status.get("status") == "PASS" and status.get("phase") == "complete", status, "PASS/complete")
    review.check(f"{experiment_id}.model_identity", manifest.get("model_revision") == "eba92302a2861cdc0098cc54bc9f17cb2c47eb61" and model.get("model_revision") == manifest.get("model_revision") and model.get("safetensor_shard_count") == 19, {"manifest_revision": manifest.get("model_revision"), "identity_revision": model.get("model_revision"), "shards": model.get("safetensor_shard_count")}, "frozen Mixtral revision and 19 shards")
    runtime_class = str(manifest.get("runtime_class"))
    review.check(f"{experiment_id}.runtime_contract", runtime_class in {"CLEAN", "ROUTING", "KERNEL_PROFILE", "MEMORY_PROFILE", "TELEMETRY"} and manifest.get("cpu_offload_gb") == 0 and manifest.get("quantization") is None and manifest.get("sampling_mode") == "FORCED_LENGTH_CONTROLLED", {key: manifest.get(key) for key in ("runtime_class", "cpu_offload_gb", "quantization", "sampling_mode")}, "canonical instrument class/BF16/no-offload forced-length")
    review.check(f"{experiment_id}.length_envelope", isinstance(input_tokens, int) and isinstance(output_tokens, int) and input_tokens > 0 and output_tokens > 0 and input_tokens + output_tokens <= 32768, {"input": input_tokens, "output": output_tokens}, "positive and total <= 32768")
    warmup_count = int(status.get("warmup_count", -1))
    measured_count = int(status.get("measured_count", -1))
    review.check(f"{experiment_id}.request_count", len(requests) == warmup_count + measured_count == status.get("total_completed_requests"), {"rows": len(requests), "warmup": warmup_count, "measured": measured_count, "completed": status.get("total_completed_requests")}, "rows = warmup + measured = completed")
    bad_requests = []
    measured_durations = []
    measured_identity = []
    bad_routing = []
    for index, request in enumerate(requests):
        valid = (
            request.get("input_token_count") == input_tokens
            and request.get("output_token_count") == output_tokens
            and len(request.get("input_token_ids") or []) == input_tokens
            and len(request.get("output_token_ids") or []) == output_tokens
            and request.get("finish_reason") == "length"
            and request.get("sampling_mode") == "FORCED_LENGTH_CONTROLLED"
            and finite_positive(request.get("wall_duration_ns"))
        )
        if not valid:
            bad_requests.append(index)
        if request.get("repetition_role") == "measured" and finite_positive(request.get("wall_duration_ns")):
            measured_durations.append(float(request["wall_duration_ns"]))
            measured_identity.append({
                "repetition_index": request.get("repetition_index"),
                "input_sha256": token_digest(request.get("input_token_ids") or []),
                "output_sha256": token_digest(request.get("output_token_ids") or []),
                "wall_duration_ns": float(request["wall_duration_ns"]),
            })
        if runtime_class == "ROUTING":
            routing = request.get("routing") or {}
            shape = routing.get("shape") or []
            route_path = run_dir / str(routing.get("array_path") or "")
            route_valid = (
                routing.get("validation_status") == "PASS"
                and shape == [int(input_tokens) + int(output_tokens) - 1, 32, 2]
                and routing.get("minimum_expert_id") == 0
                and routing.get("maximum_expert_id") == 7
                and route_path.is_file()
            )
            if not route_valid:
                bad_routing.append(index)
    review.check(f"{experiment_id}.request_semantics", not bad_requests, bad_requests, "all token counts/IDs, finish reason, sampling and durations valid")
    review.check(f"{experiment_id}.measured_repetitions", len(measured_durations) == measured_count and measured_count >= 3, len(measured_durations), ">=3 measured rows")
    if runtime_class == "ROUTING":
        review.check(f"{experiment_id}.routing_conservation", not bad_routing, bad_routing, "every request has [input+output-1,32,2], expert IDs 0..7 and preserved raw array")
    bad_memory = [row.get("label") for row in memory if row.get("cuda_available") is not True or not finite_positive(row.get("cuda_total_bytes"))]
    review.check(f"{experiment_id}.memory_capture", bool(memory) and not bad_memory and (run_dir / "nvidia-smi-post-load.json").is_file() and (run_dir / "nvidia-smi-post-request.json").is_file(), {"rows": len(memory), "bad": bad_memory}, "CUDA allocator and process-aware snapshots present")
    review.check(f"{experiment_id}.raw_completeness", all((run_dir / name).is_file() for name in ("environment.json", "resolved_runtime.json", "requested_engine_args.json", "result.json", "SHA256SUMS")), str(run_dir), "required raw files present")
    return {"experiment_id": experiment_id, "runtime_class": runtime_class, "input_tokens": input_tokens, "output_tokens": output_tokens, "median_wall_duration_ns": finite_median(measured_durations), "measured_identity": measured_identity}


def review_model_run_trends(summaries: list[dict[str, Any]], review: Review) -> None:
    comparable = sorted((row for row in summaries if finite_positive(row.get("input_tokens")) and finite_positive(row.get("median_wall_duration_ns"))), key=lambda row: int(row["input_tokens"]))
    if len(comparable) < 2:
        return
    bad_pairs = []
    for left, right in zip(comparable, comparable[1:]):
        if int(right["input_tokens"]) > int(left["input_tokens"]) and float(right["median_wall_duration_ns"]) < 0.5 * float(left["median_wall_duration_ns"]):
            bad_pairs.append({"left": left, "right": right})
    review.check("model_run.cross_anchor_physical_sanity", not bad_pairs, bad_pairs, "larger context must not be implausibly faster than adjacent anchor")
    review.warn("model_run.cross_anchor_latency", comparable, "reported for envelope review; no monotonic fit assumption forced")


def review_instrumentation_pairs(summaries: list[dict[str, Any]], review: Review) -> None:
    groups: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
    for row in summaries:
        if isinstance(row.get("input_tokens"), int) and isinstance(row.get("output_tokens"), int):
            groups.setdefault((int(row["input_tokens"]), int(row["output_tokens"])), {})[str(row.get("runtime_class"))] = row
    for (input_tokens, output_tokens), variants in sorted(groups.items()):
        if "ROUTING" not in variants:
            continue
        pair_id = f"PAIR-{input_tokens}x{output_tokens}-CLEAN-vs-ROUTING"
        clean = variants.get("CLEAN")
        routing = variants["ROUTING"]
        review.check(f"{pair_id}.clean_present", clean is not None, sorted(variants), "CLEAN and ROUTING")
        if clean is None:
            continue
        clean_rows = {row["repetition_index"]: row for row in clean.get("measured_identity") or []}
        routing_rows = {row["repetition_index"]: row for row in routing.get("measured_identity") or []}
        common = sorted(set(clean_rows) & set(routing_rows), key=str)
        identity_failures = [index for index in common if clean_rows[index]["input_sha256"] != routing_rows[index]["input_sha256"] or clean_rows[index]["output_sha256"] != routing_rows[index]["output_sha256"]]
        review.check(f"{pair_id}.token_equivalence", bool(common) and not identity_failures and len(clean_rows) == len(routing_rows) == len(common), {"common": len(common), "clean": len(clean_rows), "routing": len(routing_rows), "failures": identity_failures}, "exact input/output identity for every measured repetition")
        review.check(f"{pair_id}.distribution_sample_count", len(common) >= 10, len(common), ">=10 paired measured repetitions")
        if not common:
            continue
        clean_values = [float(clean_rows[index]["wall_duration_ns"]) for index in common]
        routing_values = [float(routing_rows[index]["wall_duration_ns"]) for index in common]
        clean_median = statistics.median(clean_values)
        routing_median = statistics.median(routing_values)
        clean_p95 = percentile(clean_values, 0.95)
        routing_p95 = percentile(routing_values, 0.95)
        mad = statistics.median([abs(value - clean_median) for value in clean_values])
        robust_noise_fraction = (1.4826 * mad / clean_median) if clean_median else float("inf")
        median_limit = max(0.05, 2.0 * robust_noise_fraction)
        p95_limit = max(0.10, 3.0 * robust_noise_fraction)
        median_shift = abs(routing_median / clean_median - 1.0) if clean_median else float("inf")
        p95_shift = abs(routing_p95 / clean_p95 - 1.0) if clean_p95 else float("inf")
        observed = {"clean_median_ns": clean_median, "routing_median_ns": routing_median, "median_shift_fraction": median_shift, "median_limit_fraction": median_limit, "clean_p95_ns": clean_p95, "routing_p95_ns": routing_p95, "p95_shift_fraction": p95_shift, "p95_limit_fraction": p95_limit, "clean_robust_noise_fraction": robust_noise_fraction}
        review.check(f"{pair_id}.latency_distribution_perturbation", median_shift <= median_limit and p95_shift <= p95_limit, observed, "median and p95 shift within preregistered clean-noise limits")


def review_transfer(transfer_dir: Path, review: Review) -> None:
    manifest = read_json(transfer_dir / "manifest.json")
    rows = read_jsonl(transfer_dir / "measurements.jsonl")
    groups = {str(group) for group in manifest.get("groups", [])}
    review.check("transfer.groups_known", groups <= {"L", "E", "Q", "O"}, sorted(groups), "subset of L/E/Q/O")
    review.check("transfer.manifest_status", manifest.get("status") == "PASS", manifest.get("status"), "PASS")
    review.check("transfer.measurement_class", manifest.get("measurement_class") == "GPU_TRANSFER_PROBE", manifest.get("measurement_class"), "GPU_TRANSFER_PROBE")
    review.check("transfer.object_E_bytes", finite_positive(manifest.get("object_bytes_E")), manifest.get("object_bytes_E"), "positive bytes")
    expected = expected_transfer_ids(groups)
    actual = {row.get("id") for row in rows}
    review.check("transfer.requested_matrix", actual == expected, sorted(actual ^ expected), f"complete matrix for groups {sorted(groups)}")
    review.check("transfer.topology_artifact", (transfer_dir / "topology.json").is_file(), str(transfer_dir / "topology.json"), "file")
    for row in rows:
        point_id = row.get("id")
        family = str(row.get("family"))
        if row.get("status") == "NOT_RUN_UNAVAILABLE_ALLOCATOR":
            review.check(f"{point_id}.unavailable_is_topology_variant", point_id.startswith("XFER-L3-"), row.get("status"), "only XFER-L3 may be unavailable")
            continue
        samples = row.get("samples") or []
        review.check(f"{point_id}.status", row.get("status") == "PASS", row.get("status"), "PASS")
        review.check(f"{point_id}.repetitions", 10 <= len(samples) <= 30 and row.get("measured_repetition_count") == len(samples), len(samples), "10..30")
        review.check(f"{point_id}.direction", row.get("direction") in {"H2D", "D2H", "H2D+D2H"}, row.get("direction"), "H2D, D2H, or H2D+D2H")
        if family in {"XFER-O1", "XFER-O2"}:
            bad_overlap = [sample.get("repetition") for sample in samples if not all(finite_positive(sample.get(field)) for field in ("copy_duration_ns", "compute_duration_ns", "overlap_wall_ns"))]
            review.check(f"{point_id}.overlap_duration_finiteness", not bad_overlap, bad_overlap[:10], "positive finite copy/compute/wall durations")
            review.check(f"{point_id}.overlap_bytes", all(sample.get("requested_bytes") == row.get("requested_bytes") for sample in samples), [sample.get("repetition") for sample in samples if sample.get("requested_bytes") != row.get("requested_bytes")], "requested bytes conserved")
            review.warn(f"{point_id}.overlap_relation", {"wall_median_ns": (row.get("wall_gpu_duration_ns") or {}).get("median"), "copy_median_ns": finite_median([sample.get("copy_duration_ns") for sample in samples]), "compute_median_ns": finite_median([sample.get("compute_duration_ns") for sample in samples])}, "interpret with stream/event ordering; do not force additive latency")
        else:
            bad_bytes = [sample.get("repetition") for sample in samples if sample.get("completed_bytes") != row.get("requested_bytes")]
            review.check(f"{point_id}.byte_conservation", not bad_bytes, bad_bytes[:10], "completed=requested for every sample")
            bad_duration = [sample.get("repetition") for sample in samples if not finite_positive(sample.get("gpu_duration_ns"))]
            review.check(f"{point_id}.duration_finiteness", not bad_duration, bad_duration[:10], "positive finite")
        bad_nvml = [sample.get("repetition") for sample in samples if not isinstance(sample.get("nvml"), dict) or "error" in sample.get("nvml", {})]
        review.check(f"{point_id}.nvml_completeness", not bad_nvml, bad_nvml[:10], "no NVML errors")
        profiler = row.get("profiler_canary") or {}
        events = profiler.get("events") or []
        copy_event = any(any(token in str(event.get("key", "")).lower() for token in ("copy", "memcpy", "to_device")) for event in events)
        review.check(f"{point_id}.copy_profiler_canary", profiler.get("status") == "PASS" and copy_event, {"status": profiler.get("status"), "copy_event": copy_event}, "PASS with copy event")
        if family not in {"XFER-O1", "XFER-O2"}:
            bandwidth_gbps = float(row["requested_bytes"]) / float(row["gpu_duration_ns"]["median"]) if finite_positive(row.get("requested_bytes")) and finite_positive((row.get("gpu_duration_ns") or {}).get("median")) else 0.0
            bandwidth_gbps *= 1_000_000_000 / (1024 ** 3)
            review.check(f"{point_id}.bandwidth_range", 0.0 < bandwidth_gbps < 1000.0, bandwidth_gbps, "0 < GiB/s < 1000")
            review.warn(f"{point_id}.effective_bandwidth", bandwidth_gbps, "reported for cross-point reasonableness review")
    if "E" in groups:
        review.check("transfer.content_canary_artifact", (transfer_dir / "content_canary.json").is_file(), str(transfer_dir / "content_canary.json"), "file")
        if (transfer_dir / "content_canary.json").is_file():
            canary = read_json(transfer_dir / "content_canary.json")
            review.check("transfer.content_canary", canary.get("status") == "PASS" and canary.get("content_equal_after_h2d_d2h") is True, canary, "PASS and content equal")
    elif (transfer_dir / "content_canary.json").is_file():
        canary = read_json(transfer_dir / "content_canary.json")
        review.check("transfer.content_canary", canary.get("status") == "PASS" and canary.get("content_equal_after_h2d_d2h") is True, canary, "PASS and content equal")
    review_catalog(transfer_dir / "expert_catalog.json", review, require_runtime_semantics=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component-dir", action="append", type=Path, default=[])
    parser.add_argument("--run-dir", action="append", type=Path, default=[])
    parser.add_argument("--k-batch", type=Path)
    parser.add_argument("--k-runs-root", type=Path)
    parser.add_argument("--transfer-dir", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    review = Review()
    for component_dir in args.component_dir:
        review_component(component_dir, review)
    run_summaries = [review_model_run(run_dir, review) for run_dir in args.run_dir]
    review_model_run_trends(run_summaries, review)
    review_instrumentation_pairs(run_summaries, review)
    if args.k_batch and args.k_runs_root:
        review_k(args.k_batch, args.k_runs_root, review)
    for transfer_dir in args.transfer_dir:
        review_transfer(transfer_dir, review)
    report = {
        "schema_version": "phase7-preliminary-measurement-review-v2",
        "reviewer_tool_revision": REVIEW_REVISION,
        "status": "PASS" if not review.failures else "PRELIMINARY_REVIEW_FAIL",
        "critical_failure_count": len(review.failures),
        "warning_count": len(review.warnings),
        "checks": review.checks,
        "critical_failures": review.failures,
        "warnings": review.warnings,
        "raw_unchanged": True,
        "adoption_allowed": not review.failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not review.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
