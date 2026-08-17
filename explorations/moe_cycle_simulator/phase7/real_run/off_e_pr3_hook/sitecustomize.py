"""Opt-in OFF-E-PR3 capacity replay over a frozen measured routing trace."""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path


MODE = os.environ.get("OFF_E_PR3_HOOK_MODE")
LABEL = os.environ.get("OFF_E_PR3_CAPACITY_LABEL")
ROOT = Path(os.environ.get("OFF_E_PR3_TRACE_DIR", "/tmp/off-e-pr3-unset"))
ROUTING = Path(os.environ.get("OFF_E_PR3_ROUTING_NPY", "/tmp/off-e-pr3-routing-unset.npy"))
OBJECT_BYTES = 352_321_536
ROUTING_SHA256 = "0a9225ec4b302ea237bc21fe532fa1efb790905bbc5832e2ea5dab72b20e50d6"
CAPACITY_OBJECTS = {
    "025": 64,
    "050": 128,
    "075": 192,
    "080": 204,
    "085": 217,
    "090": 230,
    "095": 243,
    "099": 253,
    "0375": 96,
    "0625": 160,
    "0825": 211,
    "0875": 224,
    "0925": 236,
    "097": 248,
    "100": 256,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _routing_sequence() -> tuple[list[int], list[int]]:
    import numpy as np

    if _sha256(ROUTING) != ROUTING_SHA256:
        raise RuntimeError("OFF-E-PR3 frozen routing hash mismatch")
    array = np.load(ROUTING, allow_pickle=False)
    if list(array.shape) != [159, 32, 2] or str(array.dtype) != "uint8":
        raise RuntimeError(f"OFF-E-PR3 routing shape/dtype mismatch: {array.shape}/{array.dtype}")
    sequence = [
        int(layer * 8 + expert)
        for token in array
        for layer, pair in enumerate(token)
        for expert in pair
    ]
    if len(sequence) != 10_176 or set(sequence) != set(range(256)):
        raise RuntimeError("OFF-E-PR3 routing object coverage mismatch")
    return sequence, [int(value) for value in array.reshape(-1)]


def _compile_lru(sequence: list[int], capacity: int) -> dict:
    cache: OrderedDict[int, None] = OrderedDict()
    loads: list[dict] = []
    hit_count = 0
    eviction_count = 0
    for index, object_id in enumerate(sequence):
        if object_id in cache:
            hit_count += 1
            cache.move_to_end(object_id)
            continue
        evicted = None
        if len(cache) >= capacity:
            evicted, _ = cache.popitem(last=False)
            eviction_count += 1
        cache[object_id] = None
        loads.append(
            {
                "demand_index": index,
                "object_id": object_id,
                "evicted_object_id": evicted,
            }
        )
    return {
        "loads": loads,
        "load_count": len(loads),
        "hit_count": hit_count,
        "eviction_count": eviction_count,
        "terminal_resident_object_ids": list(cache),
    }


def _install() -> None:
    import torch
    from vllm.model_executor.layers.fused_moe.layer import FusedMoE

    if getattr(FusedMoE, "_off_e_pr3_installed", False):
        return
    if LABEL not in CAPACITY_OBJECTS:
        raise RuntimeError(f"invalid OFF-E-PR3 capacity label: {LABEL}")
    original = FusedMoE.forward
    state = {"done": False, "active": False, "calls": 0}

    def forward(self, hidden_states, router_logits, input_ids=None):
        state["calls"] += 1
        if state["active"] or state["done"] or MODE != "REPLAY":
            return original(self, hidden_states, router_logits, input_ids)
        state["active"] = True
        ROOT.mkdir(parents=True, exist_ok=True)
        try:
            sequence, routing_flat = _routing_sequence()
            capacity = CAPACITY_OBJECTS[LABEL]
            plan = _compile_lru(sequence, capacity)
            num_experts = int(router_logits.shape[-1])
            parameters = [
                (name, parameter)
                for name, parameter in self.named_parameters()
                if parameter.ndim > 0
                and int(parameter.shape[0]) == num_experts
                and parameter.dtype == torch.bfloat16
            ]
            object_bytes = sum(
                int(parameter[0].numel() * parameter.element_size())
                for _, parameter in parameters
            )
            if object_bytes != OBJECT_BYTES:
                raise RuntimeError(f"expert object bytes mismatch: {object_bytes}")

            torch.cuda.synchronize()
            setup_start = time.monotonic_ns()
            host_object = [
                (name, parameter[0].detach().to("cpu").pin_memory())
                for name, parameter in parameters
            ]
            torch.cuda.synchronize()
            setup_end = time.monotonic_ns()
            transfer_events = []
            first_h2d_start = None
            last_h2d_complete = setup_end
            total_cuda_elapsed_ms = 0.0

            if LABEL != "100":
                for ordinal, logical in enumerate(plan["loads"]):
                    start_event = torch.cuda.Event(enable_timing=True)
                    end_event = torch.cuda.Event(enable_timing=True)
                    decision = time.monotonic_ns()
                    start = time.monotonic_ns()
                    if first_h2d_start is None:
                        first_h2d_start = start
                    start_event.record()
                    with torch.no_grad():
                        for (name, target), (host_name, source) in zip(parameters, host_object):
                            if name != host_name:
                                raise RuntimeError("OFF-E-PR3 parameter order mismatch")
                            target[0].copy_(source, non_blocking=True)
                    end_event.record()
                    end_event.synchronize()
                    complete = time.monotonic_ns()
                    elapsed_ms = float(start_event.elapsed_time(end_event))
                    total_cuda_elapsed_ms += elapsed_ms
                    last_h2d_complete = complete
                    transfer_events.append(
                        {
                            "load_ordinal": ordinal,
                            "demand_index": logical["demand_index"],
                            "logical_object_id": logical["object_id"],
                            "logical_evicted_object_id": logical["evicted_object_id"],
                            "eviction_semantics": (
                                None
                                if logical["evicted_object_id"] is None
                                else "IMMUTABLE_DISCARD"
                            ),
                            "decision_monotonic_ns": decision,
                            "h2d_start_monotonic_ns": start,
                            "h2d_complete_monotonic_ns": complete,
                            "cuda_elapsed_ms": elapsed_ms,
                            "h2d_bytes": object_bytes,
                            "physical_service_proxy": "ACTUAL_LAYER0_EXPERT0_OBJECT",
                        }
                    )

            torch.cuda.synchronize()
            compute_start = time.monotonic_ns()
            output = original(self, hidden_states, router_logits, input_ids)
            torch.cuda.synchronize()
            compute_end = time.monotonic_ns()
            state["done"] = True
            h2d_bytes = len(transfer_events) * object_bytes
            doc = {
                "schema_version": "phase7-off-e-pr3-capacity-replay-v1",
                "canonical_experiment_id": f"OFF-E-PR3-CAP-{LABEL}",
                "fit_role": (
                    "CONTROL"
                    if LABEL == "100"
                    else "FIT"
                    if LABEL in {"025", "050", "075", "080", "085", "090", "095", "099"}
                    else "HELD_OUT"
                ),
                "capacity_label": LABEL,
                "capacity_rule": "floor(256 * requested_fraction) to whole immutable expert objects",
                "capacity_objects": capacity,
                "capacity_bytes": capacity * object_bytes,
                "catalog_objects": 256,
                "catalog_bytes": 256 * object_bytes,
                "expert_object_bytes": object_bytes,
                "logical_policy": "DETERMINISTIC_LRU_EMPTY_INITIAL_CACHE",
                "logical_demand_count": len(sequence),
                "logical_unique_object_count": len(set(sequence)),
                "routing_shape": [159, 32, 2],
                "routing_sha256": ROUTING_SHA256,
                "routing_flat_sha256": hashlib.sha256(bytes(routing_flat)).hexdigest(),
                "demand_load_count": plan["load_count"] if LABEL != "100" else 0,
                "hit_count": plan["hit_count"] if LABEL != "100" else len(sequence),
                "immutable_discard_count": plan["eviction_count"] if LABEL != "100" else 0,
                "h2d_bytes": h2d_bytes,
                "d2h_writeback_bytes": 0,
                "host_snapshot_setup_d2h_bytes": object_bytes,
                "host_snapshot_setup_excluded_from_demand_path": True,
                "setup_start_monotonic_ns": setup_start,
                "setup_end_monotonic_ns": setup_end,
                "first_h2d_start_monotonic_ns": first_h2d_start,
                "all_h2d_complete_monotonic_ns": last_h2d_complete,
                "actual_expert_compute_start_monotonic_ns": compute_start,
                "actual_expert_compute_end_monotonic_ns": compute_end,
                "actual_expert_compute": True,
                "dependency_gate": "PASS" if last_h2d_complete <= compute_start else "FAIL",
                "total_h2d_cuda_elapsed_ms": total_cuda_elapsed_ms,
                "transfer_events": transfer_events,
                "terminal_resident_object_ids": (
                    list(range(256))
                    if LABEL == "100"
                    else plan["terminal_resident_object_ids"]
                ),
                "physical_transfer_semantics": (
                    "No demand H2D: actual all-resident control"
                    if LABEL == "100"
                    else "Each logical miss issues one measured 352,321,536-byte H2D into the exact actual layer-0 expert-0 parameter object; logical object identity and LRU residency are trace replay state."
                ),
                "claim_boundary": "Compute-integrated GPU policy replay with actual object-sized H2D service and actual FusedMoE compute; not runtime-native expert residency and not per-object physical movement for all 256 logical objects.",
            }
            (ROOT / "capacity_replay.json").write_text(
                json.dumps(doc, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return output
        finally:
            state["active"] = False

    FusedMoE.forward = forward
    FusedMoE._off_e_pr3_installed = True


if MODE == "REPLAY":
    _install()
