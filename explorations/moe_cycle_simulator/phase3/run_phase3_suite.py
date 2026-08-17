#!/usr/bin/env python3
"""Build, test and seal the CPU-only Phase 3 engine suite."""
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

PHASE3_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PHASE3_ROOT.parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PHASE3_ROOT))
from validate_phase3 import source_root, validate_source  # noqa: E402


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def execute(
    command: list[str], *, environment: dict[str, str] | None = None
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


def log_result(
    logs: Path, name: str, command: list[str], result: tuple[int, str, str]
) -> None:
    code, stdout, stderr = result
    write_json(
        logs / f"{name}.command.json",
        {"argv": command, "cwd": str(REPO_ROOT), "return_code": code},
    )
    (logs / f"{name}.stdout.log").write_text(stdout, encoding="utf-8")
    (logs / f"{name}.stderr.log").write_text(stderr, encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def boost_version() -> dict[str, str]:
    result = execute(
        [
            "g++",
            "-dM",
            "-E",
            "-include",
            "boost/version.hpp",
            "-x",
            "c++",
            "/dev/null",
        ]
    )
    if result[0] != 0:
        raise RuntimeError("unable to inventory Boost version")
    macros: dict[str, str] = {}
    for line in result[1].splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and parts[1] in {
            "BOOST_VERSION",
            "BOOST_LIB_VERSION",
        }:
            macros[parts[1]] = parts[2].strip('"')
    if set(macros) != {"BOOST_VERSION", "BOOST_LIB_VERSION"}:
        raise RuntimeError("Boost version macros are incomplete")
    return macros


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
            if path.name != "checksums.sha256":
                members.append(path)
    (output / "checksums.sha256").write_text(
        "\n".join(
            f"{sha256(path)}  {path.relative_to(output).as_posix()}"
            for path in sorted(members)
        )
        + "\n",
        encoding="utf-8",
    )


def run(output: Path, command: list[str]) -> bool:
    if output.exists():
        raise RuntimeError("fresh Phase 3 run directory already exists")
    source = validate_source()
    logs = output / "logs"
    environment = output / "environment"
    logs.mkdir(parents=True)
    environment.mkdir()
    build_root = Path(tempfile.mkdtemp(prefix="moe-phase3-build-"))
    asan_root = Path(tempfile.mkdtemp(prefix="moe-phase3-asan-"))
    results: list[tuple[str, list[str], tuple[int, str, str]]] = []
    try:
        normal_commands = [
            [
                "cmake",
                "-S",
                str(PHASE3_ROOT),
                "-B",
                str(build_root),
                "-DCMAKE_BUILD_TYPE=Release",
            ],
            ["cmake", "--build", str(build_root), "--parallel", "2"],
            [
                "ctest",
                "--test-dir",
                str(build_root),
                "--output-on-failure",
            ],
        ]
        for name, item in zip(
            ("configure", "build", "ctest"), normal_commands
        ):
            result = execute(item)
            results.append((name, item, result))
            if result[0]:
                break
        sanitizer_flags = (
            "-fsanitize=address,undefined -fno-omit-frame-pointer"
        )
        if all(result[2][0] == 0 for result in results):
            asan_commands = [
                [
                    "cmake",
                    "-S",
                    str(PHASE3_ROOT),
                    "-B",
                    str(asan_root),
                    "-DCMAKE_BUILD_TYPE=Debug",
                    f"-DCMAKE_CXX_FLAGS={sanitizer_flags}",
                    "-DCMAKE_SHARED_LINKER_FLAGS=-fsanitize=address,undefined",
                    "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined",
                ],
                ["cmake", "--build", str(asan_root), "--parallel", "2"],
            ]
            for name, item in zip(("asan_configure", "asan_build"), asan_commands):
                result = execute(item)
                results.append((name, item, result))
                if result[0]:
                    break
        if all(result[2][0] == 0 for result in results):
            asan_environment = os.environ.copy()
            asan_environment["ASAN_OPTIONS"] = "detect_leaks=1"
            item = [str(asan_root / "moe_sim_phase3_tests")]
            results.append(
                ("asan_cpp", item, execute(item, environment=asan_environment))
            )
        if all(result[2][0] == 0 for result in results):
            preload = ":".join(
                subprocess.check_output(
                    ["g++", f"-print-file-name={name}"], text=True
                ).strip()
                for name in ("libasan.so", "libubsan.so")
            )
            binding_environment = os.environ.copy()
            binding_environment.update(
                {
                    "ASAN_OPTIONS": "detect_leaks=0",
                    "LD_PRELOAD": preload,
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            item = [
                sys.executable,
                str(PHASE3_ROOT / "tests" / "python_binding_test.py"),
                "--library",
                str(asan_root / "libmoe_sim_phase3.so"),
            ]
            results.append(
                (
                    "asan_python_binding",
                    item,
                    execute(item, environment=binding_environment),
                )
            )
        for name, item, result in results:
            log_result(logs, name, item, result)
        passed = len(results) == 7 and all(item[2][0] == 0 for item in results)
        write_json(
            environment / "tool_versions.json",
            {
                "python": platform.python_version(),
                "cmake": execute(["cmake", "--version"])[1].splitlines()[0],
                "compiler": execute(["g++", "--version"])[1].splitlines()[0],
                "boost": boost_version(),
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
                "experiment_id": "moe_cycle_simulator_phase3_event_core",
                "stage": "S3",
                "status": "passed" if passed else "failed",
                "command": command,
                "gpu_used": False,
                "model_downloaded": False,
                "service_mode": "REPLAY_VALIDATE",
            },
        )
        metrics = {
            "status": "PASS" if passed else "FAIL",
            "source_root_sha256": source_root(),
            "ctest_passed": 2 if passed else 0,
            "asan_ubsan_pass": passed,
            "python_binding_pass": passed,
            "gpu_used": False,
        }
        write_json(output / "metrics.json", metrics)
        write_json(
            output / "suite_result.json",
            {
                "schema_version": "moe-simulator-phase3-suite-result-v1",
                "status": metrics["status"],
                "gpu_used": False,
                "model_downloaded": False,
            },
        )
        (logs / "command.log").write_text(
            " ".join(command) + "\n", encoding="utf-8"
        )
        write_ledger(output)
        return passed
    finally:
        shutil.rmtree(build_root, ignore_errors=True)
        shutil.rmtree(asan_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = [sys.executable, *sys.argv]
    passed = run(args.output.resolve(), command)
    print(f"PHASE3_EVENT_CORE_SUITE: {'PASS' if passed else 'FAIL'}")
    print(f"run: {args.output}")
    print("GPU_USED: false")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
