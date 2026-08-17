#!/usr/bin/env python3
"""Capture GPU-side clock alignment evidence without loading the model.

This is an observability calibration probe only.  Its CUDA tensor operation is
not model evidence and is explicitly labelled as such in the output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any


FS_PER_NS = 1_000_000
FS_PER_US = 1_000_000_000
FS_PER_MS = 1_000_000_000_000


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def quantile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return int(round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight))


def fit_identity_alignment(
    alignment_id: str,
    source_clock_id: str,
    target_clock_id: str,
    points: list[dict[str, int]],
    method: str,
    grade: str,
    source_content_ids: list[str],
    shortest_component_duration_fs: int,
    validity_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    offsets = [point["target_time_fs"] - point["source_time_fs"] for point in points]
    offset = int(statistics.median(offsets)) if offsets else 0
    residuals = [value - offset for value in offsets]
    source_values = [point["source_time_fs"] for point in points]
    drift_ppm = 0.0
    if len(points) >= 2 and source_values[-1] != source_values[0]:
        drift_ppm = abs((offsets[-1] - offsets[0]) / (source_values[-1] - source_values[0])) * 1_000_000.0
    result: dict[str, Any] = {
        "schema_version": "phase7-clock-alignment-evidence-v1",
        "alignment_id": alignment_id,
        "source_clock_id": source_clock_id,
        "target_clock_id": target_clock_id,
        "units": "femtoseconds for source/target calibration values",
        "transform_type": "AFFINE_RATIONAL",
        "scale_numerator": "1",
        "scale_denominator": "1",
        "offset_fs": str(offset),
        "calibration_method": method,
        "calibration_points": points,
        "residual_error_fs": str(max((abs(value) for value in residuals), default=0)),
        "confidence_interval_95_fs": {
            "lower_error_fs": str(quantile(residuals, 0.025)),
            "upper_error_fs": str(quantile(residuals, 0.975)),
        },
        "valid_time_range": {
            "source_start_fs": str(min(source_values) if source_values else 0),
            "source_end_fs": str(max(source_values) if source_values else 0),
        },
        "drift_bound_ppm": f"{drift_ppm:.9f}",
        "shortest_component_duration_fs": str(max(1, shortest_component_duration_fs)),
        "claimed_grade": grade,
        "provenance": {
            "producer": "clock_alignment_probe.py",
            "producer_version": "phase7-clock-probe-v1",
            "source_content_ids": source_content_ids,
        },
    }
    if validity_extra:
        result.update(validity_extra)
    return result


def parse_nvidia_timestamp(value: str) -> int | None:
    value = value.strip()
    for fmt in ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(value, fmt).replace(tzinfo=dt.timezone.utc)
            return int(parsed.timestamp() * 1_000_000_000) * FS_PER_NS
        except ValueError:
            continue
    return None


def nvidia_sample() -> dict[str, Any]:
    mono_before = time.monotonic_ns()
    wall_before = time.time_ns()
    command = [
        "nvidia-smi",
        "--query-gpu=timestamp,index,uuid,temperature.gpu,power.draw,clocks.current.graphics,clocks.current.memory,memory.used,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    mono_after = time.monotonic_ns()
    wall_after = time.time_ns()
    line = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
    fields = [field.strip() for field in line.split(",")]
    target_wall_fs = parse_nvidia_timestamp(fields[0]) if fields else None
    return {
        "captured_at_utc": utc_now(),
        "source_monotonic_before_fs": mono_before * FS_PER_NS,
        "source_monotonic_after_fs": mono_after * FS_PER_NS,
        "source_wall_before_fs": wall_before * FS_PER_NS,
        "source_wall_after_fs": wall_after * FS_PER_NS,
        "capture_midpoint_monotonic_fs": ((mono_before + mono_after) // 2) * FS_PER_NS,
        "capture_midpoint_wall_fs": ((wall_before + wall_after) // 2) * FS_PER_NS,
        "nvidia_timestamp_fs": target_wall_fs,
        "command_returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "fields": fields,
    }


def run_probe(output_dir: Path, count: int) -> None:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(
        output_dir / "manifest.json",
        {
            "schema_version": "phase7-clock-probe-v1",
            "created_at_utc": utc_now(),
            "argv": sys.argv,
            "probe_class": "GPU_OBSERVABILITY_CALIBRATION_ONLY",
            "model_loaded": False,
            "synthetic_cuda_tensor_operation": True,
            "sample_count_requested": count,
            "platform": {
                "hostname": platform.node(),
                "python": sys.version,
                "torch": torch.__version__,
                "cuda_runtime": torch.version.cuda,
                "device_name": torch.cuda.get_device_name(0),
                "device_properties": {
                    "major": torch.cuda.get_device_properties(0).major,
                    "minor": torch.cuda.get_device_properties(0).minor,
                    "total_memory": torch.cuda.get_device_properties(0).total_memory,
                },
            },
        },
    )
    nvidia_samples = []
    for _ in range(max(8, min(count, 64))):
        nvidia_samples.append(nvidia_sample())
    with (output_dir / "nvml_capture.jsonl").open("w", encoding="utf-8") as handle:
        for sample in nvidia_samples:
            handle.write(json.dumps(sample, sort_keys=True) + "\n")

    torch.cuda.synchronize()
    cuda_points: list[dict[str, int]] = []
    cuda_durations: list[int] = []
    origin = torch.cuda.Event(enable_timing=True)
    origin.record()
    origin_host = time.monotonic_ns()
    torch.cuda.synchronize()
    for index in range(max(8, min(count, 64))):
        torch.cuda.synchronize()
        host_before = time.monotonic_ns()
        point = torch.cuda.Event(enable_timing=True)
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        tensor = torch.ones((4096,), device="cuda", dtype=torch.float32)
        result = tensor + 1.0
        end.record()
        point.record()
        point.synchronize()
        host_after = time.monotonic_ns()
        elapsed_from_origin_fs = int(round(origin.elapsed_time(point) * FS_PER_MS))
        operation_fs = max(1, int(round(start.elapsed_time(end) * FS_PER_MS)))
        cuda_points.append(
            {
                "index": index,
                "source_time_fs": ((host_before + host_after) // 2) * FS_PER_NS,
                "target_time_fs": elapsed_from_origin_fs,
                "host_before_fs": host_before * FS_PER_NS,
                "host_after_fs": host_after * FS_PER_NS,
            }
        )
        cuda_durations.append(operation_fs)
        del tensor, result
    torch.cuda.synchronize()
    write_json(output_dir / "cuda_event_points.json", {"origin_host_monotonic_fs": origin_host * FS_PER_NS, "points": cuda_points, "operation_durations_fs": cuda_durations})

    profiler_points: list[dict[str, int]] = []
    profiler_trace = output_dir / "profiler_anchor.json"
    activities = [torch.profiler.ProfilerActivity.CPU, torch.profiler.ProfilerActivity.CUDA]
    with torch.profiler.profile(activities=activities, record_shapes=False, with_stack=False) as profile:
        for index in range(max(8, min(count, 64))):
            host_before = time.monotonic_ns()
            with torch.profiler.record_function(f"CLK1_ANCHOR_{index:04d}"):
                tensor = torch.ones((4096,), device="cuda", dtype=torch.float32)
                result = tensor + 1.0
                torch.cuda.synchronize()
            host_after = time.monotonic_ns()
            profiler_points.append(
                {
                    "index": index,
                    "source_before_fs": host_before * FS_PER_NS,
                    "source_after_fs": host_after * FS_PER_NS,
                }
            )
            del tensor, result
    profile.export_chrome_trace(str(profiler_trace))
    with profiler_trace.open("r", encoding="utf-8") as handle:
        trace = json.load(handle)
    anchors = {
        str(event.get("name")): event
        for event in trace.get("traceEvents", [])
        if str(event.get("name", "")).startswith("CLK1_ANCHOR_")
        and event.get("cat") in {"user_annotation", "gpu_user_annotation"}
    }
    trace_points = []
    for point in profiler_points:
        event = anchors.get(f"CLK1_ANCHOR_{point['index']:04d}")
        if not event:
            continue
        trace_points.append(
            {
                "index": point["index"],
                "source_time_fs": ((point["source_before_fs"] + point["source_after_fs"]) // 2),
                "target_time_fs": int(round(float(event["ts"]) * FS_PER_US)),
                "target_duration_fs": max(1, int(round(float(event.get("dur", 0.0)) * FS_PER_US))),
                "event_category": event.get("cat"),
            }
        )
    write_json(output_dir / "profiler_anchor_points.json", {"host_points": profiler_points, "trace_points": trace_points, "trace_event_count": len(trace.get("traceEvents", []))})

    if len(trace_points) < 8:
        raise RuntimeError(f"insufficient profiler clock anchors: {len(trace_points)}")
    if len(cuda_points) < 8:
        raise RuntimeError(f"insufficient CUDA clock anchors: {len(cuda_points)}")

    source_ids = [hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in ("manifest.json", "cuda_event_points.json", "profiler_anchor_points.json", "nvml_capture.jsonl")]
    clk0_points = []
    for sample in nvidia_samples:
        clk0_points.append(
            {
                "source_time_fs": sample["capture_midpoint_monotonic_fs"],
                "target_time_fs": sample["capture_midpoint_wall_fs"],
            }
        )
    clk1_points = [{"source_time_fs": p["source_time_fs"], "target_time_fs": p["target_time_fs"]} for p in trace_points]
    clk2_points = [{"source_time_fs": p["source_time_fs"], "target_time_fs": p["target_time_fs"]} for p in cuda_points]
    clk3_points = [
        {"source_time_fs": sample["capture_midpoint_monotonic_fs"], "target_time_fs": sample["nvidia_timestamp_fs"]}
        for sample in nvidia_samples
        if sample.get("nvidia_timestamp_fs") is not None
    ]
    if len(clk3_points) < 8:
        raise RuntimeError(f"insufficient NVML clock anchors with parseable timestamps: {len(clk3_points)}")
    shortest = min(cuda_durations + [p.get("target_duration_fs", 0) for p in trace_points] or [1])
    alignments = []
    alignments.append(fit_identity_alignment("CLK0", "cpu_monotonic_fs", "cpu_wall_fs", clk0_points, "paired nvidia-smi host anchors", "ORDERING_ONLY", source_ids, shortest))
    alignments.append(fit_identity_alignment("CLK1", "cpu_monotonic_fs", "profiler_fs", clk1_points, "record_function host bounds matched to profiler user annotations", "ORDERING_ONLY", source_ids, shortest))
    alignments.append(fit_identity_alignment("CLK2", "cpu_monotonic_fs", "cuda_event_fs", clk2_points, "CUDA event host completion bounds and origin event", "ORDERING_ONLY", source_ids, shortest))
    alignments.append(fit_identity_alignment("CLK3", "cpu_monotonic_fs", "nvml_wall_fs", clk3_points, "nvidia-smi timestamp bracketed by host capture", "AGGREGATE_ONLY", source_ids, shortest))
    alignments.append(
        {
            "schema_version": "phase7-clock-alignment-evidence-v1",
            "alignment_id": "CLK4",
            "source_clock_id": "profiler_or_cuda_fs",
            "target_clock_id": "simulator_target_clock_unselected",
            "transform_type": "UNAVAILABLE",
            "claimed_grade": "UNAVAILABLE",
            "reason": "support-processor target frequency and clock profile are not selected before calibration/DSE",
            "control_latency_requirement": "UNAVAILABLE_NOT_VALIDATED",
            "provenance": {"producer": "clock_alignment_probe.py", "producer_version": "phase7-clock-probe-v1", "source_content_ids": source_ids},
        }
    )
    write_json(output_dir / "clock_alignments.json", {"schema_version": "phase7-clock-alignment-set-v1", "alignments": alignments, "shortest_component_duration_fs": shortest, "clk4_status": "UNAVAILABLE_NOT_VALIDATED"})
    write_json(output_dir / "status.json", {"status": "PASS", "finished_at_utc": utc_now(), "profiler_anchor_count": len(trace_points), "cuda_point_count": len(cuda_points), "nvml_point_count": len(nvidia_samples), "clk4": "UNAVAILABLE_NOT_VALIDATED"})
    rows = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            rows.append(f"{sha256_file(path)}  {path.relative_to(output_dir)}")
    (output_dir / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=64)
    args = parser.parse_args()
    try:
        run_probe(args.output_dir, args.count)
    except Exception as exc:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_json(args.output_dir / "status.json", {"status": "FAIL", "finished_at_utc": utc_now(), "exception_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
        print(json.dumps({"status": "FAIL", "message": str(exc)}))
        return 1
    print(json.dumps({"status": "PASS", "output_dir": str(args.output_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
