#!/usr/bin/env python3
"""Fail-closed source, bundle and run validator for simulator Phase 2."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

PHASE2_ROOT = Path(__file__).resolve().parent
SIM_ROOT = PHASE2_ROOT.parent
REPO_ROOT = SIM_ROOT.parents[1]
sys.path.insert(0, str(PHASE2_ROOT))
sys.path.insert(0, str(SIM_ROOT / "tools"))

from build_fixture import build_fixture  # noqa: E402
from canonical_ir import (  # noqa: E402
    IR_KINDS,
    CanonicalIRError,
    read_bundle,
    semantic_hashes,
    validate_records,
    write_bundle,
)
from contract_runtime import validate_runtime_variant  # noqa: E402
from validate_phase0 import load_json, sha256_file  # noqa: E402

PHASE1_LEDGER_SHA256 = (
    "478674f22a8022c5ae8fdc343cb67173435d8308cf4e742b1629da975310b445"
)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_RUN_FILES = {
    "artifacts/canonical_bundle/artifact-envelope.json",
    "artifacts/canonical_bundle/clock_alignment.arrow.zst",
    "artifacts/canonical_bundle/calibration.arrow.zst",
    "artifacts/canonical_bundle/event.arrow.zst",
    "artifacts/canonical_bundle/model.arrow.zst",
    "artifacts/canonical_bundle/placement.arrow.zst",
    "artifacts/canonical_bundle/platform.arrow.zst",
    "artifacts/canonical_bundle/result.arrow.zst",
    "artifacts/canonical_bundle/routing.arrow.zst",
    "artifacts/canonical_bundle/workload.arrow.zst",
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


def fixture_records() -> list[dict[str, Any]]:
    fixture = load_json(PHASE2_ROOT / "fixtures" / "canonical_ir_bundle.json")
    if fixture.get("schema_version") != "canonical-ir-fixture-bundle-v1":
        raise CanonicalIRError("fixture schema version mismatch")
    return fixture["records"]


def validate_source() -> dict[str, Any]:
    if importlib.metadata.version("jsonschema") != "4.24.0":
        raise CanonicalIRError("jsonschema must be exactly 4.24.0")
    if importlib.metadata.version("pyarrow") != "20.0.0":
        raise CanonicalIRError("pyarrow must be exactly 20.0.0")
    import jsonschema

    for path in sorted((PHASE2_ROOT / "schemas").glob("*.schema.json")):
        schema = load_json(path)
        jsonschema.Draft202012Validator.check_schema(schema)
        if schema.get("additionalProperties") is not False:
            raise CanonicalIRError(f"{path}: root must be strict")
    records = fixture_records()
    if build_fixture() != records:
        raise CanonicalIRError("generated fixture does not match frozen fixture")
    ordered = validate_records(records)
    runtime_variant = load_json(
        PHASE2_ROOT / "contracts" / "runtime_variant_fixture.json"
    )
    runtime_schema = load_json(
        SIM_ROOT / "schemas" / "runtime_variant.schema.json"
    )
    jsonschema.Draft202012Validator(runtime_schema).validate(runtime_variant)
    validate_runtime_variant(runtime_variant)
    runtime_variant_hash = runtime_variant["variant_id"]
    observed_runtime_hashes = {
        record["payload"]["runtime_variant_hash"]
        for record in ordered
        if record["ir_kind"]
        in {"WorkloadIR", "EventIR", "CalibrationIR", "ResultIR"}
    }
    if observed_runtime_hashes != {runtime_variant_hash}:
        raise CanonicalIRError("runtime variant fixture binding mismatch")
    root = semantic_hashes(ordered)[1]
    phase1_ledger = SIM_ROOT / "phase1" / "governance" / "checksums.sha256"
    if sha256_file(phase1_ledger) != PHASE1_LEDGER_SHA256:
        raise CanonicalIRError("Phase 1 ledger identity mismatch")
    phase1_review = load_json(
        SIM_ROOT
        / "phase1"
        / "governance"
        / "reviews"
        / "phase1_r2_aggregate.json"
    )
    if phase1_review["verdict"] != "GO" or phase1_review["blockers"]:
        raise CanonicalIRError("Phase 1 promotion authority is absent")
    with tempfile.TemporaryDirectory(prefix="phase2-source-validation-") as temp:
        first = write_bundle(Path(temp) / "batch1", ordered, batch_size=1)
        second = write_bundle(
            Path(temp) / "batch64k",
            list(reversed(ordered)),
            batch_size=65_536,
        )
        third = write_bundle(
            Path(temp) / "split",
            ordered,
            batch_size=1,
            max_rows_per_partition=1,
        )
        read_bundle(Path(temp) / "batch1" / "artifact-envelope.json")
        read_bundle(Path(temp) / "batch64k" / "artifact-envelope.json")
        read_bundle(Path(temp) / "split" / "artifact-envelope.json")
        if len(third["partitions"]) != len(ordered) or (
            len(
                {
                    first["bundle_semantic_root"],
                    second["bundle_semantic_root"],
                    third["bundle_semantic_root"],
                }
            )
            != 1
        ):
            raise CanonicalIRError("physical layout changed semantic root")
    return {
        "record_count": len(ordered),
        "ir_kind_count": len(IR_KINDS),
        "bundle_semantic_root": root,
    }


def scan_tree(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = {"."}
    for directory, names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        base = Path(directory)
        for name in names:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise CanonicalIRError(f"forbidden run entry: {path}")
            directories.add(path.relative_to(root).as_posix())
        for name in file_names:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise CanonicalIRError(f"forbidden run entry: {path}")
            files.add(path.relative_to(root).as_posix())
    return files, directories


def validate_ledger(run_dir: Path) -> None:
    files, directories = scan_tree(run_dir)
    if "checksums.sha256" not in files:
        raise CanonicalIRError("run checksum ledger is missing")
    ledger_members = files - {"checksums.sha256"}
    if ledger_members != EXPECTED_RUN_FILES:
        raise CanonicalIRError("run file closure mismatch")
    expected_directories = {"."}
    for relative in ledger_members:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if directories != expected_directories:
        raise CanonicalIRError("run directory closure mismatch")
    seen: set[str] = set()
    for line in (run_dir / "checksums.sha256").read_text().splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or not HASH_RE.fullmatch(parts[0]):
            raise CanonicalIRError("invalid run ledger line")
        relative = parts[1]
        if relative in seen or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise CanonicalIRError("unsafe or duplicate run ledger path")
        seen.add(relative)
        if sha256_file(run_dir / relative) != parts[0]:
            raise CanonicalIRError(f"run checksum mismatch: {relative}")
    if seen != ledger_members:
        raise CanonicalIRError("run ledger set mismatch")


def validate_run(run_dir: Path) -> dict[str, Any]:
    validate_ledger(run_dir)
    source = validate_source()
    records, envelope = read_bundle(
        run_dir / "artifacts" / "canonical_bundle" / "artifact-envelope.json"
    )
    if records != validate_records(fixture_records()):
        raise CanonicalIRError("run bundle differs from frozen fixture")
    if envelope["bundle_semantic_root"] != source["bundle_semantic_root"]:
        raise CanonicalIRError("run bundle semantic root mismatch")
    manifest = load_json(run_dir / "manifest.json")
    if (
        manifest.get("run_id") != run_dir.name
        or manifest.get("stage") != "S2"
        or manifest.get("status") != "passed"
        or manifest.get("gpu_used") is not False
        or manifest.get("model_downloaded") is not False
        or manifest.get("validator_return_code") != 0
        or manifest.get("test_return_code") != 0
    ):
        raise CanonicalIRError("run manifest mismatch")
    resolved = load_json(run_dir / "resolved_config.yaml")
    if resolved != {
        "note": "JSON syntax is valid YAML",
        "codec_profile": "arrow-ipc-file-outer-zstd-v1",
        "batch_size": 65536,
        "dependency_root": manifest["command_environment_overrides"]["PYTHONPATH"],
    }:
        raise CanonicalIRError("resolved configuration mismatch")
    inventory_path = run_dir / "environment" / "dependency_inventory.json"
    inventory = load_json(inventory_path)
    if manifest.get("dependency_inventory_sha256") != sha256_file(inventory_path):
        raise CanonicalIRError("dependency inventory hash mismatch")
    from run_phase2_suite import dependency_inventory

    if dependency_inventory(Path(inventory["dependency_root"])) != inventory:
        raise CanonicalIRError("live dependency inventory mismatch")
    tool_versions = load_json(
        run_dir / "environment" / "tool_versions.json"
    )
    import ctypes
    import ctypes.util
    import platform

    zstd = ctypes.CDLL(ctypes.util.find_library("zstd"))
    zstd.ZSTD_versionString.restype = ctypes.c_char_p
    expected_tools = {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "jsonschema": importlib.metadata.version("jsonschema"),
        "pyarrow": importlib.metadata.version("pyarrow"),
        "pytest": importlib.metadata.version("pytest"),
        "libzstd": zstd.ZSTD_versionString().decode("ascii"),
        "phase2_tool_sha256": sha256_file(
            PHASE2_ROOT / "run_phase2_suite.py"
        ),
        "canonical_ir_tool_sha256": sha256_file(
            PHASE2_ROOT / "canonical_ir.py"
        ),
    }
    if tool_versions != expected_tools:
        raise CanonicalIRError("tool version provenance mismatch")
    dependency_root = inventory["dependency_root"]
    expected_environment = {"PYTHONPATH": dependency_root}
    expected_validator = {
        "argv": [
            sys.executable,
            str(PHASE2_ROOT / "validate_phase2.py"),
            "--bundle",
            str(
                run_dir
                / "artifacts"
                / "canonical_bundle"
                / "artifact-envelope.json"
            ),
        ],
        "cwd": str(REPO_ROOT),
        "environment_overrides": expected_environment,
        "return_code": 0,
    }
    expected_tests = {
        "argv": [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(PHASE2_ROOT / "tests" / "test_canonical_ir.py"),
        ],
        "cwd": str(REPO_ROOT),
        "environment_overrides": expected_environment,
        "return_code": 0,
    }
    if load_json(run_dir / "logs" / "validator.command.json") != expected_validator:
        raise CanonicalIRError("validator command provenance mismatch")
    if load_json(run_dir / "logs" / "tests.command.json") != expected_tests:
        raise CanonicalIRError("test command provenance mismatch")
    if manifest["command_cwd"] != str(REPO_ROOT) or (
        manifest["command_environment_overrides"] != expected_environment
    ):
        raise CanonicalIRError("suite execution context mismatch")
    if json.loads(
        (run_dir / "logs" / "suite_command.log").read_text()
    ) != manifest["command"]:
        raise CanonicalIRError("suite command record mismatch")
    if (run_dir / "logs" / "command.log").read_text() != (
        " ".join(manifest["command"]) + "\n"
    ):
        raise CanonicalIRError("human command log mismatch")
    test_stdout = (run_dir / "logs" / "tests.stdout.log").read_text()
    test_match = re.search(r"(?m)^([0-9]+) passed", test_stdout)
    if test_match is None:
        raise CanonicalIRError("test PASS summary is absent")
    test_count = int(test_match.group(1))
    if (run_dir / "logs" / "tests.stderr.log").read_text():
        raise CanonicalIRError("test stderr is nonempty")
    validator_stdout = (
        run_dir / "logs" / "validator.stdout.log"
    ).read_text()
    if (
        "PHASE2_CANONICAL_IR_VALIDATION: PASS" not in validator_stdout
        or source["bundle_semantic_root"] not in validator_stdout
        or (run_dir / "logs" / "validator.stderr.log").read_text()
    ):
        raise CanonicalIRError("validator log provenance mismatch")
    suite_result = load_json(run_dir / "suite_result.json")
    expected_suite_result = {
        "schema_version": "moe-simulator-phase2-suite-result-v1",
        "status": "PASS",
        "validator_return_code": 0,
        "test_return_code": 0,
        "gpu_used": False,
        "model_downloaded": False,
    }
    if suite_result != expected_suite_result:
        raise CanonicalIRError("suite result mismatch")
    metrics = load_json(run_dir / "metrics.json")
    if metrics != {
        "status": "PASS",
        "record_count": source["record_count"],
        "ir_kind_count": 9,
        "partition_count": len(envelope["partitions"]),
        "bundle_semantic_root": source["bundle_semantic_root"],
        "validator_return_code": 0,
        "test_return_code": 0,
        "test_count": test_count,
        "gpu_used": False,
    }:
        raise CanonicalIRError("run metrics mismatch")
    if manifest["validator_return_code"] != suite_result["validator_return_code"] or (
        manifest["test_return_code"] != suite_result["test_return_code"]
    ):
        raise CanonicalIRError("cross-file return code mismatch")
    return source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    result = validate_source()
    if args.bundle:
        records, envelope = read_bundle(args.bundle.resolve())
        if records != validate_records(fixture_records()):
            raise CanonicalIRError("bundle differs from source fixture")
        if envelope["bundle_semantic_root"] != result["bundle_semantic_root"]:
            raise CanonicalIRError("bundle root differs from source")
    if args.run_dir:
        result = validate_run(args.run_dir.resolve())
    print(
        "PHASE2_CANONICAL_IR_VALIDATION: PASS: "
        f"{result['record_count']} records / {result['ir_kind_count']} kinds"
    )
    print(f"semantic_root: {result['bundle_semantic_root']}")
    print("gpu_used: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
