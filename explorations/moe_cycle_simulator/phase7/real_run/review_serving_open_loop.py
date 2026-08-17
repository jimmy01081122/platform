#!/usr/bin/env python3
"""Preliminary review for deterministic Poisson open-loop serving runs."""

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


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
    values: list[float],
    fraction: float,
    seed: int,
    resamples: int,
) -> dict[str, Any] | None:
    """Return a fixed-seed percentile bootstrap CI for one tail metric."""
    if not values:
        return None
    if resamples < 1:
        raise ValueError("bootstrap resamples must be positive")
    point = percentile(values, fraction)
    assert point is not None
    count = len(values)
    estimates: list[float] = []
    if np is not None:
        # Chunked allocation keeps a 10,000 x 10,000 review below device/GPU
        # memory and makes the CPU-only review practical for the 10k extension.
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
            sample = [values[rng.randrange(count)] for _ in range(count)]
            estimate = percentile(sample, fraction)
            assert estimate is not None
            estimates.append(estimate)
        backend = "python_random_fallback"
    estimates.sort()
    low = percentile(estimates, 0.025)
    high = percentile(estimates, 0.975)
    assert low is not None and high is not None
    half_width = (high - low) / 2.0
    relative_half_width = half_width / point if point > 0 else math.inf
    return {
        "point_estimate": point,
        "ci95_low": low,
        "ci95_high": high,
        "half_width": half_width,
        "relative_half_width_to_p99": relative_half_width,
        "seed": seed,
        "resamples": resamples,
        "backend": backend,
    }


def check(name: str, expected: Any, observed: Any, critical: bool, passed: bool) -> dict[str, Any]:
    return {
        "name": name,
        "expected": expected,
        "observed": observed,
        "critical": critical,
        "status": "PASS" if passed else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--expected-request-count", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-resamples", type=int, default=10000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260812)
    parser.add_argument(
        "--require-stable-p99-ci",
        action="store_true",
        help="fail review when any p99 95%% bootstrap CI exceeds the frozen 5%% relative-width rule",
    )
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    manifest = read_json(run_dir / "manifest.json")
    status = read_json(run_dir / "status.json")
    result = read_json(run_dir / "result.json")
    records = result.get("records", [])
    arrival_trace = read_jsonl(run_dir / "arrival_trace.jsonl")
    checks: list[dict[str, Any]] = []

    checks.append(check("manifest.status", "PASS", status.get("status"), True, status.get("status") == "PASS"))
    checks.append(check("runtime_class", "SERVING_VARIANT", manifest.get("runtime_class"), True, manifest.get("runtime_class") == "SERVING_VARIANT"))
    checks.append(check("arrival_mode", "POISSON_OPEN_LOOP", result.get("arrival_mode"), True, result.get("arrival_mode") == "POISSON_OPEN_LOOP"))
    checks.append(check("sampling_mode", "NATURAL_EOS_CAPPED", manifest.get("sampling_mode"), True, manifest.get("sampling_mode") == "NATURAL_EOS_CAPPED"))
    checks.append(check("request_count", args.expected_request_count, len(records), True, len(records) == args.expected_request_count))
    checks.append(check("arrival_trace_count", args.expected_request_count, len(arrival_trace), True, len(arrival_trace) == args.expected_request_count))

    request_ids = [record.get("request_id") for record in records]
    trace_ids = [row.get("request_id") for row in arrival_trace]
    checks.append(check("request_ids_unique", True, len(request_ids) == len(set(request_ids)), True, len(request_ids) == len(set(request_ids))))
    observed_indices = [row.get("arrival_index") for row in arrival_trace]
    expected_indices = list(range(len(arrival_trace)))
    checks.append(check("arrival_indices_contiguous", expected_indices, observed_indices, True, observed_indices == expected_indices))
    checks.append(check("arrival_request_ids_match", True, request_ids == trace_ids, True, request_ids == trace_ids))

    errors = [record.get("error") for record in records if record.get("error") is not None]
    checks.append(check("request_errors", [], errors, True, not errors))
    output_lengths = sorted({int(record.get("output_tokens", -1)) for record in records})
    checks.append(check("positive_output_lengths", True, output_lengths, True, bool(output_lengths) and min(output_lengths) > 0))

    timing_bad = []
    queue_delays_ns: list[float] = []
    ttft_ns: list[float] = []
    completion_ns: list[float] = []
    for record in records:
        scheduled = record.get("client_scheduled_arrival_monotonic_ns")
        observed = record.get("server_observed_arrival_monotonic_ns")
        first = record.get("first_yield_monotonic_ns")
        completed = record.get("completed_monotonic_ns")
        if not all(isinstance(value, int) for value in (scheduled, observed, first, completed)):
            timing_bad.append(record.get("request_id"))
            continue
        if not (scheduled <= observed <= first <= completed):
            timing_bad.append(record.get("request_id"))
            continue
        queue_delays_ns.append(float(observed - scheduled))
        ttft_ns.append(float(first - observed))
        completion_ns.append(float(completed - observed))
    checks.append(check("timing_order_and_arrival", [], timing_bad, True, not timing_bad))

    trace_bad = []
    if arrival_trace:
        seed = arrival_trace[0].get("arrival_seed")
        rate = arrival_trace[0].get("arrival_rate_rps")
        rng = random.Random(seed)
        offset = 0
        for index, row in enumerate(arrival_trace):
            if index:
                offset += int(rng.expovariate(rate) * 1_000_000_000)
            if row.get("scheduled_offset_ns") != offset:
                trace_bad.append(row.get("request_id"))
        checks.append(check("arrival_trace_reproducible", [], trace_bad, True, not trace_bad))
    else:
        checks.append(check("arrival_trace_reproducible", "present", "missing", True, False))

    telemetry_rows = len(read_jsonl(run_dir / "telemetry.jsonl")) if (run_dir / "telemetry.jsonl").is_file() else 0
    checks.append(check("telemetry_rows", ">=2", telemetry_rows, True, telemetry_rows >= 2))

    overlap = 0
    for point in sorted({record["server_observed_arrival_monotonic_ns"] for record in records if isinstance(record.get("server_observed_arrival_monotonic_ns"), int)}):
        active = sum(
            record["server_observed_arrival_monotonic_ns"] <= point < record["completed_monotonic_ns"]
            for record in records
            if isinstance(record.get("completed_monotonic_ns"), int)
        )
        overlap = max(overlap, active)
    checks.append(check("observed_concurrent_overlap", ">=1", overlap, True, overlap >= 1))

    metric_values = {
        "queue_delay_ns": queue_delays_ns,
        "ttft_ns": ttft_ns,
        "completion_latency_ns": completion_ns,
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
            {
                "unstable_metrics": unstable_p99_metrics,
                "ci": p99_bootstrap_ci,
            },
            args.require_stable_p99_ci,
            not unstable_p99_metrics,
        )
    )

    critical_failures = [item for item in checks if item["critical"] and item["status"] == "FAIL"]
    report = {
        "schema_version": "phase7-serving-open-loop-review-v1",
        "status": "PASS" if not critical_failures else "PRELIMINARY_REVIEW_FAIL",
        "adoption_allowed": not critical_failures,
        "raw_unchanged": True,
        "checks": checks,
        "critical_failure_count": len(critical_failures),
        "critical_failures": critical_failures,
        "summary": {
            "completed_request_count": len(records),
            "completion_rate": len(records) / args.expected_request_count if args.expected_request_count else 0.0,
            "queue_delay_ns": {"p50": percentile(queue_delays_ns, 0.50), "p95": percentile(queue_delays_ns, 0.95), "p99": percentile(queue_delays_ns, 0.99)},
            "ttft_ns": {"p50": percentile(ttft_ns, 0.50), "p95": percentile(ttft_ns, 0.95), "p99": percentile(ttft_ns, 0.99)},
            "completion_latency_ns": {"p50": percentile(completion_ns, 0.50), "p95": percentile(completion_ns, 0.95), "p99": percentile(completion_ns, 0.99)},
            "p99_bootstrap_ci": p99_bootstrap_ci,
            "p99_ci_rule": {
                "required": args.require_stable_p99_ci,
                "max_relative_half_width_to_p99": 0.05,
                "unstable_metrics": unstable_p99_metrics,
            },
            "max_observed_overlap": overlap,
            "sampling_modes": sorted({record.get("sampling_mode") for record in records}),
            "output_lengths": output_lengths,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "adoption_allowed", "critical_failure_count", "summary")}, indent=2, sort_keys=True))
    return 0 if not critical_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
