#!/usr/bin/env python3
"""Capture Phase 7 mechanism guards around one canonical GPU canary.

The wrapper deliberately records observations first and leaves PASS/FAIL
promotion to the amendment reviewer.  In particular, zero VmSwap is not
treated as proof that no unrelated migration occurred, and absent Unified
Memory telemetry is recorded as unavailable rather than as zero activity.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def command_snapshot(command: list[str], timeout: float = 5.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "argv": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except Exception as exc:
        return {"argv": command, "error": f"{type(exc).__name__}: {exc}"}


def process_table() -> list[dict[str, Any]]:
    result = command_snapshot(["ps", "-eo", "pid=,ppid=,args="])
    if result.get("returncode") != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in str(result.get("stdout", "")).splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            rows.append({"pid": int(fields[0]), "ppid": int(fields[1]), "cmdline": fields[2]})
        except ValueError:
            continue
    return rows


def descendants(root_pid: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row["ppid"] in selected and row["pid"] not in selected:
                selected.add(row["pid"])
                changed = True
    return [row for row in rows if row["pid"] in selected]


def proc_observation(pid: int) -> dict[str, Any]:
    root = Path("/proc") / str(pid)
    observation: dict[str, Any] = {"pid": pid, "captured_at_ns": time.time_ns()}
    try:
        status: dict[str, Any] = {}
        for line in (root / "status").read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            status[key] = value.strip()
        observation["status"] = {
            key: status.get(key)
            for key in ("Name", "State", "Pid", "PPid", "VmRSS", "VmSize", "VmSwap", "Threads")
        }
        stat_text = (root / "stat").read_text(encoding="utf-8", errors="replace")
        stat_tail = stat_text[stat_text.rfind(")") + 1 :].split()
        if len(stat_tail) >= 10:
            observation["major_faults"] = int(stat_tail[9])
        observation["cmdline"] = (root / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        observation["cgroup"] = (root / "cgroup").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError) as exc:
        observation["read_error"] = f"{type(exc).__name__}: {exc}"
    return observation


def discover_runner_dir(run_root: Path, experiment_id: str) -> Path | None:
    candidates = sorted(run_root.glob(f"*__{experiment_id}*"))
    return candidates[-1] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument(
        "--runner-interface",
        choices=("gpu_campaign", "serving_burst"),
        default="gpu_campaign",
        help="argument contract of the supplied runner",
    )
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--input-tokens", type=int, default=128)
    parser.add_argument("--output-tokens", type=int, default=32)
    parser.add_argument("--sample-period-ms", type=int, default=250)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.97)
    args = parser.parse_args()
    if args.sample_period_ms < 50:
        raise SystemExit("--sample-period-ms must be at least 50")

    args.attempt_root.mkdir(parents=True, exist_ok=False)
    runner_root = args.attempt_root / "runner_runs"
    runner_root.mkdir()
    stdout_path = args.attempt_root / "runner.stdout.log"
    stderr_path = args.attempt_root / "runner.stderr.log"
    trace_path = args.attempt_root / "guard_trace.jsonl"
    command = [sys.executable, str(args.runner), "--model-path", str(args.model_path), "--run-root", str(runner_root), "--experiment-id", args.experiment_id, "--input-tokens", str(args.input_tokens), "--output-tokens", str(args.output_tokens), "--sampling-mode", "NATURAL_EOS_CAPPED", "--max-num-seqs", "1", "--max-num-batched-tokens", "1024", "--gpu-memory-utilization", str(args.gpu_memory_utilization)]
    if args.runner_interface == "gpu_campaign":
        command[command.index("--sampling-mode") : command.index("--sampling-mode") + 2] = ["--sampling-mode", "NATURAL_EOS_CAPPED"]
        command.extend(["--runtime-class", "CLEAN", "--warmup-count", "1", "--measured-count", "1"])
    else:
        command.extend(["--concurrency", "1", "--bursts", "1", "--warmup-burst"])
    write_json(args.attempt_root / "exact_argv.json", {"argv": command})
    write_json(args.attempt_root / "guard_manifest.json", {
        "schema_version": "phase7-special-mechanism-guard-manifest-v1",
        "experiment_id": args.experiment_id,
        "mechanism_variant": "CANONICAL_GUARD_ONLY",
        "canonical_expected": {
            "tp_pp_ep": "1/1/1",
            "weights": "BF16",
            "kv": "BF16",
            "enforce_eager": True,
            "max_num_seqs": 1,
            "persistent_cpu_model_offload": False,
            "runtime_kv_swap": False,
            "quantization": "none",
        },
        "guard_ids": ["MECH-G0", "KV-G0", "OS-SWAP-G0", "UM-G0"],
        "observation_policy": {
            "zero_vmswap_is_not_global_no_swap_proof": True,
            "system_swap_is_context_only": True,
            "managed_memory_absence_is_unavailable_not_zero": True,
        },
    })
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, text=True)
        while process.poll() is None:
            rows = process_table()
            observed = descendants(process.pid, rows)
            append_jsonl(trace_path, {
                "schema_version": "phase7-special-mechanism-guard-observation-v1",
                "wrapper_pid": process.pid,
                "processes": [proc_observation(row["pid"]) | {"ppid": row["ppid"], "cmdline_from_ps": row["cmdline"]} for row in observed],
                "gpu": command_snapshot(["nvidia-smi", "--query-gpu=timestamp,name,memory.used,memory.free,memory.total,utilization.gpu,utilization.memory,power.draw,temperature.gpu,clocks.gr,clocks.mem", "--format=csv,noheader,nounits"]),
                "compute_apps": command_snapshot(["nvidia-smi", "--query-compute-apps=timestamp,pid,process_name,used_memory", "--format=csv,noheader,nounits"]),
                "system_vmstat": command_snapshot(["vmstat", "-s"]),
                "um_probe": {
                    "status": "UNAVAILABLE_UNLESS_RUNTIME_OR_PROFILER_EXPOSES_MANAGED_MEMORY",
                    "nvidia_smi_memory_query": command_snapshot(["nvidia-smi", "-q", "-d", "MEMORY"]),
                },
            })
            time.sleep(args.sample_period_ms / 1000.0)
        returncode = process.wait()

    runner_dir = discover_runner_dir(runner_root, args.experiment_id)
    write_json(args.attempt_root / "guard_terminal.json", {
        "schema_version": "phase7-special-mechanism-guard-terminal-v1",
        "runner_returncode": returncode,
        "runner_dir": None if runner_dir is None else str(runner_dir),
        "raw_capture_status": "RAW_CAPTURED" if runner_dir is not None else "PARTIAL",
        "promotion_status": "PENDING_REVIEW",
        "um_claim_status": "UNAVAILABLE_UNLESS_RUNTIME_OR_PROFILER_EVIDENCE_PRESENT",
    })
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
