#!/usr/bin/env python3
"""Build, test and seal the CPU-only Phase 5 suite."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PHASE5_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PHASE5_ROOT.parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PHASE5_ROOT))
from validate_phase5 import source_root, validate_source  # noqa: E402


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def execute(
    command: list[str], environment: dict[str, str] | None = None
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


def log(
    logs: Path,
    name: str,
    command: list[str],
    result: tuple[int, str, str],
) -> None:
    write_json(
        logs / f"{name}.command.json",
        {"argv": command, "cwd": str(REPO_ROOT), "return_code": result[0]},
    )
    (logs / f"{name}.stdout.log").write_text(result[1], encoding="utf-8")
    (logs / f"{name}.stderr.log").write_text(result[2], encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_ledger(output: Path) -> None:
    members: list[Path] = []
    for root, directories, files in os.walk(output, followlinks=False):
        base = Path(root)
        for name in [*directories, *files]:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (
                stat.S_ISDIR(mode) or stat.S_ISREG(mode)
            ):
                raise RuntimeError(f"forbidden run entry: {path}")
        for name in files:
            path = base / name
            if name != "checksums.sha256":
                members.append(path)
    (output / "checksums.sha256").write_text(
        "\n".join(
            f"{file_sha256(path)}  {path.relative_to(output).as_posix()}"
            for path in sorted(members)
        )
        + "\n",
        encoding="utf-8",
    )


def run(output: Path, command: list[str]) -> bool:
    if output.exists():
        raise RuntimeError("fresh Phase 5 run already exists")
    validate_source()
    logs = output / "logs"
    environment = output / "environment"
    logs.mkdir(parents=True)
    environment.mkdir()
    normal = Path(tempfile.mkdtemp(prefix="moe-phase5-build-"))
    sanitizer = Path(tempfile.mkdtemp(prefix="moe-phase5-asan-"))
    results: list[
        tuple[str, list[str], tuple[int, str, str]]
    ] = []
    try:
        commands = [
            [
                "cmake",
                "-S",
                str(PHASE5_ROOT),
                "-B",
                str(normal),
                "-DCMAKE_BUILD_TYPE=Release",
            ],
            ["cmake", "--build", str(normal), "--parallel", "2"],
            ["ctest", "--test-dir", str(normal), "--output-on-failure"],
            [
                "cmake",
                "-S",
                str(PHASE5_ROOT),
                "-B",
                str(sanitizer),
                "-DCMAKE_BUILD_TYPE=Debug",
                "-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined "
                "-fno-omit-frame-pointer",
                "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined",
            ],
            ["cmake", "--build", str(sanitizer), "--parallel", "2"],
        ]
        names = [
            "configure",
            "build",
            "ctest",
            "asan_configure",
            "asan_build",
        ]
        for name, item in zip(names, commands):
            result = execute(item)
            results.append((name, item, result))
            if result[0]:
                break
        asan_environment = os.environ.copy()
        asan_environment["ASAN_OPTIONS"] = "detect_leaks=1"
        if len(results) == 5 and all(value[2][0] == 0 for value in results):
            for name, binary in [
                ("asan_phase5", sanitizer / "moe_sim_phase5_tests"),
                ("asan_phase4", sanitizer / "phase4_model/moe_sim_phase4_tests"),
                (
                    "asan_phase3",
                    sanitizer
                    / "phase4_model/phase3_core/moe_sim_phase3_tests",
                ),
            ]:
                item = [str(binary)]
                results.append(
                    (name, item, execute(item, asan_environment))
                )
        for name, item, result in results:
            log(logs, name, item, result)
        passed = len(results) == 8 and all(
            item[2][0] == 0 for item in results
        )
        write_json(
            environment / "tool_versions.json",
            {
                "python": platform.python_version(),
                "cmake": execute(["cmake", "--version"])[1].splitlines()[0],
                "compiler": execute(["g++", "--version"])[1].splitlines()[0],
                "operating_system": platform.system(),
                "kernel_release": platform.release(),
                "machine": platform.machine(),
                "source_root_sha256": source_root(),
            },
        )
        write_json(
            output / "manifest.json",
            {
                "run_id": output.name,
                "experiment_id": (
                    "moe_cycle_simulator_phase5_routing_residency_policy"
                ),
                "stage": "S4",
                "status": "passed" if passed else "failed",
                "command": command,
                "execution_mode": "TRACE_COMPILED_NON_ADAPTIVE",
                "gpu_used": False,
                "model_downloaded": False,
                "profile_origin": "CPU_SYNTHETIC",
            },
        )
        write_json(
            output / "metrics.json",
            {
                "status": "PASS" if passed else "FAIL",
                "source_root_sha256": source_root(),
                "ctest_passed": 4 if passed else 0,
                "asan_ubsan_pass": passed,
                "gpu_used": False,
                "production_scale_claim": False,
                "calibration_pass": False,
            },
        )
        (logs / "command.log").write_text(
            " ".join(command) + "\n", encoding="utf-8"
        )
        write_ledger(output)
        return passed
    finally:
        shutil.rmtree(normal)
        shutil.rmtree(sanitizer)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    command = [
        sys.executable,
        str(Path(__file__)),
        "--output",
        str(args.output),
    ]
    passed = run((REPO_ROOT / args.output).resolve(), command)
    print("PHASE5_ROUTING_RESIDENCY_SUITE:", "PASS" if passed else "FAIL")
    print("GPU_USED: false")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
