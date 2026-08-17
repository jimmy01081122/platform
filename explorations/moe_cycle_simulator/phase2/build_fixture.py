#!/usr/bin/env python3
"""Generate the deterministic nine-kind Phase 2 Canonical IR fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from canonical_ir import IR_KINDS, load_contracts

import sys

SIM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SIM_ROOT / "tools"))
from contract_runtime import canonical_bytes, dataset_semantic_hash  # noqa: E402

ZERO_HASH = "0" * 64


def provenance() -> dict[str, Any]:
    return {
        "producer": "phase2-canonical-fixture",
        "producer_version": "1",
        "source_content_ids": [ZERO_HASH],
    }


def build_fixture() -> list[dict[str, Any]]:
    _, catalog, descriptor_hashes = load_contracts()
    runtime_variant = json.loads(
        (
            Path(__file__).resolve().parent
            / "contracts"
            / "runtime_variant_fixture.json"
        ).read_text(encoding="utf-8")
    )
    runtime_variant_hash = runtime_variant["variant_id"]
    records: dict[str, list[dict[str, Any]]] = {
        kind: [] for kind in IR_KINDS
    }

    def record(kind: str, record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        value = {
            "schema_version": "canonical-ir-v1",
            "ir_kind": kind,
            "record_id": record_id,
            "semantic_descriptor_hash": descriptor_hashes[kind],
            "refs": [],
            "provenance": provenance(),
            "payload": payload,
        }
        records[kind].append(value)
        return value

    model = record(
        "ModelIR",
        "model-mixtral-shape",
        {
            "model_id": "synthetic-mixtral-shape",
            "revision": "fixture-v1",
            "precision": "BF16",
            "layers": 2,
            "experts": 8,
            "expert_bytes": "1000",
            "top_k": 2,
            "hidden_size": 64,
            "total_parameter_count": "46700000000",
            "active_parameter_count": "12900000000",
            "operators": [
                {
                    "operator_id": f"layer-0-expert-{expert_id}",
                    "layer_index": 0,
                    "operator_kind": "EXPERT_FFN",
                    "expert_id": expert_id,
                }
                for expert_id in range(8)
            ],
            "tensors": [
                {
                    "tensor_id": f"expert-{expert_id}-weight-{shard_index}",
                    "expert_id": expert_id,
                    "dtype": "BF16",
                    "layout": "ROW_MAJOR",
                    "shape": [250],
                    "exact_bytes": "500",
                }
                for expert_id in range(8)
                for shard_index in range(2)
            ] + [
                {
                    "tensor_id": "shared-weight",
                    "expert_id": None,
                    "dtype": "BF16",
                    "layout": "ROW_MAJOR",
                    "shape": [100],
                    "exact_bytes": "200",
                }
            ],
        },
    )
    calibrated_component = {
        "component_record_id": "synthetic-kernel-service-minimum",
        "duration_fs": "500000",
        "evidence_root": hashlib.sha256(
            canonical_bytes(
                {
                    "component_record_id": "synthetic-kernel-service-minimum",
                    "duration_fs": "500000",
                }
            )
        ).hexdigest(),
    }
    platform = record(
        "PlatformIR",
        "platform-synthetic-discrete",
        {
            "platform_id": "synthetic-discrete",
            "memory_domains": [
                {"domain_id": "host", "kind": "HOST", "capacity_bytes": "100000"},
                {"domain_id": "vram0", "kind": "DEVICE", "capacity_bytes": "10000"},
            ],
            "compute_domains": [
                {
                    "domain_id": "gpu0",
                    "kind": "GPU",
                    "clock_id": "gpu0-clock",
                    "service_slots": 2,
                    "service_rate_units_per_second": "1000000000",
                }
            ],
            "clocks": [
                {
                    "clock_id": "global",
                    "frequency_numerator_hz": "1000000000000000",
                    "frequency_denominator_hz": "1",
                    "phase_offset_fs": "0",
                },
                {
                    "clock_id": "gpu0-clock",
                    "frequency_numerator_hz": "400000000000000",
                    "frequency_denominator_hz": "1",
                    "phase_offset_fs": "1000",
                },
            ],
            "bridges": [
                {
                    "schema_version": "bridge-v1",
                    "bridge_id": "global-gpu0",
                    "source_clock_id": "global",
                    "target_clock_id": "gpu0-clock",
                    "protocol": "REQUEST_ACK",
                    "forward_latency_fs": "1000",
                    "reverse_latency_fs": "1000",
                    "receiver_sync_cycles": 2,
                    "ack_sync_cycles": 2,
                    "queue_capacity": 8,
                    "backpressure_policy": "STALL_SOURCE",
                }
            ],
            "interconnects": [
                {
                    "link_id": "gpu0-vram0",
                    "source_domain_id": "gpu0",
                    "destination_domain_id": "vram0",
                    "bandwidth_bytes_per_second": "1000000000",
                    "latency_fs": "1000",
                    "shared_resource_id": "gpu0-vram-fabric",
                    "copy_engine_id": "copy0",
                }
            ],
            "queues": [
                {
                    "queue_id": "gpu0-submit",
                    "domain_id": "gpu0",
                    "capacity": 8,
                    "arbitration": "FIFO",
                }
            ],
            "calibrated_components": [calibrated_component],
        },
    )

    def root(kind: str) -> str:
        return dataset_semantic_hash(
            records[kind], catalog["descriptors"][kind]
        )[1]

    def reference(kind: str, record_id: str) -> dict[str, Any]:
        return {
            "target_ir_kind": kind,
            "target_schema_version": "canonical-ir-v1",
            "target_semantic_root": root(kind),
            "target_primary_key": {"record_id": record_id},
        }

    workload = record(
        "WorkloadIR",
        "workload-request-1",
        {
            "dataset_id": "synthetic",
            "dataset_revision": "fixture-v1",
            "sample_id": "sample-1",
            "input_token_ids": [1, 2, 3],
            "arrival_time_fs": "0",
            "generation_profile_id": "deterministic-fixture-v1",
            "model_record_id": model["record_id"],
            "runtime_variant_hash": runtime_variant_hash,
            "request_instance_id": "request-1-attempt-0",
            "attempt": 0,
            "sequence_id": "sequence-1",
            "tokens": [
                {"position": 0, "role": "USER", "token_id": 1},
                {"position": 1, "role": "USER", "token_id": 2},
                {"position": 2, "role": "USER", "token_id": 3},
            ],
        },
    )
    workload["refs"] = [reference("ModelIR", model["record_id"])]
    heldout_workload = record(
        "WorkloadIR",
        "workload-request-2",
        {
            "dataset_id": "synthetic",
            "dataset_revision": "fixture-v1",
            "sample_id": "sample-2",
            "input_token_ids": [5, 6],
            "arrival_time_fs": "0",
            "generation_profile_id": "deterministic-fixture-v1",
            "model_record_id": model["record_id"],
            "runtime_variant_hash": runtime_variant_hash,
            "request_instance_id": "request-2-attempt-0",
            "attempt": 0,
            "sequence_id": "sequence-2",
            "tokens": [
                {"position": 0, "role": "USER", "token_id": 5},
                {"position": 1, "role": "USER", "token_id": 6},
            ],
        },
    )
    heldout_workload["refs"] = [
        reference("ModelIR", model["record_id"])
    ]

    alignment = record(
        "ClockAlignmentIR",
        "alignment-gpu0-global",
        {
            "platform_record_id": platform["record_id"],
            "source_clock_id": "gpu0-clock",
            "target_clock_id": "global",
            "transform_type": "AFFINE_RATIONAL",
            "scale_numerator": "5",
            "scale_denominator": "2",
            "offset_fs": "1000",
            "confidence_interval_95_fs": {
                "lower_error_fs": "0",
                "upper_error_fs": "0",
            },
            "calibration_method": "synthetic-two-point-anchor",
            "calibration_points": [
                {"source_time": "0", "target_time": "1000"},
                {"source_time": "1200000", "target_time": "3001000"},
            ],
            "residual_error_fs": "0",
            "valid_time_range": {
                "source_start": "0",
                "source_end": "2000000",
            },
            "drift_bound_ppm": "0",
            "grading_inputs": {
                "target_period_numerator_fs": "1",
                "target_period_denominator": "1",
                "shortest_component_duration_fs": "500000",
                "target_clock_profile_hash": hashlib.sha256(
                    canonical_bytes(platform["payload"]["clocks"][0])
                ).hexdigest(),
                "shortest_component_record_hash": calibrated_component[
                    "evidence_root"
                ],
            },
            "claimed_grade": "CYCLE_GRADE",
            "segments": [],
        },
    )
    alignment["refs"] = [reference("PlatformIR", platform["record_id"])]

    placement = record(
        "PlacementIR",
        "placement-snapshot-1",
        {
            "platform_id": platform["payload"]["platform_id"],
            "platform_record_id": platform["record_id"],
            "model_record_id": model["record_id"],
            "policy_id": "all-resident-fixture",
            "snapshot_id": "snapshot-1",
            "predecessor_snapshot_id": None,
            "migration_event_ids": [],
            "version": 1,
            "valid_from_fs": "0",
            "valid_to_fs": None,
            "expert_locations": [
                {
                    "expert_id": expert_id,
                    "tensor_id": f"expert-{expert_id}-weight-{shard_index}",
                    "shard_offset_bytes": "0",
                    "memory_offset_bytes": str(
                        expert_id * 1000 + shard_index * 500
                    ),
                    "memory_domain_id": "vram0",
                    "compute_domain_id": "gpu0",
                    "owner": True,
                    "replica_id": (
                        f"expert-{expert_id}-tensor-{shard_index}-owner"
                    ),
                    "shard_bytes": "500",
                }
                for expert_id in range(8)
                for shard_index in range(2)
            ],
            "state_allocations": [
                {
                    "allocation_id": "shared-weight-owner",
                    "object_class": "NON_EXPERT_WEIGHT",
                    "tensor_id": "shared-weight",
                    "memory_domain_id": "vram0",
                    "compute_domain_id": "gpu0",
                    "offset_bytes": "8000",
                    "exact_bytes": "200",
                },
                {
                    "allocation_id": "request-1-kv",
                    "object_class": "KV_CACHE",
                    "tensor_id": None,
                    "memory_domain_id": "vram0",
                    "compute_domain_id": "gpu0",
                    "offset_bytes": "8200",
                    "exact_bytes": "100",
                },
            ],
        },
    )
    placement["refs"] = [
        reference("ModelIR", model["record_id"]),
        reference("PlatformIR", platform["record_id"]),
    ]

    routing = record(
        "RoutingIR",
        "routing-request-1-token-0-layer-0",
        {
            "request_id": "request-1-attempt-0",
            "routing_scope": "TOKEN",
            "workload_record_id": workload["record_id"],
            "model_record_id": model["record_id"],
            "token_index": 0,
            "layer_index": 0,
            "score_dtype": "float32",
            "canonical_scores": [
                "1",
                "0.89999997615814208984375",
                "0.89999902248382568359375",
                "0.5",
                "0.4000000059604644775390625",
                "0.300000011920928955078125",
                "0.20000000298023223876953125",
                "0.100000001490116119384765625"
            ],
            "score_tolerance_absolute": "0.000000238418579101562500",
            "score_tolerance_relative": "0.00001",
            "selected_experts": [0, 1],
            "k_boundary_score": "0.89999997615814208984375",
            "ambiguity_set": [1, 2],
            "aggregate_expert_demand": None,
        },
    )
    routing["refs"] = [
        reference("ModelIR", model["record_id"]),
        reference("WorkloadIR", workload["record_id"]),
    ]

    event_refs = [
        reference("WorkloadIR", workload["record_id"]),
        reference("PlatformIR", platform["record_id"]),
        reference("PlacementIR", placement["record_id"]),
        reference("ClockAlignmentIR", alignment["record_id"]),
    ]
    event_start = record(
        "EventIR",
        "event-compute-start",
        {
            "event_type": "COMPUTE_START",
            "resource_action": "ACQUIRE",
            "resource_id": "gpu0",
            "quantity": "1",
            "service_demand": "0",
            "time_fs": "2501000",
            "event_priority": 100,
            "request_id": "request-1-attempt-0",
            "token_index": 0,
            "layer_index": 0,
            "runtime_variant_hash": runtime_variant_hash,
            "component_id": "gpu0",
            "dependencies": [],
            "workload_record_id": workload["record_id"],
            "platform_record_id": platform["record_id"],
            "placement_record_id": placement["record_id"],
            "source_clock_id": "gpu0-clock",
            "source_timestamp": "1000000",
            "alignment_record_id": alignment["record_id"],
            "aligned_interval_fs": {"lower_fs": "2501000", "upper_fs": "2501000"},
            "alignment_grade": "CYCLE_GRADE",
        },
    )
    event_start["refs"] = list(event_refs)
    event_end = record(
        "EventIR",
        "event-compute-complete",
        {
            "event_type": "COMPUTE_COMPLETE",
            "resource_action": "RELEASE",
            "resource_id": "gpu0",
            "quantity": "1",
            "service_demand": "500000",
            "time_fs": "3001000",
            "event_priority": 30,
            "request_id": "request-1-attempt-0",
            "token_index": 0,
            "layer_index": 0,
            "runtime_variant_hash": runtime_variant_hash,
            "component_id": "gpu0",
            "dependencies": [event_start["record_id"]],
            "workload_record_id": workload["record_id"],
            "platform_record_id": platform["record_id"],
            "placement_record_id": placement["record_id"],
            "source_clock_id": "gpu0-clock",
            "source_timestamp": "1200000",
            "alignment_record_id": alignment["record_id"],
            "aligned_interval_fs": {"lower_fs": "3001000", "upper_fs": "3001000"},
            "alignment_grade": "CYCLE_GRADE",
        },
    )
    event_end["refs"] = list(event_refs)

    calibration = record(
        "CalibrationIR",
        "calibration-kernel-service-1",
        {
            "metric": "kernel_service_time",
            "unit": "femtosecond",
            "measured_value": "500000",
            "predicted_value": "500000",
            "evidence_class": "SYNTHETIC",
            "fidelity": "FUNCTIONAL_ONLY",
            "range_status": "RANGE_UNKNOWN",
            "calibration_profile_hash": None,
            "model_record_id": model["record_id"],
            "platform_record_id": platform["record_id"],
            "runtime_variant_hash": runtime_variant_hash,
            "training_workload_record_ids": [workload["record_id"]],
            "held_out_workload_record_ids": [heldout_workload["record_id"]],
            "calibration_envelope": None,
            "evaluation_coordinate": None,
            "repetitions": 3,
            "bootstrap_seed": "1",
            "bootstrap_resamples": 10000,
            "sample_count": 3,
            "measurement_noise_floor": "0",
            "resampling_strata": ["synthetic-fixture"],
            "bootstrap_ci_95": {"lower": "0", "upper": "0"},
        },
    )
    calibration["refs"] = [
        reference("ModelIR", model["record_id"]),
        reference("PlatformIR", platform["record_id"]),
        reference("WorkloadIR", workload["record_id"]),
        reference("WorkloadIR", heldout_workload["record_id"]),
    ]

    result = record(
        "ResultIR",
        "result-request-1",
        {
            "request_id": "request-1-attempt-0",
            "workload_record_id": workload["record_id"],
            "model_record_id": model["record_id"],
            "platform_record_id": platform["record_id"],
            "calibration_record_id": calibration["record_id"],
            "evidence_class": "SYNTHETIC",
            "runtime_variant_hash": runtime_variant_hash,
            "execution_valid": True,
            "completed": True,
            "output_token_ids": [4],
            "stop_reason": "EOS",
            "latency_fs": "3001000",
            "formal_pass": False,
            "range_status": "RANGE_UNKNOWN",
            "evidence_availability": "CONFIRMED",
        },
    )
    result["refs"] = [
        reference("WorkloadIR", workload["record_id"]),
        reference("ModelIR", model["record_id"]),
        reference("PlatformIR", platform["record_id"]),
        reference("CalibrationIR", calibration["record_id"]),
    ]

    return sorted(
        [item for values in records.values() for item in values],
        key=lambda item: (item["ir_kind"], item["record_id"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("fixture output already exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "schema_version": "canonical-ir-fixture-bundle-v1",
                "records": build_fixture(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
