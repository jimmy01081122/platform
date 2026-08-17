"""Opt-in vLLM 0.23 scheduler/worker correlation hook for Phase 7.

The hook activates only when ``PHASE7_ENABLE_STEP_TRACE=1``.  It never edits
vLLM sources.  The campaign runner assigns ``PHASE7_STEP_TRACE_DIR`` after it
creates the immutable run directory; wrappers read that path at call time.
"""

from __future__ import annotations

import json
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any


HOOK_REVISION = "phase7-step-trace-hook-v2"
_CURRENT_FORWARD_STEP: ContextVar[int | str] = ContextVar(
    "phase7_current_forward_step", default="UNAVAILABLE"
)


def _trace_dir() -> Path | None:
    value = os.environ.get("PHASE7_STEP_TRACE_DIR")
    if not value:
        return None
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_jsonl(name: str, record: dict[str, Any]) -> None:
    root = _trace_dir()
    if root is None:
        return
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(root / name, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)


def _request_phase(scheduler_output: Any, request_id: str) -> tuple[str, int | None]:
    for request in scheduler_output.scheduled_new_reqs:
        if str(request.req_id) == request_id:
            prior = int(request.num_computed_tokens)
            return ("prefill" if prior == 0 else "chunked-prefill", prior)
    cached = scheduler_output.scheduled_cached_reqs
    prior_by_id = {
        str(req_id): int(value)
        for req_id, value in zip(cached.req_ids, cached.num_computed_tokens)
    }
    if request_id in prior_by_id:
        phase = "chunked-prefill" if cached.is_context_phase(request_id) else "decode"
        return phase, prior_by_id[request_id]
    return "UNAVAILABLE", None


def _materialized_num_experts(scheduler: Any) -> int:
    hf_config = scheduler.vllm_config.model_config.hf_text_config
    for name in ("num_experts", "n_routed_experts", "num_local_experts"):
        value = getattr(hf_config, name, None)
        if value is not None:
            value = int(value)
            if value > 0:
                return value
    raise RuntimeError("cannot resolve materialized expert count from vLLM model config")


def _install() -> None:
    import numpy as np
    import torch
    from vllm.model_executor.layers.attention.attention import Attention
    from vllm.v1.core.sched.scheduler import Scheduler
    from vllm.v1.worker.gpu_model_runner import GPUModelRunner

    if getattr(Scheduler, "_phase7_step_trace_installed", False):
        return

    original_schedule = Scheduler.schedule
    original_update = Scheduler.update_from_output
    original_execute = GPUModelRunner.execute_model
    original_attention_forward = Attention.forward

    def schedule(self: Any, *args: Any, **kwargs: Any) -> Any:
        output = original_schedule(self, *args, **kwargs)
        step_id = int(getattr(self, "_phase7_forward_step_counter", 0))
        setattr(self, "_phase7_forward_step_counter", step_id + 1)
        setattr(output, "phase7_forward_step_id", step_id)
        return output

    def update_from_output(self: Any, scheduler_output: Any, model_runner_output: Any) -> Any:
        routed = getattr(model_runner_output, "routed_experts", None)
        step_id = getattr(scheduler_output, "phase7_forward_step_id", "UNAVAILABLE")
        scheduled = {
            str(request_id): int(count)
            for request_id, count in scheduler_output.num_scheduled_tokens.items()
        }
        request_order = [str(request_id) for request_id in getattr(model_runner_output, "req_ids", [])]
        if routed is not None:
            routing_data = np.asarray(routed.routing_data)
            slot_mapping = np.asarray(routed.slot_mapping)
            num_experts = _materialized_num_experts(self)
            offset = 0
            for request_id in request_order:
                count = scheduled[request_id]
                request_routing = routing_data[offset : offset + count]
                request_slots = slot_mapping[offset : offset + count]
                offset += count
                phase, prior_computed = _request_phase(scheduler_output, request_id)
                vectors = [
                    np.bincount(
                        request_routing[:, layer, :].reshape(-1),
                        minlength=num_experts,
                    )[:num_experts].astype(int).tolist()
                    for layer in range(request_routing.shape[1])
                ]
                expected_routes_per_layer = count * int(request_routing.shape[2])
                conservation = all(sum(vector) == expected_routes_per_layer for vector in vectors)
                correlation_key = f"phase7_forward_step_id={step_id}"
                _append_jsonl(
                    "scheduler_steps.jsonl",
                    {
                        "schema_version": "phase7-routing-step-v1",
                        "hook_revision": HOOK_REVISION,
                        "process_role": "scheduler",
                        "pid": os.getpid(),
                        "forward_step_id": step_id,
                        "scheduler_iteration": step_id,
                        "request_id": request_id,
                        "phase": phase,
                        "chunk": {
                            "scheduled_tokens": count,
                            "prior_computed_tokens": prior_computed,
                            "post_computed_tokens": None if prior_computed is None else prior_computed + count,
                        },
                        "batch": {
                            "active_sequences": len(scheduled),
                            "request_ids": list(scheduled),
                            "total_scheduled_tokens": int(scheduler_output.total_num_scheduled_tokens),
                        },
                        "routing_shape": [int(value) for value in request_routing.shape],
                        "materialized_num_experts": num_experts,
                        "slot_mapping_count": int(request_slots.size),
                        "slot_mapping_min": int(request_slots.min()) if request_slots.size else None,
                        "slot_mapping_max": int(request_slots.max()) if request_slots.size else None,
                        "expert_load_vectors_by_layer": vectors,
                        "expected_routes_per_layer": expected_routes_per_layer,
                        "route_conservation_status": "PASS" if conservation else "FAIL",
                        "kernel_correlation_key": correlation_key,
                        "kernel_correlation_id": "PENDING_PROFILER_RESOLUTION",
                        "wall_time_ns": time.time_ns(),
                        "monotonic_ns": time.monotonic_ns(),
                    },
                )
            if offset != int(routing_data.shape[0]):
                _append_jsonl(
                    "hook_errors.jsonl",
                    {
                        "schema_version": "phase7-routing-step-hook-error-v1",
                        "hook_revision": HOOK_REVISION,
                        "forward_step_id": step_id,
                        "error": "routing offset does not match routing_data rows",
                        "offset": offset,
                        "routing_rows": int(routing_data.shape[0]),
                    },
                )
        return original_update(self, scheduler_output, model_runner_output)

    def execute_model(self: Any, scheduler_output: Any, *args: Any, **kwargs: Any) -> Any:
        step_id = getattr(scheduler_output, "phase7_forward_step_id", "UNAVAILABLE")
        correlation_key = f"phase7_forward_step_id={step_id}"
        started = time.monotonic_ns()
        token = _CURRENT_FORWARD_STEP.set(step_id)
        try:
            with torch.profiler.record_function(correlation_key):
                output = original_execute(self, scheduler_output, *args, **kwargs)
        finally:
            _CURRENT_FORWARD_STEP.reset(token)
        _append_jsonl(
            "worker_annotations.jsonl",
            {
                "schema_version": "phase7-worker-step-annotation-v1",
                "hook_revision": HOOK_REVISION,
                "process_role": "worker",
                "pid": os.getpid(),
                "forward_step_id": step_id,
                "kernel_correlation_key": correlation_key,
                "started_monotonic_ns": started,
                "ended_monotonic_ns": time.monotonic_ns(),
            },
        )
        return output

    def attention_forward(self: Any, *args: Any, **kwargs: Any) -> Any:
        step_id = _CURRENT_FORWARD_STEP.get()
        layer_name = str(getattr(self, "layer_name", "UNAVAILABLE"))
        marker = f"phase7_attention_step={step_id};layer={layer_name}"
        with torch.profiler.record_function(marker):
            return original_attention_forward(self, *args, **kwargs)

    Scheduler.schedule = schedule
    Scheduler.update_from_output = update_from_output
    Scheduler._phase7_step_trace_installed = True
    GPUModelRunner.execute_model = execute_model
    GPUModelRunner._phase7_step_trace_installed = True
    Attention.forward = attention_forward
    Attention._phase7_step_trace_installed = True


if os.environ.get("PHASE7_ENABLE_STEP_TRACE") == "1":
    _install()
