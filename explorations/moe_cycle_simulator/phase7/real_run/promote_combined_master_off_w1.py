#!/usr/bin/env python3
"""Validate and promote OFF-W1 disabled-control equivalence evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from promote_combined_master_swap_k1_v5 import (
    aggregate_hash, append_unique, checksum_mismatches, evidence_file_hashes,
    legally_closed, now_utc, read_json, sha256_file, write_json,
)

EXPECTED_CONTRACT_SHA256 = "fcf9c490cefc7222c577edf00fbe354a3a8b9348f62296829341c16ff25abdee"
EXPECTED_ROUTING_SHA256 = "0a9225ec4b302ea237bc21fe532fa1efb790905bbc5832e2ea5dab72b20e50d6"


def append_record_once(records, record):
    if not any(item.get("attempt_id") == record["attempt_id"] for item in records):
        records.append(record)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--runner-dir-name", required=True)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    parser.add_argument("--remote-manifest-sha256", required=True)
    parser.add_argument("--remote-stdout-sha256", required=True)
    parser.add_argument("--remote-stderr-sha256", required=True)
    args = parser.parse_args()
    runner = args.local_attempt_root / "runner_runs" / args.runner_dir_name
    if [path for path in (args.local_attempt_root / "runner_runs").iterdir() if path.is_dir()] != [runner]:
        raise SystemExit("OFF-W1 attempt must contain exactly one runner")
    if checksum_mismatches(runner):
        raise SystemExit("OFF-W1 SHA256SUMS verification failed")
    for path, expected in (
        (runner / "SHA256SUMS", args.remote_manifest_sha256),
        (runner / "stdout.log", args.remote_stdout_sha256),
        (runner / "stderr.log", args.remote_stderr_sha256),
    ):
        if sha256_file(path) != expected:
            raise SystemExit(f"OFF-W1 remote/local mismatch: {path.name}")
    raw_hash = aggregate_hash(evidence_file_hashes(args.local_attempt_root))
    if raw_hash != args.expected_aggregate_sha256:
        raise SystemExit(f"OFF-W1 aggregate mismatch: {raw_hash}")
    status = read_json(runner / "status.json")
    result = read_json(runner / "result.json")
    engine = read_json(runner / "requested_engine_args.json")
    terminal = read_json(runner / "off_w1_terminal.json")
    audit = read_json(runner / "off_w1_disabled_control_audit.json")
    routing_npy = list((runner / "routing").glob("*.npy"))
    if (
        status.get("status") != "PASS" or status.get("total_completed_requests") != 1
        or result.get("input_token_count") != 128 or result.get("output_token_count") != 32
        or result.get("finish_reason") != "length" or engine.get("cpu_offload_gb") != 0.0
        or engine.get("cpu_offload_params") != [] or engine.get("offload_backend") != "auto"
        or terminal.get("status") != "PASS"
        or terminal.get("control_status") != "DISABLED_CONTROL_EQUIVALENCE_PASS"
        or audit.get("requested_disabled_gate") != "PASS"
        or audit.get("resolved_disabled_gate") != "PASS"
        or audit.get("no_runtime_offload_gate") != "PASS"
        or audit.get("request_correctness") != "PASS"
        or audit.get("off_w0_output_and_routing_equivalence") != "PASS"
        or audit.get("gpu_terminal_cleanup", {}).get("compute_apps") != []
        or len(routing_npy) != 1 or sha256_file(routing_npy[0]) != EXPECTED_ROUTING_SHA256
        or sha256_file(runner / "off_w1_contract.json") != EXPECTED_CONTRACT_SHA256
    ):
        raise SystemExit("OFF-W1 validation gate failed")

    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    row = next(item for item in rows if item["master_row_id"] == "OFF-W1")
    for key, value in (
        ("attempt_ids", "OFF-W1-V1-MASTER"), ("remote_raw_paths", args.remote_attempt_root),
        ("local_raw_paths", str(args.local_attempt_root)), ("source_raw_sha256", raw_hash),
    ):
        values = row.setdefault(key, [])
        if value not in values: values.append(value)
    append_unique(row.setdefault("manifest_sha256", []), [
        args.remote_manifest_sha256, args.remote_stdout_sha256, args.remote_stderr_sha256,
        sha256_file(runner / "off_w1_disabled_control_audit.json"), EXPECTED_CONTRACT_SHA256,
    ])
    transition_id = "MR11-OFF-W1-OFF-W1-V1-MASTER-PROMOTION"
    row.update({
        "execution_state": "EXECUTION_COMPLETE", "raw_state": "COMPLETE",
        "backup_state": "VERIFIED", "review_state": "REVIEW_WITH_LIMITATION",
        "validation_state": "VALIDATION_PASS", "adoption_state": "ADOPTED",
        "blocker_or_failure": None,
        "claims_supported": append_unique(list(row.get("claims_supported", [])), [
            "The source-bound no-offload control completed the exact 128-to-32 canary with output token IDs and routed-expert array identical to OFF-W0.",
            "Requested/resolved CPU weight offload was disabled and no positive runtime offloaded-parameter value was observed.",
        ]),
        "claims_forbidden": append_unique(list(row.get("claims_forbidden", [])), [
            "Performance or speedup conclusions from this single correctness control.",
            "Dynamic expert residency conclusions from OFF-W1.",
        ]),
        "next_action": "Freeze and run OFF-W2 preregistered byte-budget sweep with actual runtime-reported offload bytes.",
        "last_transition_record": transition_id,
    })
    transition = {
        "transition_id": transition_id, "timestamp_utc": now_utc(), "changed_rows": ["OFF-W1"],
        "reason": "Promote frozen no-offload correctness/routing equivalence control.",
        "prior_ledger_sha256": prior_hash, "attempt_id": "OFF-W1-V1-MASTER",
        "raw_file_set_sha256": raw_hash, "remote_local_hashes_verified": True,
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition_id
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(1 for item in rows if legally_closed(item))
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    inventory_path = root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    append_record_once(inventory.setdefault("off_w1_disabled_control_attempts", []), {
        "attempt_id": "OFF-W1-V1-MASTER", "status": "VALIDATION_PASS",
        "remote_raw_path": args.remote_attempt_root, "local_raw_path": str(args.local_attempt_root),
        "raw_file_set_sha256": raw_hash, "routing_array_sha256": EXPECTED_ROUTING_SHA256,
    })
    write_json(inventory_path, inventory)
    backup_path = root / "local_backup_manifest.json"
    backup = read_json(backup_path)
    append_record_once(backup.setdefault("phase7_attempt_backups", []), {
        "attempt_id": "OFF-W1-V1-MASTER", "remote_attempt": args.remote_attempt_root,
        "local_attempt": str(args.local_attempt_root), "status": "VERIFIED_RAW_VALIDATION_PASS",
        "file_set_sha256": raw_hash,
    })
    write_json(backup_path, backup)

    remaining = [item for item in rows if not legally_closed(item)]
    conditional = [item["master_row_id"] for item in remaining if item.get("trigger_state") == "PENDING"]
    blocked = [{"id": item["master_row_id"], "reason": item["blocker_or_failure"]} for item in remaining if item.get("blocker_or_failure")]
    write_json(root / "master_remaining_ledger.json", {
        "schema_version": "phase7-combined-master-remaining-ledger-v1",
        "master_campaign_id": ledger["master_campaign_id"],
        "generated_from_execution_ledger_sha256": execution_hash,
        "required_total": len(rows), "required_legally_closed": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "required_remaining_ids": [item["master_row_id"] for item in remaining],
        "blocked_rows": blocked, "conditional_pending_count": len(conditional),
        "conditional_pending_ids": conditional, "phase7_status": ledger["status"],
    })
    queue_path = root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue.update({
        "generated_from_execution_ledger_sha256": execution_hash,
        "next_gpu_unit": "OFF-W2", "ready_gpu_units": ["OFF-W2"],
        "next_gate_action": "FREEZE_AND_RUN_OFF_W2_BYTE_BUDGET_SWEEP",
        "dispatch_guards": [
            "MR2 read-only preflight clear", "no foreign serving/GPU process at dispatch",
            "OFF-W0 capability and OFF-W1 disabled control validated and backed up",
            "OFF-W2 low/mid/high and held-out budgets frozen before dispatch",
            "actual runtime-reported offload bytes retained", "no filler workload", "raw namespace independent",
        ],
    })
    write_json(queue_path, queue)
    review_name = "MR11-OFF-W1-OFF-W1-V1-MASTER-PROMOTION.json"
    write_json(root / "reviews" / review_name, {
        "schema_version": "phase7-combined-master-off-w1-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"], "validation_state": "VALIDATION_PASS",
        "disabled_state": "PASS", "request_correctness": "PASS", "routing_equivalence": "PASS",
        "raw_backup": "VERIFIED", "raw_file_set_sha256": raw_hash, "next_ready_unit": "OFF-W2",
    })
    write_json(root / "checkpoints" / review_name, {
        "schema_version": "phase7-combined-master-checkpoint-v1", "checkpoint_id": transition_id,
        "timestamp_utc": transition["timestamp_utc"], "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(root / "master_remaining_ledger.json"),
        "required_closed_count": len(rows) - len(remaining), "required_remaining_count": len(remaining),
        "next_ready_gpu_unit": "OFF-W2", "raw_file_set_sha256": raw_hash,
    })
    print(json.dumps({
        "execution_ledger_sha256": execution_hash, "raw_file_set_sha256": raw_hash,
        "required_closed_count": len(rows) - len(remaining), "required_remaining_count": len(remaining),
        "next_ready_gpu_unit": "OFF-W2",
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
