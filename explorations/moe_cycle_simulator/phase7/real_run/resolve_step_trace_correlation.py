#!/usr/bin/env python3
"""Resolve Phase-7 scheduler-step records against a vLLM worker trace.

The raw scheduler and profiler files remain unchanged.  This tool emits a
derived, auditable join plus a correlation acceptance-gate report.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "phase7-step-kernel-correlation-v1"
TOOL_REVISION = "resolve-step-trace-correlation-v5"
STEP_RE = re.compile(r"^phase7_forward_step_id=(\d+)$")
ATTENTION_RE = re.compile(r"^phase7_attention_step=(\d+);layer=(.+)$")
LAYER_INDEX_RE = re.compile(r"(?:^|\.)layers\.(\d+)(?:\.|$)")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_trace(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload.get("traceEvents"), list):
        raise ValueError(f"traceEvents is not a list: {path}")
    return payload


def step_id(event: dict[str, Any]) -> int | None:
    match = STEP_RE.match(str(event.get("name", "")))
    return int(match.group(1)) if match else None


def attention_identity(event: dict[str, Any]) -> tuple[int, str] | None:
    match = ATTENTION_RE.match(str(event.get("name", "")))
    return (int(match.group(1)), match.group(2)) if match else None


def event_end_us(event: dict[str, Any]) -> float:
    return float(event.get("ts", 0.0)) + float(event.get("dur", 0.0))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    scheduler_path = run_dir / "routing_steps" / "scheduler_steps.jsonl"
    worker_path = run_dir / "routing_steps" / "worker_annotations.jsonl"
    trace_paths = sorted((run_dir / "profiler" / "worker").glob("*.pt.trace.json*"))
    if not scheduler_path.is_file() or not worker_path.is_file() or not trace_paths:
        raise SystemExit("missing scheduler, worker annotation, or profiler trace input")

    scheduler_rows = read_jsonl(scheduler_path)
    worker_rows = read_jsonl(worker_path)
    worker_by_id = {int(row["forward_step_id"]): row for row in worker_rows}

    all_events: list[dict[str, Any]] = []
    for trace_path in trace_paths:
        all_events.extend(load_trace(trace_path)["traceEvents"])

    cpu_annotations: dict[int, dict[str, Any]] = {}
    gpu_annotations: dict[int, dict[str, Any]] = {}
    for event in all_events:
        identifier = step_id(event)
        if identifier is None:
            continue
        if event.get("cat") == "user_annotation":
            cpu_annotations[identifier] = event
        elif event.get("cat") == "gpu_user_annotation":
            gpu_annotations[identifier] = event

    kernel_events = [event for event in all_events if event.get("cat") == "kernel"]
    attention_cpu: dict[tuple[int, str], dict[str, Any]] = {}
    attention_gpu: dict[tuple[int, str], dict[str, Any]] = {}
    for event in all_events:
        identity = attention_identity(event)
        if identity is None:
            continue
        if event.get("cat") == "user_annotation":
            attention_cpu[identity] = event
        elif event.get("cat") == "gpu_user_annotation":
            attention_gpu[identity] = event
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for scheduler in scheduler_rows:
        identifier = int(scheduler["forward_step_id"])
        cpu = cpu_annotations.get(identifier)
        gpu = gpu_annotations.get(identifier)
        worker = worker_by_id.get(identifier)
        vectors = scheduler.get("expert_load_vectors_by_layer")
        expected = scheduler.get("expected_routes_per_layer")
        routing_shape = scheduler.get("routing_shape")
        materialized_num_experts = scheduler.get("materialized_num_experts")
        materialized_expert_count_ok = (
            isinstance(materialized_num_experts, int)
            and not isinstance(materialized_num_experts, bool)
            and materialized_num_experts > 0
        )
        expected_layer_count = (
            int(routing_shape[1])
            if isinstance(routing_shape, list) and len(routing_shape) == 3
            else None
        )

        vector_shape_ok = (
            materialized_expert_count_ok
            and expected_layer_count is not None
            and isinstance(vectors, list)
            and len(vectors) == expected_layer_count
            and all(
                isinstance(vector, list) and len(vector) == materialized_num_experts
                for vector in vectors
            )
        )
        conservation_ok = bool(
            vector_shape_ok
            and isinstance(expected, int)
            and all(sum(vector) == expected for vector in vectors)
            and scheduler.get("route_conservation_status") == "PASS"
        )

        contained_kernels: list[dict[str, Any]] = []
        if gpu is not None:
            start_us = float(gpu.get("ts", 0.0))
            end_us = event_end_us(gpu)
            contained_kernels = [
                event
                for event in kernel_events
                if float(event.get("ts", -1.0)) >= start_us
                and event_end_us(event) <= end_us + 1e-6
            ]

        cpu_external_id = (cpu or {}).get("args", {}).get("External id")
        gpu_external_id = (gpu or {}).get("args", {}).get("External id")
        annotation_id_match = (
            cpu_external_id is not None
            and gpu_external_id is not None
            and cpu_external_id == gpu_external_id
        )
        kernel_correlation_ids = sorted(
            {
                int(event["args"]["correlation"])
                for event in contained_kernels
                if event.get("args", {}).get("correlation") is not None
            }
        )
        kernel_external_ids = sorted(
            {
                int(event["args"]["External id"])
                for event in contained_kernels
                if event.get("args", {}).get("External id") is not None
            }
        )
        fused_moe_kernel_count = sum(
            1
            for event in contained_kernels
            if any(token in str(event.get("name", "")).lower() for token in ("moe", "gemm", "cutlass"))
        )

        attention_layers: list[dict[str, Any]] = []
        attention_identities = sorted(
            (identity for identity in attention_cpu if identity[0] == identifier),
            key=lambda identity: identity[1],
        )
        for identity in attention_identities:
            _, layer_name = identity
            layer_match = LAYER_INDEX_RE.search(layer_name)
            layer_index = int(layer_match.group(1)) if layer_match else None
            attention_cpu_event = attention_cpu[identity]
            attention_gpu_event = attention_gpu.get(identity)
            attention_kernels: list[dict[str, Any]] = []
            if attention_gpu_event is not None:
                attention_start_us = float(attention_gpu_event.get("ts", 0.0))
                attention_end_us = event_end_us(attention_gpu_event)
                attention_kernels = [
                    event
                    for event in kernel_events
                    if float(event.get("ts", -1.0)) >= attention_start_us
                    and event_end_us(event) <= attention_end_us + 1e-6
                ]
            attention_cpu_external = attention_cpu_event.get("args", {}).get("External id")
            attention_gpu_external = (attention_gpu_event or {}).get("args", {}).get("External id")
            attention_layers.append(
                {
                    "layer_index": layer_index,
                    "layer_name": layer_name,
                    "annotation_correlation_id": attention_gpu_external,
                    "annotation_external_id_match": (
                        attention_cpu_external is not None
                        and attention_cpu_external == attention_gpu_external
                    ),
                    "gpu_duration_ns": (
                        float(attention_gpu_event.get("dur", 0.0)) * 1000.0
                        if attention_gpu_event is not None
                        else None
                    ),
                    "kernel_count": len(attention_kernels),
                    "kernel_launch_correlation_ids": sorted(
                        {
                            int(event["args"]["correlation"])
                            for event in attention_kernels
                            if event.get("args", {}).get("correlation") is not None
                        }
                    ),
                    "streams": sorted(
                        {
                            int(event["args"]["stream"])
                            for event in attention_kernels
                            if event.get("args", {}).get("stream") is not None
                        }
                    ),
                    "kernel_names": sorted({str(event.get("name", "")) for event in attention_kernels}),
                }
            )

        attention_layer_indices = [
            layer["layer_index"] for layer in attention_layers if layer["layer_index"] is not None
        ]
        attention_layers_complete = bool(
            expected_layer_count is not None
            and len(attention_layers) == expected_layer_count
            and sorted(attention_layer_indices) == list(range(expected_layer_count))
            and all(layer["annotation_external_id_match"] for layer in attention_layers)
            and all(layer["kernel_count"] > 0 for layer in attention_layers)
            and all(layer["kernel_launch_correlation_ids"] for layer in attention_layers)
        )

        checks = {
            "worker_annotation_present": worker is not None,
            "cpu_annotation_present": cpu is not None,
            "gpu_annotation_present": gpu is not None,
            "annotation_external_id_match": annotation_id_match,
            "kernel_events_present": bool(contained_kernels),
            "kernel_correlation_ids_present": bool(kernel_correlation_ids),
            "materialized_expert_count_present": materialized_expert_count_ok,
            "expert_vector_shape_matches_materialized_config": vector_shape_ok,
            "expert_route_conservation": conservation_ok,
            "vllm_attention_layers_correlated": attention_layers_complete,
        }
        failed_checks = [name for name, passed in checks.items() if not passed]
        if failed_checks:
            failures.append({"forward_step_id": identifier, "failed_checks": failed_checks})

        records.append(
            {
                "schema_version": SCHEMA_VERSION,
                "forward_step_id": identifier,
                "scheduler_iteration": scheduler.get("scheduler_iteration"),
                "phase": scheduler.get("phase"),
                "chunk": scheduler.get("chunk"),
                "batch": scheduler.get("batch"),
                "routing_shape": scheduler.get("routing_shape"),
                "materialized_num_experts": materialized_num_experts,
                "expert_load_vectors_by_layer": vectors,
                "kernel_correlation_key": scheduler.get("kernel_correlation_key"),
                "kernel_correlation_id": gpu_external_id,
                "kernel_launch_correlation_ids": kernel_correlation_ids,
                "kernel_external_ids": kernel_external_ids,
                "kernel_count": len(contained_kernels),
                "fused_moe_kernel_count": fused_moe_kernel_count,
                "vllm_attention_scope": (
                    "vllm.model_executor.layers.attention.Attention.forward; "
                    "actual KV-cache update plus configured fused attention backend"
                ),
                "attention_layer_correlations": attention_layers,
                "checks": checks,
                "status": "PASS" if not failed_checks else "FAIL",
            }
        )

    scheduler_ids = {int(row["forward_step_id"]) for row in scheduler_rows}
    extra_worker_ids = sorted(set(worker_by_id) - scheduler_ids)
    ordered_ids = [int(row["forward_step_id"]) for row in scheduler_rows]
    ordered_iterations = [int(row["scheduler_iteration"]) for row in scheduler_rows]
    phase_rank = {"prefill": 0, "chunked-prefill": 1, "decode": 2}
    phases = [str(row.get("phase")) for row in scheduler_rows]
    chunk_continuity_ok = all(
        index == 0
        or int(scheduler_rows[index]["chunk"]["prior_computed_tokens"])
        == int(scheduler_rows[index - 1]["chunk"]["post_computed_tokens"])
        for index in range(len(scheduler_rows))
    )
    annotation_ids = [row["kernel_correlation_id"] for row in records]
    all_launch_correlations = [
        correlation
        for row in records
        for correlation in row["kernel_launch_correlation_ids"]
    ]
    global_checks = {
        "forward_step_ids_contiguous": ordered_ids == list(range(len(ordered_ids))),
        "scheduler_iterations_contiguous": ordered_iterations == list(range(len(ordered_iterations))),
        "phase_order_monotonic": all(
            phase in phase_rank for phase in phases
        ) and all(
            phase_rank[phases[index]] <= phase_rank[phases[index + 1]]
            for index in range(len(phases) - 1)
        ),
        "chunk_token_continuity": chunk_continuity_ok,
        "annotation_correlation_ids_unique": len(annotation_ids) == len(set(annotation_ids)),
        "kernel_launch_correlations_disjoint_across_steps": (
            len(all_launch_correlations) == len(set(all_launch_correlations))
        ),
    }
    failed_global_checks = [name for name, passed in global_checks.items() if not passed]
    if failed_global_checks:
        failures.append({"scope": "global", "failed_checks": failed_global_checks})

    warnings: list[dict[str, Any]] = []
    if extra_worker_ids:
        warnings.append(
            {
                "name": "worker_annotations_without_scheduler_routing",
                "observed": extra_worker_ids,
                "interpretation": (
                    "worker execute_model annotations outside routed scheduler outputs are preserved "
                    "but excluded from the routing-correlation acceptance scope"
                ),
            }
        )
    acceptance = {
        "schema_version": SCHEMA_VERSION,
        "tool_revision": TOOL_REVISION,
        "status": "PASS" if not failures else "FAIL",
        "acceptance_scope": "real vLLM worker fused-path scheduler steps with routed-expert output",
        "raw_unchanged": True,
        "scheduler_step_count": len(scheduler_rows),
        "correlated_scheduler_step_count": sum(row["status"] == "PASS" for row in records),
        "global_checks": global_checks,
        "extra_worker_annotation_ids_without_scheduler_routing": extra_worker_ids,
        "failure_count": len(failures),
        "failures": failures,
        "warning_count": len(warnings),
        "warnings": warnings,
        "trace_files": [str(path.relative_to(run_dir)) for path in trace_paths],
    }

    records_path = output_dir / "routing_step_kernel_correlation.jsonl"
    records_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
        encoding="utf-8",
    )
    acceptance_path = output_dir / "correlation_acceptance_gate.json"
    acceptance_path.write_text(json.dumps(acceptance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(acceptance, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
