#!/usr/bin/env python3
"""Passive NVML sidecar for per-run model load and VRAM eviction timing.

The monitor never launches, signals, or modifies the GPU workload.  It samples
NVML process/memory state and joins those samples to an existing controlled
batch's status and console logs.  Eviction is reported as a polling-bounded
transition, not as an exact CUDA free duration.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import pynvml


LOAD_RE = re.compile(r"Model loading took\s+([0-9]+(?:\.[0-9]+)?)\s+seconds")
STOP_REQUESTED = False


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def wall_ns_to_utc(value: int | None) -> str | None:
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value / 1_000_000_000, tz=dt.timezone.utc).isoformat()


def parse_utc_ns(value: str | None) -> int | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1_000_000_000)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")
        handle.flush()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_progress(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        return {row["experiment_id"]: row for row in csv.DictReader(handle, delimiter="\t")}


def reported_load_seconds(console_path: Path) -> float | None:
    if not console_path.is_file():
        return None
    matches = LOAD_RE.findall(console_path.read_text(encoding="utf-8", errors="replace"))
    return float(matches[-1]) if matches else None


def query_nvml(handle: Any) -> tuple[int, list[dict[str, Any]]]:
    memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
    processes = []
    try:
        process_rows = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
    except pynvml.NVMLError:
        process_rows = []
    for process in process_rows:
        used = getattr(process, "usedGpuMemory", 0)
        if used is None or used < 0:
            used = 0
        processes.append(
            {
                "pid": int(process.pid),
                "used_memory_bytes": int(used),
                "used_memory_mib": round(int(used) / (1024 * 1024), 3),
            }
        )
    return int(memory.used), processes


def new_live_record(
    experiment_id: str,
    batch_status: dict[str, Any],
    now_ns: int,
    used_bytes_at_first_observation: int,
    idle_bytes: int,
) -> dict[str, Any]:
    dispatch_ns = parse_utc_ns(batch_status.get("updated_at_utc"))
    left_censored = used_bytes_at_first_observation > idle_bytes
    return {
        "experiment_id": experiment_id,
        "coverage": "live_nvml",
        "dispatch_wall_time_ns": dispatch_ns,
        "dispatch_utc": wall_ns_to_utc(dispatch_ns),
        "monitor_first_seen_wall_time_ns": now_ns,
        "monitor_first_seen_utc": wall_ns_to_utc(now_ns),
        "used_memory_bytes_at_first_observation": used_bytes_at_first_observation,
        "load_transition_left_censored": left_censored,
        "first_gpu_allocation_wall_time_ns": None,
        "resident_wall_time_ns": None,
        "last_resident_wall_time_ns": None,
        "below_resident_wall_time_ns": None,
        "idle_wall_time_ns": None,
        "peak_used_memory_bytes": 0,
        "vllm_reported_model_loading_seconds": None,
        "run_dir": None,
        "run_status": None,
    }


def enrich_record(
    record: dict[str, Any],
    batch_dir: Path,
    progress: dict[str, dict[str, str]],
    interval_ns: int,
) -> None:
    experiment_id = record["experiment_id"]
    console = batch_dir / "consoles" / f"{experiment_id}.console.log"
    record["console_path"] = str(console)
    record["console_sha256"] = sha256_file(console) if console.is_file() else None
    record["vllm_reported_model_loading_seconds"] = reported_load_seconds(console)
    progress_row = progress.get(experiment_id)
    if progress_row:
        record["run_dir"] = progress_row.get("run_dir")
        record["run_status"] = progress_row.get("status")
        record["completed_at_utc"] = progress_row.get("completed_at_utc")
    dispatch = record.get("dispatch_wall_time_ns")
    first_alloc = record.get("first_gpu_allocation_wall_time_ns")
    resident = record.get("resident_wall_time_ns")
    below = record.get("below_resident_wall_time_ns")
    idle = record.get("idle_wall_time_ns")
    last_resident = record.get("last_resident_wall_time_ns")
    record["dispatch_to_first_gpu_allocation_ns"] = (
        first_alloc - dispatch if dispatch is not None and first_alloc is not None else None
    )
    record["dispatch_to_resident_ns"] = (
        resident - dispatch if dispatch is not None and resident is not None else None
    )
    record["first_gpu_allocation_to_resident_ns"] = (
        resident - first_alloc if first_alloc is not None and resident is not None else None
    )
    if record.get("load_transition_left_censored"):
        record["dispatch_to_first_gpu_allocation_ns"] = None
        record["dispatch_to_resident_ns"] = None
        record["first_gpu_allocation_to_resident_ns"] = None
        record["load_timing_limitation"] = (
            "The monitor first observed this experiment after GPU allocation had begun; "
            "the complete load transition is left-censored and no load duration is claimed."
        )
    else:
        record["load_timing_limitation"] = None
    record["observed_below_resident_to_idle_ns"] = (
        idle - below if below is not None and idle is not None else None
    )
    record["resident_to_idle_transition_upper_bound_ns"] = (
        idle - last_resident if last_resident is not None and idle is not None else None
    )
    record["transition_resolution_ns"] = interval_ns
    record["eviction_semantics"] = (
        "Polling-bounded VRAM transition. The exact CUDA free/driver teardown instant lies between samples; "
        "resident_to_idle_transition_upper_bound_ns is not an exact kernel or memcpy duration."
    )


def historical_backfill(batch_dir: Path, progress: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for experiment_id, progress_row in sorted(progress.items(), key=lambda item: item[1]["completed_at_utc"]):
        console = batch_dir / "consoles" / f"{experiment_id}.console.log"
        rows.append(
            {
                "experiment_id": experiment_id,
                "coverage": "historical_log_only",
                "vllm_reported_model_loading_seconds": reported_load_seconds(console),
                "console_path": str(console),
                "console_sha256": sha256_file(console) if console.is_file() else None,
                "run_dir": progress_row.get("run_dir"),
                "run_status": progress_row.get("status"),
                "completed_at_utc": progress_row.get("completed_at_utc"),
                "eviction_timing": None,
                "limitation": "Monitor was not active; high-resolution allocation/eviction timing cannot be reconstructed.",
            }
        )
    return rows


def stop_handler(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP_REQUESTED
    STOP_REQUESTED = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--interval-seconds", type=float, default=0.25)
    parser.add_argument("--resident-threshold-mib", type=float, default=80000.0)
    parser.add_argument("--idle-threshold-mib", type=float, default=1024.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval_seconds <= 0:
        raise ValueError("interval-seconds must be positive")
    if args.idle_threshold_mib >= args.resident_threshold_mib:
        raise ValueError("idle threshold must be lower than resident threshold")
    batch_dir = args.batch_dir.resolve()
    output_dir = (args.output_dir or (batch_dir / "model_residency_monitor")).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    samples_path = output_dir / "samples.jsonl"
    summary_path = output_dir / "per_run_summary.json"
    status_path = output_dir / "status.json"
    interval_ns = int(args.interval_seconds * 1_000_000_000)
    resident_bytes = int(args.resident_threshold_mib * 1024 * 1024)
    idle_bytes = int(args.idle_threshold_mib * 1024 * 1024)

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    write_json_atomic(
        output_dir / "manifest.json",
        {
            "schema_version": "phase7-model-residency-monitor-v1",
            "created_at_utc": utc_now(),
            "batch_dir": str(batch_dir),
            "gpu_index": args.gpu_index,
            "interval_seconds": args.interval_seconds,
            "resident_threshold_mib": args.resident_threshold_mib,
            "idle_threshold_mib": args.idle_threshold_mib,
            "measurement_class": "PASSIVE_NVML_SIDECAR",
            "does_not_signal_or_modify_workload": True,
        },
    )
    progress = read_progress(batch_dir / "progress.tsv")
    backfill = historical_backfill(batch_dir, progress)
    write_json_atomic(output_dir / "historical_load_backfill.json", {"records": backfill})

    records: dict[str, dict[str, Any]] = {}
    current_experiment: str | None = None
    terminal_idle_samples = 0
    sample_index = 0
    started_at = utc_now()
    write_json_atomic(status_path, {"status": "RUNNING", "started_at_utc": started_at})

    pynvml.nvmlInit()
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(args.gpu_index)
        while not STOP_REQUESTED:
            loop_started = time.monotonic_ns()
            now_ns = time.time_ns()
            batch_status = read_json(batch_dir / "status.json")
            progress = read_progress(batch_dir / "progress.tsv")
            used_bytes, processes = query_nvml(handle)
            observed_experiment = batch_status.get("current_experiment")

            if observed_experiment and observed_experiment != current_experiment:
                if current_experiment and current_experiment in records:
                    previous = records[current_experiment]
                    if previous["resident_wall_time_ns"] is not None and previous["idle_wall_time_ns"] is None:
                        if used_bytes <= idle_bytes:
                            previous["below_resident_wall_time_ns"] = (
                                previous["below_resident_wall_time_ns"] or now_ns
                            )
                            previous["idle_wall_time_ns"] = now_ns
                            previous["idle_observation_kind"] = "nvml_idle_threshold"
                        else:
                            progress_row = progress.get(current_experiment)
                            completed_ns = parse_utc_ns(
                                progress_row.get("completed_at_utc") if progress_row else None
                            )
                            if completed_ns is not None:
                                previous["below_resident_wall_time_ns"] = (
                                    previous["below_resident_wall_time_ns"] or completed_ns
                                )
                                previous["idle_wall_time_ns"] = completed_ns
                                previous["idle_observation_kind"] = (
                                    "runner_completion_proxy_no_idle_nvml_sample"
                                )
                    enrich_record(previous, batch_dir, progress, interval_ns)
                current_experiment = observed_experiment
                records.setdefault(
                    current_experiment,
                    new_live_record(current_experiment, batch_status, now_ns, used_bytes, idle_bytes),
                )

            if current_experiment and current_experiment in records:
                record = records[current_experiment]
                record["peak_used_memory_bytes"] = max(record["peak_used_memory_bytes"], used_bytes)
                if used_bytes > idle_bytes and record["first_gpu_allocation_wall_time_ns"] is None:
                    record["first_gpu_allocation_wall_time_ns"] = now_ns
                if used_bytes >= resident_bytes:
                    if record["resident_wall_time_ns"] is None:
                        record["resident_wall_time_ns"] = now_ns
                    record["last_resident_wall_time_ns"] = now_ns
                elif record["resident_wall_time_ns"] is not None and record["below_resident_wall_time_ns"] is None:
                    record["below_resident_wall_time_ns"] = now_ns
                if (
                    used_bytes <= idle_bytes
                    and record["resident_wall_time_ns"] is not None
                    and record["idle_wall_time_ns"] is None
                ):
                    record["idle_wall_time_ns"] = now_ns
                    record["idle_observation_kind"] = "nvml_idle_threshold"

            append_jsonl(
                samples_path,
                {
                    "sample_index": sample_index,
                    "wall_time_ns": now_ns,
                    "wall_time_utc": wall_ns_to_utc(now_ns),
                    "monotonic_ns": loop_started,
                    "batch_status": batch_status.get("status"),
                    "current_experiment": observed_experiment,
                    "device_used_memory_bytes": used_bytes,
                    "device_used_memory_mib": round(used_bytes / (1024 * 1024), 3),
                    "compute_processes": processes,
                },
            )
            sample_index += 1
            if sample_index % 4 == 0:
                write_json_atomic(
                    summary_path,
                    {
                        "historical_log_only_count": len(backfill),
                        "live_nvml_count": len(records),
                        "records": list(records.values()),
                    },
                )
                write_json_atomic(
                    status_path,
                    {
                        "status": "RUNNING",
                        "started_at_utc": started_at,
                        "updated_at_utc": utc_now(),
                        "sample_count": sample_index,
                        "batch_status": batch_status.get("status"),
                        "current_experiment": observed_experiment,
                    },
                )

            if batch_status.get("status") in {"PASS", "FAIL"} and used_bytes <= idle_bytes:
                terminal_idle_samples += 1
            else:
                terminal_idle_samples = 0
            if terminal_idle_samples >= 3:
                break
            sleep_ns = interval_ns - (time.monotonic_ns() - loop_started)
            if sleep_ns > 0:
                time.sleep(sleep_ns / 1_000_000_000)

        progress = read_progress(batch_dir / "progress.tsv")
        for record in records.values():
            enrich_record(record, batch_dir, progress, interval_ns)
        write_json_atomic(
            summary_path,
            {
                "historical_log_only_count": len(backfill),
                "live_nvml_count": len(records),
                "records": list(records.values()),
            },
        )
        terminal = "STOPPED" if STOP_REQUESTED else read_json(batch_dir / "status.json").get("status", "UNKNOWN")
        write_json_atomic(
            status_path,
            {
                "status": terminal,
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
                "sample_count": sample_index,
                "live_nvml_count": len(records),
                "historical_log_only_count": len(backfill),
            },
        )
        return 0 if terminal in {"PASS", "STOPPED"} else 1
    except BaseException as exc:
        write_json_atomic(
            status_path,
            {
                "status": "FAIL",
                "failed_at_utc": utc_now(),
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        return 1
    finally:
        pynvml.nvmlShutdown()


if __name__ == "__main__":
    raise SystemExit(main())
