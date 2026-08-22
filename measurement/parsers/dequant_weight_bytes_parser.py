#!/usr/bin/env python3
"""Fail-closed parser for GAP-1 fixed-token dequant weight-byte sweeps."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import ValidationError, load_json, require_equal, require_key, require_mapping, require_positive_int
    from measurement.parsers.target4_phase2_parser_common import validate_base, validate_row_point_match
    from measurement.probes.dequant_weight_bytes_probe import PACKED_BYTES_PER_GROUP, PROBE_SCHEMA
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import ValidationError, load_json, require_equal, require_key, require_mapping, require_positive_int
    from measurement.parsers.target4_phase2_parser_common import validate_base, validate_row_point_match
    from measurement.probes.dequant_weight_bytes_probe import PACKED_BYTES_PER_GROUP, PROBE_SCHEMA


def validate(result: Any) -> dict[str, Any]:
    root, rows, by_source = validate_base(result, probe_schema=PROBE_SCHEMA)
    require_equal(root.get("evidence_limit"), "DEQUANT_PROXY_ONLY", "evidence_limit")
    axes = require_mapping(require_key(root, "axes", "result"), "axes")
    weight_axis = axes.get("weight_bytes")
    if not isinstance(weight_axis, list) or len(weight_axis) < 2:
        raise ValidationError("axes.weight_bytes must contain at least two values")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in weight_axis):
        raise ValidationError("axes.weight_bytes must be positive integers")
    if weight_axis != sorted(set(weight_axis)):
        raise ValidationError("axes.weight_bytes must be strictly increasing and unique")
    if any(value % PACKED_BYTES_PER_GROUP for value in weight_axis):
        raise ValidationError("axes.weight_bytes is not aligned to INT4 group128")
    fixed_axis = axes.get("fixed_expert_tokens")
    if not isinstance(fixed_axis, list) or len(fixed_axis) != 1:
        raise ValidationError("axes.fixed_expert_tokens must contain exactly one value")
    fixed = require_positive_int(fixed_axis[0], "axes.fixed_expert_tokens[0]")
    require_equal(axes.get("concurrency"), [1], "axes.concurrency")
    if len(rows) != len(weight_axis):
        raise ValidationError(
            f"incomplete GAP-1 grid: {len(rows)} rows != {len(weight_axis)} byte values"
        )
    observed: set[int] = set()
    for raw in rows:
        require_equal(raw.get("operation"), "dequant", "raw.operation")
        weight_bytes = require_positive_int(raw.get("weight_bytes"), "raw.weight_bytes")
        if weight_bytes not in weight_axis:
            raise ValidationError(f"raw weight_bytes {weight_bytes} outside declared axis")
        if weight_bytes in observed:
            raise ValidationError(f"duplicate GAP-1 weight_bytes cell {weight_bytes}")
        observed.add(weight_bytes)
        require_equal(raw.get("expert_tokens"), fixed, "raw.expert_tokens")
        require_equal(raw.get("fixed_expert_tokens"), fixed, "raw.fixed_expert_tokens")
        require_equal(raw.get("concurrency"), 1, "raw.concurrency")
        require_equal(
            raw.get("weight_layout"),
            "synthetic_symmetric_int4_proxy_not_checkpoint_awq",
            "raw.weight_layout",
        )
        point = by_source[raw["record_id"]]
        validate_row_point_match(
            raw,
            point,
            feature_names=(
                "weight_bytes", "expert_tokens", "fixed_expert_tokens", "concurrency"
            ),
        )
        require_equal(
            point["features"].get("gpu_operations"), {"dequant": 1},
            "point.features.gpu_operations",
        )
    if observed != set(weight_axis):
        raise ValidationError(f"GAP-1 grid missing byte values: {sorted(set(weight_axis) - observed)}")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args(argv)
    root = validate(load_json(args.path))
    print(
        f"dequant_weight_bytes OK: {len(root['raw_benchmarks'])} cells, "
        "n=5, PROXY_ONLY, GAP-4=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

