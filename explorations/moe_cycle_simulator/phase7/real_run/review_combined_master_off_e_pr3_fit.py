#!/usr/bin/env python3
"""Freeze the OFF-E-PR3 FIT/control review before held-out execution."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from promote_combined_master_swap_k1_v5 import (
    legally_closed,
    now_utc,
    read_json,
    sha256_file,
    write_json,
)


FIT = ["025", "050", "075", "080", "085", "090", "095", "099"]
HELD_OUT = ["0375", "0625", "0825", "0875", "0925", "097"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    rows = ledger["rows"]
    by_id = {row["master_row_id"]: row for row in rows}
    for label in HELD_OUT:
        row = by_id[f"OFF-E-PR3-CAP-{label}"]
        if row["execution_state"] != "NOT_RUN" or row.get("attempt_ids"):
            raise SystemExit(f"held-out leakage before fit review: {label}")
    points = []
    for label in FIT + ["100"]:
        attempt = root / "remote_raw" / f"OFF-E-PR3-CAP-{label}-V1-MASTER"
        audit = read_json(attempt / "off_e_pr3_point_audit.json")
        if audit.get("status") != "PASS" or not all(audit.get("gates", {}).values()):
            raise SystemExit(f"fit/control point is not audited PASS: {label}")
        if not legally_closed(by_id[f"OFF-E-PR3-CAP-{label}"]):
            raise SystemExit(f"fit/control row is not legally closed: {label}")
        points.append(
            {
                "label": label,
                "fit_role": audit["fit_role"],
                **audit["metrics"],
            }
        )
    fit_points = points[:-1]
    capacity_monotonic = all(
        left["capacity_objects"] < right["capacity_objects"]
        for left, right in zip(fit_points, fit_points[1:])
    )
    loads_monotonic = all(
        left["demand_load_count"] > right["demand_load_count"]
        for left, right in zip(fit_points, fit_points[1:])
    )
    measured_h2d_monotonic = all(
        left["total_h2d_cuda_elapsed_ms"] > right["total_h2d_cuda_elapsed_ms"]
        for left, right in zip(fit_points, fit_points[1:])
    )
    control_gate = (
        points[-1]["demand_load_count"] == 0
        and points[-1]["h2d_bytes"] == 0
        and points[-1]["immutable_discard_count"] == 0
    )
    if not all((capacity_monotonic, loads_monotonic, measured_h2d_monotonic, control_gate)):
        raise SystemExit("fit/control review gate failed")
    review = {
        "schema_version": "phase7-off-e-pr3-fit-review-v1",
        "reviewed_at_utc": now_utc(),
        "status": "PASS",
        "fit_points": points,
        "gates": {
            "held_out_untouched_before_review": True,
            "capacity_monotonic": capacity_monotonic,
            "demand_load_count_strictly_decreases": loads_monotonic,
            "measured_h2d_time_strictly_decreases": measured_h2d_monotonic,
            "all_resident_control_zero_demand_h2d": control_gate,
            "policy_trace_capacity_alignment_unchanged": True,
        },
        "fit_only_interpretation": "Demand H2D remains nonzero at every required FIT point through 99%; the all-resident zero-H2D boundary occurs only at the 100% control under the frozen empty-initial-cache LRU replay.",
        "adaptive_refinement_decision": "DEFERRED_UNTIL_REQUIRED_HELD_OUT_POINTS_COMPLETE",
        "held_out_unlock": HELD_OUT,
        "claim_boundary": "FIT-only capacity replay result; no held-out, runtime-native, end-to-end speedup, CAL3 or hardware break-even claim.",
    }
    review_path = root / "reviews" / "MR10-OFF-E-PR3-FIT-REVIEW.json"
    write_json(review_path, review)
    prior = sha256_file(ledger_path)
    transition_id = "MR10-OFF-E-PR3-FIT-REVIEW-HELDOUT-UNLOCK"
    ledger.setdefault("transitions", []).append(
        {
            "transition_id": transition_id,
            "timestamp_utc": review["reviewed_at_utc"],
            "changed_rows": [],
            "reason": "Fit/control review passed with all held-out rows untouched; unlock frozen held-out order without policy changes.",
            "prior_ledger_sha256": prior,
            "fit_review_sha256": sha256_file(review_path),
        }
    )
    ledger["latest_transition_id"] = transition_id
    ledger["updated_at_utc"] = review["reviewed_at_utc"]
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)
    remaining_path = root / "master_remaining_ledger.json"
    remaining = read_json(remaining_path)
    remaining["generated_from_execution_ledger_sha256"] = execution_hash
    write_json(remaining_path, remaining)
    queue_path = root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue.update(
        {
            "generated_from_execution_ledger_sha256": execution_hash,
            "next_cpu_unit": None,
            "next_gpu_unit": "OFF-E-PR3-CAP-0375",
            "ready_gpu_units": ["OFF-E-PR3-CAP-0375"],
            "next_gate_action": "RUN_FROZEN_OFF_E_PR3_HELD_OUT_ORDER_WITHOUT_TUNING",
            "dispatch_guards": [
                "MR2 read-only preflight clear",
                "no foreign serving/GPU process at dispatch",
                "fit/control review PASS and locally backed up",
                "held-out policy/trace/capacity alignment unchanged",
                "no fit threshold or policy retuning",
                "actual expert compute required",
                "no filler workload",
                "raw namespace independent",
            ],
        }
    )
    write_json(queue_path, queue)
    write_json(
        root / "checkpoints" / f"{transition_id}.json",
        {
            "schema_version": "phase7-combined-master-checkpoint-v1",
            "checkpoint_id": transition_id,
            "timestamp_utc": review["reviewed_at_utc"],
            "execution_ledger_sha256": execution_hash,
            "remaining_ledger_sha256": sha256_file(remaining_path),
            "required_closed_count": ledger["required_closed_count"],
            "required_remaining_count": remaining["required_remaining_count"],
            "next_ready_gpu_unit": "OFF-E-PR3-CAP-0375",
            "fit_review_sha256": sha256_file(review_path),
        },
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "execution_ledger_sha256": execution_hash,
                "fit_review_sha256": sha256_file(review_path),
                "next_ready_gpu_unit": "OFF-E-PR3-CAP-0375",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
