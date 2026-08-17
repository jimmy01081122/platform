#!/usr/bin/env python3
"""Promote OFF-W0 positive capability evidence and trigger OFF-W1/2/3."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from promote_combined_master_swap_k1_v5 import (
    aggregate_hash, append_unique, checksum_mismatches, evidence_file_hashes,
    legally_closed, now_utc, read_json, sha256_file, write_json,
)

EXPECTED_CONTRACT_SHA256 = "38eab12307b667ebb5b3a754515802f26641ac075bb13a22cfc0138075d5aed1"
CHILDREN = ("OFF-W1", "OFF-W2", "OFF-W3")


def add_value(row: dict[str, Any], key: str, value: str) -> None:
    values = row.setdefault(key, [])
    if value not in values:
        values.append(value)


def append_record_once(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    if not any(item.get("attempt_id") == record.get("attempt_id") for item in records):
        records.append(record)


def only_runner(attempt: Path, name: str) -> Path:
    runner = attempt / "runner_runs" / name
    siblings = [path for path in (attempt / "runner_runs").iterdir() if path.is_dir()]
    if siblings != [runner]:
        raise SystemExit(f"attempt must contain exactly runner {name}: {siblings}")
    return runner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--v1-attempt-root", type=Path, required=True)
    parser.add_argument("--v1-runner-dir-name", required=True)
    parser.add_argument("--v1-expected-aggregate-sha256", required=True)
    parser.add_argument("--v2-attempt-root", type=Path, required=True)
    parser.add_argument("--v2-runner-dir-name", required=True)
    parser.add_argument("--v2-expected-aggregate-sha256", required=True)
    parser.add_argument("--remote-v1-root", required=True)
    parser.add_argument("--remote-v2-root", required=True)
    parser.add_argument("--remote-v2-manifest-sha256", required=True)
    parser.add_argument("--remote-v2-stdout-sha256", required=True)
    parser.add_argument("--remote-v2-stderr-sha256", required=True)
    args = parser.parse_args()

    v1 = only_runner(args.v1_attempt_root, args.v1_runner_dir_name)
    v2 = only_runner(args.v2_attempt_root, args.v2_runner_dir_name)
    if checksum_mismatches(v1) or checksum_mismatches(v2):
        raise SystemExit("OFF-W0 local SHA256SUMS verification failed")
    v1_raw = aggregate_hash(evidence_file_hashes(args.v1_attempt_root))
    v2_raw = aggregate_hash(evidence_file_hashes(args.v2_attempt_root))
    if v1_raw != args.v1_expected_aggregate_sha256 or v2_raw != args.v2_expected_aggregate_sha256:
        raise SystemExit(f"OFF-W0 raw aggregate mismatch: v1={v1_raw} v2={v2_raw}")

    v1_status = read_json(v1 / "status.json")
    v1_failure = read_json(v1 / "failure.json")
    if (
        v1_status.get("status") != "FAIL"
        or v1_status.get("phase") != "engine_load"
        or v1_failure.get("exception_type") != "TypeError"
        or "set is not JSON serializable" not in v1_failure.get("message", "")
    ):
        raise SystemExit("OFF-W0 V1 is not the declared technical serialization failure")

    required = (
        "status.json", "result.json", "manifest.json", "requested_engine_args.json",
        "requests.jsonl", "resolved_runtime.json", "off_w0_capability_audit.json",
        "off_w0_contract.json", "off_w0_terminal.json", "stdout.log", "stderr.log",
        "SHA256SUMS",
    )
    missing = [name for name in required if not (v2 / name).is_file()]
    if missing:
        raise SystemExit(f"OFF-W0 V2 missing evidence: {missing}")
    for path, expected in (
        (v2 / "SHA256SUMS", args.remote_v2_manifest_sha256),
        (v2 / "stdout.log", args.remote_v2_stdout_sha256),
        (v2 / "stderr.log", args.remote_v2_stderr_sha256),
    ):
        if sha256_file(path) != expected:
            raise SystemExit(f"OFF-W0 remote/local mismatch: {path.name}")

    status = read_json(v2 / "status.json")
    result = read_json(v2 / "result.json")
    terminal = read_json(v2 / "off_w0_terminal.json")
    audit = read_json(v2 / "off_w0_capability_audit.json")
    contract = read_json(v2 / "off_w0_contract.json")
    engine = read_json(v2 / "requested_engine_args.json")
    records = [json.loads(line) for line in (v2 / "requests.jsonl").read_text().splitlines() if line]
    routing_json = list((v2 / "routing").glob("*.json"))
    routing_npy = list((v2 / "routing").glob("*.npy"))
    if (
        status.get("status") != "PASS" or status.get("total_completed_requests") != 1
        or result.get("input_token_count") != 128 or result.get("output_token_count") != 32
        or result.get("finish_reason") != "length" or len(records) != 1
        or len(routing_json) != 1 or len(routing_npy) != 1
        or result.get("routing", {}).get("validation_status") != "PASS"
    ):
        raise SystemExit("OFF-W0 V2 request/routing gate failed")
    params = engine.get("cpu_offload_params", [])
    if (
        engine.get("offload_backend") != "uva" or engine.get("cpu_offload_gb") != 1.0
        or "experts" not in params or engine.get("enable_return_routed_experts") is not True
    ):
        raise SystemExit("OFF-W0 V2 requested engine gate failed")
    if (
        terminal.get("status") != "PASS"
        or terminal.get("capability_status") != "RUNTIME_NATIVE_CPU_WEIGHT_OFFLOAD_AVAILABLE"
        or audit.get("capability_status") != "RUNTIME_NATIVE_CPU_WEIGHT_OFFLOAD_AVAILABLE"
        or audit.get("configuration_gate") != "PASS"
        or audit.get("request_correctness") != "PASS"
        or audit.get("runtime_log_offloaded_gib") != 1.75
        or audit.get("gpu_terminal_cleanup", {}).get("compute_apps") != []
        or contract.get("contract_state") != "FROZEN_BEFORE_EXECUTION"
        or sha256_file(v2 / "off_w0_contract.json") != EXPECTED_CONTRACT_SHA256
    ):
        raise SystemExit("OFF-W0 positive capability adjudication gate failed")
    expected_sources = {item["path"]: item["sha256"] for item in contract["source_contract"]}
    if audit.get("source_hashes") != expected_sources:
        raise SystemExit("OFF-W0 source contract mismatch")

    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    transition_id = "MR7-OFF-W0-OFF-W0-V2-MASTER-POSITIVE-PROMOTION"
    parent = next(row for row in rows if row["master_row_id"] == "OFF-W0")
    for key, value in (
        ("attempt_ids", "OFF-W0-V1-MASTER"), ("attempt_ids", "OFF-W0-V2-MASTER"),
        ("remote_raw_paths", args.remote_v1_root), ("remote_raw_paths", args.remote_v2_root),
        ("local_raw_paths", str(args.v1_attempt_root)), ("local_raw_paths", str(args.v2_attempt_root)),
        ("source_raw_sha256", v1_raw), ("source_raw_sha256", v2_raw),
    ):
        add_value(parent, key, value)
    append_unique(parent.setdefault("manifest_sha256", []), [
        sha256_file(v1 / "SHA256SUMS"), args.remote_v2_manifest_sha256,
        args.remote_v2_stdout_sha256, args.remote_v2_stderr_sha256,
        sha256_file(v2 / "off_w0_capability_audit.json"), EXPECTED_CONTRACT_SHA256,
    ])
    parent.setdefault("repair_lineage", []).append({
        "failed_attempt_id": "OFF-W0-V1-MASTER",
        "failure_class": "TECHNICAL_JSON_SERIALIZATION_AFTER_SUCCESSFUL_ENGINE_LOAD",
        "repair": "Serialize resolved_runtime through json_safe and accept the source-defined bare format_gib log payload.",
        "replacement_attempt_id": "OFF-W0-V2-MASTER",
        "scientific_contract_changed": False,
    })
    parent.update({
        "execution_state": "EXECUTION_COMPLETE", "raw_state": "COMPLETE",
        "backup_state": "VERIFIED", "review_state": "REVIEW_WITH_LIMITATION",
        "validation_state": "VALIDATION_PASS", "adoption_state": "ADOPTED",
        "blocker_or_failure": None,
        "claims_supported": append_unique(list(parent.get("claims_supported", [])), [
            "Installed vLLM 0.23.0 executed source-bound UVA CPU weight offload with a 1.0-GiB budget and experts parameter filter; the runtime logged 1.75 GiB actually offloaded under its fused-parameter budget semantics.",
            "The OFF-W0 128-to-32 forced-length canary completed with valid routed-expert evidence and terminal GPU cleanup.",
        ]),
        "claims_forbidden": append_unique(list(parent.get("claims_forbidden", [])), [
            "Route-conditioned dynamic expert residency or expert object movement claims from generic selective weight offload.",
            "Performance, speedup, bandwidth, byte-sweep, or representative-workload claims before OFF-W1/2/3 close.",
        ]),
        "contamination_flags": append_unique(list(parent.get("contamination_flags", [])), [
            "OFF_W0_V1_TECHNICAL_SERIALIZATION_FAILURE_PRESERVED_AND_REPAIRED",
            "GENERIC_WEIGHT_OFFLOAD_NOT_DYNAMIC_EXPERT_RESIDENCY",
        ]),
        "next_action": "Run independently frozen OFF-W1 disabled control, then OFF-W2 budget sweep and OFF-W3 representative profiler comparison.",
        "last_transition_record": transition_id,
    })
    for child_id in CHILDREN:
        child = next(row for row in rows if row["master_row_id"] == child_id)
        child.update({
            "trigger_state": "TRIGGERED", "blocker_or_failure": None,
            "next_action": (
                "Freeze and run the disabled-control contract paired to OFF-W0."
                if child_id == "OFF-W1" else
                "Await OFF-W1, then freeze and run the independent byte-budget sweep."
                if child_id == "OFF-W2" else
                "Await OFF-W1/OFF-W2, then freeze and run the representative profiler comparison."
            ),
            "last_transition_record": transition_id,
        })

    transition = {
        "transition_id": transition_id, "timestamp_utc": now_utc(),
        "changed_rows": ["OFF-W0", *CHILDREN],
        "reason": "Promote source/runtime-bound positive generic CPU weight-offload capability and trigger its independent controls/sweep/comparator.",
        "prior_ledger_sha256": prior_hash,
        "failed_attempt_id": "OFF-W0-V1-MASTER", "replacement_attempt_id": "OFF-W0-V2-MASTER",
        "v1_raw_file_set_sha256": v1_raw, "v2_raw_file_set_sha256": v2_raw,
        "remote_local_hashes_verified": True, "capability_status": audit["capability_status"],
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
            entry["trigger_state"] = "TRIGGERED"
            append_unique(entry.setdefault("observed_evidence", []), [
                "OFF-W0-V2-MASTER: runtime-native UVA selective CPU weight offload available; 1.75 GiB logged and canonical routing canary passed."
            ])
            append_unique(entry.setdefault("source_evidence_hashes", []), [v2_raw])
    write_json(trigger_path, trigger)

    inventory_path = root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    records_out = inventory.setdefault("off_w0_capability_attempts", [])
    append_record_once(records_out, {
        "attempt_id": "OFF-W0-V1-MASTER", "status": "TECHNICAL_FAILURE_PRESERVED",
        "remote_raw_path": args.remote_v1_root, "local_raw_path": str(args.v1_attempt_root),
        "raw_file_set_sha256": v1_raw,
    })
    append_record_once(records_out, {
        "attempt_id": "OFF-W0-V2-MASTER", "status": "VALIDATION_PASS",
        "capability_status": audit["capability_status"], "runtime_log_offloaded_gib": 1.75,
        "remote_raw_path": args.remote_v2_root, "local_raw_path": str(args.v2_attempt_root),
        "raw_file_set_sha256": v2_raw, "manifest_sha256": args.remote_v2_manifest_sha256,
    })
    write_json(inventory_path, inventory)

    backup_path = root / "local_backup_manifest.json"
    backup = read_json(backup_path)
    backups = backup.setdefault("phase7_attempt_backups", [])
    for attempt_id, remote, local, raw, status_value in (
        ("OFF-W0-V1-MASTER", args.remote_v1_root, args.v1_attempt_root, v1_raw, "VERIFIED_RAW_TECHNICAL_FAILURE"),
        ("OFF-W0-V2-MASTER", args.remote_v2_root, args.v2_attempt_root, v2_raw, "VERIFIED_RAW_VALIDATION_PASS"),
    ):
        append_record_once(backups, {
            "attempt_id": attempt_id, "remote_attempt": remote, "local_attempt": str(local),
            "status": status_value, "file_set_sha256": raw,
        })
    write_json(backup_path, backup)

    gap_path = root / "gap_register.json"
    gap = read_json(gap_path)
    gap["entries"] = [item for item in gap.setdefault("entries", []) if item.get("gap_id") != "GAP-OFF-W-CAPABILITY"]
    gap["entries"].append({
        "gap_id": "GAP-OFF-W-CAPABILITY", "status": "CLOSED_POSITIVE_CAPABILITY_TRIGGERED_CHILDREN",
        "source": str(args.v2_attempt_root),
        "consequence": "OFF-W1/2/3 are triggered and remain open; dynamic expert residency claims remain forbidden.",
    })
    write_json(gap_path, gap)
    claims_path = root / "claim_boundary_register.json"
    claims = read_json(claims_path)
    append_unique(claims.setdefault("claims_allowed_now", []), [
        "OFF-W0 source-bound generic/selective UVA CPU weight-offload capability with 1.75 GiB runtime-logged offload"
    ])
    append_unique(claims.setdefault("claims_forbidden_now", []), [
        "OFF-W performance or byte-budget conclusions until OFF-W1/2/3 close",
        "Dynamic expert residency claims from OFF-W generic weight offload",
    ])
    write_json(claims_path, claims)

    remaining = [row for row in rows if not legally_closed(row)]
    conditional = [row["master_row_id"] for row in remaining if row.get("trigger_state") == "PENDING"]
    blocked = [{"id": row["master_row_id"], "reason": row["blocker_or_failure"]} for row in remaining if row.get("blocker_or_failure")]
    write_json(root / "master_remaining_ledger.json", {
        "schema_version": "phase7-combined-master-remaining-ledger-v1",
        "master_campaign_id": ledger["master_campaign_id"],
        "generated_from_execution_ledger_sha256": execution_hash,
        "required_total": len(rows), "required_legally_closed": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "required_remaining_ids": [row["master_row_id"] for row in remaining],
        "blocked_rows": blocked, "conditional_pending_count": len(conditional),
        "conditional_pending_ids": conditional, "phase7_status": ledger["status"],
    })
    queue_path = root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue.update({
        "generated_from_execution_ledger_sha256": execution_hash,
        "next_gpu_unit": "OFF-W1", "ready_gpu_units": ["OFF-W1"],
        "next_gate_action": "FREEZE_AND_RUN_OFF_W1_DISABLED_CONTROL",
        "dispatch_guards": [
            "MR2 read-only preflight clear", "no foreign serving/GPU process at dispatch",
            "new-session four-guard canary validated and locally backed up",
            "OFF-W0 positive capability evidence validated and backed up",
            "OFF-W1 requires an independent frozen disabled-control contract",
            "no filler workload", "raw namespace independent",
        ],
    })
    write_json(queue_path, queue)

    review_name = "MR7-OFF-W0-OFF-W0-V2-MASTER-POSITIVE-PROMOTION.json"
    write_json(root / "reviews" / review_name, {
        "schema_version": "phase7-combined-master-off-w0-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"], "validation_state": "VALIDATION_PASS",
        "capability_status": audit["capability_status"], "runtime_log_offloaded_gib": 1.75,
        "request_correctness": "PASS", "routing_evidence": "PASS", "raw_backup": "VERIFIED",
        "repair_lineage": "OFF-W0-V1-MASTER -> OFF-W0-V2-MASTER",
        "triggered_children": list(CHILDREN), "next_ready_unit": "OFF-W1",
    })
    write_json(root / "checkpoints" / review_name, {
        "schema_version": "phase7-combined-master-checkpoint-v1",
        "checkpoint_id": transition_id, "timestamp_utc": transition["timestamp_utc"],
        "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(root / "master_remaining_ledger.json"),
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining), "next_ready_gpu_unit": "OFF-W1",
        "v1_raw_file_set_sha256": v1_raw, "v2_raw_file_set_sha256": v2_raw,
    })
    print(json.dumps({
        "capability_status": audit["capability_status"], "runtime_log_offloaded_gib": 1.75,
        "triggered_children": list(CHILDREN), "v1_raw_file_set_sha256": v1_raw,
        "v2_raw_file_set_sha256": v2_raw, "execution_ledger_sha256": execution_hash,
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining), "next_ready_gpu_unit": "OFF-W1",
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
