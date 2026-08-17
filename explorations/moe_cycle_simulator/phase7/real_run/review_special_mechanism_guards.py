#!/usr/bin/env python3
"""Review guard-capture evidence without upgrading unavailable mechanisms."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def recursive_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for name, child in value.items():
            if name == key:
                found.append(child)
            found.extend(recursive_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_values(child, key))
    return found


def verify_sha256sums(run_dir: Path) -> dict[str, Any]:
    sums = run_dir / "SHA256SUMS"
    if not sums.is_file():
        return {"status": "MISSING", "checked": 0, "failures": ["SHA256SUMS"]}
    checked = 0
    failures: list[str] = []
    for line in sums.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split(None, 1)
        relative = relative.lstrip(" *")
        path = run_dir / relative
        if not path.is_file():
            failures.append(relative)
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        checked += 1
        if digest != expected:
            failures.append(relative)
    return {"status": "PASS" if not failures else "FAIL", "checked": checked, "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    terminal = read_json(args.attempt_root / "guard_terminal.json")
    runner_dir = Path(terminal["runner_dir"]) if terminal.get("runner_dir") else None
    if runner_dir is None or not runner_dir.is_dir():
        local_candidates = sorted((args.attempt_root / "runner_runs").glob("*"))
        runner_dir = local_candidates[-1] if local_candidates else runner_dir
    required_attempt = [
        "exact_argv.json",
        "guard_manifest.json",
        "guard_trace.jsonl",
        "runner.stdout.log",
        "runner.stderr.log",
        "guard_terminal.json",
    ]
    attempt_presence = {name: (args.attempt_root / name).is_file() for name in required_attempt}
    trace_rows = [
        json.loads(line)
        for line in (args.attempt_root / "guard_trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if (args.attempt_root / "guard_trace.jsonl").is_file() else []
    process_rows = [process for row in trace_rows for process in row.get("processes", [])]
    vm_swap_observed = [process.get("status", {}).get("VmSwap") for process in process_rows if process.get("status", {}).get("VmSwap") is not None]
    major_faults_observed = [process.get("major_faults") for process in process_rows if process.get("major_faults") is not None]

    runtime: dict[str, Any] = {}
    model: dict[str, Any] = {}
    manifest: dict[str, Any] = {}
    runner_sha = {"status": "NOT_RUN", "checked": 0, "failures": []}
    if runner_dir is not None and runner_dir.is_dir():
        runtime_path = runner_dir / "resolved_runtime.json"
        model_path = runner_dir / "model_identity.json"
        manifest_path = runner_dir / "manifest.json"
        if runtime_path.is_file():
            runtime = read_json(runtime_path)
        if model_path.is_file():
            model = read_json(model_path)
        if manifest_path.is_file():
            manifest = read_json(manifest_path)
        runner_sha = verify_sha256sums(runner_dir)

    resolved = {
        "cpu_offload_gb": recursive_values(runtime, "cpu_offload_gb"),
        "enable_prefix_caching": recursive_values(runtime, "enable_prefix_caching"),
        "enable_chunked_prefill": recursive_values(runtime, "enable_chunked_prefill"),
        "enforce_eager": recursive_values(runtime, "enforce_eager"),
        "max_num_seqs": recursive_values(runtime, "max_num_seqs"),
        "swap_space": recursive_values(runtime, "swap_space"),
        "kv_offloading_size": recursive_values(runtime, "kv_offloading_size"),
        "kv_offloading_backend": recursive_values(runtime, "kv_offloading_backend"),
    }
    identity_ok = bool(model) and bool(manifest) and bool(runtime)
    process_guard_ok = bool(vm_swap_observed) and bool(major_faults_observed) and bool(trace_rows)
    kv_evidence_ok = bool(runtime) and bool(recursive_values(runtime, "block_size")) and bool(trace_rows)
    um_status = "NEGATIVE_EVIDENCE" if trace_rows and all(
        row.get("um_probe", {}).get("status") == "UNAVAILABLE_UNLESS_RUNTIME_OR_PROFILER_EXPOSES_MANAGED_MEMORY"
        for row in trace_rows
    ) else "PARTIAL"
    statuses = {
        "MECH-G0": "PASS" if identity_ok and process_guard_ok else "PARTIAL",
        "OS-SWAP-G0": "PASS" if process_guard_ok else "PARTIAL",
        "KV-G0": "PASS" if kv_evidence_ok else "PARTIAL",
        "UM-G0": um_status,
    }
    report = {
        "schema_version": "phase7-special-mechanism-guard-review-v1",
        "attempt_root": str(args.attempt_root),
        "runner_dir": None if runner_dir is None else str(runner_dir),
        "execution_status": "PASS" if terminal.get("runner_returncode") == 0 else "FAILED",
        "promotion_status": "VALIDATION_PASS" if all(value in {"PASS", "NEGATIVE_EVIDENCE"} for value in statuses.values()) else "SUPPLEMENT_REQUIRED",
        "raw_unchanged": True,
        "attempt_presence": attempt_presence,
        "runner_sha256": runner_sha,
        "trace_row_count": len(trace_rows),
        "process_observation_count": len(process_rows),
        "vm_swap_observed": vm_swap_observed,
        "major_faults_observed": major_faults_observed,
        "resolved_runtime_summary": resolved,
        "guard_status": statuses,
        "claims_forbidden": [
            "runtime-native expert offload",
            "runtime-native KV swap performance",
            "Unified Memory absence beyond the observed unavailable scope",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"promotion_status": report["promotion_status"], "guard_status": statuses, "runner_sha256": runner_sha}, indent=2, sort_keys=True))
    return 0 if report["promotion_status"] == "VALIDATION_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
