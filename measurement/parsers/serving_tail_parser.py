#!/usr/bin/env python3
"""Parser + validator for SERV-P0-25 tail-CI serving output (priority 5).

Validates the completion-latency percentile summary from a full-restart 10K
serving run. Enforces:
  * monotone percentiles p50 <= p95 <= p99 <= max
  * the run is a fresh attempt (attempt_id present) that started from request 0
    and did NOT resume partial progress (root spec §9.1 priority 5 hard rule)
  * request count matches the declared plan

A run that silently resumed prior progress, or has non-monotone percentiles,
fails loudly rather than being accepted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_key,
        require_positive_int, require_type,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.parsers.common import (
        ValidationError, load_json, require_mapping, require_key,
        require_positive_int, require_type,
    )

SCHEMA = "serving-tail-ci-result-v1"
PCTL_ORDER = ["p50_ms", "p95_ms", "p99_ms", "max_ms"]


def validate(result: Any) -> dict[str, Any]:
    root = require_mapping(result, "result")
    if require_key(root, "schema_version", "result") != SCHEMA:
        raise ValidationError(f"schema_version: expected {SCHEMA}")
    require_key(root, "attempt_id", "result")
    start = require_key(root, "start_request_index", "result")
    if start != 0:
        raise ValidationError(
            f"start_request_index {start} != 0: priority-5 must rerun from request 0, "
            "not resume partial progress"
        )
    if require_type(require_key(root, "resumed_partial_progress", "result"),
                    bool, "resumed_partial_progress"):
        raise ValidationError(
            "resumed_partial_progress is true: priority-5 forbids continuing any "
            "existing partial progress; a fresh attempt_id from request 0 is required"
        )
    planned = require_positive_int(require_key(root, "planned_requests", "result"), "planned_requests")
    completed = require_positive_int(require_key(root, "completed_requests", "result"), "completed_requests")
    if completed != planned:
        raise ValidationError(f"completed_requests {completed} != planned_requests {planned}")

    pctl = require_mapping(require_key(root, "completion_latency", "result"), "completion_latency")
    values = []
    for key in PCTL_ORDER:
        v = require_key(pctl, key, "completion_latency")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValidationError(f"completion_latency.{key}: expected number")
        values.append(float(v))
    for a, b, ka, kb in zip(values, values[1:], PCTL_ORDER, PCTL_ORDER[1:]):
        if a > b:
            raise ValidationError(f"non-monotone percentiles: {ka} {a} > {kb} {b}")
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    args = ap.parse_args(argv)
    root = validate(load_json(args.path))
    lat = root["completion_latency"]
    print(f"serving_tail OK: attempt={root['attempt_id']} "
          f"requests={root['completed_requests']} p99={lat['p99_ms']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
