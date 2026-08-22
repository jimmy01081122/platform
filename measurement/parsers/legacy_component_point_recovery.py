#!/usr/bin/env python3
"""One-time recovery of evaluation points from a frozen-harness raw result.

WHY THIS EXISTS (DECISION_LOG P-020, owner decision (a)):
``gpu_run_package_v2`` is checksum-frozen, and its ``build_evaluation_points()``
returns ``[]`` for the ``calibration`` split -- which is exactly the split
TRACK_GPU target_4 runs. So a real, passing measurement yields zero evaluation
points and ``component_eval_parser.py`` rejects it with
``missing required key 'evaluation_points'``.

Worse, even on a non-calibration split that harness omits ``expert_tokens`` from
component features: the operand shape survives only inside the ``case`` string
(``...,phase=prefill,concurrency=1,expert_tokens=704``). That is precisely
``measurement_gaps.json`` GAP-4.

This module recovers the points that the frozen harness would have emitted, and
additionally lifts ``expert_tokens`` out of the ``case`` string into the
component feature dict so the output satisfies ``--enforce-gap4``.

CLAIM BOUNDARY -- READ BEFORE REUSING:
Recovering the operand shape by parsing ``case`` is **the very join GAP-4 exists
to eliminate**. It is applied here only to salvage measurements that were
already paid for on the GPU. Component points produced this way are stamped
``derivation = "legacy_join_recovered"``.

**This path must never become the standard for new measurements.** New probes
(TRACK_GPU Phase 2, owner decision (b)) must sweep ``expert_tokens`` as an
independent variable and write it directly as a structured field.

The input raw file is treated as read-only (root spec §9.4): the recovered
points are written to a separate output file carrying the source SHA-256.
Unparseable rows raise -- they are never silently dropped or guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
    )
except ImportError:  # pragma: no cover - script-relative fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
    )


# Mirrors gpu_run_package_v2/scripts/benchmark.py:19 (frozen package, not imported
# from it: importing across the frozen boundary would couple this recovery tool to
# a checksummed artifact).
COMPONENT_OPERATIONS = frozenset({
    "selected_expert", "grouped_gemm", "gather_scatter", "dequant",
})
PCIE_OPERATIONS = frozenset({"h2d_pinned", "d2h_pinned"})

_EXPERT_TOKENS_RE = re.compile(r"(?:^|,)expert_tokens=(\d+)(?:,|$)")

RECOVERED = "legacy_join_recovered"
DIRECT = "direct"


def deterministic_id(prefix: str, *parts: object) -> str:
    """Byte-identical to benchmark.py:68 so recovered ids match the harness."""
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _domain(row: dict[str, Any], split: str, platform_name: str) -> dict[str, Any]:
    """Byte-identical to benchmark.py:77."""
    return {
        key: value for key, value in {
            "split": split,
            "phase": row.get("phase"),
            "concurrency": row.get("concurrency"),
            "platform": platform_name,
        }.items() if value is not None
    }


def recover_expert_tokens(case: Any, record_id: str) -> int:
    """Lift expert_tokens out of the case string. Raises rather than guessing."""
    if not isinstance(case, str):
        raise ValidationError(
            f"{record_id}: component row has no 'case' string to recover "
            "expert_tokens from"
        )
    match = _EXPERT_TOKENS_RE.search(case)
    if not match:
        raise ValidationError(
            f"{record_id}: case string carries no 'expert_tokens=' term "
            f"({case!r}); operand shape is unrecoverable and must NOT be guessed"
        )
    return int(match.group(1))


def _mean(row: dict[str, Any], key: str, record_id: str) -> float:
    stats = require_mapping(require_key(row, key, record_id), f"{record_id}.{key}")
    mean = require_key(stats, "mean", f"{record_id}.{key}")
    if not isinstance(mean, (int, float)) or isinstance(mean, bool):
        raise ValidationError(
            f"{record_id}.{key}.mean: expected number, got {type(mean).__name__}"
        )
    return mean


def build_points(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover the evaluation points the frozen harness did not emit."""
    split = require_key(result, "split", "result")
    platform_name = require_mapping(
        require_key(result, "device", "result"), "device"
    ).get("name")
    rows = require_list(
        require_key(result, "raw_benchmarks", "result"), "raw_benchmarks"
    )

    points: list[dict[str, Any]] = []
    for row in rows:
        require_mapping(row, "raw_benchmarks[]")
        record_id = require_key(row, "record_id", "raw_benchmarks[]")
        operation = require_key(row, "operation", record_id)
        domain = _domain(row, split, platform_name)

        if operation in PCIE_OPERATIONS:
            # bytes/direction/copy_streams are already structured fields here;
            # no join is involved, so this family is a direct derivation.
            points.append({
                "point_id": deterministic_id("point", record_id, "pcie"),
                "source_record_id": record_id,
                "metric": "pcie_transfer_latency",
                "measured": _mean(row, "statistics", record_id),
                "features": {
                    "direction": require_key(row, "direction", record_id),
                    "bytes": require_key(row, "bytes", record_id),
                    "copy_streams": require_key(row, "copy_streams", record_id),
                },
                "domain": domain,
                "derivation": DIRECT,
            })
        elif operation in COMPONENT_OPERATIONS:
            # GAP-4 recovery: expert_tokens exists ONLY in the case string.
            expert_tokens = recover_expert_tokens(row.get("case"), record_id)
            points.append({
                "point_id": deterministic_id("point", record_id, "component"),
                "source_record_id": record_id,
                "metric": "component_latency",
                "measured": _mean(row, "statistics", record_id),
                "features": {
                    "cpu_calls": 0,
                    "gpu_operations": {operation: 1},
                    "memory_bytes": 0,
                    "queue_depth": 0,
                    "concurrency": row.get("concurrency"),
                    # The GAP-4 fix: operand shape carried directly.
                    "expert_tokens": expert_tokens,
                },
                "domain": domain,
                "derivation": RECOVERED,
                "recovered_from": "case_string",
            })
        elif operation == "window_replay":
            features = {
                key: row.get(key) for key in (
                    "tokens", "cpu_calls", "gpu_operations", "memory_bytes",
                    "queue_depth", "transfers", "phase", "concurrency",
                )
            }
            for metric, stats_key in (
                ("moe_replay_tpot", "statistics"),
                ("moe_replay_throughput", "throughput_statistics"),
            ):
                points.append({
                    "point_id": deterministic_id("point", record_id, metric),
                    "source_record_id": record_id,
                    "metric": metric,
                    "measured": _mean(row, stats_key, record_id),
                    "features": features,
                    "domain": domain,
                    "derivation": DIRECT,
                })
        # Environment-probe rows (cpu_runtime, device_memory, queue_depth,
        # contention_fixed_shape) carry no calibration metric; the frozen harness
        # emits no point for them either.

    point_ids = [point["point_id"] for point in points]
    if len(point_ids) != len(set(point_ids)):
        raise ValidationError("recovered evaluation point IDs are not unique")
    return points


def recover(source_path: Path) -> dict[str, Any]:
    raw_bytes = source_path.read_bytes()
    result = require_mapping(load_json(source_path), "result")
    schema = require_key(result, "schema_version", "result")
    if schema != "gpu-benchmark-result-v1":
        raise ValidationError(
            f"schema_version: expected 'gpu-benchmark-result-v1', got {schema!r}"
        )
    if result.get("evaluation_points"):
        raise ValidationError(
            "result already carries evaluation_points; this recovery tool is only "
            "for frozen-harness outputs that emitted none"
        )

    points = build_points(result)
    if not points:
        raise ValidationError("no evaluation points could be recovered")

    recovered = sum(1 for p in points if p["derivation"] == RECOVERED)
    out = dict(result)
    out["evaluation_points"] = points
    out["evaluation_point_recovery"] = {
        "tool": "measurement/parsers/legacy_component_point_recovery.py",
        "reason": (
            "frozen gpu_run_package_v2 build_evaluation_points() returns [] for "
            "the calibration split and omits expert_tokens from component "
            "features (measurement_gaps.json GAP-4)"
        ),
        "source_path": str(source_path),
        "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "points_total": len(points),
        "points_legacy_join_recovered": recovered,
        "claim_boundary": (
            "component points recovered by parsing the case string; this join is "
            "what GAP-4 exists to eliminate and MUST NOT be the standard for new "
            "measurements -- new probes must carry expert_tokens directly"
        ),
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="frozen-harness result.json (read-only)")
    ap.add_argument("--out", required=True, help="output path for recovered result")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    source = Path(args.path)
    out_path = Path(args.out)
    if out_path.resolve() == source.resolve():
        raise SystemExit("refusing to overwrite the raw input; --out must differ")

    recovered = recover(source)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(recovered, indent=2 if args.pretty else None, sort_keys=True) + "\n"
    )
    meta = recovered["evaluation_point_recovery"]
    print(
        f"legacy_component_point_recovery: points={meta['points_total']} "
        f"legacy_join_recovered={meta['points_legacy_join_recovered']} "
        f"source_sha256={meta['source_sha256'][:16]}... -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
