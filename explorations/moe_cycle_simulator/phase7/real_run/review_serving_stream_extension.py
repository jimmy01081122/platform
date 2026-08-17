#!/usr/bin/env python3
"""Verify that an extended open-loop run preserves a frozen arrival prefix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PREFIX_FIELDS = (
    "arrival_mode",
    "arrival_seed",
    "arrival_rate_rps",
    "arrival_index",
    "request_id",
    "slot",
    "class",
    "input_tokens",
    "output_tokens",
    "scheduled_offset_ns",
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check(name: str, expected: Any, observed: Any, passed: bool) -> dict[str, Any]:
    return {
        "name": name,
        "expected": expected,
        "observed": observed,
        "status": "PASS" if passed else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-run-dir", type=Path, required=True)
    parser.add_argument("--extension-run-dir", type=Path, required=True)
    parser.add_argument("--prefix-count", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    base = read_jsonl(args.base_run_dir / "arrival_trace.jsonl")
    extension = read_jsonl(args.extension_run_dir / "arrival_trace.jsonl")
    checks: list[dict[str, Any]] = []
    checks.append(check("base_trace_present", ">=prefix-count", len(base), len(base) >= args.prefix_count))
    checks.append(check("extension_trace_present", ">=prefix-count", len(extension), len(extension) >= args.prefix_count))

    mismatches: list[dict[str, Any]] = []
    if len(base) >= args.prefix_count and len(extension) >= args.prefix_count:
        for index, (base_row, extension_row) in enumerate(zip(base[:args.prefix_count], extension[:args.prefix_count])):
            for field in PREFIX_FIELDS:
                if base_row.get(field) != extension_row.get(field):
                    mismatches.append({
                        "arrival_index": index,
                        "field": field,
                        "base": base_row.get(field),
                        "extension": extension_row.get(field),
                    })
    checks.append(check("frozen_prefix_fields", [], mismatches, not mismatches))

    extension_indices = [row.get("arrival_index") for row in extension]
    checks.append(
        check(
            "extension_indices_contiguous",
            list(range(len(extension))),
            extension_indices,
            extension_indices == list(range(len(extension))),
        )
    )
    critical_failures = [item for item in checks if item["status"] == "FAIL"]
    report = {
        "schema_version": "phase7-serving-stream-extension-review-v1",
        "status": "PASS" if not critical_failures else "FAIL",
        "base_run_dir": str(args.base_run_dir.resolve()),
        "extension_run_dir": str(args.extension_run_dir.resolve()),
        "prefix_count": args.prefix_count,
        "checks": checks,
        "critical_failure_count": len(critical_failures),
        "raw_unchanged": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "prefix_count", "critical_failure_count")}, indent=2))
    return 0 if not critical_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
