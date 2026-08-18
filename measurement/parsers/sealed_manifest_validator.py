#!/usr/bin/env python3
"""Validator for sealed-holdout-manifest-v1.

Recomputes every per-cell SHA-256 and the overall assignment hash from the
recorded seed, so a manifest that was edited after sealing fails loudly. This is
the audit that makes "sealed" mean something.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_in,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_in,
    )

SCHEMA = "sealed-holdout-manifest-v1"
SPLITS = ("fit", "validation", "holdout")


def _canonical(metric: str, params: dict) -> str:
    return metric + "|" + "|".join(f"{k}={params[k]}" for k in sorted(params))


def validate(manifest: Any) -> dict[str, Any]:
    root = require_mapping(manifest, "manifest")
    require_equal(require_key(root, "schema_version", "manifest"), SCHEMA, "schema_version")
    seed = require_key(root, "seed", "manifest")
    require_key(root, "sealed_at", "manifest")
    cells = require_list(require_key(root, "cells", "manifest"), "cells")
    if not cells:
        raise ValidationError("cells: empty")

    counts = {s: 0 for s in SPLITS}
    assignment_lines: list[str] = []
    for i, cell in enumerate(cells):
        w = f"cells[{i}]"
        require_mapping(cell, w)
        metric = require_key(cell, "metric", w)
        params = require_mapping(require_key(cell, "params", w), f"{w}.params")
        cell_id = require_key(cell, "cell_id", w)
        expected_id = _canonical(metric, params)
        if cell_id != expected_id:
            raise ValidationError(f"{w}: cell_id {cell_id!r} != canonical {expected_id!r}")
        recomputed = hashlib.sha256((seed + "\x1f" + cell_id).encode()).hexdigest()
        if cell.get("sha256") != recomputed:
            raise ValidationError(
                f"{w}: sha256 mismatch (manifest {cell.get('sha256')!r}, recomputed {recomputed!r}) "
                "-- manifest was altered after sealing"
            )
        split = require_in(require_key(cell, "split", w), SPLITS, f"{w}.split")
        counts[split] += 1
        assignment_lines.append(f"{cell_id}\t{split}")

    assignment_sha256 = hashlib.sha256("\n".join(assignment_lines).encode()).hexdigest()
    if root.get("assignment_sha256") != assignment_sha256:
        raise ValidationError(
            f"assignment_sha256 mismatch (manifest {root.get('assignment_sha256')!r}, "
            f"recomputed {assignment_sha256!r}) -- assignments were altered after sealing"
        )
    if root.get("cell_total") != len(cells):
        raise ValidationError(f"cell_total {root.get('cell_total')} != len(cells) {len(cells)}")
    for s in SPLITS:
        if root.get("cell_counts", {}).get(s) != counts[s]:
            raise ValidationError(f"cell_counts[{s}] disagrees with recomputed {counts[s]}")
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    args = ap.parse_args(argv)
    root = validate(load_json(args.path))
    c = root["cell_counts"]
    print(f"sealed manifest OK: {root['cell_total']} cells "
          f"(fit={c['fit']} val={c['validation']} holdout={c['holdout']}), "
          f"sealed_at={root['sealed_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
