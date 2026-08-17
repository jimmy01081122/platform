#!/usr/bin/env python3
"""Deterministic collector process used by scheduler fault tests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("success", "partial", "nonzero", "sigterm"),
        default="success",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw = args.output_dir / "raw.json"
    if args.mode == "partial":
        raw.write_bytes(b"")
    else:
        raw.write_text(
            json.dumps({
                "work_unit_id": os.environ["PROJECTCTL_WORK_UNIT_ID"],
                "logical_pass": os.environ["PROJECTCTL_LOGICAL_PASS"],
            }) + "\n",
            encoding="utf-8",
        )
    (args.output_dir / "COLLECTOR_RESULT.json").write_text(
        json.dumps({
            "status": "success",
            "schema_valid": True,
            "raw_files": ["raw.json"],
            "work_unit_id": os.environ["PROJECTCTL_WORK_UNIT_ID"],
        }) + "\n",
        encoding="utf-8",
    )
    if args.mode == "nonzero":
        return 7
    if args.mode == "sigterm":
        # Simulates the observable return status without signaling the test parent.
        return 143
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
