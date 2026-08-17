"""P0 clean-baseline collector."""
from __future__ import annotations

from typing import Any

from .c1_common import as_mapping, output_identity, run_generation
from .c1_contract import (
    CollectorRequest, CollectorResult, ModelRunnerLike, RecordCallback,
    build_execution_alignment_key,
)


def collect(
    runner: ModelRunnerLike,
    request: CollectorRequest,
    emit: RecordCallback | None = None,
) -> CollectorResult:
    result = CollectorResult("P0")
    output, timing = run_generation(runner, request)
    output_ids, output_hash = output_identity(output)
    input_count = int(output.get("input_token_count", 0))
    output_count = len(output_ids)
    generation_ns = timing["generation_ns"]
    ttft_ns = output.get("ttft_ns")
    tpot_ns = output.get("tpot_ns")
    timing_unavailable: dict[str, str] = {}
    if ttft_ns is None:
        timing_unavailable["ttft_ns"] = (
            "adapter does not expose streaming first-token timestamps"
        )
    if tpot_ns is None and output_count > 1 and isinstance(ttft_ns, int):
        tpot_ns = max(0, generation_ns - ttft_ns) / (output_count - 1)
    if tpot_ns is None:
        timing_unavailable["tpot_ns"] = (
            "TPOT requires streaming TTFT and at least two generated tokens"
        )
    runtime = as_mapping(runner.collect_runtime_metadata())
    try:
        native_quality = runner.collect_quality_result(output, request.sample)
    except TypeError:
        native_quality = runner.collect_quality_result(output)
    quality = as_mapping(native_quality)
    record: dict[str, Any] = {
        "schema_version": "c1-baseline-v1",
        "pass_id": "P0",
        "request_id": request.request_id,
        "execution_alignment_key": build_execution_alignment_key(request.execution),
        "model_load_ns": timing["load_ns"],
        "ttft_ns": ttft_ns,
        "tpot_ns": tpot_ns,
        "generation_ns": generation_ns,
        "e2e_ns": timing["e2e_ns"],
        "throughput_tokens_per_second": (
            output_count / (generation_ns / 1_000_000_000)
            if generation_ns > 0 else None
        ),
        "input_token_count": input_count,
        "output_token_count": output_count,
        "peak_vram_bytes": runtime.get("peak_vram_bytes"),
        "peak_host_memory_bytes": runtime.get(
            "peak_host_memory_bytes", runtime.get("peak_host_rss_bytes")
        ),
        "output_hash": output_hash,
        "stop_reason": output.get("stop_reason"),
        "quality": quality,
        "unavailable_reasons": timing_unavailable,
    }
    result.add(record, emit)
    return result
