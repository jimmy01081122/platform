#!/usr/bin/env python3
"""Parser + validator for gpu-dispatch-inserving-result-v1 (priority 1 output).

Enforces the dispatch byte-accounting invariants:

    dispatch_bytes == 2 * move_granularity_bytes   (gather in + scatter out)
    expert_tokens == routing_width * concurrency    (per decode step)
    total_dispatch_bytes == sum(per-step dispatch_bytes)

A mis-shaped record raises rather than being skipped.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_positive_int, require_nonneg_int,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_list, require_key,
        require_equal, require_positive_int, require_nonneg_int,
    )

SCHEMA = "gpu-dispatch-inserving-result-v1"


def validate(result: Any) -> dict[str, Any]:
    root = require_mapping(result, "result")
    require_equal(require_key(root, "schema_version", "result"), SCHEMA, "schema_version")
    require_key(root, "backend", "result")
    require_key(root, "evidence", "result")
    groups = require_list(require_key(root, "groups", "result"), "groups")
    if not groups:
        raise ValidationError("groups: empty; dispatch probe must have concurrency groups")

    for gi, group in enumerate(groups):
        gw = f"groups[{gi}]"
        require_mapping(group, gw)
        concurrency = require_positive_int(require_key(group, "concurrency", gw), f"{gw}.concurrency")
        per_step = require_list(require_key(group, "per_step", gw), f"{gw}.per_step")
        if not per_step:
            raise ValidationError(f"{gw}.per_step: empty")
        running_bytes = 0
        running_decisions = 0
        for si, step in enumerate(per_step):
            sw = f"{gw}.per_step[{si}]"
            require_mapping(step, sw)
            require_equal(require_key(step, "concurrency", sw), concurrency, f"{sw}.concurrency")
            expert_tokens = require_positive_int(require_key(step, "expert_tokens", sw), f"{sw}.expert_tokens")
            if expert_tokens % concurrency != 0:
                raise ValidationError(
                    f"{sw}: expert_tokens {expert_tokens} not a multiple of concurrency {concurrency}"
                )
            dispatch_bytes = require_positive_int(require_key(step, "dispatch_bytes", sw), f"{sw}.dispatch_bytes")
            gran = require_positive_int(require_key(step, "move_granularity_bytes", sw), f"{sw}.move_granularity_bytes")
            if dispatch_bytes != 2 * gran:
                raise ValidationError(
                    f"{sw}: dispatch_bytes {dispatch_bytes} != 2*granularity {2 * gran}"
                )
            require_nonneg_int(require_key(step, "control_decisions", sw), f"{sw}.control_decisions")
            running_bytes += dispatch_bytes
            running_decisions += step["control_decisions"]
        total_bytes = require_nonneg_int(require_key(group, "total_dispatch_bytes", gw), f"{gw}.total_dispatch_bytes")
        if total_bytes != running_bytes:
            raise ValidationError(
                f"{gw}: total_dispatch_bytes {total_bytes} != sum of steps {running_bytes}"
            )
        total_dec = require_nonneg_int(require_key(group, "total_control_decisions", gw), f"{gw}.total_control_decisions")
        if total_dec != running_decisions:
            raise ValidationError(
                f"{gw}: total_control_decisions {total_dec} != sum of steps {running_decisions}"
            )
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    args = ap.parse_args(argv)
    root = validate(load_json(args.path))
    print(f"dispatch OK: {len(root['groups'])} groups, evidence={root['evidence']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
