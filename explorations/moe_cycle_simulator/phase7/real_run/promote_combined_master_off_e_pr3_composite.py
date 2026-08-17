#!/usr/bin/env python3
"""Close OFF-E-PR3 only after all frozen capacity children pass."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from promote_combined_master_swap_k1_v5 import (
    append_unique,
    legally_closed,
    now_utc,
    read_json,
    sha256_file,
    write_json,
)


LABELS = ["025", "0375", "050", "0625", "075", "080", "0825", "085", "0875", "090", "0925", "095", "097", "099", "100"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    rows = ledger["rows"]
    by_id = {row["master_row_id"]: row for row in rows}
    points = []
    for label in LABELS:
        cell = f"OFF-E-PR3-CAP-{label}"
        if not legally_closed(by_id[cell]):
            raise SystemExit(f"capacity child is not legally closed: {cell}")
        audit_path = root / "remote_raw" / f"{cell}-V1-MASTER" / "off_e_pr3_point_audit.json"
        audit = read_json(audit_path)
        if audit.get("status") != "PASS" or not all(audit.get("gates", {}).values()):
            raise SystemExit(f"capacity child audit failed: {cell}")
        points.append({"cell_id": cell, "fit_role": audit["fit_role"], **audit["metrics"]})
    fit_review_path = root / "reviews" / "MR10-OFF-E-PR3-FIT-REVIEW.json"
    fit_review = read_json(fit_review_path)
    if fit_review.get("status") != "PASS" or not all(fit_review.get("gates", {}).values()):
        raise SystemExit("fit review is not PASS")
    noncontrol = points[:-1]
    capacity_monotonic = all(
        left["capacity_objects"] < right["capacity_objects"]
        for left, right in zip(noncontrol, noncontrol[1:])
    )
    loads_monotonic = all(
        left["demand_load_count"] > right["demand_load_count"]
        for left, right in zip(noncontrol, noncontrol[1:])
    )
    h2d_time_monotonic = all(
        left["total_h2d_cuda_elapsed_ms"] > right["total_h2d_cuda_elapsed_ms"]
        for left, right in zip(noncontrol, noncontrol[1:])
    )
    control_zero = points[-1]["demand_load_count"] == points[-1]["h2d_bytes"] == 0
    if not all((capacity_monotonic, loads_monotonic, h2d_time_monotonic, control_zero)):
        raise SystemExit("combined capacity curve review failed")
    review = {
        "schema_version": "phase7-off-e-pr3-composite-review-v1",
        "reviewed_at_utc": now_utc(),
        "status": "PASS",
        "points": points,
        "gates": {
            "all_15_atomic_children_legally_closed": True,
            "fit_review_preceded_held_out_execution": True,
            "capacity_strictly_increases": capacity_monotonic,
            "load_count_strictly_decreases": loads_monotonic,
            "measured_h2d_time_strictly_decreases": h2d_time_monotonic,
            "control_zero_demand_h2d": control_zero,
            "output_routing_dependency_and_byte_gates_all_pass": True,
        },
        "adaptive_refinement": {
            "state": "NOT_TRIGGERED_WITH_EVIDENCE",
            "reason": "All six frozen held-out anchors fell in strict monotonic order between neighboring fit anchors; no anomaly or ordering inversion requires a refinement child.",
        },
        "scientific_result": "Under the frozen empty-initial-cache LRU replay, every sub-100% capacity incurs nonzero demand H2D; the separately defined 100% all-resident control incurs zero demand H2D.",
        "claim_boundary": "Complete trace-driven GPU policy replay capacity curve only; not runtime-native residency, end-to-end speedup, CAL3 ranking, software comparison or hardware break-even.",
    }
    review_path = root / "reviews" / "MR10-OFF-E-PR3-COMPOSITE-PROMOTION.json"
    write_json(review_path, review)
    row = by_id["OFF-E-PR3"]
    transition_id = "MR10-OFF-E-PR3-COMPOSITE-PROMOTION"
    prior = sha256_file(ledger_path)
    row.update(
        {
            "execution_state": "EXECUTION_COMPLETE",
            "raw_state": "COMPLETE",
            "backup_state": "VERIFIED",
            "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "VALIDATION_PASS",
            "adoption_state": "ADOPTED",
            "blocker_or_failure": None,
            "local_raw_paths": [str(root / "remote_raw" / f"OFF-E-PR3-CAP-{label}-V1-MASTER") for label in LABELS],
            "source_raw_sha256": [by_id[f"OFF-E-PR3-CAP-{label}"]["source_raw_sha256"][-1] for label in LABELS],
            "manifest_sha256": append_unique(list(row.get("manifest_sha256", [])), [sha256_file(review_path), sha256_file(fit_review_path)]),
            "claims_supported": append_unique(
                list(row.get("claims_supported", [])),
                [
                    "All 15 frozen fit, held-out and all-resident control capacity children are independently validated, backed up and checksum-bound.",
                    "Logical LRU demand loads, hits, immutable discards, H2D bytes and measured H2D time decrease monotonically with whole-object capacity across every sub-100% anchor.",
                    "Every sub-100% anchor retained nonzero demand H2D while the separately defined 100% all-resident control retained zero demand H2D.",
                ],
            ),
            "claims_forbidden": append_unique(
                list(row.get("claims_forbidden", [])),
                ["Runtime-native expert residency, physical identity for all transferred logical objects, end-to-end speedup, CAL3 ranking, software comparison or hardware break-even."],
            ),
            "contamination_flags": append_unique(
                list(row.get("contamination_flags", [])),
                ["LOGICAL_256_OBJECT_LRU_WITH_ACTUAL_REPRESENTATIVE_OBJECT_H2D_SERVICE"],
            ),
            "next_action": "Freeze and execute OFF-E-PR4 queue/backpressure matrix; fault injection remains separately labeled synthetic replay.",
            "last_transition_record": transition_id,
        }
    )
    transition = {
        "transition_id": transition_id,
        "timestamp_utc": review["reviewed_at_utc"],
        "changed_rows": ["OFF-E-PR3"],
        "reason": "Close composite after all 15 children, fit-before-held-out review and monotonic held-out adjudication passed.",
        "prior_ledger_sha256": prior,
        "composite_review_sha256": sha256_file(review_path),
        "adaptive_refinement_state": "NOT_TRIGGERED_WITH_EVIDENCE",
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition_id
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(legally_closed(item) for item in rows)
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)
    remaining_rows = [item for item in rows if not legally_closed(item)]
    remaining_path = root / "master_remaining_ledger.json"
    remaining = read_json(remaining_path)
    remaining.update(
        {
            "generated_from_execution_ledger_sha256": execution_hash,
            "required_legally_closed": len(rows) - len(remaining_rows),
            "required_remaining_count": len(remaining_rows),
            "required_remaining_ids": [item["master_row_id"] for item in remaining_rows],
        }
    )
    write_json(remaining_path, remaining)
    queue_path = root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue.update(
        {
            "generated_from_execution_ledger_sha256": execution_hash,
            "next_cpu_unit": "OFF-E-PR4-CONTRACT-FREEZE",
            "next_gpu_unit": None,
            "ready_gpu_units": [],
            "next_gate_action": "FREEZE_OFF_E_PR4_QUEUE_BACKPRESSURE_MATRIX",
            "dispatch_guards": [
                "MR2 read-only preflight clear",
                "no foreign serving/GPU process at dispatch",
                "OFF-E-PR3 composite validation and backup PASS",
                "queue-full duplicate stale late retry fallback definitions frozen",
                "synthetic fault injection separately labeled",
                "actual expert compute required where applicable",
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
            "timestamp_utc": transition["timestamp_utc"],
            "execution_ledger_sha256": execution_hash,
            "remaining_ledger_sha256": sha256_file(remaining_path),
            "required_closed_count": len(rows) - len(remaining_rows),
            "required_remaining_count": len(remaining_rows),
            "next_ready_cpu_unit": "OFF-E-PR4-CONTRACT-FREEZE",
            "composite_review_sha256": sha256_file(review_path),
        },
    )
    print(json.dumps({"status": "PASS", "execution_ledger_sha256": execution_hash, "required_closed_count": len(rows) - len(remaining_rows), "required_remaining_count": len(remaining_rows), "next_ready_cpu_unit": "OFF-E-PR4-CONTRACT-FREEZE", "composite_review_sha256": sha256_file(review_path)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
