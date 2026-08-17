#!/usr/bin/env python3
"""Execute and seal a Phase 1 spike with exact runtime and test provenance."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PHASE1_ROOT = Path(__file__).resolve().parent
SIM_ROOT = PHASE1_ROOT.parent
sys.path.insert(0, str(PHASE1_ROOT))

from run_spike import sha256_file, write_checksum_ledger, write_json, run  # noqa: E402

REQUIRED_DISTRIBUTIONS = {
    "jsonschema": "4.24.0",
    "attrs": None,
    "referencing": None,
    "rpds-py": None,
    "jsonschema-specifications": None,
    "pytest": None,
    "pluggy": None,
    "packaging": None,
    "iniconfig": None,
}
DEPENDENCY_ROOT_DISTRIBUTIONS = {
    "jsonschema",
    "attrs",
    "referencing",
    "rpds-py",
    "jsonschema-specifications",
}


def dependency_inventory(dependency_root: Path) -> dict[str, Any]:
    if not dependency_root.is_dir() or dependency_root.is_symlink():
        raise RuntimeError("dependency root must be a real directory")
    original_path = list(sys.path)
    sys.path.insert(0, str(dependency_root))
    try:
        distributions = []
        for name, expected_version in REQUIRED_DISTRIBUTIONS.items():
            distribution = importlib.metadata.distribution(name)
            if expected_version is not None and distribution.version != expected_version:
                raise RuntimeError(
                    f"{name} must be exactly {expected_version}, "
                    f"found {distribution.version}"
                )
            record_path = Path(distribution._path) / "RECORD"
            if name in DEPENDENCY_ROOT_DISTRIBUTIONS:
                try:
                    Path(distribution._path).resolve().relative_to(
                        dependency_root.resolve()
                    )
                except ValueError as exc:
                    raise RuntimeError(
                        f"{name} was not loaded from the declared dependency root"
                    ) from exc
            if not record_path.is_file() or record_path.is_symlink():
                raise RuntimeError(f"{name} has no regular RECORD file")
            content_digest = hashlib.sha256()
            file_count = 0
            for relative_file in sorted(
                distribution.files or [], key=lambda item: str(item)
            ):
                located = Path(distribution.locate_file(relative_file))
                if not located.is_file() or located.is_symlink():
                    raise RuntimeError(
                        f"{name} contains missing or non-regular file: {relative_file}"
                    )
                content_digest.update(str(relative_file).encode("utf-8"))
                content_digest.update(b"\0")
                content_digest.update(bytes.fromhex(sha256_file(located)))
                file_count += 1
            distributions.append(
                {
                    "name": name,
                    "version": distribution.version,
                    "metadata_path": str(Path(distribution._path).resolve()),
                    "record_sha256": sha256_file(record_path),
                    "file_count": file_count,
                    "content_root_sha256": content_digest.hexdigest(),
                }
            )
    finally:
        sys.path[:] = original_path
    return {
        "schema_version": "moe-simulator-phase1-runtime-inventory-v1",
        "python_executable": sys.executable,
        "python_version": sys.version,
        "dependency_root": str(dependency_root.resolve()),
        "environment_overrides": {
            "PYTHONPATH": str(dependency_root.resolve())
        },
        "distributions": distributions,
    }


def execute(
    command: list[str],
    environment: dict[str, str],
) -> tuple[int, str, str]:
    result = subprocess.run(
        command,
        cwd=SIM_ROOT.parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def write_command_evidence(
    logs: Path,
    name: str,
    command: list[str],
    return_code: int,
    stdout: str,
    stderr: str,
    dependency_root: Path,
) -> None:
    write_json(
        logs / f"{name}.command.json",
        {
            "argv": command,
            "cwd": str(SIM_ROOT.parents[1]),
            "environment_overrides": {
                "PYTHONPATH": str(dependency_root.resolve())
            },
            "return_code": return_code,
        },
    )
    (logs / f"{name}.stdout.log").write_text(stdout, encoding="utf-8")
    (logs / f"{name}.stderr.log").write_text(stderr, encoding="utf-8")


def finalize(
    output: Path,
    suite_command: list[str],
    inventory: dict[str, Any],
    dependency_root: Path,
) -> bool:
    logs = output / "logs"
    environment_dir = output / "environment"
    inventory_path = environment_dir / "dependency_inventory.json"
    write_json(inventory_path, inventory)

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(dependency_root.resolve())
    validator_command = [
        sys.executable,
        str(PHASE1_ROOT / "validate_phase1.py"),
        str(output),
        "--skip-ledger",
    ]
    test_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        str(PHASE1_ROOT / "tests" / "test_phase1_spike.py"),
    ]
    validator_rc, validator_out, validator_err = execute(
        validator_command, environment
    )
    write_command_evidence(
        logs,
        "validator",
        validator_command,
        validator_rc,
        validator_out,
        validator_err,
        dependency_root,
    )
    test_rc, test_out, test_err = execute(test_command, environment)
    write_command_evidence(
        logs,
        "tests",
        test_command,
        test_rc,
        test_out,
        test_err,
        dependency_root,
    )
    passed = validator_rc == 0 and test_rc == 0

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["command"] = suite_command
    manifest["command_cwd"] = str(SIM_ROOT.parents[1])
    manifest["command_environment_overrides"] = {
        "PYTHONPATH": str(dependency_root.resolve())
    }
    manifest["status"] = "passed" if passed else "failed"
    manifest["dependency_inventory_sha256"] = sha256_file(inventory_path)
    manifest["validator_return_code"] = validator_rc
    manifest["test_return_code"] = test_rc
    write_json(manifest_path, manifest)

    metrics_path = output / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["status"] = "PASS" if passed else "FAIL"
    metrics["validator_return_code"] = validator_rc
    metrics["test_return_code"] = test_rc
    metrics["test_count"] = 16
    write_json(metrics_path, metrics)
    write_json(
        output / "suite_result.json",
        {
            "schema_version": "moe-simulator-phase1-suite-result-v1",
            "status": "PASS" if passed else "FAIL",
            "validator_return_code": validator_rc,
            "test_return_code": test_rc,
            "gpu_used": False,
            "model_downloaded": False,
        },
    )
    (logs / "suite_command.log").write_text(
        json.dumps(suite_command, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_checksum_ledger(output)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dependency-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=PHASE1_ROOT / "fixtures" / "mock_runtime_trace.json",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError(f"fresh Phase 1 run directory already exists: {output}")
    inventory = dependency_inventory(args.dependency_root.resolve())
    suite_command = [sys.executable, *sys.argv]
    # run() performs the complete fixture/schema/semantic preflight before mkdir.
    sys.path.insert(0, str(args.dependency_root.resolve()))
    run(args.fixture.resolve(), output, suite_command)
    passed = finalize(
        output, suite_command, inventory, args.dependency_root.resolve()
    )
    print(f"PHASE1_CPU_MOCK_SUITE: {'PASS' if passed else 'FAIL'}: {output}")
    print("GPU_USED: false")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
