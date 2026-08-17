#!/usr/bin/env python3
"""Apply the isolated-vs-vLLM fused Attention correlation acceptance gate."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "phase7-attention-fused-correlation-review-v1"
TOOL_REVISION = "attention-fused-correlation-review-v1"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average_rank = (start + end - 1) / 2.0
        for position in range(start, end):
            result[order[position]] = average_rank
        start = end
    return result


def pearson(left: list[float], right: list[float]) -> float:
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator else float("nan")


def recursive_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for name, child in value.items():
            if name == key:
                found.append(child)
            found.extend(recursive_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_values(child, key))
    return found


def coordinate_from_component(row: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        int(row["query_length"]),
        int(row["past_kv_length"]),
        int(row["active_sequences"]),
        str(row["phase"]),
    )


def coordinate_from_fused(row: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        int(row["chunk"]["scheduled_tokens"]),
        int(row["chunk"]["prior_computed_tokens"]),
        int(row["batch"]["active_sequences"]),
        str(row["phase"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component-dir", type=Path, required=True)
    parser.add_argument("--fused-correlation-jsonl", type=Path, required=True)
    parser.add_argument("--fused-run-dir", type=Path, required=True)
    parser.add_argument("--profile-hook-perturbation-review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    component_manifest = json.loads((args.component_dir / "manifest.json").read_text())
    component_rows = read_jsonl(args.component_dir / "measurements.jsonl")
    fused_rows = read_jsonl(args.fused_correlation_jsonl)
    fused_model = json.loads((args.fused_run_dir / "model_identity.json").read_text())
    resolved_runtime = json.loads((args.fused_run_dir / "resolved_runtime.json").read_text())

    component_by_coordinate = {coordinate_from_component(row): row for row in component_rows}
    fused_by_coordinate = {coordinate_from_fused(row): row for row in fused_rows}
    coordinates_match = set(component_by_coordinate) == set(fused_by_coordinate)

    pairs: list[dict[str, Any]] = []
    layer_zero_complete = True
    for coordinate in sorted(set(component_by_coordinate) & set(fused_by_coordinate)):
        component = component_by_coordinate[coordinate]
        fused = fused_by_coordinate[coordinate]
        layer_zero = next(
            (layer for layer in fused.get("attention_layer_correlations", []) if layer.get("layer_index") == 0),
            None,
        )
        if layer_zero is None:
            layer_zero_complete = False
            continue
        pairs.append(
            {
                "coordinate": {
                    "query_or_chunk_length": coordinate[0],
                    "past_kv_length": coordinate[1],
                    "active_sequences": coordinate[2],
                    "phase": coordinate[3],
                },
                "isolated_median_ns": float(component["gpu_duration_ns"]["median"]),
                "fused_layer0_gpu_duration_ns": float(layer_zero["gpu_duration_ns"]),
                "fused_annotation_correlation_id": layer_zero["annotation_correlation_id"],
                "fused_kernel_launch_correlation_ids": layer_zero["kernel_launch_correlation_ids"],
                "fused_streams": layer_zero.get("streams", []),
                "isolated_kernel_events": [
                    event.get("key") for event in component.get("profiler_canary", {}).get("events", [])
                ],
                "fused_kernel_names": layer_zero["kernel_names"],
            }
        )

    q256_pairs = [pair for pair in pairs if pair["coordinate"]["query_or_chunk_length"] == 256]
    isolated_values = [pair["isolated_median_ns"] for pair in q256_pairs]
    fused_values = [pair["fused_layer0_gpu_duration_ns"] for pair in q256_pairs]
    spearman = (
        pearson(rank(isolated_values), rank(fused_values))
        if len(q256_pairs) >= 3
        else float("nan")
    )
    ratios = [
        pair["isolated_median_ns"] / pair["fused_layer0_gpu_duration_ns"]
        for pair in pairs
    ]

    component_revision_matches = component_manifest.get("model_revision") == (
        fused_model.get("identity", {}).get("revision")
        or fused_model.get("revision")
        or fused_model.get("manifest_revision")
    )
    if not component_revision_matches:
        fused_revisions = recursive_values(fused_model, "revision")
        component_revision_matches = component_manifest.get("model_revision") in fused_revisions

    isolated_same_kernel_path = all(
        not row.get("not_vllm_fused_kernel", True) for row in component_rows
    )
    isolated_has_stream_and_correlation = all(
        row.get("isolated_stream_ids") and row.get("isolated_kernel_correlation_ids")
        for row in component_rows
    )
    fused_stream_and_correlation_complete = all(
        pair["fused_streams"]
        and pair["fused_annotation_correlation_id"] is not None
        and pair["fused_kernel_launch_correlation_ids"]
        for pair in pairs
    )

    block_sizes = [value for value in recursive_values(resolved_runtime, "block_size") if isinstance(value, int)]
    actual_block_size = 16 if 16 in block_sizes else (block_sizes[0] if block_sizes else None)
    kv_representation_matches = all(
        row.get("kv_fragmentation_mode") == f"vllm-paged-block-{actual_block_size}"
        for row in component_rows
    )

    profile_perturbation_pass = False
    profile_perturbation_observed: Any = "MISSING"
    if args.profile_hook_perturbation_review is not None:
        profile_review = json.loads(args.profile_hook_perturbation_review.read_text())
        profile_perturbation_observed = profile_review.get("status")
        profile_perturbation_pass = profile_review.get("status") == "PASS"

    checks = {
        "exact_coordinate_set_match": coordinates_match and len(pairs) == len(component_rows),
        "same_materialized_model_revision": component_revision_matches,
        "same_layer_identity_layer0": layer_zero_complete and len(pairs) == len(component_rows),
        "fused_stream_and_kernel_correlation_complete": fused_stream_and_correlation_complete,
        "same_kernel_path": isolated_same_kernel_path,
        "isolated_stream_and_kernel_correlation_complete": isolated_has_stream_and_correlation,
        "same_kv_fragmentation_representation": kv_representation_matches,
        "profile_hook_latency_distribution_perturbation_pass": profile_perturbation_pass,
        "q256_past_kv_trend_spearman_at_least_0_90": math.isfinite(spearman) and spearman >= 0.90,
    }
    required_for_model_bound = [
        "exact_coordinate_set_match",
        "same_materialized_model_revision",
        "same_layer_identity_layer0",
        "fused_stream_and_kernel_correlation_complete",
        "same_kernel_path",
        "isolated_stream_and_kernel_correlation_complete",
        "same_kv_fragmentation_representation",
        "profile_hook_latency_distribution_perturbation_pass",
        "q256_past_kv_trend_spearman_at_least_0_90",
    ]
    failures = [name for name in required_for_model_bound if not checks[name]]
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool_revision": TOOL_REVISION,
        "status": "PASS" if not failures else "FAIL",
        "model_bound_calibration_allowed": not failures,
        "allowed_claim": (
            "FUSED_PATH_COMPONENT_CALIBRATION"
            if not failures
            else "GPU_COMPONENT_PROBE_SHAPE_ANCHOR_ONLY"
        ),
        "raw_unchanged": True,
        "checks": checks,
        "critical_failures": failures,
        "profile_hook_perturbation_review": profile_perturbation_observed,
        "actual_vllm_kv_block_size": actual_block_size,
        "trend": {
            "q256_pair_count": len(q256_pairs),
            "spearman": spearman,
            "isolated_to_fused_ratio_min": min(ratios) if ratios else None,
            "isolated_to_fused_ratio_max": max(ratios) if ratios else None,
        },
        "pairs": pairs,
        "interpretation": (
            "Trend correlation alone cannot promote the isolated probe: exact fused kernel path, "
            "paged-KV representation, isolated stream/correlation evidence, and profile-hook "
            "latency-distribution acceptance are mandatory."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: report[key] for key in (
        "status", "model_bound_calibration_allowed", "allowed_claim", "checks",
        "critical_failures", "trend"
    )}, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
