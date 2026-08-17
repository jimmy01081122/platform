#!/usr/bin/env python3
"""Promote one verified OFF-E-PR3 atomic capacity child."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from promote_combined_master_swap_k1_v5 import (
    aggregate_hash,
    append_unique,
    evidence_file_hashes,
    legally_closed,
    now_utc,
    read_json,
    sha256_file,
    write_json,
)


ORDER = [
    "025", "050", "075", "080", "085", "090", "095", "099", "100",
    "0375", "0625", "0825", "0875", "0925", "097",
]


def add_once(records: list[dict], record: dict) -> None:
    if not any(item.get("attempt_id") == record["attempt_id"] for item in records):
        records.append(record)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    args = parser.parse_args()
    local = args.local_attempt_root
    audit = read_json(local / "off_e_pr3_point_audit.json")
    if audit.get("status") != "PASS" or not all(audit.get("gates", {}).values()):
        raise SystemExit("OFF-E-PR3 point audit is not PASS")
    cell = audit["cell_id"]
    label = cell.rsplit("-", 1)[1]
    if label not in ORDER:
        raise SystemExit(f"unexpected capacity label: {label}")
    attempt_id = local.name
    raw_hash = aggregate_hash(evidence_file_hashes(local))
    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    row = next(item for item in rows if item["master_row_id"] == cell)
    transition_id = f"MR10-{cell}-{attempt_id}-PROMOTION"
    for key, value in (
        ("attempt_ids", attempt_id),
        ("remote_raw_paths", args.remote_attempt_root),
        ("local_raw_paths", str(local)),
        ("source_raw_sha256", raw_hash),
    ):
        if value not in row.setdefault(key, []):
            row[key].append(value)
    append_unique(
        row.setdefault("manifest_sha256", []),
        [
            sha256_file(local / "SHA256SUMS"),
            sha256_file(local / "off_e_pr3_point_audit.json"),
            audit["contract_sha256"],
        ],
    )
    metrics = audit["metrics"]
    row.update(
        {
            "execution_state": "EXECUTION_COMPLETE",
            "raw_state": "COMPLETE",
            "backup_state": "VERIFIED",
            "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "VALIDATION_PASS",
            "adoption_state": "ADOPTED",
            "blocker_or_failure": None,
            "frozen_variables": {
                "capacity_objects": metrics["capacity_objects"],
                "capacity_bytes": metrics["capacity_bytes"],
                "routing_sha256": audit["routing_sha256"],
                "policy": "DETERMINISTIC_LRU_EMPTY_INITIAL_CACHE",
                "expert_object_bytes": 352321536,
            },
            "claims_supported": append_unique(
                list(row.get("claims_supported", [])),
                [
                    f"{cell} preserved exact measured routing, whole-object capacity, LRU load/hit/discard and byte conservation.",
                    f"{metrics['demand_load_count']} actual object-sized H2D service operations ({metrics['h2d_bytes']} bytes) completed before actual FusedMoE compute with zero D2H writeback.",
                    "Final output tokens and routed-expert array matched the frozen all-resident control.",
                ],
            ),
            "claims_forbidden": append_unique(
                list(row.get("claims_forbidden", [])),
                [
                    "Runtime-native expert residency, physical movement identity for every logical object, end-to-end speedup, CAL3 ranking, or hardware break-even from this point alone."
                ],
            ),
            "contamination_flags": append_unique(
                list(row.get("contamination_flags", [])),
                ["LOGICAL_256_OBJECT_LRU_WITH_ACTUAL_REPRESENTATIVE_OBJECT_H2D_SERVICE"],
            ),
            "next_action": "Retain raw point; continue the frozen capacity order without interpreting held-out results early.",
            "last_transition_record": transition_id,
        }
    )
    next_label = ORDER[ORDER.index(label) + 1] if label != ORDER[-1] else None
    transition = {
        "transition_id": transition_id,
        "timestamp_utc": now_utc(),
        "changed_rows": [cell],
        "reason": "Promote verified compute-integrated capacity replay atomic child.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": attempt_id,
        "raw_file_set_sha256": raw_hash,
        "metrics": metrics,
        "remote_local_hashes_verified": True,
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition_id
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(legally_closed(item) for item in rows)
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)
    inventory_path = root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    add_once(
        inventory.setdefault("off_e_pr3_capacity_attempts", []),
        {
            "attempt_id": attempt_id,
            "cell_id": cell,
            "fit_role": audit["fit_role"],
            "status": "VALIDATION_PASS",
            "remote_raw_path": args.remote_attempt_root,
            "local_raw_path": str(local),
            "raw_file_set_sha256": raw_hash,
            "metrics": metrics,
        },
    )
    write_json(inventory_path, inventory)
    backup_path = root / "local_backup_manifest.json"
    backup = read_json(backup_path)
    add_once(
        backup.setdefault("phase7_attempt_backups", []),
        {
            "attempt_id": attempt_id,
            "remote_attempt": args.remote_attempt_root,
            "local_attempt": str(local),
            "status": "VERIFIED_RAW_VALIDATION_PASS",
            "file_set_sha256": raw_hash,
        },
    )
    write_json(backup_path, backup)
    remaining = [item for item in rows if not legally_closed(item)]
    write_json(
        root / "master_remaining_ledger.json",
        {
            "schema_version": "phase7-combined-master-remaining-ledger-v1",
            "master_campaign_id": ledger["master_campaign_id"],
            "generated_from_execution_ledger_sha256": execution_hash,
            "required_total": len(rows),
            "required_legally_closed": len(rows) - len(remaining),
            "required_remaining_count": len(remaining),
            "required_remaining_ids": [item["master_row_id"] for item in remaining],
            "blocked_rows": [
                {"id": item["master_row_id"], "reason": item["blocker_or_failure"]}
                for item in remaining
                if item.get("blocker_or_failure")
            ],
            "conditional_pending_count": sum(item.get("trigger_state") == "PENDING" for item in remaining),
            "conditional_pending_ids": [
                item["master_row_id"] for item in remaining if item.get("trigger_state") == "PENDING"
            ],
            "phase7_status": ledger["status"],
        },
    )
    next_cell = (
        "OFF-E-PR3-FIT-REVIEW"
        if label == "100"
        else f"OFF-E-PR3-CAP-{next_label}"
        if next_label
        else "OFF-E-PR3"
    )
    cpu_gate = label in {"100", "097"}
    queue_path = root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue.update(
        {
            "generated_from_execution_ledger_sha256": execution_hash,
            "next_cpu_unit": next_cell if cpu_gate else None,
            "next_gpu_unit": None if cpu_gate else next_cell,
            "ready_gpu_units": [] if cpu_gate else [next_cell],
            "next_gate_action": (
                "REVIEW_FIT_AND_CONTROL_BEFORE_HELD_OUT_UNLOCK"
                if label == "100"
                else "REVIEW_ALL_CAPACITY_CHILDREN_AND_CLOSE_COMPOSITE"
                if label == "097"
                else "CONTINUE_FROZEN_OFF_E_PR3_CAPACITY_ORDER"
            ),
            "dispatch_guards": [
                "MR2 read-only preflight clear",
                "no foreign serving/GPU process at dispatch",
                "prior capacity child validated and locally backed up",
                "fit points and 100% control before held-out unlock",
                "no policy/capacity/trace changes",
                "actual expert compute required",
                "no filler workload",
                "raw namespace independent",
            ],
        }
    )
    write_json(queue_path, queue)
    review_name = f"{transition_id}.json"
    write_json(
        root / "reviews" / review_name,
        {
            "schema_version": "phase7-combined-master-off-e-pr3-point-review-v1",
            "reviewed_at_utc": transition["timestamp_utc"],
            "cell_id": cell,
            "validation_state": "VALIDATION_PASS",
            "metrics": metrics,
            "raw_file_set_sha256": raw_hash,
            "next_ready_unit": next_cell,
        },
    )
    write_json(
        root / "checkpoints" / review_name,
        {
            "schema_version": "phase7-combined-master-checkpoint-v1",
            "checkpoint_id": transition_id,
            "timestamp_utc": transition["timestamp_utc"],
            "execution_ledger_sha256": execution_hash,
            "remaining_ledger_sha256": sha256_file(root / "master_remaining_ledger.json"),
            "required_closed_count": len(rows) - len(remaining),
            "required_remaining_count": len(remaining),
            "next_ready_gpu_unit": None if cpu_gate else next_cell,
            "next_ready_cpu_unit": next_cell if cpu_gate else None,
            "raw_file_set_sha256": raw_hash,
        },
    )
    print(
        json.dumps(
            {
                "cell_id": cell,
                "execution_ledger_sha256": execution_hash,
                "required_closed_count": len(rows) - len(remaining),
                "required_remaining_count": len(remaining),
                "next_ready_gpu_unit": None if cpu_gate else next_cell,
                "next_ready_cpu_unit": next_cell if cpu_gate else None,
                "raw_file_set_sha256": raw_hash,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
