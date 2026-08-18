#!/usr/bin/env python3
"""Parser + validator for gpu-benchmark-result-v1 evaluation points (priority 4).

Covers the component_latency / pcie_transfer_latency / moe_replay_* evaluation
points emitted by the existing benchmark package (benchmark.py:88), which is what
priority-4 operand-shape / concurrency gap measurements produce.

It also enforces the GAP-4 anti-regression rule: a component_latency evaluation
point MUST carry an operand-shape feature (``expert_tokens``) directly, so the
downstream IR pipeline never has to join back to the raw record via
``source_record_id`` and parse the case string (measurement_gaps.json GAP-4).
Use ``--enforce-gap4`` for measurements produced AFTER the fix; without the flag
the parser reports GAP-4-affected points instead of failing (legacy outputs).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_in,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_in,
    )

METRICS = ("component_latency", "pcie_transfer_latency",
           "moe_replay_tpot", "moe_replay_throughput")


def validate(result: Any, enforce_gap4: bool = False) -> dict[str, Any]:
    root = require_mapping(result, "result")
    require_in(require_key(root, "schema_version", "result"),
               ["gpu-benchmark-result-v1"], "schema_version")
    points = require_list(require_key(root, "evaluation_points", "result"), "evaluation_points")
    if not points:
        raise ValidationError("evaluation_points: empty")

    gap4_affected: list[str] = []
    for i, pt in enumerate(points):
        w = f"evaluation_points[{i}]"
        require_mapping(pt, w)
        metric = require_in(require_key(pt, "metric", w), METRICS, f"{w}.metric")
        require_key(pt, "source_record_id", w)
        measured = require_key(pt, "measured", w)
        if not isinstance(measured, (int, float)) or isinstance(measured, bool):
            raise ValidationError(f"{w}.measured: expected number, got {type(measured).__name__}")
        features = require_mapping(require_key(pt, "features", w), f"{w}.features")
        if metric == "component_latency":
            if "expert_tokens" not in features:
                if enforce_gap4:
                    raise ValidationError(
                        f"{w}: component_latency point missing operand-shape feature "
                        "'expert_tokens' (GAP-4). A post-fix generator must carry it "
                        "directly, not require a join back to source_record_id."
                    )
                gap4_affected.append(pt.get("source_record_id", w))
        elif metric == "pcie_transfer_latency":
            require_key(features, "bytes", f"{w}.features")
            require_key(features, "direction", f"{w}.features")
        elif metric in ("moe_replay_tpot", "moe_replay_throughput"):
            require_key(features, "tokens", f"{w}.features")
            require_key(features, "cpu_calls", f"{w}.features")
    root["_gap4_affected_points"] = gap4_affected
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--enforce-gap4", action="store_true",
                    help="fail if any component_latency point lacks expert_tokens")
    args = ap.parse_args(argv)
    root = validate(load_json(args.path), enforce_gap4=args.enforce_gap4)
    affected = root["_gap4_affected_points"]
    print(f"component_eval OK: {len(root['evaluation_points'])} points; "
          f"GAP-4-affected (need join): {len(affected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
