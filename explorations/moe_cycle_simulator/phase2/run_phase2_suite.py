#!/usr/bin/env python3
"""Execute and seal the CPU-only Phase 2 Canonical IR/codec suite."""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import importlib.metadata
import json
import os
import platform
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

PHASE2_ROOT = Path(__file__).resolve().parent
SIM_ROOT = PHASE2_ROOT.parent
REPO_ROOT = SIM_ROOT.parents[1]
sys.path.insert(0, str(PHASE2_ROOT))

from canonical_ir import read_bundle, write_bundle  # noqa: E402
from validate_phase2 import fixture_records, validate_source  # noqa: E402

ROOT_DISTRIBUTIONS = {
    "jsonschema": "4.24.0",
    "attrs": None,
    "referencing": None,
    "rpds-py": None,
    "jsonschema-specifications": None,
    "pyarrow": "20.0.0",
}
SYSTEM_DISTRIBUTIONS = {
    "numpy": None,
    "pytest": None,
    "pluggy": None,
    "packaging": None,
    "iniconfig": None,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def dependency_inventory(dependency_root: Path) -> dict[str, Any]:
    if not dependency_root.is_dir() or dependency_root.is_symlink():
        raise RuntimeError("dependency root must be a real directory")
    original_path = list(sys.path)
    sys.path.insert(0, str(dependency_root))
    distributions = []
    try:
        for name, expected in {**ROOT_DISTRIBUTIONS, **SYSTEM_DISTRIBUTIONS}.items():
            distribution = importlib.metadata.distribution(name)
            metadata_path = Path(distribution._path).resolve()
            if expected is not None and distribution.version != expected:
                raise RuntimeError(
                    f"{name} must be exactly {expected}, found {distribution.version}"
                )
            if name in ROOT_DISTRIBUTIONS:
                try:
                    metadata_path.relative_to(dependency_root.resolve())
                except ValueError as exc:
                    raise RuntimeError(
                        f"{name} is outside the dependency root"
                    ) from exc
            record_path = metadata_path / "RECORD"
            if not record_path.is_file() or record_path.is_symlink():
                raise RuntimeError(f"{name} RECORD is missing")
            content = hashlib.sha256()
            file_count = 0
            for relative in sorted(
                distribution.files or [], key=lambda item: str(item)
            ):
                located = Path(distribution.locate_file(relative))
                if not located.is_file() or located.is_symlink():
                    raise RuntimeError(
                        f"{name} contains invalid file: {relative}"
                    )
                content.update(str(relative).encode("utf-8"))
                content.update(b"\0")
                content.update(bytes.fromhex(sha256_file(located)))
                file_count += 1
            distributions.append(
                {
                    "name": name,
                    "version": distribution.version,
                    "metadata_path": str(metadata_path),
                    "record_sha256": sha256_file(record_path),
                    "file_count": file_count,
                    "content_root_sha256": content.hexdigest(),
                }
            )
    finally:
        sys.path[:] = original_path
    library_name = ctypes.util.find_library("zstd")
    if not library_name:
        raise RuntimeError("libzstd is unavailable")
    zstd = ctypes.CDLL(library_name)
    zstd.ZSTD_versionString.restype = ctypes.c_char_p
    zstd_version = zstd.ZSTD_versionString().decode("ascii")
    candidates = [
        Path("/usr/lib/x86_64-linux-gnu") / f"libzstd.so.{zstd_version}",
        Path("/lib/x86_64-linux-gnu") / f"libzstd.so.{zstd_version}",
    ]
    library_path = next(
        (path.resolve() for path in candidates if path.is_file()), None
    )
    if library_path is None or library_path.is_symlink():
        raise RuntimeError("exact libzstd shared object cannot be resolved")
    return {
        "schema_version": "moe-simulator-phase2-runtime-inventory-v1",
        "python_executable": sys.executable,
        "python_version": sys.version,
        "dependency_root": str(dependency_root.resolve()),
        "environment_overrides": {"PYTHONPATH": str(dependency_root.resolve())},
        "distributions": distributions,
        "native_libraries": [
            {
                "name": "libzstd",
                "version": zstd_version,
                "path": str(library_path),
                "file_sha256": sha256_file(library_path),
            }
        ],
    }


def write_ledger(output: Path) -> None:
    members: list[Path] = []
    for directory, names, file_names in os.walk(
        output, topdown=True, followlinks=False
    ):
        base = Path(directory)
        for name in [*names, *file_names]:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                raise RuntimeError(f"forbidden run entry: {path}")
            if stat.S_ISREG(mode) and path.name != "checksums.sha256":
                members.append(path)
    lines = [
        f"{sha256_file(path)}  {path.relative_to(output).as_posix()}"
        for path in sorted(members)
    ]
    (output / "checksums.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def execute(
    command: list[str], environment: dict[str, str]
) -> tuple[int, str, str]:
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def write_command_log(
    logs: Path,
    name: str,
    command: list[str],
    dependency_root: Path,
    result: tuple[int, str, str],
) -> None:
    return_code, stdout, stderr = result
    write_json(
        logs / f"{name}.command.json",
        {
            "argv": command,
            "cwd": str(REPO_ROOT),
            "environment_overrides": {
                "PYTHONPATH": str(dependency_root.resolve())
            },
            "return_code": return_code,
        },
    )
    (logs / f"{name}.stdout.log").write_text(stdout, encoding="utf-8")
    (logs / f"{name}.stderr.log").write_text(stderr, encoding="utf-8")


def run_suite(output: Path, dependency_root: Path, command: list[str]) -> bool:
    if output.exists():
        raise RuntimeError("fresh Phase 2 run directory already exists")
    inventory = dependency_inventory(dependency_root)
    sys.path.insert(0, str(dependency_root))
    source = validate_source()
    records = fixture_records()

    artifacts = output / "artifacts"
    logs = output / "logs"
    environment_dir = output / "environment"
    artifacts.mkdir(parents=True)
    logs.mkdir()
    environment_dir.mkdir()
    envelope = write_bundle(artifacts / "canonical_bundle", records)
    read_bundle(artifacts / "canonical_bundle" / "artifact-envelope.json")
    inventory_path = environment_dir / "dependency_inventory.json"
    write_json(inventory_path, inventory)
    write_json(
        environment_dir / "tool_versions.json",
        {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "jsonschema": importlib.metadata.version("jsonschema"),
            "pyarrow": importlib.metadata.version("pyarrow"),
            "pytest": importlib.metadata.version("pytest"),
            "libzstd": inventory["native_libraries"][0]["version"],
            "phase2_tool_sha256": sha256_file(Path(__file__).resolve()),
            "canonical_ir_tool_sha256": sha256_file(
                PHASE2_ROOT / "canonical_ir.py"
            ),
        },
    )

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(dependency_root)
    validator_command = [
        sys.executable,
        str(PHASE2_ROOT / "validate_phase2.py"),
        "--bundle",
        str(
            output
            / "artifacts"
            / "canonical_bundle"
            / "artifact-envelope.json"
        ),
    ]
    test_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        str(PHASE2_ROOT / "tests" / "test_canonical_ir.py"),
    ]
    validator_result = execute(validator_command, environment)
    test_result = execute(test_command, environment)
    write_command_log(
        logs,
        "validator",
        validator_command,
        dependency_root,
        validator_result,
    )
    write_command_log(
        logs, "tests", test_command, dependency_root, test_result
    )
    passed = validator_result[0] == 0 and test_result[0] == 0
    test_match = __import__("re").search(
        r"(?m)^([0-9]+) passed", test_result[1]
    )
    test_count = int(test_match.group(1)) if test_match else 0
    write_json(
        output / "manifest.json",
        {
            "run_id": output.name,
            "experiment_id": "moe_cycle_simulator_phase2_canonical_ir",
            "stage": "S2",
            "status": "passed" if passed else "failed",
            "command": command,
            "command_cwd": str(REPO_ROOT),
            "command_environment_overrides": {
                "PYTHONPATH": str(dependency_root)
            },
            "phase1_ledger_sha256": (
                "478674f22a8022c5ae8fdc343cb67173435d8308cf4e742b1629da975310b445"
            ),
            "dependency_inventory_sha256": sha256_file(inventory_path),
            "validator_return_code": validator_result[0],
            "test_return_code": test_result[0],
            "gpu_used": False,
            "model_downloaded": False,
            "evidence_class": "synthetic-cpu",
        },
    )
    write_json(
        output / "metrics.json",
        {
            "status": "PASS" if passed else "FAIL",
            "record_count": source["record_count"],
            "ir_kind_count": source["ir_kind_count"],
            "partition_count": len(envelope["partitions"]),
            "bundle_semantic_root": source["bundle_semantic_root"],
            "validator_return_code": validator_result[0],
            "test_return_code": test_result[0],
            "test_count": test_count,
            "gpu_used": False,
        },
    )
    write_json(
        output / "resolved_config.yaml",
        {
            "note": "JSON syntax is valid YAML",
            "codec_profile": "arrow-ipc-file-outer-zstd-v1",
            "batch_size": 65536,
            "dependency_root": str(dependency_root),
        },
    )
    write_json(
        output / "suite_result.json",
        {
            "schema_version": "moe-simulator-phase2-suite-result-v1",
            "status": "PASS" if passed else "FAIL",
            "validator_return_code": validator_result[0],
            "test_return_code": test_result[0],
            "gpu_used": False,
            "model_downloaded": False,
        },
    )
    (logs / "command.log").write_text(
        " ".join(command) + "\n", encoding="utf-8"
    )
    (logs / "suite_command.log").write_text(
        json.dumps(command) + "\n", encoding="utf-8"
    )
    (logs / "stdout.log").write_text(
        f"PHASE2_CANONICAL_IR_SUITE: {'PASS' if passed else 'FAIL'}\n"
        "GPU_USED: false\n",
        encoding="utf-8",
    )
    (logs / "stderr.log").write_text("", encoding="utf-8")
    write_ledger(output)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dependency-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = [sys.executable, *sys.argv]
    passed = run_suite(
        args.output.resolve(), args.dependency_root.resolve(), command
    )
    print(f"PHASE2_CANONICAL_IR_SUITE: {'PASS' if passed else 'FAIL'}")
    print(f"run: {args.output}")
    print("GPU_USED: false")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
