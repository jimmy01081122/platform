"""P3 memory and allocator observation collector."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .c1_common import as_mapping, generate, load_runner
from .c1_contract import (
    CollectorRequest, CollectorResult, ModelRunnerLike, RecordCallback,
    build_execution_alignment_key,
)


FIELDS = (
    "model_weight_bytes",
    "activation_bytes",
    "kv_cache_bytes",
    "workspace_bytes",
    "gpu_allocated_bytes",
    "gpu_reserved_bytes",
    "peak_vram_bytes",
    "host_memory_bytes",
    "peak_host_memory_bytes",
    "allocation_events",
    "free_events",
    "oom",
    "allocator_retries",
    "fragmentation",
)


def _host_rss_bytes() -> int:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise OSError("/proc/self/status does not contain VmRSS")


def _cuda_snapshot(torch: Any) -> dict[str, int]:
    return {
        "allocated_bytes": int(torch.cuda.memory_allocated()),
        "reserved_bytes": int(torch.cuda.memory_reserved()),
        "peak_allocated_bytes": int(torch.cuda.max_memory_allocated()),
        "peak_reserved_bytes": int(torch.cuda.max_memory_reserved()),
        "allocator_retries": int(
            torch.cuda.memory_stats().get("num_alloc_retries", 0)
        ),
    }


def collect(
    runner: ModelRunnerLike,
    request: CollectorRequest,
    observations: Mapping[str, Any] | None = None,
    emit: RecordCallback | None = None,
) -> CollectorResult:
    unavailable: dict[str, str] = {}
    if observations is None:
        load_runner(runner)
        tokens = runner.tokenize(request.prompt)
        try:
            host_before = _host_rss_bytes()
        except OSError as exc:
            host_before = None
            unavailable["host_memory_bytes"] = str(exc)
        torch = None
        cuda_before = None
        try:
            import torch as torch_module
            torch = torch_module
        except (ImportError, OSError) as exc:
            unavailable.update({
                name: f"torch unavailable: {exc}"
                for name in (
                    "gpu_allocated_bytes", "gpu_reserved_bytes",
                    "peak_vram_bytes", "allocator_retries",
                )
            })
        if torch is not None and torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
            cuda_before = _cuda_snapshot(torch)
        elif torch is not None:
            unavailable.update({
                name: "CUDA is unavailable to torch"
                for name in (
                    "gpu_allocated_bytes", "gpu_reserved_bytes",
                    "peak_vram_bytes", "allocator_retries",
                )
            })
        generation = as_mapping(generate(runner, tokens, request.generation_config))
        try:
            host_after = _host_rss_bytes()
        except OSError as exc:
            host_after = None
            unavailable["peak_host_memory_bytes"] = str(exc)
        cuda_after = (
            _cuda_snapshot(torch)
            if torch is not None and torch.cuda.is_available() else None
        )
    runtime = (
        dict(observations)
        if observations is not None
        else as_mapping(runner.collect_runtime_metadata())
    )
    if "peak_host_memory_bytes" not in runtime and "peak_host_rss_bytes" in runtime:
        runtime["peak_host_memory_bytes"] = runtime["peak_host_rss_bytes"]
    unavailable.update(dict(runtime.get("unavailable_reasons") or {}))
    if observations is None:
        runtime.update({
            "host_memory_bytes": host_after,
            "peak_host_memory_bytes": max(
                value for value in (host_before, host_after) if value is not None
            ) if host_before is not None or host_after is not None else None,
            "host_rss_before_bytes": host_before,
            "host_rss_after_bytes": host_after,
            "cuda_before": cuda_before,
            "cuda_after": cuda_after,
            "oom": bool(
                generation.get("return_code")
                and "out of memory" in str(generation.get("exception", "")).lower()
            ),
        })
        if cuda_after is not None:
            runtime.update({
                "gpu_allocated_bytes": cuda_after["allocated_bytes"],
                "gpu_reserved_bytes": cuda_after["reserved_bytes"],
                "peak_vram_bytes": cuda_after["peak_allocated_bytes"],
                "allocator_retries": (
                    cuda_after["allocator_retries"]
                    - (cuda_before or {}).get("allocator_retries", 0)
                ),
            })
        runtime["oom_available"] = True
        runtime["allocator_retries_available"] = cuda_after is not None
    values = {}
    for field in FIELDS:
        values[field] = runtime.get(field)
        if field not in runtime:
            unavailable.setdefault(field, "runtime does not expose this observation")
    record = {
        "schema_version": "c1-memory-v1",
        "pass_id": "P3",
        "execution_alignment_key": build_execution_alignment_key(request.execution),
        **values,
        "host_rss_before_bytes": runtime.get("host_rss_before_bytes"),
        "host_rss_after_bytes": runtime.get("host_rss_after_bytes"),
        "cuda_before": runtime.get("cuda_before"),
        "cuda_after": runtime.get("cuda_after"),
        "oom_available": runtime.get("oom_available", "oom" in runtime),
        "allocator_retries_available": runtime.get(
            "allocator_retries_available", "allocator_retries" in runtime
        ),
        "unavailable_reasons": unavailable,
    }
    result = CollectorResult("P3", unavailable=unavailable)
    result.add(record, emit)
    return result
