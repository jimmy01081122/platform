#!/usr/bin/env python3
"""Preliminary correctness/reasonableness review for serving burst raw data."""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ImportError:  # pragma: no cover - portable fallback for minimal review hosts
    np = None


SCHEMA_VERSION = "phase7-serving-burst-review-v1"
TOOL_REVISION = "serving-burst-review-v1"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check(name: str, expected: Any, observed: Any, critical: bool, passed: bool) -> dict[str, Any]:
    return {
        "name": name,
        "expected": expected,
        "observed": observed,
        "critical": critical,
        "status": "PASS" if passed else "FAIL",
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def bootstrap_percentile_ci(
    values: list[float], fraction: float, seed: int, resamples: int
) -> dict[str, Any] | None:
    if not values:
        return None
    if resamples < 1:
        raise ValueError("bootstrap resamples must be positive")
    point = percentile(values, fraction)
    assert point is not None
    count = len(values)
    estimates: list[float] = []
    if np is not None:
        rng = np.random.default_rng(seed)
        values_array = np.asarray(values, dtype=np.float64)
        for start in range(0, resamples, 256):
            size = min(256, resamples - start)
            indices = rng.integers(0, count, size=(size, count))
            estimates.extend(
                np.quantile(values_array[indices], fraction, axis=1, method="linear").tolist()
            )
        backend = "numpy_default_rng_chunked"
    else:
        rng = random.Random(seed)
        for _ in range(resamples):
            estimate = percentile(
                [values[rng.randrange(count)] for _ in range(count)], fraction
            )
            assert estimate is not None
            estimates.append(estimate)
        backend = "python_random_fallback"
    estimates.sort()
    low = percentile(estimates, 0.025)
    high = percentile(estimates, 0.975)
    assert low is not None and high is not None
    half_width = (high - low) / 2.0
    return {
        "point_estimate": point,
        "ci95_low": low,
        "ci95_high": high,
        "half_width": half_width,
        "relative_half_width_to_p99": half_width / point if point > 0 else math.inf,
        "seed": seed,
        "resamples": resamples,
        "backend": backend,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--expected-concurrency", type=int, required=True)
    parser.add_argument("--expected-bursts", type=int, required=True)
    parser.add_argument("--require-routing", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-resamples", type=int, default=10000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260812)
    parser.add_argument(
        "--require-stable-p99-ci",
        action="store_true",
        help="fail review when any available p99 CI exceeds the frozen 5%% relative-width rule",
    )
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    status = json.loads((run_dir / "status.json").read_text())
    result = json.loads((run_dir / "result.json").read_text())
    records = result.get("records", [])
    checks: list[dict[str, Any]] = []

    checks.append(check("manifest.status", "PASS", status.get("status"), True, status.get("status") == "PASS"))
    checks.append(check("runtime_class", "SERVING_VARIANT", manifest.get("runtime_class"), True, manifest.get("runtime_class") == "SERVING_VARIANT"))
    checks.append(check("arrival_mode", "CLOSED_LOOP_BURST", result.get("arrival_mode"), True, result.get("arrival_mode") == "CLOSED_LOOP_BURST"))
    checks.append(check("sampling_mode", "NATURAL_EOS_CAPPED", manifest.get("sampling_mode"), True, manifest.get("sampling_mode") == "NATURAL_EOS_CAPPED"))
    expected_count = args.expected_concurrency * args.expected_bursts
    checks.append(check("request_count", expected_count, len(records), True, len(records) == expected_count))
    request_ids = [record.get("request_id") for record in records]
    checks.append(check("request_ids_unique", True, len(request_ids) == len(set(request_ids)), True, len(request_ids) == len(set(request_ids))))
    errors = [record.get("error") for record in records if record.get("error") is not None]
    checks.append(check("request_errors", [], errors, True, not errors))
    output_lengths = sorted({int(record.get("output_tokens", -1)) for record in records})
    checks.append(check("positive_output_lengths", True, output_lengths, True, bool(output_lengths) and min(output_lengths) > 0))
    timing_bad = [
        record.get("request_id")
        for record in records
        if not isinstance(record.get("submitted_monotonic_ns"), int)
        or not isinstance(record.get("first_yield_monotonic_ns"), int)
        or not isinstance(record.get("completed_monotonic_ns"), int)
        or record["submitted_monotonic_ns"] > record["first_yield_monotonic_ns"]
        or record["first_yield_monotonic_ns"] > record["completed_monotonic_ns"]
    ]
    checks.append(check("timing_order", [], timing_bad, True, not timing_bad))

    max_overlap = 0
    for point in sorted({record["submitted_monotonic_ns"] for record in records}):
        active = sum(
            record["submitted_monotonic_ns"] <= point < record["completed_monotonic_ns"]
            for record in records
        )
        max_overlap = max(max_overlap, active)
    checks.append(check("observed_max_overlap", f">={args.expected_concurrency}", max_overlap, True, max_overlap >= args.expected_concurrency))
    telemetry_path = run_dir / "telemetry.jsonl"
    telemetry_rows = read_jsonl(telemetry_path) if telemetry_path.is_file() else []
    checks.append(check("telemetry_rows", ">=2", len(telemetry_rows), True, len(telemetry_rows) >= 2))

    routing_summary: dict[str, Any] = {"present": False}
    routing_path = run_dir / "routing_steps" / "scheduler_steps.jsonl"
    if routing_path.is_file():
        routing_rows = read_jsonl(routing_path)
        vectors_valid = []
        batch_sizes = []
        route_statuses = []
        for row in routing_rows:
            vectors = row.get("expert_load_vectors_by_layer")
            expert_count = row.get("materialized_num_experts")
            shape_ok = (
                isinstance(expert_count, int)
                and isinstance(vectors, list)
                and bool(vectors)
                and all(isinstance(vector, list) and len(vector) == expert_count for vector in vectors)
            )
            vectors_valid.append(shape_ok)
            batch_sizes.append(int(row.get("batch", {}).get("active_sequences", 0)))
            route_statuses.append(row.get("route_conservation_status"))
        routing_summary = {
            "present": True,
            "row_count": len(routing_rows),
            "vector_shape_valid": all(vectors_valid) if vectors_valid else False,
            "route_conservation_statuses": sorted(set(route_statuses)),
            "max_active_sequences": max(batch_sizes) if batch_sizes else 0,
        }
    if args.require_routing:
        checks.append(check("routing_rows", ">0", routing_summary.get("row_count", 0), True, routing_summary.get("row_count", 0) > 0))
        checks.append(check("routing_vector_shape", True, routing_summary.get("vector_shape_valid"), True, routing_summary.get("vector_shape_valid") is True))
        checks.append(check("routing_conservation", ["PASS"], routing_summary.get("route_conservation_statuses"), True, routing_summary.get("route_conservation_statuses") == ["PASS"]))
        checks.append(check("active_batch_observed", f">={args.expected_concurrency}", routing_summary.get("max_active_sequences", 0), True, routing_summary.get("max_active_sequences", 0) >= args.expected_concurrency))

    metric_values = {
        "ttft_ns": [
            float(record["ttft_ns"])
            for record in records
            if isinstance(record.get("ttft_ns"), (int, float))
        ],
        "completion_latency_ns": [
            float(record["completion_latency_ns"])
            for record in records
            if isinstance(record.get("completion_latency_ns"), (int, float))
        ],
    }
    p99_bootstrap_ci: dict[str, Any] = {}
    unstable_p99_metrics: list[str] = []
    for offset, (metric_name, values) in enumerate(metric_values.items()):
        ci = bootstrap_percentile_ci(
            values,
            0.99,
            args.bootstrap_seed + offset,
            args.bootstrap_resamples,
        )
        p99_bootstrap_ci[metric_name] = ci
        if ci is None or ci["relative_half_width_to_p99"] > 0.05:
            unstable_p99_metrics.append(metric_name)
    checks.append(
        check(
            "p99_bootstrap_ci",
            {
                "resamples": args.bootstrap_resamples,
                "relative_half_width_to_p99_max": 0.05,
            },
            {"unstable_metrics": unstable_p99_metrics, "ci": p99_bootstrap_ci},
            args.require_stable_p99_ci,
            not unstable_p99_metrics,
        )
    )

    critical_failures = [item for item in checks if item["critical"] and item["status"] == "FAIL"]
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool_revision": TOOL_REVISION,
        "status": "PASS" if not critical_failures else "PRELIMINARY_REVIEW_FAIL",
        "adoption_allowed": not critical_failures,
        "raw_unchanged": True,
        "checks": checks,
        "critical_failure_count": len(critical_failures),
        "critical_failures": critical_failures,
        "routing_summary": routing_summary,
        "summary": {
            "request_count": len(records),
            "output_lengths": output_lengths,
            "ttft_ns": {
                "p50": percentile(metric_values["ttft_ns"], 0.50),
                "p95": percentile(metric_values["ttft_ns"], 0.95),
                "p99": percentile(metric_values["ttft_ns"], 0.99),
            },
            "completion_latency_ns": {
                "p50": percentile(metric_values["completion_latency_ns"], 0.50),
                "p95": percentile(metric_values["completion_latency_ns"], 0.95),
                "p99": percentile(metric_values["completion_latency_ns"], 0.99),
            },
            "p99_bootstrap_ci": p99_bootstrap_ci,
            "p99_ci_rule": {
                "required": args.require_stable_p99_ci,
                "max_relative_half_width_to_p99": 0.05,
                "unstable_metrics": unstable_p99_metrics,
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "adoption_allowed", "critical_failure_count", "routing_summary")}, indent=2, sort_keys=True))
    return 0 if not critical_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
