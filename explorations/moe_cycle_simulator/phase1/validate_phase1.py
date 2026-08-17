#!/usr/bin/env python3
"""Fail-closed replay validator for one Phase 1 CPU/mock spike run."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any

PHASE1_ROOT = Path(__file__).resolve().parent
SIM_ROOT = PHASE1_ROOT.parent
sys.path.insert(0, str(SIM_ROOT / "tools"))
sys.path.insert(0, str(PHASE1_ROOT))

from run_spike import (  # noqa: E402
    PHASE0_LEDGER_SHA256,
    SHAPE_EVENT,
    canonicalize,
    sha256_file,
)
from validate_phase0 import ValidationFailure, load_json, reject_floats  # noqa: E402

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_RUN_FILES = {
    "artifacts/clock_alignments.json",
    "artifacts/event_ir.jsonl",
    "artifacts/raw_mock_runtime_trace.json",
    "artifacts/routing_ir.jsonl",
    "artifacts/v0_audit.json",
    "environment/dependency_inventory.json",
    "environment/tool_versions.json",
    "logs/command.log",
    "logs/stderr.log",
    "logs/stdout.log",
    "logs/suite_command.log",
    "logs/tests.command.json",
    "logs/tests.stderr.log",
    "logs/tests.stdout.log",
    "logs/validator.command.json",
    "logs/validator.stderr.log",
    "logs/validator.stdout.log",
    "manifest.json",
    "metrics.json",
    "resolved_config.yaml",
    "suite_result.json",
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            raise ValidationFailure(f"{path}:{number}: blank JSONL line")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for raw_key, item in pairs:
                key = unicodedata.normalize("NFC", raw_key)
                if key in result:
                    raise ValidationFailure(
                        f"{path}:{number}: duplicate key after NFC normalization"
                    )
                result[key] = item
            return result

        value = json.loads(line, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValidationFailure(f"{path}:{number}: row must be an object")
        reject_floats(value, f"{path}:{number}")
        values.append(value)
    return values


def scan_run_tree(run_dir: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = {"."}
    for directory, directory_names, file_names in os.walk(
        run_dir, topdown=True, followlinks=False
    ):
        base = Path(directory)
        for name in directory_names:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValidationFailure(f"forbidden run-tree entry: {path}")
            directories.add(path.relative_to(run_dir).as_posix())
        for name in file_names:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValidationFailure(f"forbidden run-tree entry: {path}")
            files.add(path.relative_to(run_dir).as_posix())
    return files, directories


def validate_ledger(run_dir: Path) -> None:
    ledger = run_dir / "checksums.sha256"
    seen: set[str] = set()
    listed: set[str] = set()
    for number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
        parts = line.split("  ", 1)
        if len(parts) != 2 or not HASH_RE.fullmatch(parts[0]):
            raise ValidationFailure(f"invalid run ledger line {number}")
        relative = parts[1]
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or relative in seen:
            raise ValidationFailure(f"unsafe or duplicate run ledger path: {relative}")
        seen.add(relative)
        path = run_dir / candidate
        if not path.is_file() or path.is_symlink():
            raise ValidationFailure(f"run ledger member missing or symlink: {relative}")
        if sha256_file(path) != parts[0]:
            raise ValidationFailure(f"run checksum mismatch: {relative}")
        listed.add(relative)
    actual, actual_directories = scan_run_tree(run_dir)
    actual.remove("checksums.sha256")
    if listed != actual:
        raise ValidationFailure("run ledger member set does not match run files")
    if actual != EXPECTED_RUN_FILES:
        raise ValidationFailure("run file set does not match the frozen expected set")
    expected_directories = {"."}
    for relative in actual:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if actual_directories != expected_directories:
        raise ValidationFailure("run directory set does not match expected closure")


def validate_run(run_dir: Path, skip_ledger: bool = False) -> None:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValidationFailure("run directory must be a real directory")
    if not skip_ledger:
        validate_ledger(run_dir)
    phase0_ledger = SIM_ROOT / "governance" / "checksums.sha256"
    if sha256_file(phase0_ledger) != PHASE0_LEDGER_SHA256:
        raise ValidationFailure("frozen Phase 0 ledger identity mismatch")

    manifest = load_json(run_dir / "manifest.json")
    if manifest["run_id"] != run_dir.name:
        raise ValidationFailure("manifest run_id does not match directory")
    expected_manifest = {
        "experiment_id": "moe_cycle_simulator_phase1_cpu_mock",
        "stage": "S1",
        "platform_profile": "cpu_mock_multiclock_multirank",
        "status": "passed",
        "phase0_ledger_sha256": PHASE0_LEDGER_SHA256,
        "gpu_used": False,
        "model_downloaded": False,
        "evidence_class": "synthetic-cpu-mock",
    }
    for key, expected in expected_manifest.items():
        if manifest.get(key) != expected:
            raise ValidationFailure(f"manifest mismatch: {key}")
    if not isinstance(manifest.get("command"), list) or not manifest["command"]:
        raise ValidationFailure("manifest command is missing")

    raw = run_dir / "artifacts" / "raw_mock_runtime_trace.json"
    hashes, expected_alignments, expected_events, expected_routing = canonicalize(raw)
    alignments = load_json(run_dir / "artifacts" / "clock_alignments.json")
    events = load_jsonl(run_dir / "artifacts" / "event_ir.jsonl")
    routing = load_jsonl(run_dir / "artifacts" / "routing_ir.jsonl")
    if alignments != {"alignments": expected_alignments}:
        raise ValidationFailure("alignment replay mismatch")
    if events != expected_events:
        raise ValidationFailure("EventIR replay mismatch")
    if routing != expected_routing:
        raise ValidationFailure("RoutingIR replay mismatch")

    audit = load_json(run_dir / "artifacts" / "v0_audit.json")
    if audit.get("hashes") != hashes:
        raise ValidationFailure("V0 semantic hash replay mismatch")
    if {
        "status": audit.get("status"),
        "findings": audit.get("findings"),
        "execution_role": audit.get("execution_role"),
        "gpu_execution": audit.get("gpu_execution"),
        "runtime_pass": audit.get("runtime_pass"),
    } != {
        "status": "PASS",
        "findings": [],
        "execution_role": "OFFLINE_VALIDATION",
        "gpu_execution": False,
        "runtime_pass": False,
    }:
        raise ValidationFailure("V0 audit role or status mismatch")

    metrics = load_json(run_dir / "metrics.json")
    observed = sorted(
        {event["attributes"]["mock_shape"] for event in events}
    )
    expected_metrics = {
        "status": "PASS",
        "event_count": len(events),
        "routing_record_count": len(routing),
        "alignment_count": len(expected_alignments),
        "source_rank_count": 2,
        "required_shapes": sorted(SHAPE_EVENT),
        "observed_shapes": observed,
        "all_required_shapes_present": observed == sorted(SHAPE_EVENT),
        "v0_findings": 0,
        "gpu_used": False,
    }
    if not skip_ledger:
        expected_metrics.update(
            {
                "validator_return_code": 0,
                "test_return_code": 0,
                "test_count": 16,
            }
        )
    if metrics != expected_metrics:
        raise ValidationFailure("Phase 1 metrics mismatch")

    copied_raw_hash = sha256_file(raw)
    if copied_raw_hash != hashes["raw_sha256"]:
        raise ValidationFailure("raw fixture identity mismatch")
    source_fixture = PHASE1_ROOT / "fixtures" / "mock_runtime_trace.json"
    if copied_raw_hash != sha256_file(source_fixture):
        raise ValidationFailure("run raw fixture does not match reviewed source fixture")
    if not skip_ledger:
        inventory_path = run_dir / "environment" / "dependency_inventory.json"
        inventory = load_json(inventory_path)
        if manifest.get("dependency_inventory_sha256") != sha256_file(inventory_path):
            raise ValidationFailure("dependency inventory hash mismatch")
        versions = {
            item["name"]: item["version"] for item in inventory["distributions"]
        }
        if versions.get("jsonschema") != "4.24.0" or "pytest" not in versions:
            raise ValidationFailure("required dependency versions are not inventoried")
        from run_phase1_suite import dependency_inventory

        live_inventory = dependency_inventory(Path(inventory["dependency_root"]))
        if live_inventory != inventory:
            raise ValidationFailure("live dependency content inventory mismatch")
        expected_cwd = str(SIM_ROOT.parents[1])
        if manifest.get("command_cwd") != expected_cwd:
            raise ValidationFailure("suite command cwd mismatch")
        if manifest.get("command_environment_overrides") != {
            "PYTHONPATH": inventory["dependency_root"]
        }:
            raise ValidationFailure("suite command environment mismatch")
        tool_versions = load_json(run_dir / "environment" / "tool_versions.json")
        if tool_versions.get("jsonschema") != "4.24.0":
            raise ValidationFailure("executed jsonschema identity mismatch")
        suite = load_json(run_dir / "suite_result.json")
        if suite != {
            "schema_version": "moe-simulator-phase1-suite-result-v1",
            "status": "PASS",
            "validator_return_code": 0,
            "test_return_code": 0,
            "gpu_used": False,
            "model_downloaded": False,
        }:
            raise ValidationFailure("suite result mismatch")
        for name in ("validator", "tests"):
            command_record = load_json(run_dir / "logs" / f"{name}.command.json")
            if command_record.get("return_code") != 0:
                raise ValidationFailure(f"{name} recorded nonzero return code")
        if "16 passed" not in (
            run_dir / "logs" / "tests.stdout.log"
        ).read_text(encoding="utf-8"):
            raise ValidationFailure("test PASS summary is missing")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--skip-ledger", action="store_true")
    args = parser.parse_args()
    validate_run(args.run_dir.resolve(), skip_ledger=args.skip_ledger)
    print(f"PHASE1_REPLAY_VALIDATION: PASS: {args.run_dir}")
    print("GPU_USED: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
