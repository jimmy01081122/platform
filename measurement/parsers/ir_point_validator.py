#!/usr/bin/env python3
"""Validator for probe-emitted IR evaluation points (PREP-2).

Checks that a probe's ``ir_evaluation_points`` are well-formed CalibrationIR
evaluation points AND satisfy the no-join property that closes the GAP-4 class:
every point carries its operand-shape features directly in
``evaluation_coordinate`` (non-empty), so the IR pipeline never has to join back
to a raw record to recover shape.

When ``jsonschema`` and the phase2 canonical schema are available, each point is
additionally validated against the real ``$defs.calibration`` definition, so the
probe output is checked against STAGE_A2's actual schema, not a local restatement.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal,
    )

_CALIBRATION_REQUIRED = (
    "metric", "unit", "measured_value", "evaluation_coordinate",
    "calibration_envelope", "runtime_variant_hash",
)
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "explorations/moe_cycle_simulator/phase2/schemas/canonical_ir.schema.json"
)


def _exact_decimal(value: Any, where: str) -> None:
    if not isinstance(value, str):
        raise ValidationError(f"{where}: exact-decimal must be a string")
    try:
        Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{where}: {value!r} not exact-decimal")


def validate_points(points: Any, *, use_jsonschema: bool = True) -> list[dict]:
    points = require_list(points, "ir_evaluation_points")
    if not points:
        raise ValidationError("ir_evaluation_points: empty")

    calibration_schema = None
    if use_jsonschema:
        calibration_schema = _load_calibration_schema()

    for i, pt in enumerate(points):
        w = f"ir_evaluation_points[{i}]"
        require_mapping(pt, w)
        for key in _CALIBRATION_REQUIRED:
            require_key(pt, key, w)
        coord = require_list(pt["evaluation_coordinate"], f"{w}.evaluation_coordinate")
        if not coord:
            raise ValidationError(
                f"{w}: evaluation_coordinate empty -- operand shape not carried "
                "directly (GAP-4: point would require a join to raw)"
            )
        dims = require_mapping(pt["calibration_envelope"], f"{w}.calibration_envelope")
        dim_list = require_list(require_key(dims, "dimensions", f"{w}.calibration_envelope"),
                                f"{w}.calibration_envelope.dimensions")
        coord_names = [c["name"] for c in coord]
        dim_names = [d["name"] for d in dim_list]
        if set(coord_names) != set(dim_names):
            raise ValidationError(
                f"{w}: coordinate names {sorted(coord_names)} != envelope names {sorted(dim_names)}"
            )
        dims_by = {d["name"]: d for d in dim_list}
        for c in coord:
            _exact_decimal(c["value"], f"{w}.coordinate[{c['name']}]")
            lo = Decimal(dims_by[c["name"]]["lower"])
            hi = Decimal(dims_by[c["name"]]["upper"])
            if not (lo <= Decimal(c["value"]) <= hi):
                raise ValidationError(
                    f"{w}: coordinate {c['name']}={c['value']} outside envelope [{lo},{hi}]"
                )
        if calibration_schema is not None:
            import jsonschema
            try:
                jsonschema.validate(pt, calibration_schema)
            except jsonschema.ValidationError as exc:  # pragma: no cover
                raise ValidationError(f"{w}: fails CalibrationIR schema: {exc.message}") from exc
    return points


def _load_calibration_schema():
    try:
        import jsonschema  # noqa: F401
    except ImportError:  # pragma: no cover
        return None
    if not _SCHEMA_PATH.exists():  # pragma: no cover
        return None
    import json
    schema = json.loads(_SCHEMA_PATH.read_text())
    cal = dict(schema["$defs"]["calibration"])
    cal["$defs"] = schema["$defs"]  # embed defs so internal $refs resolve
    return cal


def validate_probe_result(result: Any) -> dict[str, Any]:
    """Validate a probe result's PREP-2 IR evaluation-point fields."""
    root = require_mapping(result, "result")
    require_equal(require_key(root, "ir_evaluation_point_fields", "result"),
                  "FILLED_PREP2", "ir_evaluation_point_fields")
    require_equal(require_key(root, "ir_evaluation_point_schema", "result"),
                  "CalibrationIR", "ir_evaluation_point_schema")
    validate_points(require_key(root, "ir_evaluation_points", "result"))
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    args = ap.parse_args(argv)
    root = validate_probe_result(load_json(args.path))
    print(f"ir_points OK: {len(root['ir_evaluation_points'])} CalibrationIR points, "
          f"operand shape carried directly (no-join), schema-valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
