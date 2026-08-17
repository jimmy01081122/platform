#!/usr/bin/env python3
"""Measure checkpoint-backed single-GPU transfer, queue and overlap curves."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import torch
from safetensors import safe_open


MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
DTYPE_BYTES = {"BF16": 2, "F16": 2, "F32": 4, "I64": 8, "I32": 4, "I8": 1, "U8": 1}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0


def summarize(values: list[float]) -> dict[str, Any]:
    median = statistics.median(values)
    stddev = statistics.stdev(values) if len(values) > 1 else 0.0
    half = 1.96 * stddev / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return {"count": len(values), "min": min(values), "median": median, "mean": statistics.mean(values), "max": max(values), "stddev": stddev, "ci95_halfwidth": half, "ci_rule_stable": bool(not median or half <= 0.05 * median)}


def nvml() -> dict[str, Any]:
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode(errors="replace")
        memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
        usage = pynvml.nvmlDeviceGetUtilizationRates(handle)
        optional = lambda call: _optional(call)
        return {"device_name": str(name), "memory_used_bytes": int(memory.used), "memory_total_bytes": int(memory.total), "memory_free_bytes": int(memory.free), "gpu_utilization_pct": int(usage.gpu), "memory_utilization_pct": int(usage.memory), "power_draw_w": optional(lambda: float(pynvml.nvmlDeviceGetPowerUsage(handle)) / 1000), "temperature_c": optional(lambda: int(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))), "graphics_clock_mhz": optional(lambda: int(pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS))), "memory_clock_mhz": optional(lambda: int(pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)))}
    except Exception as exc:  # pragma: no cover - platform dependent
        return {"error": f"{type(exc).__name__}: {exc}"}


def _optional(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:  # pragma: no cover - driver dependent
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_capture(command: list[str]) -> str:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as exc:  # pragma: no cover - platform dependent
        return f"UNAVAILABLE: {type(exc).__name__}: {exc}"


def capture_topology() -> dict[str, Any]:
    pci = run_capture(["nvidia-smi", "--query-gpu=index,uuid,pci.bus_id,name,driver_version", "--format=csv,noheader"])
    topo = run_capture(["nvidia-smi", "topo", "-m"])
    lscpu = run_capture(["lscpu", "-e"])
    numa = run_capture(["numactl", "--hardware"])
    numa_node = "UNAVAILABLE"
    try:
        bus = pci.split(",", 1)[0].strip() if pci else ""
        bus_path = bus.replace(":", ":")
        candidates = list(Path("/sys/bus/pci/devices").glob(f"*{bus_path.lower()}*"))
        if candidates and (candidates[0] / "numa_node").exists():
            numa_node = (candidates[0] / "numa_node").read_text().strip()
    except Exception:
        pass
    return {"pci_query": pci, "nvidia_smi_topology": topo, "lscpu_e": lscpu, "numactl_hardware": numa, "gpu_numa_node": numa_node, "remote_numa_variant": "NOT_RUN_UNAVAILABLE_ALLOCATOR"}


def discover_tensors(model_dir: Path) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for shard in sorted(model_dir.glob("*.safetensors")):
        with safe_open(str(shard), framework="pt", device="cpu") as handle:
            for name in handle.keys():
                view = handle.get_slice(name)
                shape = list(view.get_shape())
                dtype = str(view.get_dtype())
                sources[name] = {"name": name, "shard": shard.name, "shape": shape, "dtype": dtype, "bytes": int(math.prod(shape) * DTYPE_BYTES[dtype])}
    return sources


def load_cpu_tensor(model_dir: Path, source: dict[str, Any], *, pinned: bool) -> torch.Tensor:
    with safe_open(str(model_dir / source["shard"]), framework="pt", device="cpu") as handle:
        tensor = handle.get_tensor(source["name"]).contiguous()
    if pinned:
        tensor = tensor.pin_memory()
    return tensor


def make_linear_buffer(bytes_requested: int, *, pinned: bool, device: torch.device) -> tuple[torch.Tensor, torch.Tensor, int]:
    if bytes_requested <= 0 or bytes_requested % 2:
        raise ValueError(f"buffer bytes must be positive and divisible by 2: {bytes_requested}")
    host = torch.empty(bytes_requested // 2, dtype=torch.bfloat16, pin_memory=pinned)
    host.fill_(0.125)
    target = torch.empty_like(host, device=device)
    return host, target, int(host.numel() * host.element_size())


def copy_one(host: torch.Tensor, target: torch.Tensor, direction: str, stream: torch.cuda.Stream | None = None) -> int:
    if stream is None:
        if direction == "H2D":
            target.copy_(host, non_blocking=True)
        else:
            host.copy_(target, non_blocking=True)
    else:
        with torch.cuda.stream(stream):
            if direction == "H2D":
                target.copy_(host, non_blocking=True)
            else:
                host.copy_(target, non_blocking=True)
    return int(host.numel() * host.element_size())


def profiler_canary(fn: Callable[[], int]) -> dict[str, Any]:
    try:
        with torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CPU, torch.profiler.ProfilerActivity.CUDA], record_shapes=False) as profile:
            completed = fn()
            torch.cuda.synchronize()
        events = []
        for event in profile.key_averages():
            events.append({"key": event.key, "count": int(event.count), "device_time_us": float(getattr(event, "device_time_total", 0.0))})
        events.sort(key=lambda row: (-row["device_time_us"], row["key"]))
        return {"status": "PASS", "completed_bytes": completed, "events": events[:40]}
    except Exception as exc:  # pragma: no cover - runtime dependent
        return {"status": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"}


def measure_transfer(point: dict[str, Any], factory: Callable[[], tuple[Callable[[], int], Callable[[], None]]], output_dir: Path, repetitions: int = 10, max_repetitions: int = 30) -> dict[str, Any]:
    fn, cleanup = factory()
    try:
        fn()
        torch.cuda.synchronize()
        profiler = profiler_canary(fn)
        samples = []
        while len(samples) < max_repetitions:
            torch.cuda.reset_peak_memory_stats()
            start_cpu = time.monotonic_ns()
            start = torch.cuda.Event(enable_timing=True)
            end = torch.cuda.Event(enable_timing=True)
            start.record()
            after_enqueue = time.monotonic_ns()
            completed = fn()
            after_call = time.monotonic_ns()
            end.record()
            end.synchronize()
            after_sync = time.monotonic_ns()
            samples.append({"repetition": len(samples) + 1, "requested_bytes": point.get("requested_bytes"), "completed_bytes": int(completed), "gpu_duration_ns": int(round(start.elapsed_time(end) * 1_000_000)), "cpu_enqueue_ns": after_enqueue - start_cpu, "cpu_call_ns": after_call - after_enqueue, "sync_overhead_ns": after_sync - after_call, "peak_delta_bytes": max(0, int(torch.cuda.max_memory_allocated()) - int(torch.cuda.memory_allocated())), "nvml": nvml()})
            if len(samples) >= repetitions:
                summary = summarize([sample["gpu_duration_ns"] for sample in samples])
                if summary["ci_rule_stable"] or len(samples) >= max_repetitions:
                    break
        result = {**point, "status": "PASS", "measurement_class": "GPU_TRANSFER_PROBE", "warmup_count": 1, "measured_repetition_count": len(samples), "repetition_rule": "minimum 10; extend to 30 when CI95 half-width exceeds 5% of median", "gpu_duration_ns": summarize([sample["gpu_duration_ns"] for sample in samples]), "cpu_enqueue_ns": summarize([sample["cpu_enqueue_ns"] for sample in samples]), "sync_overhead_ns": summarize([sample["sync_overhead_ns"] for sample in samples]), "peak_delta_bytes": summarize([sample["peak_delta_bytes"] for sample in samples]), "profiler_canary": profiler, "samples": samples}
        append_jsonl(output_dir / "measurements.jsonl", result)
        return result
    finally:
        cleanup()
        torch.cuda.empty_cache()


def object_sources(sources: dict[str, dict[str, Any]], layer: int, expert: int) -> list[dict[str, Any]]:
    prefix = f"model.layers.{layer}.block_sparse_moe.experts.{expert}."
    selected = [source for name, source in sources.items() if name.startswith(prefix)]
    selected.sort(key=lambda source: source["name"])
    return selected


def make_object_buffers(model_dir: Path, sources: dict[str, dict[str, Any]], objects: list[tuple[int, int]], device: torch.device) -> tuple[list[torch.Tensor], list[torch.Tensor], int]:
    hosts: list[torch.Tensor] = []
    targets: list[torch.Tensor] = []
    total = 0
    for layer, expert in objects:
        selected = object_sources(sources, layer, expert)
        if len(selected) != 3:
            raise RuntimeError(f"object ({layer},{expert}) has {len(selected)} tensors")
        for source in selected:
            host = load_cpu_tensor(model_dir, source, pinned=True)
            target = torch.empty_like(host, device=device)
            hosts.append(host)
            targets.append(target)
            total += int(source["bytes"])
    return hosts, targets, total


def object_copy(hosts: list[torch.Tensor], targets: list[torch.Tensor], direction: str, stream: torch.cuda.Stream | None = None) -> int:
    completed = 0
    for host, target in zip(hosts, targets):
        completed += copy_one(host, target, direction, stream)
    return completed


def generic_factory(bytes_requested: int, direction: str, pinned: bool, device: torch.device) -> Callable[[], tuple[Callable[[], int], Callable[[], None]]]:
    def factory() -> tuple[Callable[[], int], Callable[[], None]]:
        host, target, completed = make_linear_buffer(bytes_requested, pinned=pinned, device=device)
        return lambda: copy_one(host, target, direction), lambda: None
    return factory


def object_factory(model_dir: Path, sources: dict[str, dict[str, Any]], objects: list[tuple[int, int]], direction: str, device: torch.device) -> Callable[[], tuple[Callable[[], int], Callable[[], None]]]:
    def factory() -> tuple[Callable[[], int], Callable[[], None]]:
        hosts, targets, _ = make_object_buffers(model_dir, sources, objects, device)
        return lambda: object_copy(hosts, targets, direction), lambda: None
    return factory


def add_overlap_point(point: dict[str, Any], fn_factory: Callable[[], tuple[Callable[[], dict[str, Any]], Callable[[], None]]], output_dir: Path, repetitions: int = 10) -> None:
    fn, cleanup = fn_factory()
    try:
        fn()
        torch.cuda.synchronize()
        profiler = None
        samples = []
        for index in range(repetitions):
            start = torch.cuda.Event(enable_timing=True)
            end = torch.cuda.Event(enable_timing=True)
            start.record()
            result = fn()
            end.record()
            end.synchronize()
            samples.append({"repetition": index + 1, **result, "wall_gpu_duration_ns": int(round(start.elapsed_time(end) * 1_000_000)), "nvml": nvml()})
        point = {**point, "status": "PASS", "measurement_class": "GPU_TRANSFER_PROBE", "measured_repetition_count": len(samples), "wall_gpu_duration_ns": summarize([sample["wall_gpu_duration_ns"] for sample in samples]), "samples": samples, "profiler_canary": profiler or {"status": "NOT_RUN_OVERLAP_CANARY"}}
        append_jsonl(output_dir / "measurements.jsonl", point)
    finally:
        cleanup()
        torch.cuda.empty_cache()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sequence", type=Path, required=True)
    parser.add_argument("--groups", default="L,E,Q,O")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--max-repetitions", type=int, default=30)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise SystemExit("CUDA unavailable")
    if args.repetitions < 10 or args.max_repetitions < args.repetitions or args.max_repetitions > 30:
        raise SystemExit("repetition rule violation")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    device = torch.device("cuda:0")
    sources = discover_tensors(args.model_dir)
    topology = capture_topology()
    write_json(args.output_dir / "topology.json", topology)
    config = read_json(args.model_dir / "config.json")
    expert0 = object_sources(sources, 0, 0)
    object_bytes = sum(int(source["bytes"]) for source in expert0)
    catalog = {}
    for layer in range(int(config["num_hidden_layers"])):
        for expert in range(int(config["num_local_experts"])):
            object_id = layer * int(config["num_local_experts"]) + expert
            catalog[str(object_id)] = [{"layer_id": layer, "local_expert_id": expert, "global_object_id": object_id, **source} for source in object_sources(sources, layer, expert)]
    write_json(args.output_dir / "expert_catalog.json", {"schema_version": "phase7-expert-catalog-v1", "model_revision": MODEL_REVISION, "identity_formula": "layer_id * num_local_experts + local_expert_id", "object_count": len(catalog), "objects": catalog, "content_identity_note": "checkpoint revision, tensor name, shard, dtype, shape and known bytes; no full checkpoint hash rescan"})
    write_json(args.output_dir / "manifest.json", {"schema_version": "phase7-transfer-probe-v1", "status": "RUNNING", "model_path": str(args.model_dir), "model_revision": MODEL_REVISION, "config_dimensions": {key: config.get(key) for key in ("num_hidden_layers", "num_local_experts", "num_experts_per_tok", "hidden_size", "intermediate_size")}, "object_bytes_E": object_bytes, "object_tensor_names": [source["name"] for source in expert0], "groups": [item.strip() for item in args.groups.split(",") if item.strip()], "repetitions": args.repetitions, "max_repetitions": args.max_repetitions, "measurement_class": "GPU_TRANSFER_PROBE", "raw_model_hash_not_rescanned": True})
    groups = {item.strip() for item in args.groups.split(",") if item.strip()}
    sizes = [("4KiB", 4 * 1024), ("1MiB", 1 * 1024 * 1024), ("16MiB", 16 * 1024 * 1024), ("E", object_bytes), ("4E", 4 * object_bytes)]
    held_out_sizes = [("64KiB", 64 * 1024), ("4MiB", 4 * 1024 * 1024), ("0.5E", object_bytes // 2), ("2E", 2 * object_bytes)]
    if "L" in groups:
        for role, rows, pinned in (("fit", sizes, True), ("held-out", held_out_sizes, True), ("diagnostic", sizes, False)):
            family = "XFER-L0" if role == "fit" else "XFER-L1" if role == "held-out" else "XFER-L2"
            for label, size in rows:
                for direction in ("H2D", "D2H"):
                    point = {"id": f"{family}-{label}-{direction}", "family": family, "role": role, "bytes_label": label, "requested_bytes": size, "direction": direction, "host_memory": "local_pinned" if pinned else "pageable", "allocation_node": topology.get("gpu_numa_node")}
                    measure_transfer(point, generic_factory(size, direction, pinned, device), args.output_dir, args.repetitions, args.max_repetitions)
        for label, size in (("E", object_bytes), ("2E", 2 * object_bytes)):
            for direction in ("H2D", "D2H"):
                point = {"id": f"XFER-L3-{label}-{direction}", "family": "XFER-L3", "role": "topology-variant", "bytes_label": label, "requested_bytes": size, "direction": direction, "host_memory": "remote_numa", "status": "NOT_RUN_UNAVAILABLE_ALLOCATOR"}
                append_jsonl(args.output_dir / "measurements.jsonl", point)
    if "E" in groups:
        hosts, targets, actual_bytes = make_object_buffers(args.model_dir, sources, [(0, 0)], device)
        destination = [torch.empty_like(host) for host in hosts]
        object_copy(hosts, targets, "H2D")
        torch.cuda.synchronize()
        object_copy(destination, targets, "D2H")
        torch.cuda.synchronize()
        equal = all(torch.equal(source, copy) for source, copy in zip(hosts, destination))
        write_json(args.output_dir / "content_canary.json", {"schema_version": "phase7-xfer-content-canary-v1", "status": "PASS" if equal else "FAIL", "object": {"layer_id": 0, "local_expert_id": 0, "global_object_id": 0}, "requested_bytes": actual_bytes, "completed_bytes": actual_bytes, "tensor_count": len(hosts), "source_identity": [source["name"] for source in expert0], "content_equal_after_h2d_d2h": equal})
        del hosts, targets, destination
        for count in (2, 4):
            for direction in ("H2D", "D2H"):
                measure_transfer({"id": f"XFER-E1-{count}-{direction}", "family": "XFER-E1", "role": "fit", "object_count": count, "requested_bytes": count * object_bytes, "direction": direction, "host_memory": "local_pinned", "object_identity": [(0, expert) for expert in range(count)]}, object_factory(args.model_dir, sources, [(0, expert) for expert in range(count)], direction, device), args.output_dir, args.repetitions, args.max_repetitions)
        for repeat in (1, 2, 4):
            for direction in ("H2D", "D2H"):
                objects = [(0, 0)] * repeat
                measure_transfer({"id": f"XFER-E2-{repeat}-{direction}", "family": "XFER-E2", "role": "fit", "repeat_count": repeat, "requested_bytes": repeat * object_bytes, "direction": direction, "host_memory": "local_pinned", "object_identity": objects}, object_factory(args.model_dir, sources, objects, direction, device), args.output_dir, args.repetitions, args.max_repetitions)
        sequence = read_json(args.sequence)["sequence"]
        objects = [(int(item["layer_id"]), int(item["local_expert_id"])) for item in sequence]
        for direction in ("H2D", "D2H"):
            measure_transfer({"id": f"XFER-E3-{direction}", "family": "XFER-E3", "role": "held-out", "requested_bytes": len(objects) * object_bytes, "direction": direction, "host_memory": "local_pinned", "object_identity": objects, "sequence_source": str(args.sequence)}, object_factory(args.model_dir, sources, objects, direction, device), args.output_dir, args.repetitions, args.max_repetitions)
    if "Q" in groups:
        for depth in (1, 2, 4, 8):
            point = {"id": f"XFER-Q0-{depth}-H2D", "family": "XFER-Q0", "role": "fit" if depth in (1, 4, 8) else "held-out", "queue_depth": depth, "requested_bytes": depth * object_bytes, "direction": "H2D", "host_memory": "local_pinned"}
            def factory(depth=depth):
                hosts, targets, _ = make_linear_buffer(object_bytes, pinned=True, device=device)
                stream = torch.cuda.Stream(device=device)
                def fn():
                    completed = 0
                    with torch.cuda.stream(stream):
                        for _ in range(depth):
                            completed += copy_one(hosts, targets, "H2D", stream)
                    stream.synchronize()
                    return completed
                return fn, lambda: (stream.synchronize(), hosts, targets)
            measure_transfer(point, factory, args.output_dir, args.repetitions, args.max_repetitions)
        for concurrency in (1, 2, 4):
            point = {"id": f"XFER-Q1-{concurrency}-H2D", "family": "XFER-Q1", "role": "fit" if concurrency in (1, 4) else "held-out", "copy_stream_concurrency": concurrency, "requested_bytes": concurrency * object_bytes, "direction": "H2D", "host_memory": "local_pinned"}
            def factory(concurrency=concurrency):
                buffers = [make_linear_buffer(object_bytes, pinned=True, device=device) for _ in range(concurrency)]
                streams = [torch.cuda.Stream(device=device) for _ in range(concurrency)]
                def fn():
                    for (host, target, _), stream in zip(buffers, streams):
                        copy_one(host, target, "H2D", stream)
                    for stream in streams:
                        stream.synchronize()
                    return concurrency * object_bytes
                return fn, lambda: None
            measure_transfer(point, factory, args.output_dir, args.repetitions, args.max_repetitions)
    if "O" in groups:
        for label, size in (("E", object_bytes), ("2E", 2 * object_bytes)):
            measure_transfer({"id": f"XFER-O0-{label}-H2D", "family": "XFER-O0", "role": "fit", "requested_bytes": size, "direction": "H2D", "host_memory": "local_pinned"}, generic_factory(size, "H2D", True, device), args.output_dir, args.repetitions, args.max_repetitions)
        # Overlap probes use real checkpoint q_proj and expert w1 weights with a
        # copy stream, while the transfer payload remains the actual object size.
        q_source = sources["model.layers.0.self_attn.q_proj.weight"]
        w1_source = sources["model.layers.0.block_sparse_moe.experts.0.w1.weight"]
        q_weight = load_cpu_tensor(args.model_dir, q_source, pinned=False).to(device)
        w1_weight = load_cpu_tensor(args.model_dir, w1_source, pinned=False).to(device)
        for family, weight, shape_name in (("XFER-O1", q_weight, "attention-like"), ("XFER-O2", w1_weight, "expert-FFN-like")):
            for occupancy, tokens in (("low", 1), ("high", 256)):
                host, target, _ = make_linear_buffer(object_bytes, pinned=True, device=device)
                hidden = torch.randn((tokens, int(config["hidden_size"])), device=device, dtype=torch.bfloat16)
                compute_stream = torch.cuda.Stream(device=device)
                copy_stream = torch.cuda.Stream(device=device)
                def overlap_factory(host=host, target=target, hidden=hidden, weight=weight, compute_stream=compute_stream, copy_stream=copy_stream):
                    def fn():
                        copy_start = torch.cuda.Event(enable_timing=True); copy_end = torch.cuda.Event(enable_timing=True); compute_start = torch.cuda.Event(enable_timing=True); compute_end = torch.cuda.Event(enable_timing=True); wall_start = torch.cuda.Event(enable_timing=True); wall_end = torch.cuda.Event(enable_timing=True)
                        wall_start.record()
                        with torch.cuda.stream(copy_stream):
                            copy_start.record(); target.copy_(host, non_blocking=True); copy_end.record()
                        with torch.cuda.stream(compute_stream):
                            compute_start.record(); output = torch.matmul(hidden, weight.t()); compute_end.record()
                        torch.cuda.current_stream().wait_event(copy_end); torch.cuda.current_stream().wait_event(compute_end); wall_end.record(); wall_end.synchronize()
                        return {"copy_duration_ns": int(round(copy_start.elapsed_time(copy_end) * 1_000_000)), "compute_duration_ns": int(round(compute_start.elapsed_time(compute_end) * 1_000_000)), "overlap_wall_ns": int(round(wall_start.elapsed_time(wall_end) * 1_000_000)), "requested_bytes": object_bytes, "compute_tokens": tokens}
                    return fn, lambda: None
                add_overlap_point({"id": f"{family}-{occupancy}", "family": family, "role": "fit", "occupancy": occupancy, "compute_shape": shape_name, "requested_bytes": object_bytes, "direction": "H2D"}, overlap_factory, args.output_dir, args.repetitions)
                del host, target, hidden
        for label, size in (("symmetric-E", object_bytes), ("asymmetric-2E", 2 * object_bytes)):
            def factory(size=size):
                h2d_host, h2d_target, _ = make_linear_buffer(size, pinned=True, device=device)
                d2h_host, d2h_target, _ = make_linear_buffer(size, pinned=True, device=device)
                copy_a = torch.cuda.Stream(device=device); copy_b = torch.cuda.Stream(device=device)
                def fn():
                    start = torch.cuda.Event(enable_timing=True); end = torch.cuda.Event(enable_timing=True); start.record()
                    copy_one(h2d_host, h2d_target, "H2D", copy_a); copy_one(d2h_host, d2h_target, "D2H", copy_b); copy_a.synchronize(); copy_b.synchronize(); end.record(); end.synchronize()
                    return size * 2
                return fn, lambda: None
            measure_transfer({"id": f"XFER-O3-{label}", "family": "XFER-O3", "role": "fit", "requested_bytes": size * 2, "direction": "H2D+D2H", "host_memory": "local_pinned"}, factory, args.output_dir, args.repetitions, args.max_repetitions)
    manifest = read_json(args.output_dir / "manifest.json")
    manifest["status"] = "PASS"
    manifest["completed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest["nvml_after"] = nvml()
    write_json(args.output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
