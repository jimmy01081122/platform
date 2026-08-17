#!/usr/bin/env python3
"""Create deterministic trace identities and clock anchors."""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.trace_contract import sha256_file, write_json  # noqa: E402


def file_hash(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"identity input is not a file: {path}")
    return sha256_file(path)


def clock_anchor(profiler_domain: str) -> dict:
    wall_before = time.time_ns()
    monotonic = time.monotonic_ns()
    wall_after = time.time_ns()
    return {
        "wall_clock_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "monotonic_host_ns": monotonic,
        "profiler_domain": profiler_domain,
        "timezone": "UTC",
        "alignment": {
            "method": "host wall-clock bracket around monotonic sample",
            "max_error_ns": max(1, (wall_after - wall_before) // 2),
            "anchors": [{
                "wall_clock_unix_ns": (wall_before + wall_after) // 2,
                "monotonic_host_ns": monotonic,
            }],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", required=True, type=Path)
    parser.add_argument("--configuration", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--profiler-pass", required=True,
                        choices=[f"P{x}" for x in range(7)])
    parser.add_argument("--session-id")
    parser.add_argument("--run-group-id")
    parser.add_argument("--run-id")
    parser.add_argument("--repetition-index", type=int, default=0)
    parser.add_argument("--profiler-domain", default="host_monotonic")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repetition_index < 0:
        parser.error("--repetition-index must be non-negative")
    session_id = args.session_id or f"session-{uuid.uuid4()}"
    run_group_id = args.run_group_id or f"group-{uuid.uuid4()}"
    run_id = args.run_id or f"run-{uuid.uuid4()}"
    payload = {
        "identity": {
            "session_id": session_id,
            "run_group_id": run_group_id,
            "run_id": run_id,
            "model_revision": args.model_revision,
            "workload_hash": file_hash(args.workload),
            "configuration_hash": file_hash(args.configuration),
            "environment_hash": file_hash(args.environment),
            "repetition_index": args.repetition_index,
            "profiler_pass": args.profiler_pass,
        },
        "clock": clock_anchor(args.profiler_domain),
    }
    if args.output:
        write_json(args.output, payload)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
