#!/usr/bin/env python3
"""Promote OFF-E-RT0 validated negative capability evidence and children."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from promote_combined_master_swap_k1_v5 import (
    aggregate_hash, append_unique, checksum_mismatches, evidence_file_hashes,
    legally_closed, now_utc, read_json, sha256_file, write_json,
)

EXPECTED_CONTRACT_SHA256 = "68425d7bcdb8fcd19ef8f2613e8699f2c0eb7fd9234f89934d8ec455b28dd7f9"
CHILDREN = ("OFF-E-RT1", "OFF-E-RT2", "OFF-E-RT3")


def append_record_once(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    if not any(item.get("attempt_id") == record.get("attempt_id") for item in records):
        records.append(record)


def add_value(row: dict[str, Any], key: str, value: str) -> None:
    values = row.setdefault(key, [])
    if value not in values:
        values.append(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--runner-dir-name", required=True)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    parser.add_argument("--remote-manifest-sha256", required=True)
    parser.add_argument("--remote-stdout-sha256", required=True)
    parser.add_argument("--remote-stderr-sha256", required=True)
    args = parser.parse_args()
    runner = args.local_attempt_root / "runner_runs" / args.runner_dir_name
    if [path for path in (args.local_attempt_root / "runner_runs").iterdir() if path.is_dir()] != [runner]:
        raise SystemExit("OFF-E-RT0 attempt must contain exactly one runner")
    required = (
        "status.json", "result.json", "manifest.json", "requested_engine_args.json",
        "requests.jsonl", "resolved_runtime.json", "off_e_rt0_capability_audit.json",
        "off_e_rt0_contract.json", "off_e_rt0_terminal.json", "stdout.log",
        "stderr.log", "SHA256SUMS",
    )
    missing = [name for name in required if not (runner / name).is_file()]
    if missing or checksum_mismatches(runner):
        raise SystemExit(f"OFF-E-RT0 raw/checksum failure: missing={missing} mismatches={checksum_mismatches(runner)}")
    for path, expected in (
        (runner / "SHA256SUMS", args.remote_manifest_sha256),
        (runner / "stdout.log", args.remote_stdout_sha256),
        (runner / "stderr.log", args.remote_stderr_sha256),
    ):
        if sha256_file(path) != expected:
            raise SystemExit(f"OFF-E-RT0 remote/local mismatch: {path.name}")
    status = read_json(runner / "status.json")
    result = read_json(runner / "result.json")
    terminal = read_json(runner / "off_e_rt0_terminal.json")
    audit = read_json(runner / "off_e_rt0_capability_audit.json")
    contract = read_json(runner / "off_e_rt0_contract.json")
    engine = read_json(runner / "requested_engine_args.json")
    records = [json.loads(line) for line in (runner / "requests.jsonl").read_text().splitlines() if line]
    routing_json = list((runner / "routing").glob("*.json"))
    routing_npy = list((runner / "routing").glob("*.npy"))
    if (
        status.get("status") != "PASS" or status.get("total_completed_requests") != 1
        or result.get("input_token_count") != 128 or result.get("output_token_count") != 32
        or result.get("finish_reason") != "length" or len(records) != 1
        or records[0].get("input_token_count") != 128
        or records[0].get("output_token_count") != 32
        or records[0].get("finish_reason") != "length"
        or len(routing_json) != 1 or len(routing_npy) != 1
    ):
        raise SystemExit("OFF-E-RT0 request/routing correctness gate failed")
    if (
        terminal.get("status") != "PASS_NEGATIVE_EVIDENCE"
        or terminal.get("capability_status") != "RUNTIME_EXPERT_OFFLOAD_UNAVAILABLE_WITH_CONSEQUENCE"
        or audit.get("capability_status") != "RUNTIME_EXPERT_OFFLOAD_UNAVAILABLE_WITH_CONSEQUENCE"
        or audit.get("explicit_dynamic_expert_residency_parameters") != []
        or contract.get("contract_state") != "FROZEN_BEFORE_EXECUTION"
        or sha256_file(runner / "off_e_rt0_contract.json") != EXPECTED_CONTRACT_SHA256
    ):
        raise SystemExit("OFF-E-RT0 negative capability adjudication gate failed")
    expected_sources = {item["path"]: item["sha256"] for item in contract["source_contract"]}
    if audit.get("source_hashes") != expected_sources:
        raise SystemExit("OFF-E-RT0 source contract mismatch")
    if (
        engine.get("enable_return_routed_experts") is not True
        or engine.get("enable_expert_parallel") is not False
        or engine.get("cpu_offload_gb") != 0
    ):
        raise SystemExit("OFF-E-RT0 canonical engine gate failed")
    raw_hashes = evidence_file_hashes(args.local_attempt_root)
    raw_aggregate = aggregate_hash(raw_hashes)
    if raw_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"OFF-E-RT0 raw aggregate mismatch: {raw_aggregate}")

    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    transition_id = f"MR7-OFF-E-RT0-{args.attempt_id}-NEGATIVE-PROMOTION"
    parent = next(row for row in rows if row["master_row_id"] == "OFF-E-RT0")
    parent.update({
        "execution_state": "EXECUTION_COMPLETE", "raw_state": "COMPLETE",
        "backup_state": "VERIFIED", "review_state": "REVIEW_WITH_LIMITATION",
        "validation_state": "NEGATIVE_EVIDENCE", "adoption_state": "ADOPTED",
        "blocker_or_failure": None,
        "claims_supported": append_unique(list(parent.get("claims_supported", [])), [
            "Installed vLLM 0.23.0 exposes generic UVA/static layer-prefetch weight offload, RL weight transfer and EPLB, but no route-conditioned object-observable dynamic expert host/device residency API.",
            "A canonical 128-to-32 routing canary completed with exact output and routing evidence under the source-bound build.",
        ]),
        "claims_forbidden": append_unique(list(parent.get("claims_forbidden", [])), [
            "Runtime-native dynamic expert offload availability or performance.",
            "Relabeling cpu_offload_gb, static layer prefetch, RL weight transfer, EPLB, memcpy replay or synthetic delay as dynamic expert residency.",
        ]),
        "contamination_flags": append_unique(list(parent.get("contamination_flags", [])), [
            "OFF_E_RT0_VALIDATED_NEGATIVE_CAPABILITY_EVIDENCE",
            "GENERIC_WEIGHT_OFFLOAD_FEATURES_NOT_DYNAMIC_EXPERT_RESIDENCY",
        ]),
        "next_action": "Continue required OFF-E-PR compute-integrated replay and shared-fabric OFFKV; runtime children RT1/2/3 are not triggered.",
        "last_transition_record": transition_id,
    })
    affected = [parent]
    for child_id in CHILDREN:
        child = next(row for row in rows if row["master_row_id"] == child_id)
        child.update({
            "execution_state": "NOT_RUN", "raw_state": "COMPLETE",
            "backup_state": "VERIFIED", "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "NEGATIVE_EVIDENCE", "adoption_state": "NOT_APPLICABLE",
            "trigger_state": "NOT_TRIGGERED_WITH_EVIDENCE", "blocker_or_failure": None,
            "claims_supported": append_unique(list(child.get("claims_supported", [])), [
                f"{child_id} is not triggered because OFF-E-RT0 found no route-conditioned dynamic expert residency path in the source-bound runtime."
            ]),
            "claims_forbidden": append_unique(list(child.get("claims_forbidden", [])), [
                f"Any runtime-native {child_id} expert-offload behavior or performance claim."
            ]),
            "next_action": "No runtime execution; retain OFF-E-RT0 negative evidence and continue OFF-E-PR.",
            "last_transition_record": transition_id,
        })
        affected.append(child)
    for row in affected:
        for key, value in (
            ("attempt_ids", args.attempt_id), ("remote_raw_paths", args.remote_attempt_root),
            ("local_raw_paths", str(args.local_attempt_root)), ("source_raw_sha256", raw_aggregate),
        ):
            add_value(row, key, value)
        append_unique(row.setdefault("manifest_sha256", []), [
            args.remote_manifest_sha256, args.remote_stdout_sha256, args.remote_stderr_sha256,
            sha256_file(runner / "off_e_rt0_capability_audit.json"),
            sha256_file(runner / "off_e_rt0_contract.json"),
        ])
    transition = {
        "transition_id": transition_id, "timestamp_utc": now_utc(),
        "changed_rows": ["OFF-E-RT0", *CHILDREN],
        "reason": "Promote source/API-bound runtime expert-offload negative evidence and resolve conditional children as not triggered.",
        "prior_ledger_sha256": prior_hash, "attempt_id": args.attempt_id,
        "capability_status": audit["capability_status"], "raw_file_count": len(raw_hashes),
        "raw_file_set_sha256": raw_aggregate, "remote_local_hashes_verified": True,
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition_id
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(1 for row in rows if legally_closed(row))
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    trigger_path = root / "trigger_adjudication.json"
    trigger = read_json(trigger_path)
    for entry in trigger.get("entries", []):
        if entry.get("trigger_id") in CHILDREN:
            entry["trigger_state"] = "NOT_TRIGGERED_WITH_EVIDENCE"
            append_unique(entry.setdefault("observed_evidence", []), [
                f"{args.attempt_id}: no route-conditioned object-observable dynamic expert residency path in installed vLLM 0.23.0."
            ])
            append_unique(entry.setdefault("source_evidence_hashes", []), [raw_aggregate])
    write_json(trigger_path, trigger)
    inventory_path = root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    append_record_once(inventory.setdefault("off_e_rt0_capability_attempts", []), {
        "attempt_id": args.attempt_id, "capability_status": audit["capability_status"],
        "remote_raw_path": args.remote_attempt_root, "local_raw_path": str(args.local_attempt_root),
        "raw_file_count": len(raw_hashes), "raw_file_set_sha256": raw_aggregate,
        "manifest_sha256": args.remote_manifest_sha256, "status": "VALIDATED_NEGATIVE_EVIDENCE",
    })
    write_json(inventory_path, inventory)
    backup_path = root / "local_backup_manifest.json"
    backup = read_json(backup_path)
    append_record_once(backup.setdefault("phase7_attempt_backups", []), {
        "attempt_id": args.attempt_id, "remote_attempt": args.remote_attempt_root,
        "local_attempt": str(args.local_attempt_root), "status": "VERIFIED_RAW_NEGATIVE_EVIDENCE",
        "file_count": len(raw_hashes), "file_set_sha256": raw_aggregate,
        "manifest_sha256": args.remote_manifest_sha256,
    })
    write_json(backup_path, backup)
    gap_path = root / "gap_register.json"
    gap = read_json(gap_path)
    gap["entries"] = [item for item in gap.setdefault("entries", []) if item.get("gap_id") != "GAP-OFF-E-RUNTIME-CAPABILITY"]
    gap["entries"].append({
        "gap_id": "GAP-OFF-E-RUNTIME-CAPABILITY", "status": "CLOSED_WITH_NEGATIVE_EVIDENCE",
        "source": str(args.local_attempt_root),
        "consequence": "Runtime-native OFF-E claims are forbidden; OFF-E-PR compute-integrated replay and shared-fabric OFFKV remain required.",
    })
    write_json(gap_path, gap)
    claims_path = root / "claim_boundary_register.json"
    claims = read_json(claims_path)
    append_unique(claims.setdefault("claims_allowed_now", []), [
        "OFF-E-RT0 validated absence of route-conditioned dynamic expert residency in the source-bound vLLM runtime"
    ])
    append_unique(claims.setdefault("claims_forbidden_now", []), [
        "Runtime-native OFF-E-RT1/2/3 behavior or performance; use correctly labeled compute-integrated replay only"
    ])
    write_json(claims_path, claims)

    remaining_rows = [row for row in rows if not legally_closed(row)]
    conditional = [row["master_row_id"] for row in remaining_rows if row.get("trigger_state") == "PENDING"]
    blocked = [{"id": row["master_row_id"], "reason": row["blocker_or_failure"]} for row in remaining_rows if row.get("blocker_or_failure")]
    write_json(root / "master_remaining_ledger.json", {
        "schema_version": "phase7-combined-master-remaining-ledger-v1",
        "master_campaign_id": ledger["master_campaign_id"],
        "generated_from_execution_ledger_sha256": execution_hash,
        "required_total": len(rows), "required_legally_closed": len(rows) - len(remaining_rows),
        "required_remaining_count": len(remaining_rows),
        "required_remaining_ids": [row["master_row_id"] for row in remaining_rows],
        "blocked_rows": blocked, "conditional_pending_count": len(conditional),
        "conditional_pending_ids": conditional, "phase7_status": ledger["status"],
    })
    queue_path = root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue.update({
        "generated_from_execution_ledger_sha256": execution_hash,
        "next_gpu_unit": "OFF-W0", "ready_gpu_units": ["OFF-W0"],
        "next_gate_action": "FREEZE_AND_RUN_OFF_W0_CAPABILITY_CANARY",
        "dispatch_guards": [
            "MR2 read-only preflight clear", "no foreign serving/GPU process at dispatch",
            "new-session four-guard canary validated and locally backed up",
            "CATALOG-X0 validated", "OFF-E-RT0 negative evidence validated and RT1/2/3 resolved",
            "OFF-W0 is a distinct generic weight-offload capability/semantics canary",
            "no filler workload", "raw namespace independent",
        ],
    })
    write_json(queue_path, queue)
    review_name = f"MR7-OFF-E-RT0-{args.attempt_id}-NEGATIVE-PROMOTION.json"
    review = {
        "schema_version": "phase7-combined-master-off-e-rt0-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"], "attempt_id": args.attempt_id,
        "capability_status": audit["capability_status"], "request_correctness": "PASS",
        "routing_evidence": "PASS", "raw_backup": "VERIFIED",
        "raw_file_set_sha256": raw_aggregate, "resolved_children": list(CHILDREN),
        "validation_state": "NEGATIVE_EVIDENCE", "review_state": "REVIEW_WITH_LIMITATION",
        "next_ready_unit": "OFF-W0",
    }
    write_json(root / "reviews" / review_name, review)
    write_json(root / "checkpoints" / review_name, {
        "schema_version": "phase7-combined-master-checkpoint-v1",
        "checkpoint_id": transition_id, "timestamp_utc": transition["timestamp_utc"],
        "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(root / "master_remaining_ledger.json"),
        "required_closed_count": len(rows) - len(remaining_rows),
        "required_remaining_count": len(remaining_rows), "next_ready_gpu_unit": "OFF-W0",
        "raw_file_set_sha256": raw_aggregate,
    })
    print(json.dumps({
        "attempt_id": args.attempt_id, "capability_status": audit["capability_status"],
        "resolved_children": list(CHILDREN), "raw_file_set_sha256": raw_aggregate,
        "execution_ledger_sha256": execution_hash,
        "required_closed_count": len(rows) - len(remaining_rows),
        "required_remaining_count": len(remaining_rows), "next_ready_gpu_unit": "OFF-W0",
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
