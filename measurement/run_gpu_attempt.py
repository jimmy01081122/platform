#!/usr/bin/env python3
"""Run one GPU measurement command in a complete, pullable attempt directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from measurement.model_identity_manifest import (
        ModelIdentityError,
        validate_manifest as validate_model_identity_manifest,
    )
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from measurement.model_identity_manifest import (
        ModelIdentityError,
        validate_manifest as validate_model_identity_manifest,
    )


ENV_ALLOWLIST = (
    "CUDA_HOME",
    "CUDA_PATH",
    "CUDACXX",
    "CUDA_VISIBLE_DEVICES",
    "CPATH",
    "LIBRARY_PATH",
    "LD_LIBRARY_PATH",
    "HF_HOME",
    "HF_HUB_CACHE",
    "VLLM_MODEL",
    "VLLM_ARGS",
    "VLLM_USE_SIMPLE_KV_OFFLOAD",
    "PHASE7_ENABLE_STEP_TRACE",
    "PHASE7_STEP_TRACE_HOOK_REVISION",
)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def command_output(argv: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except Exception as exc:  # environment capture must not erase the attempt
        return {"argv": argv, "error": f"{type(exc).__name__}: {exc}"}


def environment_snapshot() -> dict[str, Any]:
    selected_env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    hf_home = os.environ.get("HF_HOME")
    selected_env["HF_TOKEN_PRESENT"] = bool(os.environ.get("HF_TOKEN")) or bool(
        hf_home and (Path(hf_home) / "token").is_file()
    )
    # Hidden connector selection must be represented even when intentionally
    # unset; omission would make target_2's native connector identity ambiguous.
    selected_env["VLLM_USE_SIMPLE_KV_OFFLOAD"] = os.environ.get(
        "VLLM_USE_SIMPLE_KV_OFFLOAD"
    )
    cuda_home = os.environ.get("CUDA_HOME")
    cudacxx = os.environ.get("CUDACXX")
    if cudacxx:
        nvcc_path = cudacxx
        nvcc_source = "CUDACXX"
    elif cuda_home:
        nvcc_path = str(Path(cuda_home) / "bin" / "nvcc")
        nvcc_source = "CUDA_HOME/bin/nvcc"
    else:
        nvcc_path = shutil.which("nvcc")
        nvcc_source = "PATH"
    return {
        "captured_at_utc": utc_now(),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "selected_environment": selected_env,
        "cuda_toolkit": {
            "CUDA_HOME": cuda_home,
            "CUDACXX": cudacxx,
            "nvcc_path": nvcc_path,
            "nvcc_resolution_source": nvcc_source,
            "nvcc_version": (
                command_output([nvcc_path, "--version"])
                if nvcc_path
                else {"error": "nvcc not found on PATH"}
            ),
            "cuda_home_nvrtc_header_exists": bool(
                cuda_home and (Path(cuda_home) / "include" / "nvrtc.h").is_file()
            ),
        },
        "nvidia_smi": command_output(
            [
                "nvidia-smi",
                "--query-gpu=name,uuid,memory.total,driver_version,compute_cap",
                "--format=csv,noheader",
            ]
        ),
        "nvidia_compute_apps": command_output(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader",
            ]
        ),
        "python_packages": {
            "torch": command_output(
                [sys.executable, "-c", "import torch; print(torch.__version__)" ]
            ),
            "vllm": command_output([sys.executable, "-c", "import vllm; print(vllm.__version__)"]),
            "transformers": command_output(
                [sys.executable, "-c", "import transformers; print(transformers.__version__)" ]
            ),
        },
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_attempt_checksums(attempt_dir: Path) -> None:
    checksum_path = attempt_dir / "ATTEMPT_SHA256SUMS"
    rows = []
    for path in sorted(attempt_dir.rglob("*")):
        if not path.is_file() or path == checksum_path:
            continue
        rows.append(f"{sha256_file(path)}  {path.relative_to(attempt_dir)}")
    checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


class AttemptContractError(RuntimeError):
    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.classification = classification


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AttemptContractError(
            "EVIDENCE_CONTRACT_INCOMPLETE",
            f"cannot parse {label} {path}: {type(exc).__name__}: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise AttemptContractError(
            "EVIDENCE_CONTRACT_INCOMPLETE", f"{label} must be a JSON object"
        )
    return value


def gpu_telemetry_snapshot() -> dict[str, Any]:
    return {
        "captured_at_utc": utc_now(),
        "gpu": command_output(
            [
                "nvidia-smi",
                "--query-gpu=uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ]
        ),
        "compute_apps": command_output(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader",
            ]
        ),
    }


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")
        handle.flush()


def _preflight_compute_inventory(snapshot: dict[str, Any]) -> None:
    inventory = snapshot.get("nvidia_compute_apps", {})
    if inventory.get("returncode") != 0:
        raise AttemptContractError(
            "PREFLIGHT_GPU_INVENTORY_FAILED",
            "nvidia-smi compute-process inventory did not complete successfully",
        )
    if str(inventory.get("stdout", "")).strip():
        raise AttemptContractError(
            "FOREIGN_GPU_PROCESS_PRESENT",
            "GPU already has a compute application; exclusive attempt refused",
        )


def _declared_output(command: list[str], attempt_dir: Path) -> Path:
    values: list[str] = []
    for index, token in enumerate(command):
        if token == "--out" and index + 1 < len(command):
            values.append(command[index + 1])
        elif token.startswith("--out="):
            values.append(token.split("=", 1)[1])
    if len(values) != 1:
        raise AttemptContractError(
            "EVIDENCE_CONTRACT_INCOMPLETE",
            "measurement command must declare exactly one --out path",
        )
    output = Path(values[0])
    if not output.is_absolute():
        output = Path.cwd() / output
    output = output.resolve()
    try:
        output.relative_to(attempt_dir)
    except ValueError as exc:
        raise AttemptContractError(
            "EVIDENCE_CONTRACT_INCOMPLETE",
            f"--out must remain inside the attempt directory: {output}",
        ) from exc
    return output


def _declared_model_path(command: list[str]) -> Path | None:
    values: list[str] = []
    for index, token in enumerate(command):
        if token == "--model-path" and index + 1 < len(command):
            values.append(command[index + 1])
        elif token.startswith("--model-path="):
            values.append(token.split("=", 1)[1])
    if not values:
        return None
    if len(values) != 1:
        raise AttemptContractError(
            "EVIDENCE_CONTRACT_INCOMPLETE",
            "measurement command declares multiple --model-path values",
        )
    path = Path(values[0])
    if not path.is_absolute():
        raise AttemptContractError(
            "MODEL_IDENTITY_VERIFICATION_FAILED", "--model-path must be absolute"
        )
    return path


def _validate_probe_output(path: Path) -> dict[str, Any]:
    root = read_json_object(path, "probe output")
    if not isinstance(root.get("runtime_identity"), dict):
        raise AttemptContractError(
            "EVIDENCE_CONTRACT_INCOMPLETE",
            "probe output is missing a mapping runtime_identity",
        )
    timing_keys = {"raw_benchmarks", "cells", "groups", "records"}
    if not timing_keys.intersection(root):
        raise AttemptContractError(
            "EVIDENCE_CONTRACT_INCOMPLETE",
            "probe output contains no recognized raw timing collection",
        )
    return root


def run_streamed_command(
    command: list[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    telemetry_path: Path,
    telemetry_interval_seconds: float,
) -> int:
    """Run a child while flushing logs and periodic GPU telemetry to disk."""

    process: subprocess.Popen[str] | None = None
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            command,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        append_jsonl(telemetry_path, gpu_telemetry_snapshot())
        try:
            while True:
                try:
                    returncode = process.wait(timeout=telemetry_interval_seconds)
                    break
                except subprocess.TimeoutExpired:
                    append_jsonl(telemetry_path, gpu_telemetry_snapshot())
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            append_jsonl(telemetry_path, gpu_telemetry_snapshot())
    return returncode


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--owner-note", default="")
    parser.add_argument("--model-identity-json", type=Path, required=True)
    parser.add_argument("--input-fixture-json", type=Path, required=True)
    parser.add_argument("--telemetry-interval-seconds", type=float, default=1.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("measurement command is required after --")
    if args.telemetry_interval_seconds <= 0:
        parser.error("--telemetry-interval-seconds must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    attempt_dir = args.attempt_dir.resolve()
    if attempt_dir.exists():
        raise SystemExit(f"attempt directory already exists: {attempt_dir}")
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "artifacts").mkdir()
    (attempt_dir / "logs").mkdir()
    (attempt_dir / "environment").mkdir()

    started = utc_now()
    started_ns = time.monotonic_ns()
    manifest = {
        "schema_version": "track-gpu-attempt-v1",
        "attempt_id": attempt_dir.name,
        "target": args.target,
        "status": "RUNNING",
        "started_at_utc": started,
        "cwd": os.getcwd(),
        "exact_argv": args.command,
        "owner_note": args.owner_note,
        "failure_classification": None,
    }
    write_json(attempt_dir / "manifest.json", manifest)
    # JSON is valid YAML and avoids a runtime dependency solely for serialization.
    write_json(
        attempt_dir / "resolved_config.yaml",
        {"target": args.target, "attempt_dir": str(attempt_dir), "argv": args.command},
    )
    pre_environment = environment_snapshot()
    write_json(attempt_dir / "environment" / "tool_versions.json", pre_environment)
    (attempt_dir / "logs" / "command.log").write_text(
        json.dumps(args.command, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    returncode = 1
    failure: str | None = None
    stdout_path = attempt_dir / "logs" / "stdout.log"
    stderr_path = attempt_dir / "logs" / "stderr.log"
    telemetry_path = attempt_dir / "telemetry.jsonl"
    stdout_path.touch()
    stderr_path.touch()
    declared_output: Path | None = None
    try:
        _preflight_compute_inventory(pre_environment)
        declared_output = _declared_output(args.command, attempt_dir)

        model_identity = read_json_object(args.model_identity_json, "model identity")
        try:
            validate_model_identity_manifest(
                model_identity,
                expected_model_path=_declared_model_path(args.command),
            )
        except ModelIdentityError as exc:
            raise AttemptContractError(
                "MODEL_IDENTITY_VERIFICATION_FAILED", str(exc)
            ) from exc
        write_json(attempt_dir / "environment" / "model_identity.json", model_identity)

        input_fixture = read_json_object(args.input_fixture_json, "input fixture")
        write_json(attempt_dir / "artifacts" / "input_fixture.json", input_fixture)

        returncode = run_streamed_command(
            args.command,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            telemetry_path=telemetry_path,
            telemetry_interval_seconds=args.telemetry_interval_seconds,
        )
        if returncode != 0:
            failure = "COMMAND_NONZERO"
        else:
            _validate_probe_output(declared_output)
    except AttemptContractError as exc:
        failure = exc.classification
        returncode = 1
        with stderr_path.open("a", encoding="utf-8") as handle:
            handle.write(f"AttemptContractError: {exc}\n")
    except KeyboardInterrupt:
        failure = "INTERRUPTED"
        returncode = 130
    except Exception as exc:
        failure = "WRAPPER_EXCEPTION"
        returncode = 1
        with stderr_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{type(exc).__name__}: {exc}\n")

    finished_ns = time.monotonic_ns()
    manifest.update(
        {
            "status": "PASS" if returncode == 0 else "FAIL",
            "returncode": returncode,
            "failure_classification": failure,
            "finished_at_utc": utc_now(),
            "duration_ns": finished_ns - started_ns,
        }
    )
    write_json(attempt_dir / "manifest.json", manifest)
    write_json(
        attempt_dir / "metrics.json",
        {
            "status": manifest["status"],
            "returncode": returncode,
            "duration_ns": manifest["duration_ns"],
            "failure_classification": failure,
        },
    )
    if not telemetry_path.exists():
        append_jsonl(telemetry_path, gpu_telemetry_snapshot())
    append_jsonl(telemetry_path, gpu_telemetry_snapshot())
    write_json(
        attempt_dir / "environment" / "post_run_gpu.json",
        {
            "captured_at_utc": utc_now(),
            "nvidia_smi": command_output(
                [
                    "nvidia-smi",
                    "--query-gpu=name,uuid,memory.total,driver_version,compute_cap",
                    "--format=csv,noheader",
                ]
            ),
            "nvidia_compute_apps": command_output(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,process_name,used_gpu_memory",
                    "--format=csv,noheader",
                ]
            ),
        },
    )
    write_attempt_checksums(attempt_dir)
    print(
        json.dumps(
            {
                "attempt_dir": str(attempt_dir),
                "status": manifest["status"],
                "returncode": returncode,
            },
            sort_keys=True,
        )
    )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
