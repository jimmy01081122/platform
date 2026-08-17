#!/usr/bin/env python3
"""Audit a live trace session before the GPU instance is released."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from trace_package_verify import FAILED, verify_root


def directory_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def normalize_remediation(root: Path, finding: dict) -> None:
    rerun = finding.get("rerun_command")
    unsupported_run_sh = (
        isinstance(rerun, str)
        and rerun.startswith("./run.sh ")
        and any(option in rerun for option in (
            "--run-group", "--profiler-pass", "--resume",
        ))
    )
    details = finding.setdefault("details", {})
    manifest_path = finding.get("path")
    if isinstance(manifest_path, str) and manifest_path.endswith(
        "PASS_MANIFEST.json"
    ):
        try:
            manifest = json.loads((root / manifest_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = {}
        adapter = manifest.get("collector_adapter")
        if isinstance(adapter, str) and not (Path(__file__).parents[1] / adapter).is_file():
            finding.pop("rerun_command", None)
            details["remediation_state"] = "blocked_no_executable_collector"
            blocked = manifest.get("blocked_command")
            if isinstance(blocked, str) and blocked:
                details["blocked_state_command"] = blocked
            return
    if unsupported_run_sh or not isinstance(rerun, str) or not rerun.strip():
        finding.pop("rerun_command", None)
        details["remediation_state"] = "no_executable_remediation"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-root", required=True, type=Path)
    parser.add_argument(
        "--minimum-free-bytes", type=int,
        help="Override the packaging free-space requirement",
    )
    parser.add_argument(
        "--report", type=Path,
        help="Default: SESSION_ROOT/TRACE_COMPLETENESS_REPORT.json",
    )
    args = parser.parse_args()
    root = args.session_root.resolve()
    code, report = verify_root(root)
    current_bytes = directory_bytes(root)
    required_free = (
        args.minimum_free_bytes
        if args.minimum_free_bytes is not None
        else max(64 * 1024 * 1024, current_bytes * 2)
    )
    free_bytes = shutil.disk_usage(root).free
    report["storage"] = {
        "current_package_bytes": current_bytes,
        "required_free_bytes": required_free,
        "available_free_bytes": free_bytes,
    }
    if free_bytes < required_free:
        report["findings"].append({
            "finding_id": "TRACE.STORAGE.INSUFFICIENT",
            "severity": "error",
            "message": (
                f"packaging needs {required_free} free bytes, only {free_bytes} available"
            ),
            "path": str(root),
            "rerun_command": (
                f"python3 scripts/trace_audit.py --session-root {root}"
            ),
            "waivable": False,
        })
        report["status"] = "failed"
        code = FAILED
    for finding in report["findings"]:
        if finding.get("severity") in ("error", "incomplete"):
            normalize_remediation(root, finding)
    state_estimates = {}
    for finding in report["findings"]:
        details = finding.get("details") or {}
        state_id = details.get("state_id")
        estimate = details.get("estimate_minutes")
        if state_id and isinstance(estimate, (int, float)):
            state_estimates[state_id] = estimate
    report["estimate_minutes"] = sum(state_estimates.values())
    report["audit_dimensions"] = [
        "missing", "empty", "truncated", "hash", "token", "repetitions",
        "environment", "native", "canonical", "schema", "archive", "converter",
    ]
    report["finding_count"] = len(report["findings"])
    destination = args.report or root / "TRACE_COMPLETENESS_REPORT.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
