"""Raw MoE router trace -> canonical expert-demand event stream (S1).

Work unit
---------
The chosen candidate mechanism is *expert-prefetch / residency scheduling* for
capacity-limited Mixture-of-Experts inference on CPU-GPU edge platforms. The
canonical work unit is an ``expert_demand`` event: at a given MoE layer of a
forward pass, a set of experts is required by the tokens routed to them. Before
those tokens can compute, each expert's weights must be resident in fast
memory (GPU VRAM on the discrete profile, or a residency region of shared
memory on the integrated profile).

Provenance rules
----------------
* Reads ``batch_expert_load_trace.csv`` (measured per-batch per-layer per-expert
  token assignment counts) and ``run_metadata.json``.
* Emits only quantities that are measured or logically derived from ordering.
* Physical costs (expert weight bytes, bandwidth, latency, compute time) are
  NOT invented here. ``bytes`` and ``duration`` are left ``null`` at S1 and are
  attached from a registered assumption/measurement at S2.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CANONICAL_SCHEMA_VERSION = 1
CONVERTER_VERSION = "canonical-moe-expert-demand/1.0.0"

# Deterministic stage ordering within a single forward pass.
STAGE_ORDER = {"encoder": 0, "decoder": 1}


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BatchExpertRow:
    batch_id: int
    stage: str
    layer_index: int
    router_module_name: str
    expert_id: int
    assigned_tokens: int
    total_tokens: int
    expert_capacity: int
    overflow_tokens: int


def load_batch_expert_load(path: str | Path) -> list[BatchExpertRow]:
    """Load batch_expert_load_trace.csv into typed rows (measured data)."""
    rows: list[BatchExpertRow] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(
                BatchExpertRow(
                    batch_id=int(r["batch_id"]),
                    stage=str(r["stage"]),
                    layer_index=int(r["router_layer_index"]),
                    router_module_name=str(r["router_module_name"]),
                    expert_id=int(r["expert_id"]),
                    assigned_tokens=int(r["assigned_tokens"]),
                    total_tokens=int(r["total_tokens"]),
                    expert_capacity=int(r["expert_capacity"]),
                    overflow_tokens=int(r["overflow_tokens"]),
                )
            )
    return rows


def _group_key(row: BatchExpertRow) -> tuple[int, int, int]:
    """Ordering key for a layer-group: (batch, stage_order, layer_index)."""
    return (row.batch_id, STAGE_ORDER.get(row.stage, 99), row.layer_index)


def to_canonical_events(
    rows: list[BatchExpertRow],
    meta: dict[str, Any],
    trace_id: str,
    source_info: dict[str, Any],
    include_zero_load: bool = False,
) -> list[dict[str, Any]]:
    """Convert measured rows into a deterministic canonical event stream.

    A logical clock is assigned per distinct layer-group in execution order.
    All experts demanded at the same layer-group share the same timestamp
    (they are needed simultaneously for that layer). Each layer-group's events
    depend on the previous layer-group's events for the same batch, forming a
    real sequential dependency DAG that a discrete-event simulator can consume.
    """
    num_experts = int(meta.get("num_experts") or meta.get("model_config_num_experts") or 0)

    # Stable deterministic ordering.
    ordered = sorted(rows, key=lambda r: (_group_key(r), r.expert_id))

    # Assign a monotonic layer-step per distinct group, and remember, per batch,
    # the event ids emitted at the previous group to build dependencies.
    events: list[dict[str, Any]] = []
    step_of_group: dict[tuple[int, int, int], int] = {}
    next_step = 0
    prev_group_events_by_batch: dict[int, list[str]] = {}
    cur_group: tuple[int, int, int] | None = None
    cur_group_event_ids: list[str] = []
    seq = 0

    def flush_group(g: tuple[int, int, int] | None, ids: list[str]) -> None:
        if g is not None:
            prev_group_events_by_batch[g[0]] = ids

    for row in ordered:
        if not include_zero_load and row.assigned_tokens <= 0:
            continue
        g = _group_key(row)
        if g != cur_group:
            # New layer-group: finalize dependency bookkeeping.
            flush_group(cur_group, cur_group_event_ids)
            cur_group = g
            cur_group_event_ids = []
            if g not in step_of_group:
                step_of_group[g] = next_step
                next_step += 1
        step = step_of_group[g]
        event_id = f"{trace_id}:e{seq:07d}"
        seq += 1
        deps = list(prev_group_events_by_batch.get(row.batch_id, []))
        ev = {
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "trace_id": trace_id,
            "event_id": event_id,
            "request_id": str(row.batch_id),
            "token_id": None,
            "event_type": "expert_demand",
            "timestamp": float(step),
            "duration": None,  # compute time attached at S2 from registered model
            "device": "gpu",
            "bytes": None,      # expert weight bytes attached at S2 (registered)
            "dependencies": deps,
            "attributes": {
                "stage": row.stage,
                "layer_index": row.layer_index,
                "layer_step": step,
                "router_module_name": row.router_module_name,
                "expert_id": row.expert_id,
                "assigned_tokens": row.assigned_tokens,
                "total_tokens": row.total_tokens,
                "expert_capacity": row.expert_capacity,
                "overflow_tokens": row.overflow_tokens,
                "num_experts": num_experts,
            },
            "source": source_info,
        }
        events.append(ev)
        cur_group_event_ids.append(event_id)
    flush_group(cur_group, cur_group_event_ids)
    return events


def build_source_info(raw_path: str | Path, meta: dict[str, Any]) -> dict[str, Any]:
    raw_path = Path(raw_path)
    return {
        "raw_file": str(raw_path),
        "raw_sha256": sha256_file(raw_path),
        "converter_version": CONVERTER_VERSION,
        "model_name": meta.get("model_name"),
        "dataset_name": meta.get("dataset_name"),
        "access_class": "measured",
    }


def write_jsonl(events: list[dict[str, Any]], out_path: str | Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev, separators=(",", ":"), sort_keys=True) + "\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# S1 characterization: prove the problem exists from measured demand only.
# ---------------------------------------------------------------------------

def _cov(values: list[float]) -> float | None:
    if not values:
        return None
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var) / mean


def characterize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute workload characterization metrics from canonical demand events.

    These are S1-level observations (measured/derived only): they describe the
    expert working set, load imbalance, overflow, and temporal locality that a
    residency/prefetch mechanism would target. No hardware costs are used.
    """
    if not events:
        return {"num_events": 0}

    # Group events by (batch, layer_step).
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for ev in events:
        b = ev["request_id"]
        s = ev["attributes"]["layer_step"]
        groups.setdefault((b, s), []).append(ev)

    num_experts = events[0]["attributes"]["num_experts"] or 0
    active_counts: list[int] = []          # distinct experts demanded per layer-group
    cov_per_group: list[float] = []        # load imbalance per layer-group
    total_assigned = 0
    total_overflow = 0

    # Temporal locality: for consecutive layer-groups within a batch, Jaccard
    # overlap of the active expert sets (relevant to residency reuse/prefetch).
    reuse_jaccard: list[float] = []
    per_batch_groups: dict[str, list[tuple[int, set[int]]]] = {}

    for (b, s), evs in groups.items():
        experts = {e["attributes"]["expert_id"] for e in evs}
        active_counts.append(len(experts))
        loads = [float(e["attributes"]["assigned_tokens"]) for e in evs]
        c = _cov(loads)
        if c is not None:
            cov_per_group.append(c)
        for e in evs:
            total_assigned += e["attributes"]["assigned_tokens"]
            total_overflow += e["attributes"]["overflow_tokens"]
        per_batch_groups.setdefault(b, []).append((s, experts))

    for b, glist in per_batch_groups.items():
        glist.sort(key=lambda x: x[0])
        for i in range(1, len(glist)):
            prev = glist[i - 1][1]
            cur = glist[i][1]
            union = prev | cur
            if union:
                reuse_jaccard.append(len(prev & cur) / len(union))

    def _mean(xs: list[float]) -> float | None:
        return (sum(xs) / len(xs)) if xs else None

    return {
        "num_events": len(events),
        "num_layer_groups": len(groups),
        "num_batches": len({ev["request_id"] for ev in events}),
        "num_experts": num_experts,
        "active_experts_per_group_mean": _mean([float(x) for x in active_counts]),
        "active_experts_per_group_max": max(active_counts) if active_counts else None,
        "active_experts_per_group_min": min(active_counts) if active_counts else None,
        "load_cov_per_group_mean": _mean(cov_per_group),
        "total_assigned_tokens": total_assigned,
        "total_overflow_tokens": total_overflow,
        "overflow_fraction": (total_overflow / total_assigned) if total_assigned else 0.0,
        "consecutive_group_expert_jaccard_mean": _mean(reuse_jaccard),
    }
