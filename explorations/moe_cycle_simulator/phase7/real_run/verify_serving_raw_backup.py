#!/usr/bin/env python3
"""Verify that a completed serving run was copied to local storage.

This is intentionally a post-copy presence check. It does not hash or modify
remote data; the monitor's successful recursive scp return code remains the
primary transfer evidence.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


EXPECTED_RAW_FILES = (
    "arrival_trace.jsonl",
    "input_fixture.json",
    "manifest.json",
    "requested_engine_args.json",
    "requests.jsonl",
    "result.json",
    "status.json",
    "telemetry.jsonl",
    "warmup_requests.jsonl",
)
TERMINAL_STATES = {
    "REVIEW_COMPLETE",
    "REVIEW_COMPLETE_WITH_FAILURE",
    "BACKUP_FAILED",
}


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--local-run-dir", type=Path, required=True)
    parser.add_argument("--local-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=45)
    args = parser.parse_args()

    if args.poll_seconds < 1:
        raise SystemExit("poll interval must be positive")

    while True:
        state = read_json(args.state)
        if state and state.get("status") in TERMINAL_STATES:
            break
        time.sleep(args.poll_seconds)

    missing_files = [name for name in EXPECTED_RAW_FILES if not (args.local_run_dir / name).is_file()]
    backup = state.get("backup", {})
    run_copy_returncode = backup.get("run_returncode")
    log_copy_returncode = backup.get("log_returncode")
    report: dict[str, Any] = {
        "schema_version": "phase7-serving-raw-backup-verification-v1",
        "state": state.get("status"),
        "state_path": str(args.state.resolve()),
        "local_run_dir": str(args.local_run_dir.resolve()),
        "local_log": str(args.local_log.resolve()),
        "recursive_scp_run_returncode": run_copy_returncode,
        "scp_log_returncode": log_copy_returncode,
        "missing_expected_raw_files": missing_files,
        "local_run_dir_exists": args.local_run_dir.is_dir(),
        "local_log_exists": args.local_log.is_file(),
        "expected_raw_file_count": len(EXPECTED_RAW_FILES),
        "status": (
            "PASS"
            if state.get("status") in {"REVIEW_COMPLETE", "REVIEW_COMPLETE_WITH_FAILURE"}
            and run_copy_returncode == 0
            and log_copy_returncode == 0
            and args.local_run_dir.is_dir()
            and args.local_log.is_file()
            and not missing_files
            else "FAIL"
        ),
    }
    write_json(args.output, report)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
