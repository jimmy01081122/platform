#!/usr/bin/env python3
"""Fail-closed parser for one sealed subset of target_4 component cells."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import ValidationError, load_json, require_equal, require_key, require_mapping
    from measurement.parsers.target4_phase2_parser_common import validate_base, validate_row_point_match
    from measurement.probes.component_shape_probe import (
        PROBE_SCHEMA, SEALED_ASSIGNMENT_SHA256, SPLIT_EVIDENCE_USE,
    )
    from measurement.probes.target4_phase2_common import (
        FROZEN_COMPONENT_OPERATIONS, FROZEN_EXPERT_TOKENS, FROZEN_PHASES,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import ValidationError, load_json, require_equal, require_key, require_mapping
    from measurement.parsers.target4_phase2_parser_common import validate_base, validate_row_point_match
    from measurement.probes.component_shape_probe import (
        PROBE_SCHEMA, SEALED_ASSIGNMENT_SHA256, SPLIT_EVIDENCE_USE,
    )
    from measurement.probes.target4_phase2_common import (
        FROZEN_COMPONENT_OPERATIONS, FROZEN_EXPERT_TOKENS, FROZEN_PHASES,
    )


def validate(result: Any) -> dict[str, Any]:
    root, rows, by_source = validate_base(result, probe_schema=PROBE_SCHEMA)
    axes = require_mapping(require_key(root, "axes", "result"), "axes")
    require_equal(axes.get("operations"), list(FROZEN_COMPONENT_OPERATIONS), "axes.operations")
    require_equal(axes.get("phases"), list(FROZEN_PHASES), "axes.phases")
    require_equal(axes.get("expert_tokens"), list(FROZEN_EXPERT_TOKENS), "axes.expert_tokens")
    require_equal(axes.get("concurrency"), [1], "axes.concurrency")

    full_grid = {
        (operation, phase, tokens)
        for operation in FROZEN_COMPONENT_OPERATIONS
        for phase in FROZEN_PHASES
        for tokens in FROZEN_EXPERT_TOKENS
    }
    sealed = require_mapping(
        require_key(root, "sealed_assignment", "result"), "sealed_assignment"
    )
    require_equal(
        require_key(sealed, "manifest_id", "sealed_assignment"),
        "a4_holdout_split_v1",
        "sealed_assignment.manifest_id",
    )
    require_equal(
        require_key(sealed, "assignment_sha256", "sealed_assignment"),
        SEALED_ASSIGNMENT_SHA256,
        "sealed_assignment.assignment_sha256",
    )
    selected_split = require_key(sealed, "selected_split", "sealed_assignment")
    if selected_split not in SPLIT_EVIDENCE_USE:
        raise ValidationError(f"sealed_assignment.selected_split: {selected_split!r}")
    require_equal(
        require_key(root, "evidence_use", "result"),
        SPLIT_EVIDENCE_USE[selected_split],
        "evidence_use",
    )
    require_equal(
        require_key(sealed, "holdout_measurement_authorized", "sealed_assignment"),
        selected_split == "holdout",
        "sealed_assignment.holdout_measurement_authorized",
    )

    # Recompute the canonical assignment independently; do not trust split labels
    # copied into the result being parsed.
    from calibration.sealed.build_holdout_split_v1 import build_cells

    expected_cells = {
        (
            cell["params"]["op"],
            cell["params"]["phase"],
            cell["params"]["expert_tokens"],
        ): cell
        for cell in build_cells()
        if cell["metric"] == "component_latency"
        and cell["split"] == selected_split
    }
    require_equal(
        require_key(sealed, "selected_component_cells", "sealed_assignment"),
        len(expected_cells),
        "sealed_assignment.selected_component_cells",
    )
    if len(rows) != len(expected_cells):
        raise ValidationError(
            f"incomplete sealed {selected_split} subset: expected "
            f"{len(expected_cells)} cells, got {len(rows)}"
        )

    observed: set[tuple[str, str, int]] = set()
    for raw in rows:
        operation = require_key(raw, "operation", "raw")
        phase = require_key(raw, "phase", "raw")
        expert_tokens = require_key(raw, "expert_tokens", "raw")
        key = (operation, phase, expert_tokens)
        if key not in full_grid or key not in expected_cells:
            raise ValidationError(f"unexpected component grid cell {key!r}")
        if key in observed:
            raise ValidationError(f"duplicate component grid cell {key!r}")
        observed.add(key)
        require_equal(require_key(raw, "concurrency", "raw"), 1, "raw.concurrency")
        sealed_cell = expected_cells[key]
        require_equal(raw.get("sealed_cell_id"), sealed_cell["cell_id"], "raw.sealed_cell_id")
        require_equal(raw.get("sealed_cell_sha256"), sealed_cell["sha256"], "raw.sealed_cell_sha256")
        require_equal(raw.get("sealed_split"), selected_split, "raw.sealed_split")
        point = by_source[raw["record_id"]]
        validate_row_point_match(
            raw, point, feature_names=("expert_tokens", "phase", "concurrency")
        )
        require_equal(
            point["features"].get("gpu_operations"), {operation: 1},
            "point.features.gpu_operations",
        )
        domain = require_mapping(require_key(point, "domain", "point"), "point.domain")
        require_equal(domain.get("split"), selected_split, "point.domain.split")
        require_equal(
            domain.get("evidence_use"),
            SPLIT_EVIDENCE_USE[selected_split],
            "point.domain.evidence_use",
        )
        require_equal(point.get("fit_side_only"), selected_split == "fit", "point.fit_side_only")
    if observed != set(expected_cells):
        raise ValidationError(
            f"component sealed subset missing cells: {sorted(set(expected_cells) - observed)}"
        )
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args(argv)
    root = validate(load_json(args.path))
    print(f"component_shape OK: {len(root['raw_benchmarks'])} cells, n=5, GAP-4=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
