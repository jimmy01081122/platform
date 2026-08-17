#!/usr/bin/env python3
"""Capture conservative runtime capability evidence for OFF-E-RT0.

This probe does not claim that a generic CPU/KV offload API is dynamic expert
residency.  It records installed API/build/source evidence and binds that
evidence to a real canonical request attempt executed by the companion runner.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def run(command: list[str], timeout: float = 20.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return {
            "argv": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except Exception as exc:
        return {"argv": command, "error": f"{type(exc).__name__}: {exc}"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def module_source_scan(module: Any) -> dict[str, Any]:
    root = Path(str(getattr(module, "__path__", [""])[0]))
    keywords = (
        "expert_offload",
        "expert residency",
        "expert_residency",
        "expert weight offload",
        "expert_weight_offload",
        "offload_params",
        "weight_transfer_config",
        "kv_offloading",
    )
    hits: list[dict[str, Any]] = []
    files_scanned = 0
    if root.is_dir():
        for path in sorted(root.rglob("*.py")):
            files_scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            local_hits = []
            for line_number, line in enumerate(text.splitlines(), 1):
                lowered = line.lower()
                matched = [keyword for keyword in keywords if keyword in lowered]
                if matched:
                    local_hits.append({"line": line_number, "keywords": matched, "text": line.strip()[:300]})
            if local_hits:
                hits.append({"path": str(path), "matches": local_hits[:40]})
    return {"root": str(root), "files_scanned": files_scanned, "hits": hits}


def runtime_api_evidence() -> dict[str, Any]:
    result: dict[str, Any] = {"python": sys.version, "executable": sys.executable}
    try:
        vllm = importlib.import_module("vllm")
        result["vllm_version"] = getattr(vllm, "__version__", None)
        result["vllm_file"] = getattr(vllm, "__file__", None)
        result["vllm_source_scan"] = module_source_scan(vllm)
        targets: dict[str, Any] = {"vllm.LLM": getattr(vllm, "LLM", None)}
        try:
            engine_args_module = importlib.import_module("vllm.engine.arg_utils")
            targets["vllm.EngineArgs"] = getattr(engine_args_module, "EngineArgs", None)
            result["engine_args_module_file"] = getattr(engine_args_module, "__file__", None)
        except Exception as exc:
            result["engine_args_import_error"] = f"{type(exc).__name__}: {exc}"
        signatures: dict[str, str] = {}
        parameter_names: dict[str, list[str]] = {}
        for name, target in targets.items():
            if target is None:
                continue
            try:
                signature = inspect.signature(target)
                signatures[name] = str(signature)
                parameter_names[name] = sorted(signature.parameters)
            except (TypeError, ValueError) as exc:
                signatures[name] = f"UNAVAILABLE: {type(exc).__name__}: {exc}"
        result["signatures"] = signatures
        result["parameter_names"] = parameter_names
        all_parameters = sorted({item for values in parameter_names.values() for item in values})
        result["generic_cpu_weight_offload_api"] = [
            item for item in all_parameters if item in {"cpu_offload_gb", "offload_params", "offload_backend", "offload_group_size", "offload_num_in_group", "offload_prefetch_step"}
        ]
        result["kv_offload_api"] = [
            item for item in all_parameters if item in {"kv_offloading_size", "kv_offloading_backend", "kv_transfer_config"}
        ]
        explicit_expert_names = [
            item for item in all_parameters
            if "expert" in item.lower() and any(token in item.lower() for token in ("offload", "residen", "transfer"))
        ]
        result["explicit_dynamic_expert_api_parameters"] = explicit_expert_names
        source_text = json.dumps(result.get("vllm_source_scan", {}), sort_keys=True).lower()
        result["explicit_dynamic_expert_source_terms"] = [
            term for term in ("expert_offload", "expert residency", "expert_residency", "expert_weight_offload")
            if term in source_text
        ]
        if explicit_expert_names or result["explicit_dynamic_expert_source_terms"]:
            result["capability_status"] = "CANDIDATE_PATH_REQUIRES_TRIGGER_CANARY"
        else:
            result["capability_status"] = "RUNTIME_EXPERT_OFFLOAD_UNAVAILABLE"
        result["claim_boundary"] = (
            "Generic CPU weight-offload and KV-offload APIs are not dynamic expert residency; "
            "no runtime-native expert path is claimed without an actual object-level trigger trace."
        )
    except Exception as exc:
        result["import_error"] = {"type": type(exc).__name__, "message": str(exc)}
        result["capability_status"] = "RUNTIME_CAPABILITY_AUDIT_FAILED"
    try:
        result["vllm_distribution_version"] = importlib.metadata.version("vllm")
    except importlib.metadata.PackageNotFoundError:
        result["vllm_distribution_version"] = None
    return result


def process_snapshot(pid: int) -> dict[str, Any]:
    return {
        "pid": pid,
        "ps": run(["ps", "-o", "pid=,ppid=,stat=,etime=,args=", "-p", str(pid)]),
        "cmdline": run(["bash", "-lc", f"tr '\\0' ' ' < /proc/{pid}/cmdline"], timeout=5),
    }


def write_manifest(root: Path) -> None:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS_ATTEMPT":
            rows.append(f"{sha256(path)}  {path.relative_to(root)}")
    (root / "SHA256SUMS_ATTEMPT").write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--run-attempt-root", type=Path, required=True)
    parser.add_argument("--runner-pid", type=int, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--wait-terminal-seconds", type=int, default=240)
    args = parser.parse_args()
    args.attempt_root.mkdir(parents=True, exist_ok=False)
    json_write(args.attempt_root / "capability_probe_argv.json", {
        "argv": sys.argv,
        "experiment_id": args.experiment_id,
        "runner_pid": args.runner_pid,
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    capability = runtime_api_evidence()
    capability["experiment_id"] = args.experiment_id
    capability["runner_pid"] = args.runner_pid
    capability["runner_process_initial"] = process_snapshot(args.runner_pid)
    capability["gpu_initial"] = run(["nvidia-smi", "--query-gpu=timestamp,name,uuid,memory.used,memory.total,utilization.gpu,power.draw", "--format=csv,noheader,nounits"])
    json_write(args.attempt_root / "capability_audit.json", capability)

    terminal = args.run_attempt_root / "guard_terminal.json"
    # The capability probe is launched beside the existing guard wrapper.  Wait
    # for its terminal seal so the audit records a real request outcome without
    # changing the measured runner path.
    deadline = time.monotonic() + args.wait_terminal_seconds
    while time.monotonic() < deadline and not terminal.is_file():
        time.sleep(1.0)
    json_write(args.attempt_root / "capability_audit_terminal.json", {
        "terminal_path": str(terminal),
        "terminal_present": terminal.is_file(),
        "terminal": json.loads(terminal.read_text(encoding="utf-8")) if terminal.is_file() else None,
        "runner_process_final": process_snapshot(args.runner_pid),
        "gpu_final": run(["nvidia-smi", "--query-gpu=timestamp,name,uuid,memory.used,memory.total,utilization.gpu,power.draw", "--format=csv,noheader,nounits"]),
    })
    write_manifest(args.attempt_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
