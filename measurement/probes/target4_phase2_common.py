"""Shared, stdlib-only helpers for TRACK_GPU target_4 Phase 2 probes.

The frozen ``gpu_run_package_v2`` cannot be edited.  These helpers let the new
standalone probes emit the existing ``gpu-benchmark-result-v1`` evaluation-point
envelope while identifying their stricter probe-specific schema separately.

Most importantly, point IDs and operand-shape features are built from structured
fields.  No helper accepts or parses a human-readable ``case`` string.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable


FROZEN_EXPERT_TOKENS = (8, 16, 32, 64, 128, 256, 512, 1024)
FROZEN_COMPONENT_OPERATIONS = (
    "selected_expert",
    "grouped_gemm",
    "gather_scatter",
    "dequant",
)
FROZEN_PHASES = ("prefill", "decode")
SORT_PERMUTE_OPERATIONS = (
    "argsort_route",
    "argsort_inverse",
    "index_select_pack",
    "index_select_unpack",
)
DIRECT_DERIVATION = "direct_probe_structured_axis"


def parse_positive_int_csv(value: str, label: str) -> tuple[int, ...]:
    """Parse a nonempty, duplicate-free, positive integer axis."""
    try:
        parsed = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise ValueError(f"{label} must be a comma-separated integer list") from exc
    if not parsed or any(item <= 0 for item in parsed):
        raise ValueError(f"{label} must contain positive integers")
    if len(parsed) != len(set(parsed)):
        raise ValueError(f"{label} must not contain duplicates")
    if tuple(sorted(parsed)) != parsed:
        raise ValueError(f"{label} must be strictly increasing")
    return parsed


def deterministic_id(prefix: str, structured_axes: dict[str, Any]) -> str:
    payload = json.dumps(
        structured_axes,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def latency_summary(samples_ms: Iterable[float]) -> dict[str, Any]:
    samples = [float(value) for value in samples_ms]
    if not samples or any(not math.isfinite(value) or value <= 0 for value in samples):
        raise ValueError("latency samples must be finite positive numbers")
    n = len(samples)
    mean = statistics.fmean(samples)
    variance = statistics.variance(samples) if n > 1 else 0.0
    # target_4 is frozen at n=5 (df=4), but retain the nearby values so a
    # deliberate mechanism canary remains internally honest.
    t95_by_df = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776}
    t95 = t95_by_df.get(n - 1, 1.96)
    half = t95 * math.sqrt(variance / n) if n > 1 else 0.0
    return {
        "n": n,
        "mean": mean,
        "variance": variance,
        "stdev": math.sqrt(variance),
        "ci95": [mean - half, mean + half],
        "unit": "ms",
        "ci_method": "two-sided Student-t",
    }


def make_record(
    *,
    probe: str,
    operation: str,
    structured_axes: dict[str, Any],
    samples_ms: list[float],
    inner_iterations: int,
    warmup: int,
    minimum_inner_seconds: float,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a raw record with no case-string escape hatch."""
    axes = dict(structured_axes)
    record_id = deterministic_id(
        "target4-phase2-raw",
        {"probe": probe, "operation": operation, **axes},
    )
    record = {
        "record_id": record_id,
        "operation": operation,
        **axes,
        "warmup": warmup,
        "outer_repeats": len(samples_ms),
        "inner_iterations": inner_iterations,
        "minimum_inner_seconds": minimum_inner_seconds,
        "repeats_ms": samples_ms,
        "statistics": latency_summary(samples_ms),
        "operand_shape_source": DIRECT_DERIVATION,
    }
    if metadata:
        record.update(metadata)
    if "case" in record:
        raise ValueError("new target_4 probes must not serialize axes in a case string")
    return record


def make_component_point(
    *,
    probe: str,
    record: dict[str, Any],
    features: dict[str, Any],
    split: str = "fit",
    evidence_use: str = "FIT_SIDE_ONLY_NOT_SEALED_HOLDOUT",
) -> dict[str, Any]:
    """Project one raw record without a join or case-string parse."""
    if "expert_tokens" not in features:
        raise ValueError("component point must carry expert_tokens directly")
    source_record_id = record["record_id"]
    point_id = deterministic_id(
        "target4-phase2-point",
        {"probe": probe, "source_record_id": source_record_id},
    )
    if split not in {"fit", "validation", "holdout"}:
        raise ValueError(f"unsupported sealed split {split!r}")
    domain = {"split": split, "evidence_use": evidence_use}
    for name in ("phase", "concurrency"):
        if name in features:
            domain[name] = features[name]
    return {
        "point_id": point_id,
        "source_record_id": source_record_id,
        "metric": "component_latency",
        "measured": record["statistics"]["mean"],
        "features": features,
        "domain": domain,
        "derivation": DIRECT_DERIVATION,
        "fit_side_only": split == "fit",
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
