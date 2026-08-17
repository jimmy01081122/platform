#!/usr/bin/env python3
"""Measure checkpoint-backed Mixtral component service curves on one GPU.

This is a component probe, not a vLLM model-generation run.  It materializes
actual Mixtral checkpoint tensors and records the controlled input/dispatch
semantics explicitly so its results are not confused with end-to-end model
latency or model-generated output.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import torch
import torch.nn.functional as F
from safetensors import safe_open


MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
DTYPE_BYTES = {"BF16": 2, "F16": 2, "F32": 4, "I64": 8, "I32": 4, "I8": 1, "U8": 1}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def dtype_bytes(dtype: str) -> int:
    if dtype not in DTYPE_BYTES:
        raise ValueError(f"unsupported checkpoint dtype {dtype}")
    return DTYPE_BYTES[dtype]


def load_config(model_dir: Path) -> dict[str, Any]:
    return json.loads((model_dir / "config.json").read_text(encoding="utf-8"))


def discover_tensors(model_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    sources: dict[str, dict[str, Any]] = {}
    shard_counts: dict[str, int] = {}
    for shard in sorted(model_dir.glob("*.safetensors")):
        with safe_open(str(shard), framework="pt", device="cpu") as handle:
            names = list(handle.keys())
            shard_counts[shard.name] = len(names)
            for name in names:
                view = handle.get_slice(name)
                shape = list(view.get_shape())
                dtype = str(view.get_dtype())
                sources[name] = {
                    "name": name,
                    "shard": shard.name,
                    "shape": shape,
                    "dtype": dtype,
                    "bytes": int(math.prod(shape) * dtype_bytes(dtype)),
                }
    return sources, shard_counts


def load_tensor(model_dir: Path, source: dict[str, Any], device: torch.device) -> torch.Tensor:
    with safe_open(str(model_dir / source["shard"]), framework="pt", device="cpu") as handle:
        tensor = handle.get_tensor(source["name"])
    return tensor.to(device=device, dtype=torch.bfloat16, non_blocking=True)


def capture_nvml() -> dict[str, Any]:
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode(errors="replace")
        utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)

        def optional(call: Callable[[], Any]) -> Any:
            try:
                return call()
            except Exception as exc:  # pragma: no cover - driver dependent
                return {"error": f"{type(exc).__name__}: {exc}"}

        return {
            "memory_used_bytes": int(memory.used),
            "memory_total_bytes": int(memory.total),
            "memory_free_bytes": int(memory.free),
            "device_name": str(name),
            "gpu_utilization_pct": int(utilization.gpu),
            "memory_utilization_pct": int(utilization.memory),
            "power_draw_w": optional(lambda: float(pynvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0),
            "temperature_c": optional(lambda: int(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))),
            "graphics_clock_mhz": optional(lambda: int(pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS))),
            "memory_clock_mhz": optional(lambda: int(pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM))),
        }
    except Exception as exc:  # pragma: no cover - platform dependent
        return {"error": f"{type(exc).__name__}: {exc}"}


def profiler_canary(fn: Callable[[], torch.Tensor]) -> dict[str, Any]:
    try:
        activities = [torch.profiler.ProfilerActivity.CPU, torch.profiler.ProfilerActivity.CUDA]
        with torch.profiler.profile(activities=activities, record_shapes=False) as profile:
            output = fn()
            torch.cuda.synchronize()
        del output
        rows = []
        for event in profile.key_averages():
            rows.append(
                {
                    "key": event.key,
                    "count": int(event.count),
                    "self_device_time_us": float(getattr(event, "self_device_time_total", 0.0)),
                    "device_time_us": float(getattr(event, "device_time_total", 0.0)),
                }
            )
        rows.sort(key=lambda row: (-row["device_time_us"], row["key"]))
        return {"status": "PASS", "events": rows[:40]}
    except Exception as exc:  # pragma: no cover - runtime dependent
        return {"status": "UNAVAILABLE", "error": f"{type(exc).__name__}: {exc}"}


def summarize(values: list[float]) -> dict[str, float | int | bool]:
    median = statistics.median(values)
    if len(values) > 1:
        stddev = statistics.stdev(values)
        half = 1.96 * stddev / math.sqrt(len(values))
    else:
        stddev = 0.0
        half = 0.0
    return {
        "count": len(values),
        "min": min(values),
        "median": median,
        "mean": statistics.mean(values),
        "max": max(values),
        "stddev": stddev,
        "ci95_halfwidth": half,
        "ci95_halfwidth_over_median": (half / median) if median else float("inf"),
        "ci_rule_stable": bool(median == 0 or half <= 0.05 * median),
    }


def measure_point(
    point: dict[str, Any],
    fn: Callable[[], torch.Tensor],
    output_dir: Path,
    repetitions: int,
    max_repetitions: int,
) -> dict[str, Any]:
    warmup = fn()
    torch.cuda.synchronize()
    del warmup
    samples: list[dict[str, Any]] = []
    profiler = profiler_canary(fn)
    while len(samples) < max_repetitions:
        torch.cuda.reset_peak_memory_stats()
        before = int(torch.cuda.memory_allocated())
        cpu_start = time.monotonic_ns()
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        after_enqueue = time.monotonic_ns()
        output = fn()
        after_call = time.monotonic_ns()
        end.record()
        end.synchronize()
        after_sync = time.monotonic_ns()
        gpu_ns = int(round(start.elapsed_time(end) * 1_000_000.0))
        output_mean = float(output.float().mean().item())
        del output
        peak = int(torch.cuda.max_memory_allocated())
        samples.append(
            {
                "repetition": len(samples) + 1,
                "cpu_start_ns": cpu_start,
                "cpu_after_enqueue_ns": after_enqueue,
                "cpu_after_call_ns": after_call,
                "cpu_after_sync_ns": after_sync,
                "cpu_enqueue_ns": after_enqueue - cpu_start,
                "cpu_call_ns": after_call - after_enqueue,
                "sync_overhead_ns": after_sync - after_call,
                "gpu_duration_ns": gpu_ns,
                "output_mean": output_mean,
                "memory_allocated_before_bytes": before,
                "peak_memory_allocated_bytes": peak,
                "peak_delta_bytes": max(0, peak - before),
                "nvml": capture_nvml(),
            }
        )
        if len(samples) >= repetitions:
            summary = summarize([float(sample["gpu_duration_ns"]) for sample in samples])
            if summary["ci_rule_stable"] or len(samples) >= max_repetitions:
                break
    result = {
        **point,
        "measurement_class": "CHECKPOINT_BACKED_COMPONENT_PROBE",
        "warmup_count": 1,
        "measured_repetition_count": len(samples),
        "repetition_rule": "minimum 10; extend deterministically to 30 when normal-approximation CI95 half-width exceeds 5% of median",
        "gpu_duration_ns": summarize([float(sample["gpu_duration_ns"]) for sample in samples]),
        "cpu_enqueue_ns": summarize([float(sample["cpu_enqueue_ns"]) for sample in samples]),
        "sync_overhead_ns": summarize([float(sample["sync_overhead_ns"]) for sample in samples]),
        "peak_delta_bytes": summarize([float(sample["peak_delta_bytes"]) for sample in samples]),
        "samples": samples,
        "profiler_canary": profiler,
    }
    append_jsonl(output_dir / "measurements.jsonl", result)
    return result


def attention_call(
    x: torch.Tensor,
    q_weight: torch.Tensor,
    k_weight: torch.Tensor,
    v_weight: torch.Tensor,
    o_weight: torch.Tensor,
    num_heads: int,
    num_kv_heads: int,
    head_dim: int,
    causal: bool,
) -> torch.Tensor:
    batch, seq, _ = x.shape
    q = torch.matmul(x, q_weight.t()).view(batch, seq, num_heads, head_dim).transpose(1, 2)
    k = torch.matmul(x, k_weight.t()).view(batch, seq, num_kv_heads, head_dim).transpose(1, 2)
    v = torch.matmul(x, v_weight.t()).view(batch, seq, num_kv_heads, head_dim).transpose(1, 2)
    try:
        attended = F.scaled_dot_product_attention(q, k, v, dropout_p=0.0, is_causal=causal, enable_gqa=True)
    except (TypeError, RuntimeError):
        repeat = num_heads // num_kv_heads
        attended = F.scaled_dot_product_attention(
            q, k.repeat_interleave(repeat, dim=1), v.repeat_interleave(repeat, dim=1), dropout_p=0.0, is_causal=causal
        )
    merged = attended.transpose(1, 2).contiguous().view(batch, seq, num_heads * head_dim)
    return torch.matmul(merged, o_weight.t())


def decode_attention_call(
    query: torch.Tensor,
    k_cache: torch.Tensor,
    v_cache: torch.Tensor,
    q_weight: torch.Tensor,
    o_weight: torch.Tensor,
    num_heads: int,
    num_kv_heads: int,
    head_dim: int,
) -> torch.Tensor:
    batch = query.shape[0]
    q = torch.matmul(query, q_weight.t()).view(batch, 1, num_heads, head_dim).transpose(1, 2)
    try:
        attended = F.scaled_dot_product_attention(q, k_cache, v_cache, dropout_p=0.0, is_causal=False, enable_gqa=True)
    except (TypeError, RuntimeError):
        repeat = num_heads // num_kv_heads
        attended = F.scaled_dot_product_attention(
            q, k_cache.repeat_interleave(repeat, dim=1), v_cache.repeat_interleave(repeat, dim=1), dropout_p=0.0, is_causal=False
        )
    merged = attended.transpose(1, 2).contiguous().view(batch, 1, num_heads * head_dim)
    return torch.matmul(merged, o_weight.t())


def coordinate_attention_call(
    query_hidden: torch.Tensor,
    past_k_fragments: list[torch.Tensor],
    past_v_fragments: list[torch.Tensor],
    q_weight: torch.Tensor,
    k_weight: torch.Tensor,
    v_weight: torch.Tensor,
    o_weight: torch.Tensor,
    num_heads: int,
    num_kv_heads: int,
    head_dim: int,
    phase: str,
    past_kv_length: int,
    causal_mask: torch.Tensor | None,
) -> torch.Tensor:
    batch, query_length, _ = query_hidden.shape
    q = torch.matmul(query_hidden, q_weight.t()).view(batch, query_length, num_heads, head_dim).transpose(1, 2)
    current_k = torch.matmul(query_hidden, k_weight.t()).view(batch, query_length, num_kv_heads, head_dim).transpose(1, 2)
    current_v = torch.matmul(query_hidden, v_weight.t()).view(batch, query_length, num_kv_heads, head_dim).transpose(1, 2)
    if past_k_fragments:
        k = torch.cat([*past_k_fragments, current_k], dim=2)
        v = torch.cat([*past_v_fragments, current_v], dim=2)
    else:
        k = current_k
        v = current_v
    full_prefill = phase == "prefill" and past_kv_length == 0
    try:
        attended = F.scaled_dot_product_attention(
            q,
            k,
            v,
            attn_mask=None if full_prefill else causal_mask,
            dropout_p=0.0,
            is_causal=full_prefill,
            enable_gqa=True,
        )
    except (TypeError, RuntimeError):
        repeat = num_heads // num_kv_heads
        attended = F.scaled_dot_product_attention(
            q,
            k.repeat_interleave(repeat, dim=1),
            v.repeat_interleave(repeat, dim=1),
            attn_mask=None if full_prefill else causal_mask,
            dropout_p=0.0,
            is_causal=full_prefill,
        )
    merged = attended.transpose(1, 2).contiguous().view(batch, query_length, num_heads * head_dim)
    return torch.matmul(merged, o_weight.t())


def attention_core_kv_update_proxy_call(
    q: torch.Tensor,
    current_k: torch.Tensor,
    current_v: torch.Tensor,
    past_k_fragments: list[torch.Tensor],
    past_v_fragments: list[torch.Tensor],
    num_heads: int,
    num_kv_heads: int,
    phase: str,
    past_kv_length: int,
    causal_mask: torch.Tensor | None,
) -> torch.Tensor:
    if past_k_fragments:
        k = torch.cat([*past_k_fragments, current_k], dim=2)
        v = torch.cat([*past_v_fragments, current_v], dim=2)
    else:
        k = current_k
        v = current_v
    full_prefill = phase == "prefill" and past_kv_length == 0
    try:
        return F.scaled_dot_product_attention(
            q,
            k,
            v,
            attn_mask=None if full_prefill else causal_mask,
            dropout_p=0.0,
            is_causal=full_prefill,
            enable_gqa=True,
        )
    except (TypeError, RuntimeError):
        repeat = num_heads // num_kv_heads
        return F.scaled_dot_product_attention(
            q,
            k.repeat_interleave(repeat, dim=1),
            v.repeat_interleave(repeat, dim=1),
            attn_mask=None if full_prefill else causal_mask,
            dropout_p=0.0,
            is_causal=full_prefill,
        )


def load_attention_weights(model_dir: Path, sources: dict[str, dict[str, Any]], device: torch.device) -> dict[str, torch.Tensor]:
    names = {
        "q": "model.layers.0.self_attn.q_proj.weight",
        "k": "model.layers.0.self_attn.k_proj.weight",
        "v": "model.layers.0.self_attn.v_proj.weight",
        "o": "model.layers.0.self_attn.o_proj.weight",
    }
    return {key: load_tensor(model_dir, sources[name], device) for key, name in names.items()}


def load_moe_weights(model_dir: Path, sources: dict[str, dict[str, Any]], device: torch.device) -> tuple[torch.Tensor, dict[int, dict[str, torch.Tensor]]]:
    gate_name = "model.layers.0.block_sparse_moe.gate.weight"
    gate = load_tensor(model_dir, sources[gate_name], device)
    experts: dict[int, dict[str, torch.Tensor]] = {}
    for expert in range(8):
        experts[expert] = {}
        for short, suffix in (("w1", "w1.weight"), ("w2", "w2.weight"), ("w3", "w3.weight")):
            name = f"model.layers.0.block_sparse_moe.experts.{expert}.{suffix}"
            experts[expert][short] = load_tensor(model_dir, sources[name], device)
    return gate, experts


def make_attention_points(config: dict[str, Any]) -> list[dict[str, Any]]:
    del config

    def point(prefix: str, role: str, query_length: int, past_kv_length: int, batch: int, phase: str, fragments: int) -> dict[str, Any]:
        fragmentation = "contiguous" if fragments == 1 else f"segmented-{fragments}"
        return {
            "id": f"{prefix}-q{query_length}-p{past_kv_length}-b{batch}-{phase}-{fragmentation}",
            "family": "CMP-A",
            "role": role,
            "mode": phase,
            "query_length": query_length,
            "chunk_length": query_length,
            "past_kv_length": past_kv_length,
            "active_sequences": batch,
            "batch_size": batch,
            "phase": phase,
            "kv_fragmentation_mode": fragmentation,
            "kv_fragment_count": fragments,
            "attention_coordinate": {
                "query_length": query_length,
                "chunk_length": query_length,
                "past_kv_length": past_kv_length,
                "active_sequences": batch,
                "batch_size": batch,
                "phase": phase,
                "kv_fragmentation": {"mode": fragmentation, "fragment_count": fragments},
            },
            "fused_path_correlation_status": "PENDING_VLLM_FUSED_PATH_GATE",
            "model_bound_claim_allowed": False,
        }

    points: list[dict[str, Any]] = []
    for query_length in (128, 2048, 8192, 16384, 24576, 31744):
        points.append(point("CMP-A0", "fit", query_length, 0, 1, "prefill", 1))
    for query_length in (512, 4096, 12288, 28672):
        points.append(point("CMP-A1", "held-out", query_length, 0, 1, "prefill", 1))
    for query_length, past_kv_length, batch, phase, fragments in (
        (1, 512, 1, "decode", 1),
        (1, 4096, 4, "decode", 1),
        (1, 16384, 1, "decode", 1),
        (1, 16384, 4, "decode", 16),
        (1, 4096, 8, "decode", 16),
        (1, 16384, 8, "decode", 16),
        (1, 16384, 1, "decode", 16),
        (1, 31744, 4, "decode", 16),
        (512, 1536, 1, "chunked-prefill", 1),
        (512, 7680, 4, "chunked-prefill", 16),
        (512, 24576, 1, "chunked-prefill", 16),
        (512, 31744, 4, "chunked-prefill", 16),
    ):
        points.append(point("CMP-A2", "serving-fit", query_length, past_kv_length, batch, phase, fragments))
    for query_length, past_kv_length, batch, phase, fragments in (
        (1, 2048, 2, "decode", 1),
        (1, 8192, 8, "decode", 16),
        (1, 28672, 2, "decode", 16),
        (512, 28160, 2, "chunked-prefill", 16),
    ):
        points.append(point("CMP-A3", "serving-held-out", query_length, past_kv_length, batch, phase, fragments))
    for past_kv_length in range(0, 2048, 256):
        phase = "prefill" if past_kv_length == 0 else "chunked-prefill"
        correlation_point = point(
            "CMP-ACORR", "fused-correlation-fit", 256, past_kv_length, 1, phase, 1
        )
        correlation_point["measurement_scope"] = "attention-core-kv-update-proxy"
        correlation_point["fused_path_correlation_candidate"] = True
        points.append(correlation_point)
    correlation_decode = point(
        "CMP-ACORR", "fused-correlation-fit", 1, 2048, 1, "decode", 1
    )
    correlation_decode["measurement_scope"] = "attention-core-kv-update-proxy"
    correlation_decode["fused_path_correlation_candidate"] = True
    points.append(correlation_decode)
    return points


def make_moe_points(m3_bins: dict[str, int]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for role, values in (("fit", [1, 4, 16, 64, 256]), ("held-out", [2, 8, 32, 128])):
        prefix = "CMP-M0" if role == "fit" else "CMP-M1"
        for value in values:
            points.append({"id": f"{prefix}-{value}", "family": "CMP-M", "role": role, "routing_shape": "balanced", "tokens_per_expert_target": value})
    for value in [1, 4, 16, 64, 256]:
        points.append({"id": f"CMP-M2-{value}", "family": "CMP-M", "role": "fit", "routing_shape": "deterministic_skew_hotspot", "tokens_per_expert_target": value})
    for key in ("p10", "p50", "p90", "max"):
        points.append({"id": f"CMP-M3-{key}", "family": "CMP-M", "role": "held-out", "routing_shape": "actual_observed_distribution", "occupancy_bin": key, "tokens_per_expert_target": int(m3_bins[key])})
    return points


def moe_call(x: torch.Tensor, gate: torch.Tensor, experts: dict[int, dict[str, torch.Tensor]], tokens_per_expert: int, mode: str) -> tuple[torch.Tensor, dict[str, Any]]:
    if mode == "balanced":
        total = tokens_per_expert * 8
        primary = torch.arange(total, device=x.device, dtype=torch.long) % 8
        secondary = (primary + 1) % 8
    elif mode == "skew":
        total = tokens_per_expert * 8
        primary = torch.zeros(total, device=x.device, dtype=torch.long)
        secondary = torch.ones(total, device=x.device, dtype=torch.long)
    else:
        total = tokens_per_expert * 8
        primary = torch.arange(total, device=x.device, dtype=torch.long) % 8
        secondary = (primary + 1) % 8
    hidden = x[:total]
    router_logits = torch.matmul(hidden, gate.t())
    router_probs = torch.softmax(router_logits, dim=-1)
    route_indices = torch.stack((primary, secondary), dim=1)
    route_counts = torch.bincount(route_indices.reshape(-1), minlength=8)
    dispatched: list[torch.Tensor] = []
    dispatched_experts: list[int] = []
    for expert in range(8):
        positions = torch.nonzero((route_indices == expert).any(dim=1), as_tuple=False).flatten()
        if positions.numel() == 0:
            continue
        dispatched.append(hidden.index_select(0, positions))
        dispatched_experts.append(expert)
    output = torch.zeros_like(hidden)
    for expert, tokens in zip(dispatched_experts, dispatched):
        weights = experts[expert]
        intermediate = F.silu(torch.matmul(tokens, weights["w1"].t())) * torch.matmul(tokens, weights["w3"].t())
        result = torch.matmul(intermediate, weights["w2"].t())
        positions = torch.nonzero((route_indices == expert).any(dim=1), as_tuple=False).flatten()
        output.index_add_(0, positions, result * 0.5)
    return output, {
        "router_active_expert_count": int(torch.count_nonzero(torch.bincount(torch.topk(router_probs, k=2, dim=-1).indices.reshape(-1), minlength=8)).item()),
        "controlled_active_expert_count": int(torch.count_nonzero(route_counts).item()),
        "controlled_route_counts": [int(value) for value in route_counts.cpu().tolist()],
        "router_entropy_mean": float((-(router_probs * router_probs.clamp_min(1e-20).log()).sum(dim=-1)).mean().item()),
    }


def make_launch_points() -> list[dict[str, Any]]:
    points = []
    for value in [1, 2, 4, 8, 16]:
        points.append({"id": f"CMP-L0-{value}", "family": "CMP-L", "role": "fit", "axis": "launch_count", "value": value})
    for value in [1, 2, 4]:
        points.append({"id": f"CMP-L1-{value}", "family": "CMP-L", "role": "fit", "axis": "stream_count", "value": value})
    for value in [1, 2, 4, 8]:
        points.append({"id": f"CMP-L2-{value}", "family": "CMP-L", "role": "fit", "axis": "outstanding_work", "value": value})
    for value in [0, 1_000_000, 5_000_000, 20_000_000]:
        points.append({"id": f"CMP-L3-{value}", "family": "CMP-L", "role": "fit", "axis": "idle_gap_ns", "value": value})
    return points


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--m3-bins", type=Path, required=True)
    parser.add_argument("--groups", default="A,M,L", help="comma-separated component groups")
    parser.add_argument("--attention-point-id", action="append", default=[], help="optional exact CMP-A point filter; repeatable")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--max-repetitions", type=int, default=30)
    args = parser.parse_args()
    if args.repetitions < 10 or args.max_repetitions < args.repetitions or args.max_repetitions > 30:
        raise SystemExit("repetitions must be >=10 and max-repetitions must be within repetitions..30")
    if not torch.cuda.is_available():
        raise SystemExit("CUDA unavailable")
    device = torch.device("cuda:0")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    config = load_config(args.model_dir)
    sources, shard_counts = discover_tensors(args.model_dir)
    m3 = json.loads(args.m3_bins.read_text(encoding="utf-8"))
    all_attention_points = make_attention_points(config)
    requested_attention_ids = set(args.attention_point_id)
    known_attention_ids = {point["id"] for point in all_attention_points}
    unknown_attention_ids = sorted(requested_attention_ids - known_attention_ids)
    if unknown_attention_ids:
        raise SystemExit(f"unknown attention point IDs: {unknown_attention_ids}")
    attention_points = [point for point in all_attention_points if not requested_attention_ids or point["id"] in requested_attention_ids]
    write_json(args.output_dir / "manifest.json", {
        "schema_version": "phase7-component-probe-v2",
        "status": "RUNNING",
        "model_path": str(args.model_dir),
        "model_revision": MODEL_REVISION,
        "config_dimensions": {key: config.get(key) for key in ("num_hidden_layers", "num_local_experts", "num_experts_per_tok", "hidden_size", "intermediate_size", "num_attention_heads", "num_key_value_heads", "torch_dtype")},
        "shard_count": len(shard_counts),
        "tensor_count": len(sources),
        "shard_tensor_counts": shard_counts,
        "groups": [item.strip() for item in args.groups.split(",") if item.strip()],
        "repetitions": args.repetitions,
        "max_repetitions": args.max_repetitions,
        "m3_bins_source": str(args.m3_bins),
        "measurement_class": "CHECKPOINT_BACKED_COMPONENT_PROBE",
        "not_end_to_end_model_generation": True,
        "attention_coordinate_schema": "query/chunk length x past-KV length x active sequences/batch x phase x KV fragmentation",
        "attention_point_ids": [point["id"] for point in attention_points],
        "attention_point_filter": sorted(requested_attention_ids),
        "isolated_fused_correlation_gate": "PENDING_VLLM_FUSED_PATH_GATE",
    })
    catalog: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pattern = re.compile(r"model\.layers\.(\d+)\.block_sparse_moe\.experts\.(\d+)\.(.+)")
    for name, source in sources.items():
        match = pattern.fullmatch(name)
        if match:
            layer, expert, tensor_name = int(match.group(1)), int(match.group(2)), match.group(3)
            catalog[str(layer * int(config["num_local_experts"]) + expert)].append({"layer_id": layer, "local_expert_id": expert, "global_object_id": layer * int(config["num_local_experts"]) + expert, "tensor_name": tensor_name, **source})
    write_json(args.output_dir / "expert_catalog.json", {
        "schema_version": "phase7-expert-catalog-v1",
        "model_revision": MODEL_REVISION,
        "identity_formula": "layer_id * num_local_experts + local_expert_id",
        "num_layers": int(config["num_hidden_layers"]),
        "num_local_experts": int(config["num_local_experts"]),
        "object_count": len(catalog),
        "objects": {key: sorted(value, key=lambda item: item["tensor_name"]) for key, value in sorted(catalog.items(), key=lambda item: int(item[0]))},
        "content_identity_note": "checkpoint revision, tensor name, shard, dtype, shape and known bytes are frozen; no full checkpoint rescan/hash was performed",
    })
    groups = {item.strip() for item in args.groups.split(",") if item.strip()}
    attention = None
    if "A" in groups or "L" in groups:
        attention = load_attention_weights(args.model_dir, sources, device)
    if "A" in groups:
        num_heads = int(config["num_attention_heads"])
        num_kv_heads = int(config.get("num_key_value_heads", num_heads))
        head_dim = int(config.get("head_dim", int(config["hidden_size"]) // num_heads))
        for point in attention_points:
            batch = int(point["active_sequences"])
            query_length = int(point["query_length"])
            past_kv_length = int(point["past_kv_length"])
            fragment_count = int(point["kv_fragment_count"])
            seed = 1000 + query_length * 3 + past_kv_length * 5 + batch * 7 + fragment_count * 11
            generator = torch.Generator(device=device).manual_seed(seed)
            query_hidden = torch.randn((batch, query_length, int(config["hidden_size"])), device=device, dtype=torch.bfloat16, generator=generator)
            past_hidden = None
            past_k = None
            past_v = None
            past_k_fragments: list[torch.Tensor] = []
            past_v_fragments: list[torch.Tensor] = []
            if past_kv_length > 0:
                past_hidden = torch.randn((batch, past_kv_length, int(config["hidden_size"])), device=device, dtype=torch.bfloat16, generator=generator)
                with torch.inference_mode():
                    past_k = torch.matmul(past_hidden, attention["k"].t()).view(batch, past_kv_length, num_kv_heads, head_dim).transpose(1, 2).contiguous()
                    past_v = torch.matmul(past_hidden, attention["v"].t()).view(batch, past_kv_length, num_kv_heads, head_dim).transpose(1, 2).contiguous()
                actual_fragments = min(fragment_count, past_kv_length)
                if actual_fragments == 1:
                    past_k_fragments = [past_k]
                    past_v_fragments = [past_v]
                else:
                    past_k_fragments = [fragment.contiguous() for fragment in torch.tensor_split(past_k, actual_fragments, dim=2)]
                    past_v_fragments = [fragment.contiguous() for fragment in torch.tensor_split(past_v, actual_fragments, dim=2)]
            causal_mask = None
            if point["phase"] == "chunked-prefill":
                key_positions = torch.arange(past_kv_length + query_length, device=device).view(1, -1)
                query_positions = torch.arange(query_length, device=device).view(-1, 1) + past_kv_length
                causal_mask = key_positions <= query_positions
            correlation_candidate = bool(point.get("fused_path_correlation_candidate"))
            current_q = None
            current_k = None
            current_v = None
            if correlation_candidate:
                with torch.inference_mode():
                    current_q = torch.matmul(query_hidden, attention["q"].t()).view(
                        batch, query_length, num_heads, head_dim
                    ).transpose(1, 2).contiguous()
                    current_k = torch.matmul(query_hidden, attention["k"].t()).view(
                        batch, query_length, num_kv_heads, head_dim
                    ).transpose(1, 2).contiguous()
                    current_v = torch.matmul(query_hidden, attention["v"].t()).view(
                        batch, query_length, num_kv_heads, head_dim
                    ).transpose(1, 2).contiguous()
                fn = lambda current_q=current_q, current_k=current_k, current_v=current_v, past_k_fragments=past_k_fragments, past_v_fragments=past_v_fragments, causal_mask=causal_mask, point=point: attention_core_kv_update_proxy_call(
                    current_q,
                    current_k,
                    current_v,
                    past_k_fragments,
                    past_v_fragments,
                    num_heads,
                    num_kv_heads,
                    str(point["phase"]),
                    int(point["past_kv_length"]),
                    causal_mask,
                )
            else:
                fn = lambda query_hidden=query_hidden, past_k_fragments=past_k_fragments, past_v_fragments=past_v_fragments, causal_mask=causal_mask, point=point: coordinate_attention_call(
                    query_hidden,
                    past_k_fragments,
                    past_v_fragments,
                    attention["q"],
                    attention["k"],
                    attention["v"],
                    attention["o"],
                    num_heads,
                    num_kv_heads,
                    head_dim,
                    str(point["phase"]),
                    int(point["past_kv_length"]),
                    causal_mask,
                )
            past_kv_bytes = sum(int(tensor.numel() * tensor.element_size()) for tensor in past_k_fragments + past_v_fragments)
            point = {
                **point,
                "query_input_bytes": int(query_hidden.numel() * query_hidden.element_size()),
                "past_kv_bytes": past_kv_bytes,
                "current_kv_bytes": int(batch * query_length * num_kv_heads * head_dim * 2 * 2),
                "kv_cache_bytes": past_kv_bytes,
                "weight_bytes": sum(int(value.numel() * value.element_size()) for value in attention.values()),
                "kv_fragmentation_semantics": "single contiguous KV tensor" if fragment_count == 1 else "synthetic segmented KV fragments concatenated inside timed isolated probe; not vLLM paged-attention",
                "kernel_identity_source": (
                    "isolated torch.nn.functional.scaled_dot_product_attention with precomputed "
                    "checkpoint layer0 q/k/v and timed KV assembly"
                    if correlation_candidate
                    else "isolated torch.nn.functional.scaled_dot_product_attention + checkpoint layer0 self_attn q/k/v/o"
                ),
                "not_vllm_fused_kernel": True,
            }
            with torch.inference_mode():
                measure_point(point, fn, args.output_dir, args.repetitions, args.max_repetitions)
            del query_hidden, past_k_fragments, past_v_fragments
            if current_q is not None:
                del current_q, current_k, current_v
            if past_hidden is not None:
                del past_hidden, past_k, past_v
            if causal_mask is not None:
                del causal_mask
            torch.cuda.empty_cache()
    if "M" in groups:
        gate, experts = load_moe_weights(args.model_dir, sources, device)
        hidden_size = int(config["hidden_size"])
        m3_bins = m3["bins"]
        for point in make_moe_points(m3_bins):
            target = int(point["tokens_per_expert_target"])
            total = max(target * 8, 8)
            generator = torch.Generator(device=device).manual_seed(3000 + target)
            x = torch.randn((total, hidden_size), device=device, dtype=torch.bfloat16, generator=generator)
            mode = "balanced" if point["routing_shape"] == "balanced" else "skew" if "skew" in point["routing_shape"] else "observed"
            route_holder: dict[str, Any] = {}
            def fn(x=x, mode=mode, target=target):
                output, details = moe_call(x, gate, experts, target, mode)
                route_holder.clear()
                route_holder.update(details)
                return output
            point = {**point, "input_bytes": int(x.numel() * 2), "expert_weight_bytes": int(sum(weight.numel() * 2 for expert in experts.values() for weight in expert.values())), "router_weight_bytes": int(gate.numel() * 2), "kernel_identity_source": "checkpoint layer0 gate + actual layer0 expert w1/w2/w3; controlled dispatch schedule", "dispatch_semantics": mode}
            result = measure_point(point, fn, args.output_dir, args.repetitions, args.max_repetitions)
            result["routing_details_last_repetition"] = route_holder
            # Rewrite the just-appended JSON line with the routing details attached.
            lines = (args.output_dir / "measurements.jsonl").read_text(encoding="utf-8").splitlines()
            lines[-1] = json.dumps(result, sort_keys=True)
            (args.output_dir / "measurements.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            del x
            torch.cuda.empty_cache()
        del gate, experts
    if "L" in groups:
        assert attention is not None
        q_weight = attention["q"]
        hidden_size = int(config["hidden_size"])
        for point in make_launch_points():
            value = int(point["value"])
            generator = torch.Generator(device=device).manual_seed(4000 + value)
            x = torch.randn((1, hidden_size), device=device, dtype=torch.bfloat16, generator=generator)
            if point["axis"] == "launch_count":
                fn = lambda x=x, value=value: sum((torch.matmul(x, q_weight.t()) for _ in range(value)))
            elif point["axis"] == "stream_count":
                streams = [torch.cuda.Stream(device=device) for _ in range(value)]
                def fn(x=x, streams=streams):
                    outputs = []
                    for stream in streams:
                        with torch.cuda.stream(stream):
                            outputs.append(torch.matmul(x, q_weight.t()))
                    for stream in streams:
                        stream.synchronize()
                    return torch.stack(outputs).sum(dim=0)
            elif point["axis"] == "outstanding_work":
                fn = lambda x=x, value=value: torch.stack([torch.matmul(x, q_weight.t()) for _ in range(value)]).sum(dim=0)
            else:
                gap_ns = value
                def fn(x=x, gap_ns=gap_ns):
                    if gap_ns:
                        time.sleep(gap_ns / 1_000_000_000)
                    return torch.matmul(x, q_weight.t())
            point = {**point, "input_bytes": int(x.numel() * 2), "weight_bytes": int(q_weight.numel() * 2), "kernel_identity_source": "checkpoint layer0 self_attn.q_proj.weight matmul"}
            with torch.inference_mode():
                measure_point(point, fn, args.output_dir, args.repetitions, args.max_repetitions)
            del x
            torch.cuda.empty_cache()
    manifest = json.loads((args.output_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["status"] = "PASS"
    manifest["completed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest["nvml_after"] = capture_nvml()
    write_json(args.output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
