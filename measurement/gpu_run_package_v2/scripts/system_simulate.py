#!/usr/bin/env python3
"""Run a deterministic resource/queue simulator over canonical T0 system IR."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _entropy(counts: Counter[int]) -> float:
    total = sum(counts.values())
    if not total:
        return 0.0
    return -sum(
        (count / total) * math.log2(count / total)
        for count in counts.values() if count
    )


def _simulate_m0(ir: dict[str, Any], ir_path: Path, output: Path) -> dict[str, Any]:
    accesses = [event for event in ir["events"] if event["event_type"] == "expert_access"]
    queues = [event for event in ir["events"] if event["event_type"] == "queue_demand"]
    popularity: Counter[int] = Counter(event["expert_id"] for event in accesses)
    by_benchmark: dict[str, Counter[int]] = defaultdict(Counter)
    by_layer: dict[int, Counter[int]] = defaultdict(Counter)
    for event in accesses:
        by_benchmark[event["benchmark_id"]][event["expert_id"]] += 1
        by_layer[event["layer_index"]][event["expert_id"]] += 1

    lru_stack: list[tuple[int, int]] = []
    finite_reuse: list[int] = []
    cold_reuse = 0
    capacity = ir["provenance"]["residency_model"]["capacity_layer_experts"]
    resident: list[tuple[int, int]] = []
    hits = 0
    misses = 0
    transfer_bytes = 0
    for event in accesses:
        key = (event["layer_index"], event["expert_id"])
        if key in lru_stack:
            distance = lru_stack.index(key)
            finite_reuse.append(distance)
            lru_stack.pop(distance)
        else:
            cold_reuse += 1
        lru_stack.insert(0, key)
        if key in resident:
            hits += 1
            resident.remove(key)
        else:
            misses += 1
            transfer_bytes += event["expert_bytes"]["value"]
            if len(resident) >= capacity:
                resident.pop()
        resident.insert(0, key)

    queue_pressure = [
        event for event in queues if event["estimated_queue_excess_assignments"] > 0
    ]
    result = {
        "schema_version": "m0-system-analysis-result-v1",
        "identity": ir["identity"],
        "provenance": {
            "simulator": "m0-route-residency-analyzer-v1",
            "input_ir_name": ir_path.name,
            "input_ir_sha256": hashlib.sha256(ir_path.read_bytes()).hexdigest(),
            "hardware_latency_claim": False,
            "analysis_scope": "route-derived analytical model",
        },
        "route_statistics": {
            "evidence": "measured_gpu_route",
            "assignment_count": len(accesses),
            "expert_popularity": {
                str(expert): count for expert, count in sorted(popularity.items())
            },
            "expert_popularity_by_benchmark": {
                benchmark: {
                    str(expert): count for expert, count in sorted(counts.items())
                }
                for benchmark, counts in sorted(by_benchmark.items())
            },
            "expert_popularity_by_layer": {
                str(layer): {
                    str(expert): count for expert, count in sorted(counts.items())
                }
                for layer, counts in sorted(by_layer.items())
            },
            "expert_entropy_bits": _entropy(popularity),
            "expert_entropy_normalized": (
                _entropy(popularity) / math.log2(len(popularity))
                if len(popularity) > 1 else 0.0
            ),
            "reuse_distance": {
                "definition": "LRU stack distance over (layer, expert) accesses",
                "cold_accesses": cold_reuse,
                "finite_count": len(finite_reuse),
                "mean": (
                    sum(finite_reuse) / len(finite_reuse) if finite_reuse else None
                ),
                "max": max(finite_reuse) if finite_reuse else None,
                "histogram": {
                    str(distance): count
                    for distance, count in sorted(Counter(finite_reuse).items())
                },
            },
        },
        "residency_estimate": {
            "evidence": "estimate",
            "policy": ir["provenance"]["residency_model"],
            "hits": hits,
            "misses": misses,
            "hit_rate": hits / len(accesses) if accesses else None,
            "transfer_bytes": transfer_bytes,
            "transfer_bytes_claim": "analytical cold-miss bytes, not observed PCIe traffic",
        },
        "queue_estimate": {
            "evidence": "estimate",
            "queue_demand_events": len(queues),
            "queue_pressure_events": len(queue_pressure),
            "estimated_excess_assignments": sum(
                event["estimated_queue_excess_assignments"] for event in queues
            ),
            "service_time_available": False,
            "queue_wait_time": None,
        },
        "latency": {
            "predicted": None,
            "unit": None,
            "evidence": "unavailable",
            "reason": "no measured or calibrated per-expert service-time model",
            "must_not_compare_to_p0_latency": True,
        },
        "limitations": [
            "P2 provides selected experts and scores, not kernel service time.",
            "P3 allocator snapshots do not identify per-expert residency or DMA.",
            "Residency, transfer bytes, and queue pressure are explicitly estimates.",
            "The M0 random tiny model is pipeline evidence, not performance evidence.",
        ],
    }
    write_json(output, result)
    return result


def simulate(ir_path: Path, output: Path) -> dict[str, Any]:
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    if ir.get("schema_version") != "canonical-moe-ir-v1" or ir.get("ir_kind") != "system-events":
        raise ValueError("simulator requires canonical system-events IR")
    if ir.get("provenance", {}).get("expander") == "m0-benchmark-workload-expander-v1":
        return _simulate_m0(ir, ir_path, output)
    events = ir.get("events", [])
    ids: set[str] = set()
    for index, event in enumerate(events):
        event_id = event.get("event_id")
        if not event_id or event_id in ids:
            raise ValueError(f"duplicate or empty event_id at sequence {index}")
        if event.get("sequence") != index:
            raise ValueError(f"event sequence mismatch at {event_id}")
        unknown = [item for item in event.get("depends_on", []) if item not in ids]
        if unknown:
            raise ValueError(f"ordering violation: {event_id} depends on future/unknown {unknown}")
        ids.add(event_id)

    end_by_id: dict[str, int] = {}
    resource_end: dict[str, int] = defaultdict(int)
    prior_by_resource: dict[str, list[tuple[int, int]]] = defaultdict(list)
    waits: dict[str, int] = defaultdict(int)
    backpressure: dict[str, int] = defaultdict(int)
    max_depth: dict[str, int] = defaultdict(int)
    schedule: list[dict[str, Any]] = []
    residency_points: list[tuple[int, int]] = []

    for event in events:
        dependencies = event.get("depends_on", [])
        ready = max(
            [int(event.get("arrival_ns", 0))]
            + [end_by_id[item] for item in dependencies]
        )
        resource = event["resource"]
        queued_ahead = sum(1 for prior_ready, prior_end in prior_by_resource[resource]
                           if prior_ready <= ready < prior_end)
        max_depth[resource] = max(max_depth[resource], queued_ahead)
        start = max(ready, resource_end[resource])
        wait = start - ready
        end = start + int(event["duration_ns"])
        if wait:
            waits[resource] += wait
            backpressure[resource] += 1
        resource_end[resource] = end
        prior_by_resource[resource].append((ready, end))
        end_by_id[event["event_id"]] = end
        scheduled = {
            **event,
            "ready_ns": ready,
            "start_ns": start,
            "end_ns": end,
            "queue_wait_ns": wait,
            "queue_depth_on_arrival": queued_ahead,
        }
        schedule.append(scheduled)
        if event["event_type"] == "expert_load":
            residency_points.append((end, 1))
        elif event["event_type"] == "expert_evict":
            residency_points.append((start, -1))

    resident = 0
    high_water = 0
    for _, delta in sorted(residency_points, key=lambda item: (item[0], -item[1])):
        resident += delta
        high_water = max(high_water, resident)
        if resident < 0:
            raise ValueError("residency ordering violation: evict before load")
    if resident != 0:
        raise ValueError("residency leak: not all experts were evicted")
    combine = next((event for event in schedule if event["event_type"] == "combine"), None)
    if combine is None:
        raise ValueError("missing combine event")
    dma_resource = next(
        event["resource"] for event in events if event["event_type"] == "expert_load"
    )
    result = {
        "schema_version": "t0-system-simulation-result-v1",
        "identity": ir["identity"],
        "provenance": {
            "simulator": "deterministic-t0-resource-simulator-v1",
            "input_ir_name": ir_path.name,
            "input_ir_sha256": hashlib.sha256(ir_path.read_bytes()).hexdigest(),
            "measurement_claim": False,
            "timing_semantics": "synthetic fixture nanoseconds",
        },
        "summary": {
            "latency_ns": combine["end_ns"],
            "total_queue_wait_ns": sum(waits.values()),
            "backpressure_events": sum(backpressure.values()),
            "dma_wait_ns": waits[dma_resource],
            "dma_backpressure_events": backpressure[dma_resource],
            "max_dma_queue_depth": max_depth[dma_resource],
            "residency_high_water_experts": high_water,
            "routing_assignments": sum(
                1 for event in events if event["event_type"] == "expert_compute"
            ),
            "generated_tokens": combine["generated_tokens"],
        },
        "per_resource": {
            resource: {
                "queue_wait_ns": waits[resource],
                "backpressure_events": backpressure[resource],
                "max_queue_depth": max_depth[resource],
            }
            for resource in sorted(resource_end)
        },
        "schedule": schedule,
    }
    write_json(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    simulate(args.ir, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
