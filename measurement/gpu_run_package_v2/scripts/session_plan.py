#!/usr/bin/env python3
"""Create a trace session plan without claiming any capture is complete."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.trace_contract import PASSES, sha256_file, write_json  # noqa: E402

TRACE_PROFILES = {
    "minimal": list(PASSES),
    "standard": list(PASSES),
    "maximal": list(PASSES),
}

PROFILE_REQUIREMENTS = {
    "minimal": {
        "P0": "smoke_only",
        "P1": "manifest_only",
        "P2": "manifest_only",
        "P3": "manifest_only",
        "P4": "manifest_only",
        "P5": "environment_snapshot_only",
        "P6": "manifest_only",
    },
    "standard": {
        "P0": "required",
        "P1": "required",
        "P2": "required",
        "P3": "required",
        "P4": "representative_scope_required",
        "P5": "required",
        "P6": "manifest_or_selected_replay",
    },
    "maximal": {pass_id: "mandatory_manifest" for pass_id in PASSES},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def copy_core(source: Path, destination: Path) -> Path:
    source = source.resolve()
    if not source.is_file() or source.is_symlink() or source.stat().st_size == 0:
        raise ValueError(f"core artifact must be a non-empty regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def group_id(model_revision: str, index: int) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model_revision).strip("-")[:40]
    suffix = hashlib.sha256(model_revision.encode()).hexdigest()[:10]
    return f"model-{index:02d}-{slug or 'revision'}-{suffix}"


def refresh_checksums(root: Path) -> None:
    paths = [
        path for path in root.rglob("*")
        if path.is_file()
        and path.name not in ("checksums.sha256", "TRACE_COMPLETENESS_REPORT.json")
    ]
    (root / "checksums.sha256").write_text("".join(
        f"{sha256_file(path)}  {path.relative_to(root).as_posix()}\n"
        for path in sorted(paths)
    ), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-root", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--workload", required=True, type=Path)
    parser.add_argument("--configuration", required=True, type=Path)
    parser.add_argument("--model-revision", required=True, action="append")
    parser.add_argument("--trace-profile", choices=sorted(TRACE_PROFILES),
                        default="maximal")
    parser.add_argument("--required-repetitions", type=int, default=3)
    parser.add_argument("--session-id")
    args = parser.parse_args()
    if args.required_repetitions < 3:
        parser.error("--required-repetitions must be at least 3")
    root = args.session_root.resolve()
    session_path = root / "SESSION_MANIFEST.json"
    if session_path.exists():
        raise SystemExit(f"refusing to overwrite existing session: {session_path}")
    root.mkdir(parents=True, exist_ok=True)
    environment = copy_core(
        args.environment, root / "environment/environment.json"
    )
    workload = copy_core(
        args.workload, root / "workloads/workload_manifest.json"
    )
    configuration = copy_core(
        args.configuration, root / "models/configs/configuration.json"
    )
    environment_hash = sha256_file(environment)
    workload_hash = sha256_file(workload)
    configuration_hash = sha256_file(configuration)
    planned_passes = TRACE_PROFILES[args.trace_profile]
    session_id = args.session_id or f"session-{uuid.uuid4()}"
    expected_runs = []
    for index, revision in enumerate(args.model_revision):
        planned_group = group_id(revision, index)
        expected_runs.append({
            "run_group_id": planned_group,
            "model_revision": revision,
            "workload_hash": workload_hash,
            "configuration_hash": configuration_hash,
            "environment_hash": environment_hash,
            "planned_passes": planned_passes,
            "pass_requirements": PROFILE_REQUIREMENTS[args.trace_profile],
        })
        for pass_id, directory in PASSES.items():
            pass_root = root / "runs" / planned_group / directory
            (pass_root / "runs").mkdir(parents=True, exist_ok=True)
            write_json(pass_root / "PASS_PLAN.json", {
                "schema_version": "trace-pass-plan-v2",
                "pass_id": pass_id,
                "capture_requirement": PROFILE_REQUIREMENTS[
                    args.trace_profile
                ][pass_id],
                "pass_manifest_required": True,
                "unsupported_requires_manifest": True,
                "required_repetitions": args.required_repetitions,
                "initial_status": "planned",
            })
    inventory_path = root / "raw_traces/RAW_INVENTORY.json"
    write_json(inventory_path, {
        "schema_version": "trace-raw-inventory-v2",
        "digest_algorithm": "sha256",
        "content_addressed": True,
        "immutable": True,
        "entries": [],
        "conversions": [],
    })
    capture_plan = {
        "schema_version": "trace-capture-plan-v2",
        "created_utc": utc_now(),
        "session_id": session_id,
        "trace_profile": args.trace_profile,
        "required_repetitions": args.required_repetitions,
        "recommended_repetitions": (
            5 if args.trace_profile == "maximal" else args.required_repetitions
        ),
        "expected_runs": expected_runs,
        "initial_status": "planned",
        "complete_raw_claimed": False,
    }
    (root / "TRACE_CAPTURE_PLAN.yaml").write_text(
        json.dumps(capture_plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_json(session_path, {
        "schema_version": "trace-session-manifest-v2",
        "identity": {
            "session_id": session_id,
            "model_revision": args.model_revision[0],
            "workload_hash": workload_hash,
            "configuration_hash": configuration_hash,
            "environment_hash": environment_hash,
        },
        "required_repetitions": args.required_repetitions,
        "expected_runs": expected_runs,
        "accepted_incomplete": False,
        "artifacts": {
            "environment": environment.relative_to(root).as_posix(),
            "workload": workload.relative_to(root).as_posix(),
            "configuration": configuration.relative_to(root).as_posix(),
            "raw_inventory": inventory_path.relative_to(root).as_posix(),
            "capture_plan": "TRACE_CAPTURE_PLAN.yaml",
        },
    })
    (root / "canonical_traces").mkdir(exist_ok=True)
    (root / "derived_metrics").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "failures").mkdir(exist_ok=True)
    refresh_checksums(root)
    print(session_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
