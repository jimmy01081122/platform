#!/usr/bin/env python3
"""Fail-closed parser for the 32-cell V2-GAP-C operator probe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import ValidationError, load_json, require_equal, require_key, require_mapping
    from measurement.parsers.target4_phase2_parser_common import validate_base, validate_row_point_match
    from measurement.probes.sort_permute_probe import PROBE_SCHEMA
    from measurement.probes.target4_phase2_common import FROZEN_EXPERT_TOKENS, SORT_PERMUTE_OPERATIONS
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import ValidationError, load_json, require_equal, require_key, require_mapping
    from measurement.parsers.target4_phase2_parser_common import validate_base, validate_row_point_match
    from measurement.probes.sort_permute_probe import PROBE_SCHEMA
    from measurement.probes.target4_phase2_common import FROZEN_EXPERT_TOKENS, SORT_PERMUTE_OPERATIONS


def validate(result: Any) -> dict[str, Any]:
    root, rows, by_source = validate_base(
        result, probe_schema=PROBE_SCHEMA, expected_cells=32
    )
    axes = require_mapping(require_key(root, "axes", "result"), "axes")
    require_equal(axes.get("operations"), list(SORT_PERMUTE_OPERATIONS), "axes.operations")
    require_equal(axes.get("expert_tokens"), list(FROZEN_EXPERT_TOKENS), "axes.expert_tokens")
    require_equal(axes.get("phase"), ["decode"], "axes.phase")
    require_equal(axes.get("concurrency"), [1], "axes.concurrency")
    expected = {
        (operation, tokens)
        for tokens in FROZEN_EXPERT_TOKENS
        for operation in SORT_PERMUTE_OPERATIONS
    }
    observed: set[tuple[str, int]] = set()
    for raw in rows:
        operation = require_key(raw, "operation", "raw")
        expert_tokens = require_key(raw, "expert_tokens", "raw")
        key = (operation, expert_tokens)
        if key not in expected:
            raise ValidationError(f"unexpected V2-GAP-C grid cell {key!r}")
        if key in observed:
            raise ValidationError(f"duplicate V2-GAP-C grid cell {key!r}")
        observed.add(key)
        require_equal(raw.get("phase"), "decode", "raw.phase")
        require_equal(raw.get("concurrency"), 1, "raw.concurrency")
        point = by_source[raw["record_id"]]
        validate_row_point_match(
            raw, point, feature_names=("expert_tokens", "phase", "concurrency")
        )
        require_equal(
            point["features"].get("gpu_operations"), {operation: 1},
            "point.features.gpu_operations",
        )
    if observed != expected:
        raise ValidationError(f"V2-GAP-C grid missing cells: {sorted(expected - observed)}")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args(argv)
    root = validate(load_json(args.path))
    print(f"sort_permute OK: {len(root['raw_benchmarks'])} cells, n=5, GAP-4=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

