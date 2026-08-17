#!/usr/bin/env python3
"""Executable PyTorch CUDA microbenchmarks; dry-run requires no GPU or torch."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
from pathlib import Path

OPS = ("h2d_pinned", "d2h_pinned", "selected_expert", "grouped_gemm",
       "gather_scatter", "dequant", "window_replay", "cpu_runtime",
       "device_memory", "queue_depth", "contention_fixed_shape")
COMPONENT_OPERATIONS = {
    "selected_expert", "grouped_gemm", "gather_scatter", "dequant",
}


def load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("PyYAML is required except for --dry-run") from exc
    return yaml.safe_load(path.read_text())


def load_inputs(package: Path, experiment: str) -> tuple[dict, dict, dict]:
    matrix = load_yaml(package / "configs/benchmark_matrix.yaml")
    workloads = json.loads((package / "workloads/windows.json").read_text())
    if experiment not in matrix["experiments"]:
        raise SystemExit(f"unknown experiment: {experiment}")
    spec = matrix["experiments"][experiment]
    selected = workloads["splits"].get(spec["split"])
    if not selected:
        raise SystemExit(f"no workloads for split {spec['split']}")
    return matrix, spec, {"schema_version": workloads["schema_version"],
                          "items": selected}


def summary(samples: list[float], unit: str) -> dict:
    n = len(samples)
    mean = statistics.fmean(samples)
    variance = statistics.variance(samples) if n > 1 else 0.0
    t95_by_df = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
                 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 29: 2.045}
    t95 = t95_by_df.get(n - 1, 1.96)
    half = t95 * math.sqrt(variance / n) if n > 1 else 0.0
    return {
        "n": n,
        "mean": mean,
        "variance": variance,
        "stdev": math.sqrt(variance),
        "ci95": [mean - half, mean + half],
        "unit": unit,
        "ci_method": "two-sided Student-t",
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deterministic_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def make_record_id(split: str, operation: str, case: str) -> str:
    return deterministic_id(split, operation, case)


def _domain(row: dict, split: str, platform_name: str) -> dict:
    return {
        key: value for key, value in {
            "split": split,
            "phase": row.get("phase"),
            "concurrency": row.get("concurrency"),
            "platform": platform_name,
        }.items() if value is not None
    }


def build_evaluation_points(
    split: str, rows: list[dict], platform_name: str
) -> list[dict]:
    """Derive measured evaluation points exclusively from raw repeat summaries."""
    if split == "calibration":
        return []
    points: list[dict] = []
    for row in rows:
        operation = row["operation"]
        domain = _domain(row, split, platform_name)
        if operation in {"h2d_pinned", "d2h_pinned"}:
            points.append({
                "point_id": deterministic_id("point", row["record_id"], "pcie"),
                "source_record_id": row["record_id"],
                "metric": "pcie_transfer_latency",
                "measured": row["statistics"]["mean"],
                "features": {
                    "direction": row["direction"],
                    "bytes": row["bytes"],
                    "copy_streams": row["copy_streams"],
                },
                "domain": domain,
            })
        elif operation in COMPONENT_OPERATIONS:
            points.append({
                "point_id": deterministic_id("point", row["record_id"], "component"),
                "source_record_id": row["record_id"],
                "metric": "component_latency",
                "measured": row["statistics"]["mean"],
                "features": {
                    "cpu_calls": 0,
                    "gpu_operations": {operation: 1},
                    "memory_bytes": 0,
                    "queue_depth": 0,
                    "concurrency": row["concurrency"],
                },
                "domain": domain,
            })
        elif operation == "window_replay":
            features = {
                key: row[key] for key in (
                    "tokens", "cpu_calls", "gpu_operations", "memory_bytes",
                    "queue_depth", "transfers", "phase", "concurrency",
                )
            }
            for metric, measured in (
                ("moe_replay_tpot", row["statistics"]["mean"]),
                ("moe_replay_throughput", row["throughput_statistics"]["mean"]),
            ):
                points.append({
                    "point_id": deterministic_id(
                        "point", row["record_id"], metric
                    ),
                    "source_record_id": row["record_id"],
                    "metric": metric,
                    "measured": measured,
                    "features": features,
                    "domain": domain,
                })
    point_ids = [point["point_id"] for point in points]
    if len(point_ids) != len(set(point_ids)):
        raise RuntimeError("deterministic evaluation point IDs are not unique")
    return points


def package_provenance(package: Path, command: list[str]) -> dict:
    manifest_path = package / "package_manifest.json"
    checksums_path = package / "checksums.txt"
    manifest = json.loads(manifest_path.read_text())
    revision = manifest.get("package_revision")
    if not revision:
        raise SystemExit("package_manifest.json lacks package_revision")
    return {
        "command": command,
        "package_id": manifest["package_id"],
        "package_revision": revision,
        "package_manifest_sha256": sha256_file(manifest_path),
        "checksums_sha256": sha256_file(checksums_path),
    }


class CpuTimer:
    @staticmethod
    def _elapsed_ms(fn, iterations: int) -> float:
        start = time.perf_counter_ns()
        for _ in range(iterations):
            fn()
        return (time.perf_counter_ns() - start) / 1_000_000.0

    def run(
        self, fn, warmup: int, outer_repeats: int, minimum_inner_seconds: float
    ) -> tuple[list[float], int]:
        for _ in range(warmup):
            fn()
        probe_ms = max(self._elapsed_ms(fn, 100) / 100.0, 1e-6)
        inner_iterations = max(
            1, min(10_000_000, math.ceil(
                minimum_inner_seconds * 1000.0 / probe_ms
            ))
        )
        values = [
            self._elapsed_ms(fn, inner_iterations) / inner_iterations
            for _ in range(outer_repeats)
        ]
        return values, inner_iterations


class Timer:
    def __init__(self, torch_mod):
        self.torch = torch_mod

    def _elapsed_ms(self, fn, iterations: int) -> float:
        start = self.torch.cuda.Event(enable_timing=True)
        end = self.torch.cuda.Event(enable_timing=True)
        start.record()
        for _ in range(iterations):
            fn()
        end.record()
        end.synchronize()
        return float(start.elapsed_time(end))

    def run(
        self, fn, warmup: int, outer_repeats: int, minimum_inner_seconds: float
    ) -> tuple[list[float], int]:
        for _ in range(warmup):
            fn()
        self.torch.cuda.synchronize()
        probe_ms = max(self._elapsed_ms(fn, 1), 1e-6)
        inner_iterations = max(
            1, min(1_000_000, math.ceil(minimum_inner_seconds * 1000.0 / probe_ms))
        )
        out = [
            self._elapsed_ms(fn, inner_iterations) / inner_iterations
            for _ in range(outer_repeats)
        ]
        return out, inner_iterations


def run_cuda(
    matrix: dict,
    spec: dict,
    workloads: dict,
    smoke: bool,
    profiler_output: Path | None,
    provenance: dict,
) -> dict:
    import torch

    if not torch.cuda.is_available():
        raise SystemExit("CUDA unavailable; refusing to emit measured results")
    defaults = matrix["defaults"]
    warmup = 2 if smoke else int(defaults["warmup"])
    outer_repeats = 3 if smoke else int(defaults["outer_repeats"])
    minimum_inner_seconds = 0.05 if smoke else float(defaults["minimum_inner_seconds"])
    hidden = 256 if smoke else int(defaults["hidden_size"])
    intermediate = 512 if smoke else int(defaults["intermediate_size"])
    sizes = [65536, 1048576] if smoke else defaults["transfer_sizes_bytes"]
    device_name = torch.cuda.get_device_name(0)
    import re
    if not re.search(spec["target_gpu_regex"], device_name, re.I):
        raise SystemExit(
            f"GPU '{device_name}' does not match experiment target "
            f"/{spec['target_gpu_regex']}/"
        )
    seed = int(defaults["seed"])
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    timer = Timer(torch)
    cpu_timer = CpuTimer()
    raw: list[dict] = []
    run_started = time.monotonic()

    def record(operation: str, case: str, fn, **metadata) -> None:
        values, inner_iterations = timer.run(
            fn, warmup, outer_repeats, minimum_inner_seconds
        )
        metadata = {
            key: value for key, value in metadata.items() if value is not None
        }
        raw.append({"record_id": make_record_id(spec["split"], operation, case),
                    "operation": operation, "case": case, "warmup": warmup,
                    "outer_repeats": outer_repeats,
                    "inner_iterations": inner_iterations,
                    "minimum_inner_seconds": minimum_inner_seconds,
                    "repeats_ms": values, "statistics": summary(values, "ms"), **metadata})
        print(
            "PROGRESS "
            + json.dumps(
                {
                    "completed_records": len(raw),
                    "operation": operation,
                    "case": case,
                    "elapsed_seconds": round(time.monotonic() - run_started, 3),
                },
                sort_keys=True,
            ),
            flush=True,
        )

    if spec["split"] == "calibration":
        def cpu_runtime_call() -> None:
            return None

        values, inner_iterations = cpu_timer.run(
            cpu_runtime_call, warmup, outer_repeats, minimum_inner_seconds
        )
        raw.append({
            "record_id": make_record_id(
                spec["split"], "cpu_runtime", "python_function_call"
            ),
            "operation": "cpu_runtime",
            "case": "python_function_call",
            "calibration_role": "cpu_runtime",
            "warmup": warmup,
            "outer_repeats": outer_repeats,
            "inner_iterations": inner_iterations,
            "minimum_inner_seconds": minimum_inner_seconds,
            "repeats_ms": values,
            "statistics": summary(values, "ms"),
            "implementation": "perf_counter_ns_python_function_call",
        })

    for n in sizes:
        host = torch.empty(n, dtype=torch.uint8, pin_memory=True)
        dev = torch.empty(n, dtype=torch.uint8, device="cuda")
        for stream_count in ([1] if smoke else defaults["copy_streams"]):
            streams = [torch.cuda.Stream() for _ in range(stream_count)]

            def h2d(h=host, d=dev, ss=streams):
                chunk = math.ceil(h.numel() / len(ss))
                current = torch.cuda.current_stream()
                for index, stream in enumerate(ss):
                    begin, finish = index * chunk, min((index + 1) * chunk, h.numel())
                    if begin < finish:
                        with torch.cuda.stream(stream):
                            d[begin:finish].copy_(h[begin:finish], non_blocking=True)
                    current.wait_stream(stream)

            def d2h(h=host, d=dev, ss=streams):
                chunk = math.ceil(h.numel() / len(ss))
                current = torch.cuda.current_stream()
                for index, stream in enumerate(ss):
                    begin, finish = index * chunk, min((index + 1) * chunk, h.numel())
                    if begin < finish:
                        with torch.cuda.stream(stream):
                            h[begin:finish].copy_(d[begin:finish], non_blocking=True)
                    current.wait_stream(stream)

            role = "pcie_transfer" if stream_count == 1 else "copy_engine"
            transfer_metadata = {
                "bytes": n,
                "copy_streams": stream_count,
                "calibration_role": role,
            }
            record(
                "h2d_pinned", f"bytes={n},streams={stream_count}", h2d,
                direction="h2d", **transfer_metadata,
            )
            record(
                "d2h_pinned", f"bytes={n},streams={stream_count}", d2h,
                direction="d2h", **transfer_metadata,
            )

    if spec["split"] == "calibration":
        memory_sizes = (
            [65536, 1048576] if smoke
            else [int(value) for value in defaults["device_memory_sizes_bytes"]]
        )
        for n in memory_sizes:
            source = torch.empty(n, dtype=torch.uint8, device="cuda")
            destination = torch.empty_like(source)

            def device_memory_copy(src=source, dst=destination):
                dst.copy_(src)

            record(
                "device_memory", f"bytes={n}", device_memory_copy,
                calibration_role="memory", bytes=n,
                implementation="cuda_device_to_device_copy",
            )

    activation_dtype = (
        torch.bfloat16
        if defaults.get("activation_dtype") == "bfloat16"
        and torch.cuda.is_bf16_supported()
        else torch.float16
    )
    gate = torch.randn(hidden, intermediate, device="cuda", dtype=activation_dtype)
    up = torch.randn(hidden, intermediate, device="cuda", dtype=activation_dtype)
    down = torch.randn(intermediate, hidden, device="cuda", dtype=activation_dtype)
    scale = torch.tensor(0.03125, device="cuda", dtype=torch.float16)
    concurrency_values = [1] if smoke else defaults["concurrency"]
    profile_target = None

    if spec["split"] == "calibration":
        queue_tensor = torch.ones(4096, device="cuda", dtype=activation_dtype)
        queue_output = torch.empty_like(queue_tensor)
        for depth in [int(value) for value in defaults["queue_depths"]]:
            def queued_add(
                x=queue_tensor, out=queue_output, queue_depth=depth
            ):
                for _ in range(queue_depth):
                    torch.add(x, 1, out=out)

            record(
                "queue_depth", f"depth={depth}", queued_add,
                calibration_role="queueing", queue_depth=depth,
                implementation="fixed_shape_queued_cuda_add",
            )

        fixed_tokens = int(defaults["contention_fixed_expert_tokens"])
        contention_input = torch.randn(
            fixed_tokens, hidden, device="cuda", dtype=activation_dtype
        )
        contention_means: dict[int, float] = {}
        for concurrency in (1, 4):
            streams = [torch.cuda.Stream() for _ in range(concurrency)]

            def fixed_shape_contention(x=contention_input, ss=streams):
                current = torch.cuda.current_stream()
                for stream in ss:
                    with torch.cuda.stream(stream):
                        y = torch.nn.functional.silu(x @ gate) * (x @ up)
                        y @ down
                    current.wait_stream(stream)

            metadata = {
                "probe_family": "contention",
                "concurrency": concurrency,
                "fixed_expert_tokens": fixed_tokens,
                "implementation": "fixed_shape_dense_expert_gemm_per_stream",
            }
            if concurrency == 4:
                metadata.update({
                    "calibration_role": "contention",
                    "base_service_ms": contention_means[1],
                })
            record(
                "contention_fixed_shape",
                f"expert_tokens={fixed_tokens},concurrency={concurrency}",
                fixed_shape_contention,
                **metadata,
            )
            contention_means[concurrency] = raw[-1]["statistics"]["mean"]

    for item in workloads["items"][:1 if smoke else len(workloads["items"])]:
        phase_steps = (("prefill", item["prefill_step"]), ("decode", item["steps"][0]))
        for phase, phase_step in phase_steps:
            base_route = torch.tensor(
                phase_step["selected_experts"], dtype=torch.long, device="cuda"
            ).flatten()
            for concurrency in concurrency_values:
                route = base_route.repeat(concurrency)
                expert_tokens = max(1, route.numel())
                activations = torch.randn(
                    expert_tokens, hidden, device="cuda", dtype=activation_dtype
                )
                order = torch.argsort(route)
                inverse = torch.argsort(order)
                packed = torch.randint(
                    0,
                    255,
                    (expert_tokens, (hidden + 1) // 2),
                    device="cuda",
                    dtype=torch.uint8,
                )
                case = (
                    f"{item['workload_id']},phase={phase},concurrency={concurrency},"
                    f"expert_tokens={expert_tokens}"
                )

                def selected_expert_gemm(x=activations):
                    y = torch.nn.functional.silu(x @ gate) * (x @ up)
                    return y @ down

                def gather_scatter(x=activations, idx=order, inv=inverse):
                    return x.index_select(0, idx).index_select(0, inv)

                def symmetric_int4_proxy_dequant(p=packed):
                    low = (p & 0x0F).to(torch.int8)
                    high = (p >> 4).to(torch.int8)
                    low = torch.where(low >= 8, low - 16, low)
                    high = torch.where(high >= 8, high - 16, high)
                    return torch.stack((low, high), dim=-1).flatten(-2)[
                        ..., :hidden
                    ].to(torch.float16) * scale

                def grouped_gemm(x=activations, idx=order):
                    grouped = x.index_select(0, idx)
                    y = torch.nn.functional.silu(grouped @ gate) * (grouped @ up)
                    return y @ down

                record(
                    "selected_expert",
                    case,
                    selected_expert_gemm,
                    calibration_role=(
                        "gpu_service" if spec["split"] == "calibration" else None
                    ),
                    phase=phase,
                    concurrency=concurrency,
                    implementation="shape-faithful_dense_expert_gemm",
                )
                record(
                    "gather_scatter", case, gather_scatter,
                    calibration_role=(
                        "gpu_service" if spec["split"] == "calibration" else None
                    ),
                    phase=phase,
                    concurrency=concurrency,
                )
                record(
                    "dequant",
                    case + ",group=128",
                    symmetric_int4_proxy_dequant,
                    calibration_role=(
                        "gpu_service" if spec["split"] == "calibration" else None
                    ),
                    phase=phase,
                    concurrency=concurrency,
                    implementation="synthetic_symmetric_int4_proxy_not_checkpoint_awq",
                    evidence_limit="A-020b real AWQ layout compatibility remains open",
                )
                record(
                    "grouped_gemm", case, grouped_gemm,
                    calibration_role=(
                        "gpu_service" if spec["split"] == "calibration" else None
                    ),
                    phase=phase,
                    concurrency=concurrency,
                )

        replay_steps = item["steps"][:2 if smoke else len(item["steps"])]
        base_replay_routes = [
            torch.tensor(
                step["selected_experts"], device="cuda", dtype=torch.long
            ).flatten()
            for step in replay_steps
        ]
        for concurrency in concurrency_values:
            replay_routes = [route.repeat(concurrency) for route in base_replay_routes]
            max_expert_tokens = max(route.numel() for route in replay_routes)
            replay_activations = torch.randn(
                max_expert_tokens, hidden, device="cuda", dtype=activation_dtype
            )

            def window_replay(routes=replay_routes, x=replay_activations):
                out = None
                for route in routes:
                    count = route.numel()
                    idx = torch.argsort(route)
                    grouped = x[:count].index_select(0, idx)
                    y = torch.nn.functional.silu(grouped @ gate) * (grouped @ up)
                    out = (y @ down).index_select(0, torch.argsort(idx))
                return out

            values, inner_iterations = timer.run(
                window_replay, warmup, outer_repeats, minimum_inner_seconds
            )
            measured_tokens = max(
                1, sum(step["num_tokens"] for step in replay_steps) * concurrency
            )
            per_token = [value / measured_tokens for value in values]
            throughput = [measured_tokens * 1000.0 / value for value in values]
            logical_activation_bytes = sum(
                route.numel() * hidden * replay_activations.element_size()
                for route in replay_routes
            )
            replay_case = f"{item['workload_id']},concurrency={concurrency}"
            raw.append(
                {
                    "record_id": make_record_id(
                        spec["split"], "window_replay", replay_case
                    ),
                    "operation": "window_replay",
                    "metric_name": "MoE-replay TPOT",
                    "metric_scope": "not full-model TPOT",
                    "unit": "ms/token",
                    "case": replay_case,
                    "warmup": warmup,
                    "outer_repeats": outer_repeats,
                    "inner_iterations": inner_iterations,
                    "minimum_inner_seconds": minimum_inner_seconds,
                    "repeats_ms_per_token": per_token,
                    "statistics": summary(per_token, "ms/token"),
                    "throughput_metric_name": "MoE-replay throughput",
                    "throughput_unit": "tokens/s",
                    "repeats_tokens_per_second": throughput,
                    "throughput_statistics": summary(throughput, "tokens/s"),
                    "tokens": measured_tokens,
                    "cpu_calls": len(replay_routes),
                    "gpu_operations": {
                        "grouped_gemm": len(replay_routes),
                        "gather_scatter": len(replay_routes),
                    },
                    "memory_bytes": logical_activation_bytes,
                    "memory_bytes_semantics": "logical_activation_payload",
                    "queue_depth": 1,
                    "transfers": [],
                    "phase": "decode",
                    "concurrency": concurrency,
                }
            )
            print(
                "PROGRESS "
                + json.dumps(
                    {
                        "completed_records": len(raw),
                        "operation": "window_replay",
                        "case": replay_case,
                        "elapsed_seconds": round(time.monotonic() - run_started, 3),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            profile_target = window_replay
    profiler_path = None
    if profiler_output:
        profiler_output.parent.mkdir(parents=True, exist_ok=True)
        with torch.profiler.profile(
            activities=[torch.profiler.ProfilerActivity.CPU,
                        torch.profiler.ProfilerActivity.CUDA],
            record_shapes=True,
        ) as prof:
            if profile_target is None:
                raise RuntimeError("no replay workload was generated")
            profile_target()
            torch.cuda.synchronize()
        prof.export_chrome_trace(str(profiler_output))
        profiler_path = str(profiler_output)
    record_ids = [row["record_id"] for row in raw]
    if len(record_ids) != len(set(record_ids)):
        raise RuntimeError("deterministic raw benchmark record IDs are not unique")
    evaluation_points = build_evaluation_points(spec["split"], raw, device_name)
    result = {
        "schema_version": "gpu-benchmark-result-v1",
        "status": "measured",
        "evidence": "measured",
        "metric_scope": "MoE-replay; not full-model TPOT/throughput",
        "split": spec["split"],
        "experiment": spec,
        "seed": seed,
        **provenance,
        "device": {"name": device_name,
                   "capability": list(torch.cuda.get_device_capability(0)),
                   "total_memory_bytes": torch.cuda.get_device_properties(0).total_memory},
        "runtime": {"python": platform.python_version(), "torch": torch.__version__,
                    "cuda_runtime": torch.version.cuda},
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "raw_profiler_output": profiler_path,
        "raw_benchmarks": raw,
    }
    if evaluation_points:
        result["evaluation_points"] = evaluation_points
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--experiment", default="rtx-pro-6000-calibration")
    p.add_argument("--output", type=Path)
    p.add_argument("--profiler-output", type=Path)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--package-root", type=Path,
                   default=Path(__file__).resolve().parents[1])
    args = p.parse_args()
    package = args.package_root.resolve()
    if args.dry_run:
        # JSON and source syntax are validated by the package validator; no torch import.
        workloads = json.loads((package / "workloads/windows.json").read_text())
        if not workloads.get("splits"):
            raise SystemExit("workloads/windows.json has no splits")
        print(json.dumps({"status": "dry-run", "experiment": args.experiment,
                          "operations": OPS, "gpu_used": False}, indent=2))
        return 0
    matrix, spec, workloads = load_inputs(package, args.experiment)
    output = args.output or package / "results" / args.experiment / "result.json"
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    provenance = package_provenance(package, command)
    result = run_cuda(
        matrix, spec, workloads, args.smoke, args.profiler_output, provenance
    )
    if args.profiler_output:
        result["raw_profiler_output"] = os.path.relpath(
            args.profiler_output.resolve(), output.parent.resolve()
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
