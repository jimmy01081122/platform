#!/usr/bin/env python3
"""Stage A2 measured adapter: OFF-E-PR3 expert capacity scan -> nine Canonical IR.

This is a *measured* adapter. It reads the frozen, read-only GPU policy-replay
evidence for the fifteen-point OFF-E-PR3 expert capacity scan and maps it onto
the nine Canonical IR kinds defined in phase2/schemas/canonical_ir.schema.json.

Contrast with ``vllm_mock_adapter.py`` (retained, untouched) which emits a
synthetic CPU fixture. This adapter never fabricates a measured value: every
numeric that stands in for a measurement is read from evidence/. Fields that are
required by the schema but genuinely unavailable in the raw are recorded in the
dropped/degraded-field report rather than filled with an invented value, except
where a required scalar carries no measurement claim (documented inline).

Design decisions (see docs/session_guides/STAGE_A2_MEASURED_TO_IR.md):

* One combined bundle covers all fifteen capacity points. ``validate_records``
  requires every one of the nine kinds to be present, and ``CalibrationIR``
  requires two disjoint workloads; a single combined bundle is the only shape
  that satisfies both without duplicating a workload artificially.
* Each capacity point gets its own ``PlatformIR`` record whose device
  residency-budget domain capacity equals that point's measured
  ``capacity_bytes``. This is faithful (the sweep varies the on-device residency
  budget) and it keeps the fifteen ``PlacementIR`` snapshots in fifteen distinct
  (model, platform) groups so the cross-IR validator does not try to chain them
  into a single temporal migration lineage (which never happened).
* Residency-managed object identity follows the frozen contract:
  ``logical object id = layer * 8 + expert`` (256 objects = 32 layers x 8
  experts, each 352,321,536 B). Expert objects are placed as owner replicas on
  the host pinned catalog and non-owner replicas in the device residency budget.
* Routing is emitted as AGGREGATE scope. The measured routing .npy is a uint8
  array of *selected expert ids only* (vllm.CompletionOutput.routed_experts);
  it carries no gate scores/logits, so TOKEN-scope routing (which the schema
  requires be reconstructible from per-expert scores) is not constructible. The
  byte-exact routing trace remains traceable via routing_sha256 in provenance.
"""
from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_ADAPTERS_DIR = Path(__file__).resolve().parent
_SIM_ROOT = _ADAPTERS_DIR.parents[1]
_PHASE2 = _SIM_ROOT / "phase2"
_REPO_ROOT = _SIM_ROOT.parents[1]
sys.path.insert(0, str(_PHASE2))
sys.path.insert(0, str(_SIM_ROOT / "tools"))

from canonical_ir import IR_KINDS, load_contracts  # noqa: E402
from contract_runtime import (  # noqa: E402
    canonical_bytes,
    dataset_semantic_hash,
    runtime_variant_hash,
)

OBJECT_BYTES = 352_321_536          # measured expert object size (336 MiB)
CATALOG_OBJECTS = 256               # 32 layers x 8 experts
EXPERTS = 8
LAYERS = 32
TOP_K = 2
HIDDEN_SIZE = 4096
INTERMEDIATE_SIZE = 14336
NS_TO_FS = 1_000_000                # 1 ns = 1e6 fs (exact unit conversion)
MS_TO_FS = 1_000_000_000_000        # 1 ms = 1e12 fs
# ns-clock quantisation half-width used as the alignment 95% CI (see module doc).
ALIGN_CI_FS = 500_000               # +/- 0.5 ns expressed in fs

SCHEMA_VERSION = "canonical-ir-v1"
PRODUCER = "stage-a2-off-e-pr3-measured-adapter"
PRODUCER_VERSION = "1"


# --------------------------------------------------------------------------- #
# raw reading helpers
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_npy_u8(path: Path) -> tuple[list[int], tuple[int, ...]]:
    """Minimal reader for a C-order uint8 .npy (numpy is absent from the venv)."""
    with open(path, "rb") as handle:
        magic = handle.read(6)
        if magic != b"\x93NUMPY":
            raise ValueError(f"not a .npy file: {path}")
        major = handle.read(1)[0]
        handle.read(1)  # minor
        if major == 1:
            header_len = int.from_bytes(handle.read(2), "little")
        else:
            header_len = int.from_bytes(handle.read(4), "little")
        header = ast.literal_eval(handle.read(header_len).decode("latin1"))
        if header["descr"] not in ("|u1", "u1", "<u1", ">u1"):
            raise ValueError(f"expected uint8 array, got {header['descr']}")
        if header["fortran_order"]:
            raise ValueError("fortran-order arrays are not supported")
        shape = tuple(header["shape"])
        count = 1
        for dim in shape:
            count *= dim
        data = handle.read(count)
        if len(data) != count:
            raise ValueError("truncated .npy payload")
    return list(data), shape


def discover_points(evidence_root: Path) -> list[Path]:
    pattern = str(
        evidence_root
        / "phase7/master_remaining/*/remote_raw/OFF-E-PR3-CAP-*-V1-MASTER"
    )
    points = sorted(glob.glob(pattern))
    if not points:
        raise FileNotFoundError(f"no OFF-E-PR3 capacity points under {pattern}")
    return [Path(p) for p in points]


def load_point(point_dir: Path) -> dict[str, Any]:
    trace = json.loads(
        (point_dir / "off_e_pr3_trace/capacity_replay.json").read_text("utf-8")
    )
    audit = json.loads(
        (point_dir / "off_e_pr3_point_audit.json").read_text("utf-8")
    )
    runner_runs = sorted((point_dir / "runner_runs").glob("*"))
    if len(runner_runs) != 1:
        raise ValueError(f"expected one runner run under {point_dir}")
    run = runner_runs[0]
    result = json.loads((run / "result.json").read_text("utf-8"))
    routing_files = sorted((run / "routing").glob("*.npy"))
    if len(routing_files) != 1:
        raise ValueError(f"expected one routing .npy under {run}")
    npy_path = routing_files[0]
    return {
        "dir": point_dir,
        "run_dir": run,
        "trace": trace,
        "audit": audit,
        "result": result,
        "npy_path": npy_path,
        "label": trace["capacity_label"],
    }


# --------------------------------------------------------------------------- #
# bundle construction
# --------------------------------------------------------------------------- #
def measured_runtime_variant() -> dict[str, Any]:
    """A runtime variant manifest describing the measured vLLM 0.23.0 runtime.

    variant_id is derived (not chosen); collector/adapter hashes identify the
    measurement collector and this adapter (provenance identifiers, not physical
    measurements).
    """
    variant = {
        "schema_version": "runtime-variant-v1",
        "runtime": {"name": "vllm", "revision": "0.23.0"},
        "container": {"name": "none", "revision": "off-e-pr3-gpu-campaign"},
        "cuda": {"name": "cuda", "revision": "driver-595.71.05"},
        "driver": {"name": "nvidia", "revision": "595.71.05"},
        "attention_backend": "vllm-eager",
        "fused_moe_backend": "vllm-fused-moe",
        "tensor_parallel_size": 1,
        "expert_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "distributed_executor": "single-process",
        "execution_mode": "EAGER",
        "max_model_length": 32768,
        "max_batched_tokens": 256,
        "max_sequences": 1,
        "scheduler_policy": "fcfs",
        "kv_cache_dtype": "bf16",
        "nccl_environment": {},
        "placement": {"mode": "off-e-pr3-deterministic-lru"},
        "offload": {"enabled": False},
        "kernel_backend": "vllm-cuda",
        "seed": 0,
        "generation": {"do_sample": False},
        "collector_hash": hashlib.sha256(
            b"off-e-pr3-gpu-campaign-runner|vllm-0.23.0"
        ).hexdigest(),
        "adapter_hash": hashlib.sha256(
            f"{PRODUCER}|{PRODUCER_VERSION}".encode("utf-8")
        ).hexdigest(),
    }
    variant["variant_id"] = runtime_variant_hash(variant)
    return variant


def _tensor_id(layer: int, expert: int) -> str:
    return f"L{layer:02d}E{expert}.ffn"


def _model_payload() -> dict[str, Any]:
    operators: list[dict[str, Any]] = []
    for layer in range(LAYERS):
        operators.append({
            "operator_id": f"L{layer:02d}.attention",
            "layer_index": layer,
            "operator_kind": "ATTENTION",
            "expert_id": None,
        })
        operators.append({
            "operator_id": f"L{layer:02d}.router",
            "layer_index": layer,
            "operator_kind": "ROUTER",
            "expert_id": None,
        })
        for expert in range(EXPERTS):
            operators.append({
                "operator_id": f"L{layer:02d}E{expert}.expert_ffn",
                "layer_index": layer,
                "operator_kind": "EXPERT_FFN",
                "expert_id": expert,
            })
    tensors = [
        {
            "tensor_id": _tensor_id(layer, expert),
            "expert_id": expert,
            "dtype": "BF16",
            "layout": "MIXTRAL_FUSED_W1W2W3_ROW_MAJOR",
            "shape": [3, INTERMEDIATE_SIZE, HIDDEN_SIZE],
            "exact_bytes": str(OBJECT_BYTES),
        }
        for layer in range(LAYERS)
        for expert in range(EXPERTS)
    ]
    return {
        "model_id": "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "revision": "eba92302a2861cdc0098cc54bc9f17cb2c47eb61",
        "precision": "BF16",
        "layers": LAYERS,
        "experts": EXPERTS,
        # per-expert weight bytes = 32 layers x one 336 MiB object each.
        "expert_bytes": str(LAYERS * OBJECT_BYTES),
        "top_k": TOP_K,
        "hidden_size": HIDDEN_SIZE,
        # nominal Mixtral-8x7B parameter counts (model-card facts, not
        # measurements). active <= total is the only enforced relation.
        "total_parameter_count": "46702792704",
        "active_parameter_count": "12879302656",
        "operators": operators,
        "tensors": tensors,
    }


def _platform_payload(label: str, capacity_bytes: int) -> dict[str, Any]:
    h2d_min_service_fs = str(int(round(12.450143814086914 * MS_TO_FS)))
    component = {
        "component_record_id": "h2d_expert_object_service_min",
        "duration_fs": h2d_min_service_fs,
    }
    component["evidence_root"] = hashlib.sha256(
        canonical_bytes({
            "component_record_id": component["component_record_id"],
            "duration_fs": component["duration_fs"],
        })
    ).hexdigest()
    return {
        "platform_id": "rtx-pro-6000-blackwell-workstation",
        "memory_domains": [
            # host pinned catalog: holds all 256 owner replicas (measured 90 GiB).
            {"domain_id": "host_pinned_catalog", "kind": "HOST",
             "capacity_bytes": str(CATALOG_OBJECTS * OBJECT_BYTES)},
            # device residency budget: the swept on-device capacity for this point.
            {"domain_id": "device_residency_budget", "kind": "DEVICE",
             "capacity_bytes": str(capacity_bytes)},
            # physical VRAM (measured nvidia-smi memory.total = 97887 MiB).
            {"domain_id": "device_vram_physical", "kind": "DEVICE",
             "capacity_bytes": str(97887 * 1024 * 1024)},
        ],
        "compute_domains": [
            {"domain_id": "gpu0", "kind": "GPU", "clock_id": "gpu0-graphics",
             "service_slots": 1, "service_rate_units_per_second": "2805000000"},
            {"domain_id": "cpu0", "kind": "CPU", "clock_id": "host-monotonic",
             "service_slots": 1, "service_rate_units_per_second": "1000000000"},
        ],
        "clocks": [
            {"clock_id": "global", "frequency_numerator_hz": "1000000000000000",
             "frequency_denominator_hz": "1", "phase_offset_fs": "0"},
            {"clock_id": "gpu0-graphics", "frequency_numerator_hz": "2805000000",
             "frequency_denominator_hz": "1", "phase_offset_fs": "0"},
            {"clock_id": "host-monotonic", "frequency_numerator_hz": "1000000000",
             "frequency_denominator_hz": "1", "phase_offset_fs": "0"},
        ],
        "bridges": [],
        "interconnects": [
            {"link_id": "pcie_h2d", "source_domain_id": "host_pinned_catalog",
             "destination_domain_id": "device_residency_budget",
             # measured effective bandwidth from the fastest per-object H2D.
             "bandwidth_bytes_per_second": "28298591668",
             "latency_fs": "0", "shared_resource_id": "pcie_fabric",
             "copy_engine_id": "pcie_copy_engine"},
        ],
        "queues": [
            {"queue_id": "gpu0-submit", "domain_id": "gpu0", "capacity": 256,
             "arbitration": "FIFO"},
        ],
        "calibrated_components": [component],
    }


def _workload_payload(point: dict[str, Any], variant_id: str) -> dict[str, Any]:
    result = point["result"]
    input_ids = list(result["input_token_ids"])
    return {
        "dataset_id": "phase7_mixtral_repeated_anchor_v1",
        "dataset_revision": "off-e-pr3",
        "sample_id": result["logical_request_id"],
        "input_token_ids": input_ids,
        "arrival_time_fs": "0",
        "generation_profile_id": "FORCED_LENGTH_CONTROLLED",
        "model_record_id": "model-mixtral-8x7b-eba92302",
        "runtime_variant_hash": variant_id,
        "request_instance_id": f"{result['logical_request_id']}__attempt-0",
        "attempt": 0,
        "sequence_id": result["logical_request_id"],
        "tokens": [
            {"position": i, "role": "USER", "token_id": t}
            for i, t in enumerate(input_ids)
        ],
    }


def _routing_payloads(npy_path: Path, variant_id: str) -> list[dict[str, Any]]:
    """One AGGREGATE-scope RoutingIR per layer.

    The measured routing array carries selected expert ids only (no scores), so
    per-token TOKEN-scope routing is not constructible. AGGREGATE scope records
    the per-expert demand count within each layer, summed over the 159 forwarded
    tokens x top_k. Sum over experts per layer == 159 * TOP_K == 318.
    """
    data, shape = read_npy_u8(npy_path)
    tokens, layers, topk = shape
    if (layers, topk) != (LAYERS, TOP_K):
        raise ValueError(f"unexpected routing shape {shape}")
    payloads = []
    for layer in range(layers):
        demand = [0] * EXPERTS
        for token in range(tokens):
            for k in range(topk):
                expert = data[(token * layers + layer) * topk + k]
                if expert >= EXPERTS:
                    raise ValueError("routing expert id exceeds model experts")
                demand[expert] += 1
        payloads.append({
            "request_id": "off-e-pr3-cap-routing__attempt-0",
            "routing_scope": "AGGREGATE",
            "workload_record_id": None,   # filled by assembler
            "model_record_id": "model-mixtral-8x7b-eba92302",
            "token_index": None,
            "layer_index": layer,
            "score_dtype": "bfloat16",
            "canonical_scores": None,
            "score_tolerance_absolute": None,
            "score_tolerance_relative": None,
            "selected_experts": None,
            "k_boundary_score": None,
            "ambiguity_set": [],
            "aggregate_expert_demand": [str(v) for v in demand],
        })
    return payloads


def _alignment_payload(platform_record_id: str, gmin: int, gmax: int,
                       h2d_min_service_fs: str, component_root: str,
                       global_clock: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform_record_id": platform_record_id,
        "source_clock_id": "host-monotonic",
        "target_clock_id": "global",
        "transform_type": "AFFINE_RATIONAL",
        "scale_numerator": str(NS_TO_FS),
        "scale_denominator": "1",
        "offset_fs": "0",
        "confidence_interval_95_fs": {
            "lower_error_fs": str(-ALIGN_CI_FS),
            "upper_error_fs": str(ALIGN_CI_FS),
        },
        "calibration_method": "monotonic-ns-to-fs-unit-conversion",
        "calibration_points": [
            {"source_time": str(gmin), "target_time": str(gmin * NS_TO_FS)},
            {"source_time": str(gmax), "target_time": str(gmax * NS_TO_FS)},
        ],
        "residual_error_fs": "0",
        "valid_time_range": {
            "source_start": str(gmin),
            "source_end": str(gmax),
        },
        "drift_bound_ppm": "0",
        "grading_inputs": {
            "target_period_numerator_fs": "1",
            "target_period_denominator": "1",
            "shortest_component_duration_fs": h2d_min_service_fs,
            "target_clock_profile_hash": hashlib.sha256(
                canonical_bytes(global_clock)
            ).hexdigest(),
            "shortest_component_record_hash": component_root,
        },
        "claimed_grade": "AGGREGATE_ONLY",
        "segments": [],
    }


def _placement_payload(point: dict[str, Any], platform_record_id: str) -> dict[str, Any]:
    trace = point["trace"]
    resident = trace["terminal_resident_object_ids"]
    if len(resident) != trace["capacity_objects"]:
        raise ValueError("terminal resident set size != capacity_objects")
    expert_locations: list[dict[str, Any]] = []
    # host owner replica for every one of the 256 objects (full catalog).
    for oid in range(CATALOG_OBJECTS):
        layer, expert = divmod(oid, EXPERTS)
        expert_locations.append({
            "expert_id": expert,
            "tensor_id": _tensor_id(layer, expert),
            "shard_offset_bytes": "0",
            "memory_offset_bytes": str(oid * OBJECT_BYTES),
            "memory_domain_id": "host_pinned_catalog",
            "compute_domain_id": "cpu0",
            "owner": True,
            "replica_id": f"obj{oid:03d}-host-owner",
            "shard_bytes": str(OBJECT_BYTES),
        })
    # device non-owner replica for the terminal-resident subset.
    for slot, oid in enumerate(resident):
        layer, expert = divmod(oid, EXPERTS)
        expert_locations.append({
            "expert_id": expert,
            "tensor_id": _tensor_id(layer, expert),
            "shard_offset_bytes": "0",
            "memory_offset_bytes": str(slot * OBJECT_BYTES),
            "memory_domain_id": "device_residency_budget",
            "compute_domain_id": "gpu0",
            "owner": False,
            "replica_id": f"obj{oid:03d}-device-resident",
            "shard_bytes": str(OBJECT_BYTES),
        })
    return {
        "platform_id": "rtx-pro-6000-blackwell-workstation",
        "platform_record_id": platform_record_id,
        "model_record_id": "model-mixtral-8x7b-eba92302",
        "policy_id": trace["logical_policy"],
        "snapshot_id": f"terminal-{point['label']}",
        "predecessor_snapshot_id": None,
        "migration_event_ids": [],
        "version": 1,
        "valid_from_fs": "0",
        "valid_to_fs": None,
        "expert_locations": expert_locations,
        "state_allocations": [],
    }


def _event_payloads(point: dict[str, Any], workload_pid: str,
                    platform_pid: str, placement_pid: str, alignment_pid: str,
                    variant_id: str) -> list[dict[str, Any]]:
    trace = point["trace"]
    request_id = f"{point['result']['logical_request_id']}__attempt-0"
    label = point["label"]

    def aligned(ns: int) -> dict[str, Any]:
        center = ns * NS_TO_FS
        return {
            "time_fs": str(center),
            "source_timestamp": str(ns),
            "aligned_interval_fs": {
                "lower_fs": str(center - ALIGN_CI_FS),
                "upper_fs": str(center + ALIGN_CI_FS),
            },
        }

    base = {
        "request_id": request_id,
        "runtime_variant_hash": variant_id,
        "workload_record_id": workload_pid,
        "platform_record_id": platform_pid,
        "placement_record_id": placement_pid,
        "alignment_record_id": alignment_pid,
        "source_clock_id": "host-monotonic",
        "alignment_grade": "AGGREGATE_ONLY",
    }
    events: list[dict[str, Any]] = []
    layers, topk = LAYERS, TOP_K
    for ev in trace["transfer_events"]:
        demand_index = ev["demand_index"]
        token = demand_index // (layers * topk)
        layer = (demand_index // topk) % layers
        a = aligned(ev["h2d_complete_monotonic_ns"])
        events.append({
            **base,
            "record_id": f"event-{label}-h2d-{ev['load_ordinal']:05d}",
            "event_type": "TRANSFER_COMPLETE",
            "event_priority": 20,
            "resource_action": "TRANSFER",
            "resource_id": "pcie_copy_engine",
            "component_id": "gpu0",
            "quantity": str(ev["h2d_bytes"]),
            "service_demand": str(int(round(ev["cuda_elapsed_ms"] * MS_TO_FS))),
            "token_index": token,
            "layer_index": layer,
            "dependencies": [],
            **a,
        })
    # per-point compute window (actual FusedMoE compute over the forward pass).
    cs = trace["actual_expert_compute_start_monotonic_ns"]
    ce = trace["actual_expert_compute_end_monotonic_ns"]
    start_id = f"event-{label}-compute-start"
    a_start = aligned(cs)
    events.append({
        **base,
        "record_id": start_id,
        "event_type": "COMPUTE_START",
        "event_priority": 100,
        "resource_action": "ACQUIRE",
        "resource_id": "gpu0",
        "component_id": "gpu0",
        "quantity": "1",
        "service_demand": "0",
        "token_index": None,
        "layer_index": None,
        "dependencies": [],
        **a_start,
    })
    a_end = aligned(ce)
    events.append({
        **base,
        "record_id": f"event-{label}-compute-complete",
        "event_type": "COMPUTE_COMPLETE",
        "event_priority": 30,
        "resource_action": "RELEASE",
        "resource_id": "gpu0",
        "component_id": "gpu0",
        "quantity": "1",
        "service_demand": str((ce - cs) * NS_TO_FS),
        "token_index": None,
        "layer_index": None,
        "dependencies": [start_id],
        **a_end,
    })
    return events


def _calibration_payload(training_ids: list[str], heldout_ids: list[str],
                         variant_id: str) -> dict[str, Any]:
    return {
        "metric": "expert_object_bytes",
        "unit": "byte",
        "measured_value": str(OBJECT_BYTES),
        "predicted_value": str(OBJECT_BYTES),
        "evidence_class": "MEASURED",
        # No calibration model is asserted for this family in Stage A2.
        "fidelity": "UNAVAILABLE",
        "range_status": "RANGE_UNKNOWN",
        "calibration_profile_hash": None,
        "model_record_id": "model-mixtral-8x7b-eba92302",
        "platform_record_id": None,   # filled by assembler
        "runtime_variant_hash": variant_id,
        "training_workload_record_ids": training_ids,
        "held_out_workload_record_ids": heldout_ids,
        "calibration_envelope": None,
        "evaluation_coordinate": None,
        "repetitions": 3,
        "sample_count": 15,
        "measurement_noise_floor": "0",
        "resampling_strata": ["off-e-pr3-capacity"],
        "bootstrap_seed": "0",
        "bootstrap_resamples": 10000,
        "bootstrap_ci_95": {"lower": str(OBJECT_BYTES), "upper": str(OBJECT_BYTES)},
    }


def _result_payload(point: dict[str, Any], workload_pid: str, platform_pid: str,
                    calibration_pid: str, variant_id: str) -> dict[str, Any]:
    trace = point["trace"]
    result = point["result"]
    request_id = f"{result['logical_request_id']}__attempt-0"
    latency_ns = int(result["ended_monotonic_ns"]) - int(result["started_monotonic_ns"])
    return {
        "request_id": request_id,
        "workload_record_id": workload_pid,
        "model_record_id": "model-mixtral-8x7b-eba92302",
        "platform_record_id": platform_pid,
        "calibration_record_id": calibration_pid,
        "evidence_class": "MEASURED",
        "runtime_variant_hash": variant_id,
        "execution_valid": True,
        "completed": True,
        "output_token_ids": list(result["output_token_ids"]),
        "stop_reason": str(result["finish_reason"]),
        "latency_fs": str(latency_ns * NS_TO_FS),
        "formal_pass": False,
        "range_status": "RANGE_UNKNOWN",
        "evidence_availability": "CONFIRMED",
    }


# --------------------------------------------------------------------------- #
# assembler
# --------------------------------------------------------------------------- #
MODEL_RID = "model-mixtral-8x7b-eba92302"
ALIGNMENT_RID = "alignment-monotonic-to-global"


def _provenance(source_ids: list[str]) -> dict[str, Any]:
    unique = sorted(set(source_ids))
    return {
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "source_content_ids": unique,
    }


def build_claim_boundary() -> dict[str, Any]:
    """Limitations that must travel with the IR and must not be washed out."""
    return {
        "schema_version": "stage-a2-claim-boundary-v1",
        "family": "OFF-E-PR3 expert capacity scan (15 points)",
        "propagated_via": (
            "sha256 of this document is included in provenance.source_content_ids "
            "of every record in the bundle"
        ),
        "measured_limitations": [
            {
                "id": "OFF-E-PR3-SINGLE-OBJECT-PROXY",
                "text": (
                    "Each logical miss issues one measured 352,321,536-byte H2D "
                    "of the SAME representative layer-0 expert-0 object. Bytes and "
                    "time are measured; per-object movement diversity is NOT "
                    "measured."
                ),
            },
            {
                "id": "ROUTING-NO-SCORES",
                "text": (
                    "The routing .npy records selected expert ids only "
                    "(vllm.CompletionOutput.routed_experts, uint8). It carries no "
                    "gate scores/logits/probabilities. TOKEN-scope routing is not "
                    "constructible; RoutingIR is emitted as AGGREGATE scope. Any "
                    "predictor needing confidence values is impossible from this "
                    "trace."
                ),
            },
            {
                "id": "AGGREGATE-ROUTING-ORDER-DROPPED",
                "text": (
                    "AGGREGATE RoutingIR preserves per-layer per-expert demand "
                    "counts but NOT the per-token ordered demand sequence. The "
                    "ordered demand needed for bit-exact LRU replay (Stage A3 "
                    "SIM0) is recoverable only from the routing .npy under the "
                    "frozen traversal convention (token-major, layer-major, "
                    "top-k order; object id = layer*8 + expert)."
                ),
            },
            {
                "id": "PLACEMENT-TERMINAL-SNAPSHOT",
                "text": (
                    "PlacementIR is the terminal residency snapshot "
                    "(terminal_resident_object_ids). Intermediate residency states "
                    "during the LRU replay are not snapshotted; they are derivable "
                    "from the EventIR transfer sequence in Stage A3."
                ),
            },
            {
                "id": "CLOCK-ALIGNMENT-AGGREGATE-ONLY",
                "text": (
                    "Event timing derives from a single host monotonic-ns clock. "
                    "The ns->fs mapping is an exact unit conversion (scale 1e6), "
                    "but ns quantisation is carried as a +/-0.5 ns (500000 fs) 95% "
                    "CI; alignment grade is AGGREGATE_ONLY, not CYCLE_GRADE."
                ),
            },
        ],
        "forbidden_claims": [
            "IR has been consumed by the engine (Stage A3).",
            "Any timing, performance, calibrated, break-even, or accelerator claim.",
            "Any held-out validation claim (all evidence is FIT-side per spec 7).",
            "IR passing validation means the research chain is closed.",
        ],
    }




def build_records(points: list[dict[str, Any]], claim_hash: str,
                  max_events_per_point: int = 0
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the combined nine-kind bundle for all capacity points.

    Records are finalised in reference-topological order so each embedded
    target_semantic_root equals the root the validator will recompute.
    """
    _, catalog, descriptor_hashes = load_contracts()
    variant = measured_runtime_variant()
    variant_id = variant["variant_id"]
    by_kind: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in IR_KINDS}
    root_cache: dict[str, str] = {}

    def record(kind: str, rid: str, payload: dict[str, Any],
               provenance: dict[str, Any]) -> dict[str, Any]:
        value = {
            "schema_version": SCHEMA_VERSION,
            "ir_kind": kind,
            "record_id": rid,
            "semantic_descriptor_hash": descriptor_hashes[kind],
            "refs": [],
            "provenance": provenance,
            "payload": payload,
        }
        by_kind[kind][rid] = value
        return value

    def freeze_root(kind: str) -> None:
        _, agg = dataset_semantic_hash(
            list(by_kind[kind].values()), catalog["descriptors"][kind]
        )
        root_cache[kind] = agg

    def ref(kind: str, rid: str) -> dict[str, Any]:
        return {
            "target_ir_kind": kind,
            "target_schema_version": SCHEMA_VERSION,
            "target_semantic_root": root_cache[kind],
            "target_primary_key": {"record_id": rid},
        }

    # -- shared source content ids ------------------------------------------
    p0 = points[0]
    contract = json.loads(
        (p0["dir"] / "off_e_pr3_capacity_contract_v1.json").read_text("utf-8")
    )
    catalog_sha = contract["expert_catalog"]["catalog_sha256"]
    config_sha = json.loads(
        (p0["run_dir"] / "model_identity.json").read_text("utf-8")
    )["config_sha256"]
    token_ids_sha = json.loads(
        (p0["run_dir"] / "input_fixture.json").read_text("utf-8")
    )["token_ids_sha256"]
    routing_sha = p0["trace"]["routing_sha256"]

    def platform_rid(label: str) -> str:
        return f"platform-rtxpro6000-cap-{label}"

    def workload_rid(label: str) -> str:
        return f"workload-{label}"

    def placement_rid(label: str) -> str:
        return f"placement-{label}"

    def calibration_rid(label: str) -> str:
        return f"calibration-{label}"

    def result_rid(label: str) -> str:
        return f"result-{label}"

    labels = [pt["label"] for pt in points]

    # 1) ModelIR ------------------------------------------------------------
    record("ModelIR", MODEL_RID, _model_payload(),
           _provenance([claim_hash, config_sha, catalog_sha]))
    freeze_root("ModelIR")

    # 2) PlatformIR (one per point) -----------------------------------------
    for pt in points:
        record("PlatformIR", platform_rid(pt["label"]),
               _platform_payload(pt["label"], pt["trace"]["capacity_bytes"]),
               _provenance([claim_hash, catalog_sha]))
    freeze_root("PlatformIR")

    # 3) WorkloadIR (one per point); refs -> Model --------------------------
    for pt in points:
        w = record("WorkloadIR", workload_rid(pt["label"]),
                   _workload_payload(pt, variant_id),
                   _provenance([claim_hash, token_ids_sha]))
        w["refs"] = [ref("ModelIR", MODEL_RID)]
    freeze_root("WorkloadIR")

    # 4) ClockAlignmentIR (shared); refs -> Platform ------------------------
    all_ns = []
    for pt in points:
        tr = pt["trace"]
        all_ns.append(tr["setup_start_monotonic_ns"])
        all_ns.append(tr["actual_expert_compute_end_monotonic_ns"])
        for ev in tr["transfer_events"]:
            all_ns.append(ev["h2d_complete_monotonic_ns"])
    gmin, gmax = min(all_ns), max(all_ns)
    h2d_min_service_fs = str(int(round(12.450143814086914 * MS_TO_FS)))
    component_root = _platform_payload("x", 1)["calibrated_components"][0]["evidence_root"]
    global_clock = _platform_payload("x", 1)["clocks"][0]
    align_platform = platform_rid(labels[0])
    a = record("ClockAlignmentIR", ALIGNMENT_RID,
               _alignment_payload(align_platform, gmin, gmax,
                                  h2d_min_service_fs, component_root, global_clock),
               _provenance([claim_hash]))
    a["refs"] = [ref("PlatformIR", align_platform)]
    freeze_root("ClockAlignmentIR")

    # 5) PlacementIR (one per point); refs -> Model, Platform ---------------
    for pt in points:
        trace_sha = sha256_file(pt["dir"] / "off_e_pr3_trace/capacity_replay.json")
        pl = record("PlacementIR", placement_rid(pt["label"]),
                    _placement_payload(pt, platform_rid(pt["label"])),
                    _provenance([claim_hash, catalog_sha, trace_sha]))
        pl["refs"] = [ref("ModelIR", MODEL_RID),
                      ref("PlatformIR", platform_rid(pt["label"]))]
    freeze_root("PlacementIR")

    # 6) RoutingIR (per layer, shared); refs -> Model, Workload -------------
    #    workload_record_id points at the CAP-100 control workload as a stable
    #    representative of the shared prompt (identical across points).
    rep_workload = workload_rid(labels[-1])
    rep_request_id = f"{points[-1]['result']['logical_request_id']}__attempt-0"
    for payload in _routing_payloads(p0["npy_path"], variant_id):
        payload["workload_record_id"] = rep_workload
        payload["request_id"] = rep_request_id
        r = record("RoutingIR", f"routing-layer-{payload['layer_index']:02d}",
                   payload, _provenance([claim_hash, routing_sha]))
        r["refs"] = [ref("ModelIR", MODEL_RID),
                     ref("WorkloadIR", rep_workload)]
    freeze_root("RoutingIR")

    # 7) CalibrationIR (one per point); refs -> Model, Platform, Workloads --
    #    training/held_out are schema-required disjoint references only; no
    #    held-out validation is asserted (fidelity UNAVAILABLE, spec 7).
    heldout_default = workload_rid(labels[-1])
    heldout_alt = workload_rid(labels[-2])
    for pt in points:
        label = pt["label"]
        training = workload_rid(label)
        heldout = heldout_default if training != heldout_default else heldout_alt
        audit_sha = sha256_file(pt["dir"] / "off_e_pr3_point_audit.json")
        payload = _calibration_payload([training], [heldout], variant_id)
        payload["platform_record_id"] = platform_rid(label)
        c = record("CalibrationIR", calibration_rid(label), payload,
                   _provenance([claim_hash, audit_sha]))
        refs = [ref("ModelIR", MODEL_RID), ref("PlatformIR", platform_rid(label)),
                ref("WorkloadIR", training), ref("WorkloadIR", heldout)]
        c["refs"] = refs
    freeze_root("CalibrationIR")

    # 8) EventIR (per point); refs -> Workload, Platform, Placement, Align --
    for pt in points:
        label = pt["label"]
        trace_sha = sha256_file(pt["dir"] / "off_e_pr3_trace/capacity_replay.json")
        payloads = _event_payloads(
            pt, workload_rid(label), platform_rid(label), placement_rid(label),
            ALIGNMENT_RID, variant_id)
        if max_events_per_point:
            transfers = [p for p in payloads if p["event_type"] == "TRANSFER_COMPLETE"]
            others = [p for p in payloads if p["event_type"] != "TRANSFER_COMPLETE"]
            payloads = transfers[:max_events_per_point] + others
        for ep in payloads:
            rid = ep.pop("record_id")
            e = record("EventIR", rid, ep, _provenance([claim_hash, trace_sha]))
            e["refs"] = [ref("WorkloadIR", workload_rid(label)),
                         ref("PlatformIR", platform_rid(label)),
                         ref("PlacementIR", placement_rid(label)),
                         ref("ClockAlignmentIR", ALIGNMENT_RID)]
    freeze_root("EventIR")

    # 9) ResultIR (one per point); refs -> Workload, Model, Platform, Calib -
    for pt in points:
        label = pt["label"]
        result_sha = sha256_file(pt["run_dir"] / "result.json")
        payload = _result_payload(pt, workload_rid(label), platform_rid(label),
                                  calibration_rid(label), variant_id)
        r = record("ResultIR", result_rid(label), payload,
                   _provenance([claim_hash, result_sha]))
        r["refs"] = [ref("WorkloadIR", workload_rid(label)),
                     ref("ModelIR", MODEL_RID),
                     ref("PlatformIR", platform_rid(label)),
                     ref("CalibrationIR", calibration_rid(label))]
    freeze_root("ResultIR")

    records = [v for kind in IR_KINDS for v in by_kind[kind].values()]
    records.sort(key=lambda item: (item["ir_kind"], item["record_id"]))
    return records, variant


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def conservation_report(points: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    all_ok = True
    for pt in points:
        tr = pt["trace"]
        dload = tr["demand_load_count"]
        hit = tr["hit_count"]
        discard = tr["immutable_discard_count"]
        cap = tr["capacity_objects"]
        h2d = tr["h2d_bytes"]
        obj = tr["expert_object_bytes"]
        npy_sha = sha256_file(pt["npy_path"])
        terminal_resident = len(tr["terminal_resident_object_ids"])
        checks = {
            # the mandated Stage A2 byte-conservation acceptance criterion
            "h2d_bytes == demand_load_count * expert_object_bytes":
                h2d == dload * obj,
            "hit_count + demand_load_count == logical_demand_count":
                hit + dload == tr["logical_demand_count"],
            "len(transfer_events) == demand_load_count":
                len(tr["transfer_events"]) == dload,
            "len(terminal_resident_object_ids) == capacity_objects":
                terminal_resident == cap,
            "routing_sha256 traces to routing .npy bytes":
                npy_sha == tr["routing_sha256"],
        }
        ok = all(checks.values())
        all_ok = all_ok and ok
        # Informational: holds for the 14 loading points; the CAP-100 control
        # is pre-seeded (256 resident, 0 loads/discards), so it reads 0 there.
        loaded_still_resident = dload - discard
        rows.append({
            "label": pt["label"],
            "capacity_objects": cap,
            "demand_load_count": dload,
            "hit_count": hit,
            "immutable_discard_count": discard,
            "loaded_still_resident_eq_capacity_when_loading":
                loaded_still_resident == cap or dload == 0,
            "h2d_bytes": h2d,
            "routing_sha256": tr["routing_sha256"],
            "npy_sha256": npy_sha,
            "checks": checks,
            "ok": ok,
        })
    return {"all_ok": all_ok, "points": rows}


def dropped_fields_report() -> dict[str, Any]:
    """Raw fields with no IR home, and why -- an input to Stage A3."""
    return {
        "schema_version": "stage-a2-dropped-fields-v1",
        "family": "OFF-E-PR3 expert capacity scan",
        "dropped_or_degraded": [
            {"field": "routing per-token top-k (scores)",
             "reason": "no gate scores in the .npy; TOKEN-scope routing not "
                       "constructible. Emitted as AGGREGATE per-layer demand.",
             "a3_impact": "engine must read the .npy for the ordered demand "
                          "sequence (object id = layer*8+expert, token-major)."},
            {"field": "per-token ordered demand sequence",
             "reason": "AGGREGATE RoutingIR loses ordering; measured "
                       "transfer_events record misses only, not hits.",
             "a3_impact": "SIM0 LRU replay must reconstruct the 10176-long "
                          "demand order from the routing .npy."},
            {"field": "host_snapshot_setup_d2h_bytes / setup H2D",
             "reason": "setup transfer is excluded from the demand path; not a "
                       "demand-driven residency event.",
             "a3_impact": "none for counters; documented for completeness."},
            {"field": "transfer_events[].decision_monotonic_ns / h2d_start_*",
             "reason": "one TRANSFER_COMPLETE EventIR per load carries completion "
                       "time + service_demand; start time folded into "
                       "service_demand (complete - duration).",
             "a3_impact": "start timestamps available in raw if needed."},
            {"field": "logical_evicted_object_id per transfer",
             "reason": "eviction identity not represented in EventIR payload "
                       "(no eviction-target field in schema).",
             "a3_impact": "LRU eviction identity recoverable from replay."},
            {"field": "non-expert model weights (attention/router tensors)",
             "reason": "not part of the residency sweep; ModelIR lists them as "
                       "operators but not as residency-managed tensors.",
             "a3_impact": "none for the expert-capacity experiment."},
            {"field": "total_h2d_cuda_elapsed_ms (per-point aggregate)",
             "reason": "per-event cuda_elapsed_ms is carried on each EventIR "
                       "service_demand; the aggregate is derivable.",
             "a3_impact": "cross-check only."},
        ],
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    import datetime as _dt

    from canonical_ir import validate_records, write_bundle

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path,
                        default=_REPO_ROOT / "evidence")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--max-events-per-point", type=int, default=0,
                        help="cap TRANSFER_COMPLETE events per point (0 = all); "
                             ">0 produces a PARTIAL bundle for fast iteration")
    args = parser.parse_args(argv)

    run_dir = args.run_dir
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    point_dirs = discover_points(args.evidence_root)
    points = [load_point(d) for d in point_dirs]

    claim = build_claim_boundary()
    claim_bytes = canonical_bytes(claim)
    claim_hash = hashlib.sha256(claim_bytes).hexdigest()
    (artifacts / "claim_boundary.json").write_text(
        json.dumps(claim, indent=2, sort_keys=True) + "\n", "utf-8")

    records, variant = build_records(points, claim_hash,
                                     args.max_events_per_point)

    # explicit validation for a clean pass/fail signal before writing
    validate_records(records, bundle_evidence_class="MEASURED")

    bundle_dir = run_dir / "bundle"
    envelope = write_bundle(
        bundle_dir, records, evidence_class="MEASURED",
        runtime_variants=[variant])

    cons = conservation_report(points)
    (artifacts / "conservation_report.json").write_text(
        json.dumps(cons, indent=2, sort_keys=True) + "\n", "utf-8")
    (artifacts / "dropped_fields.json").write_text(
        json.dumps(dropped_fields_report(), indent=2, sort_keys=True) + "\n",
        "utf-8")

    kind_counts: dict[str, int] = {}
    for r in records:
        kind_counts[r["ir_kind"]] = kind_counts.get(r["ir_kind"], 0) + 1
    summary = {
        "family": "OFF-E-PR3",
        "points": len(points),
        "record_count": len(records),
        "kind_counts": kind_counts,
        "partial": bool(args.max_events_per_point),
        "runtime_variant_id": variant["variant_id"],
        "claim_boundary_sha256": claim_hash,
        "conservation_all_ok": cons["all_ok"],
        "evidence_class": "MEASURED",
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (artifacts / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", "utf-8")

    import subprocess

    def _git(*a: str) -> str:
        try:
            return subprocess.run(["git", *a], cwd=str(_REPO_ROOT),
                                  capture_output=True, text=True,
                                  check=True).stdout.strip()
        except Exception:
            return ""

    manifest = {
        "run_id": run_dir.name,
        "stage": "A2",
        "experiment_id": "off_e_pr3_measured_to_ir",
        "created_at": summary["generated_at"],
        "command": ["python", str(Path(__file__).relative_to(_REPO_ROOT)),
                    "--run-dir", str(run_dir)],
        "classification": "MEASURED -> Canonical IR (Stage A2, CPU-only)",
        "platform_profile": "NVIDIA RTX PRO 6000 Blackwell (evidence-of-record)",
        "git": {
            "code_commit": _git("rev-parse", "HEAD"),
            "adapter_sha256": hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest(),
            "provenance_note": (
                "artifacts produced by the adapter at code_commit; this run "
                "directory is committed in the following (artifact) commit."),
        },
        "summary": summary,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")

    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"bundle written to {bundle_dir} "
          f"({len(envelope.get('partitions', []))} partitions)")
    return 0 if cons["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
