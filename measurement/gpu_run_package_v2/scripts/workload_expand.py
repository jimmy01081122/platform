#!/usr/bin/env python3
"""Expand canonical T0 fixtures or measured M0 routing into system events."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def expand(fixture_path: Path, routing_path: Path, output: Path) -> dict[str, Any]:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    if routing.get("ir_kind") != "moe-routing":
        raise ValueError("routing input must be moe-routing IR")
    profile = fixture["platform_profile"]
    model = fixture["model_profile"]
    decisions = routing["events"]
    if len(decisions) != len(fixture["routing"]):
        raise ValueError("routing decision count differs from fixture")

    operations: list[dict[str, Any]] = []

    def add(event_type: str, event_id: str, resource: str, duration: int,
            dependencies: list[str], **extra: Any) -> None:
        operations.append({
            "event_id": event_id,
            "event_type": event_type,
            "sequence": len(operations),
            "arrival_ns": 0,
            "duration_ns": duration,
            "resource": resource,
            "depends_on": dependencies,
            "queue_capacity": profile["queue_capacity"],
            "evidence": {
                "class": "fixture_ground_truth",
                "source_pass": "P2" if event_type in {"dispatch", "expert_compute"} else "P3",
                "synthetic": True,
            },
            **extra,
        })

    used_experts = sorted({expert for event in decisions for expert in event["experts"]})
    for expert in used_experts:
        add("expert_load", f"load-e{expert}", profile["dma_resource"],
            profile["dma_load_ns"], [], expert_id=expert, bytes=4096)

    compute_ids: list[str] = []
    last_compute: dict[int, str] = {}
    for decision in decisions:
        token_index = decision["token_index"]
        expected = fixture["routing"][token_index]
        if (decision["token_id"], decision["experts"], decision["weights"]) != (
            expected["token_id"], expected["experts"], expected["weights"]
        ):
            raise ValueError(f"routing mismatch at token {token_index}")
        for rank, expert in enumerate(decision["experts"]):
            dispatch_id = f"dispatch-t{token_index}-e{expert}-r{rank}"
            add("dispatch", dispatch_id, "fixture_dispatch", profile["dispatch_ns"],
                [f"load-e{expert}"], token_index=token_index, expert_id=expert, rank=rank)
            compute_id = f"compute-t{token_index}-e{expert}-r{rank}"
            dependencies = [dispatch_id]
            if expert in last_compute:
                dependencies.append(last_compute[expert])
            add("expert_compute", compute_id, f"fixture_expert_{expert}",
                model["expert_service_ns"][str(expert)], dependencies,
                token_index=token_index, expert_id=expert, rank=rank)
            last_compute[expert] = compute_id
            compute_ids.append(compute_id)

    add("combine", "combine-output", "fixture_combine", profile["combine_ns"],
        compute_ids, generated_tokens=fixture["expected_generated_tokens"])
    for expert in used_experts:
        add("expert_evict", f"evict-e{expert}", profile["dma_resource"], 0,
            ["combine-output"], expert_id=expert, bytes=4096)

    expanded = {
        "schema_version": "canonical-moe-ir-v1",
        "ir_kind": "system-events",
        "identity": routing["identity"],
        "provenance": {
            **routing["provenance"],
            "expander": "t0-workload-expander-v1",
            "model_profile_id": model["profile_id"],
            "platform_profile_id": profile["profile_id"],
        },
        "events": operations,
    }
    write_json(output, expanded)
    return expanded


def expand_m0(routing_path: Path, output: Path) -> dict[str, Any]:
    """Build benchmark-aware analytical events without inventing GPU timing."""
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    if routing.get("ir_kind") != "moe-routing":
        raise ValueError("routing input must be moe-routing IR")
    if routing.get("provenance", {}).get("converter") != (
        "m0-qwen2moe-native-p2-to-canonical-v1"
    ):
        raise ValueError("M0 expansion requires verified measured M0 routing IR")
    dimensions = routing["provenance"]["model_dimensions"]
    hardware = routing["provenance"]["hardware"]["cuda"]
    dtype_bytes = {"float32": 4, "float16": 2, "bfloat16": 2}.get(
        dimensions["dtype"]
    )
    if dtype_bytes is None:
        raise ValueError(f"unsupported dtype for size model: {dimensions['dtype']}")
    expert_bytes = (
        3 * dimensions["hidden_size"] * dimensions["expert_intermediate_size"]
        * dtype_bytes
    )
    resident_capacity = min(
        dimensions["num_layers"] * dimensions["num_experts"],
        hardware["total_memory_bytes"] // expert_bytes,
    )
    groups: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for route in routing["events"]:
        key = (route["request_id"], route["layer_index"], route["call_index"])
        groups.setdefault(key, []).append(route)
    operations: list[dict[str, Any]] = []

    def append(event: dict[str, Any]) -> None:
        event["sequence"] = len(operations)
        operations.append(event)

    for (request_id, layer, call_index), routes in groups.items():
        dispatch_counts = {
            expert: sum(
                expert in route["selected_experts"] for route in routes
            )
            for expert in range(dimensions["num_experts"])
        }
        queue_capacity = 1
        append({
            "event_id": f"queue-{len(operations):06d}",
            "event_type": "queue_demand",
            "request_id": request_id,
            "benchmark_id": routes[0]["benchmark_id"],
            "suite_class": routes[0]["suite_class"],
            "layer_index": layer,
            "call_index": call_index,
            "phase": routes[0]["phase"],
            "dispatch_counts": dispatch_counts,
            "queue_capacity_assumption": queue_capacity,
            "estimated_queue_excess_assignments": sum(
                max(0, count - queue_capacity)
                for count in dispatch_counts.values()
            ),
            "service_time": {
                "value": None,
                "unit": None,
                "evidence": "unmeasured",
                "basis": "P2 contains routing, not queue timestamps or service time",
            },
            "evidence": {
                "class": "estimate",
                "source_pass": "P2",
                "synthetic": True,
                "basis": "dispatch counts from measured routes; queue capacity is assumption",
            },
        })
        for route in routes:
            for rank, (expert, score) in enumerate(zip(
                route["selected_experts"], route["selected_scores"]
            )):
                append({
                    "event_id": f"access-{len(operations):06d}",
                    "event_type": "expert_access",
                    "request_id": request_id,
                    "benchmark_id": route["benchmark_id"],
                    "suite_class": route["suite_class"],
                    "sample_id": route["sample_id"],
                    "phase": route["phase"],
                    "call_index": call_index,
                    "layer_index": layer,
                    "token_index": route["token_index"],
                    "token_id": route["token_id"],
                    "expert_id": expert,
                    "rank": rank,
                    "score": score,
                    "expert_bytes": {
                        "value": expert_bytes,
                        "unit": "bytes",
                        "evidence": "estimate",
                        "basis": (
                            "3 dense expert matrices * hidden_size * "
                            "intermediate_size * dtype_bytes"
                        ),
                    },
                    "service_time": {
                        "value": None,
                        "unit": None,
                        "evidence": "unmeasured",
                        "basis": "no per-expert timing exists in M0 P2/P3",
                    },
                    "evidence": {
                        "class": "measured_gpu_route",
                        "source_pass": "P2",
                        "synthetic": False,
                    },
                })
    expanded = {
        "schema_version": "canonical-moe-ir-v1",
        "ir_kind": "system-events",
        "identity": routing["identity"],
        "provenance": {
            **routing["provenance"],
            "expander": "m0-benchmark-workload-expander-v1",
            "platform_profile": {
                "profile_id": "rtx3050-6gb-from-m0-provenance",
                "exact_name": hardware["name"],
                "compute_capability": hardware["capability"],
                "vram_bytes": hardware["total_memory_bytes"],
                "evidence": "measured_provenance",
            },
            "residency_model": {
                "policy": "cold-start-lru",
                "capacity_layer_experts": resident_capacity,
                "initial_residency": "empty",
                "evidence": "estimate",
                "limitation": (
                    "P3 has allocator snapshots but no per-expert residency or "
                    "transfer trace; results are analytical, not observed transfers"
                ),
            },
            "timing_model": {
                "service_time": None,
                "evidence": "unmeasured",
                "latency_prediction_allowed": False,
            },
        },
        "events": operations,
    }
    write_json(output, expanded)
    return expanded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--routing", type=Path)
    parser.add_argument("--m0-routing", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.m0_routing:
        if args.fixture or args.routing:
            parser.error("--m0-routing cannot be combined with T0 inputs")
        expand_m0(args.m0_routing, args.output)
    else:
        if not args.fixture or not args.routing:
            parser.error("T0 expansion requires --fixture and --routing")
        expand(args.fixture, args.routing, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
