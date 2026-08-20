#!/usr/bin/env python3
"""Parser + validator for gpu-multistream-aggregate-result-v1 (PREP-3, V2-GAP-A).

Enforces the physical and axis invariants of a multi-object concurrent-transfer
measurement:

  * concurrency_axis == "num_objects"  -- this is MULTI-OBJECT concurrency, and
    every cell must carry copy_streams == 1. Borrowing the intra-object S /
    copy_streams axis to express multi-object concurrency is a category error
    (OWNER_RESOLUTION 2026-08-18) and is rejected, not silently accepted.
  * per_object_ms has exactly num_objects positive entries; max/sum summaries
    match the list.
  * aggregate envelope:  max_per_object_ms <= aggregate_mean <= sum_per_object_ms.
    An aggregate below its slowest constituent is non-physical (it cannot finish
    before the slowest transfer); an aggregate above the fully-serialized sum is
    non-physical too. Either raises.
  * aggregate_completion_ms_samples has exactly `repeats` entries and its mean
    matches aggregate_completion_ms_mean.

A mis-shaped or non-physical record RAISES rather than being skipped -- the
deliberate opposite of the silent-skip anti-pattern (parsers/__init__.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_positive_int, require_in,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_positive_int, require_in,
    )

SCHEMA = "gpu-multistream-aggregate-result-v1"
_REL_TOL = 1e-6


def _require_positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{where}: expected number, got {type(value).__name__}")
    if value <= 0:
        raise ValidationError(f"{where}: expected positive number, got {value}")
    return float(value)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _REL_TOL * max(1.0, abs(a), abs(b))


def validate(result: Any) -> dict[str, Any]:
    root = require_mapping(result, "result")
    require_equal(require_key(root, "schema_version", "result"), SCHEMA, "schema_version")
    require_key(root, "backend", "result")
    require_key(root, "evidence", "result")
    require_equal(
        require_key(root, "concurrency_axis", "result"), "num_objects",
        "concurrency_axis",
    )
    repeats = require_positive_int(require_key(root, "repeats", "result"), "repeats")
    cells = require_list(require_key(root, "cells", "result"), "cells")
    if not cells:
        raise ValidationError("cells: empty; V2-GAP-A probe must have measured cells")

    for ci, cell in enumerate(cells):
        cw = f"cells[{ci}]"
        require_mapping(cell, cw)
        num_objects = require_positive_int(
            require_key(cell, "num_objects", cw), f"{cw}.num_objects")
        _require_positive_number(require_key(cell, "object_bytes", cw), f"{cw}.object_bytes")
        require_in(require_key(cell, "direction", cw), ("h2d", "d2h"), f"{cw}.direction")
        # Axis discipline: multi-object concurrency must NOT ride the S axis.
        streams = require_key(cell, "copy_streams", cw)
        if streams != 1:
            raise ValidationError(
                f"{cw}.copy_streams: expected 1 (V2-GAP-A is multi-object "
                f"concurrency on the num_objects axis; the intra-object S / "
                f"copy_streams axis MUST NOT be borrowed), got {streams!r}"
            )

        per_object = require_list(require_key(cell, "per_object_ms", cw), f"{cw}.per_object_ms")
        if len(per_object) != num_objects:
            raise ValidationError(
                f"{cw}.per_object_ms: has {len(per_object)} entries != num_objects {num_objects}"
            )
        per_vals = [
            _require_positive_number(v, f"{cw}.per_object_ms[{i}]")
            for i, v in enumerate(per_object)
        ]
        max_reported = _require_positive_number(
            require_key(cell, "max_per_object_ms", cw), f"{cw}.max_per_object_ms")
        sum_reported = _require_positive_number(
            require_key(cell, "sum_per_object_ms", cw), f"{cw}.sum_per_object_ms")
        if not _close(max_reported, max(per_vals)):
            raise ValidationError(
                f"{cw}.max_per_object_ms {max_reported} != max(per_object_ms) {max(per_vals)}")
        if not _close(sum_reported, sum(per_vals)):
            raise ValidationError(
                f"{cw}.sum_per_object_ms {sum_reported} != sum(per_object_ms) {sum(per_vals)}")

        samples = require_list(
            require_key(cell, "aggregate_completion_ms_samples", cw),
            f"{cw}.aggregate_completion_ms_samples")
        if len(samples) != repeats:
            raise ValidationError(
                f"{cw}.aggregate_completion_ms_samples: has {len(samples)} entries "
                f"!= repeats {repeats}")
        sample_vals = [
            _require_positive_number(v, f"{cw}.aggregate_completion_ms_samples[{i}]")
            for i, v in enumerate(samples)
        ]
        mean_reported = _require_positive_number(
            require_key(cell, "aggregate_completion_ms_mean", cw),
            f"{cw}.aggregate_completion_ms_mean")
        if not _close(mean_reported, sum(sample_vals) / len(sample_vals)):
            raise ValidationError(
                f"{cw}.aggregate_completion_ms_mean {mean_reported} != mean(samples) "
                f"{sum(sample_vals) / len(sample_vals)}")

        # Physical envelope: max(per_object) <= aggregate <= sum(per_object).
        if mean_reported < max_reported and not _close(mean_reported, max_reported):
            raise ValidationError(
                f"{cw}: non-physical aggregate {mean_reported} ms < slowest object "
                f"{max_reported} ms (aggregate cannot finish before its slowest transfer)")
        if mean_reported > sum_reported and not _close(mean_reported, sum_reported):
            raise ValidationError(
                f"{cw}: non-physical aggregate {mean_reported} ms > serialized sum "
                f"{sum_reported} ms (aggregate cannot exceed running all objects serially)")
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    args = ap.parse_args(argv)
    root = validate(load_json(args.path))
    print(f"multistream_aggregate OK: {len(root['cells'])} cells, "
          f"axis={root['concurrency_axis']}, evidence={root['evidence']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
