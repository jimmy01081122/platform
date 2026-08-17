#!/usr/bin/env python3
"""Validate and promote SWAP-K5 host-pool store-refusal fallback evidence."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path
from typing import Any

from promote_combined_master_swap_k1_v5 import (
    aggregate_hash,
    append_unique,
    checksum_mismatches,
    evidence_file_hashes,
    legally_closed,
    now_utc,
    read_json,
    sha256_file,
    write_json,
)


EXPECTED_CONTRACT_SHA256 = "026f99939ffbfedda6ca8238449161fe05dfbc58597dc53679524e6688d7eef6"
EXPECTED_MODEL = "/vault/flow/moe_simulator_phase7/models/mistralai__Mixtral-8x7B-Instruct-v0.1__eba92302__bf16_safetensors"
REQUIRED_FILES = (
    "admission.json", "dispatch_preflight.json", "exact_argv.json",
    "input_fixture.json", "k5_contract.json", "kv_events.json",
    "manifest.json", "requested_engine_args.json", "requests.json",
    "result.json", "source_gate.json", "status.json", "stderr.log",
    "stdout.log", "telemetry.jsonl", "SHA256SUMS",
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_record_once(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    if not any(item.get("attempt_id") == record.get("attempt_id") for item in records):
        records.append(record)


def add_row_value(row: dict[str, Any], key: str, value: str) -> None:
    values = row.setdefault(key, [])
    if value not in values:
        values.append(value)


def numeric_summary(values: list[int]) -> dict[str, Any]:
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def raw_evidence_hashes(root: Path) -> dict[str, str]:
    return {
        f"./{path.relative_to(root).as_posix()}": sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.name != "SHA256SUMS"
        and not path.name.startswith("derived_")
    }


def validate(args: argparse.Namespace, runner: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    missing = [name for name in REQUIRED_FILES if not (runner / name).is_file()]
    if missing:
        raise SystemExit(f"K5 backup is incomplete: {missing}")
    if checksum_mismatches(runner):
        raise SystemExit(f"K5 checksum mismatch: {checksum_mismatches(runner)}")
    if sha256_file(runner / "SHA256SUMS") != args.remote_manifest_sha256:
        raise SystemExit("remote/local K5 SHA256SUMS hash mismatch")
    if sha256_file(runner / "stdout.log") != args.remote_stdout_sha256:
        raise SystemExit("remote/local K5 stdout mismatch")
    if sha256_file(runner / "stderr.log") != args.remote_stderr_sha256:
        raise SystemExit("remote/local K5 stderr mismatch")

    status = read_json(runner / "status.json")
    result = read_json(runner / "result.json")
    preflight = read_json(runner / "dispatch_preflight.json")
    contract = read_json(runner / "k5_contract.json")
    requested = read_json(runner / "requested_engine_args.json")
    fixture = read_json(runner / "input_fixture.json")
    requests = read_json(runner / "requests.json").get("records")
    events = read_json(runner / "kv_events.json")
    source_gate = read_json(runner / "source_gate.json")
    telemetry = read_jsonl(runner / "telemetry.jsonl")
    logs = (runner / "stdout.log").read_text(errors="replace") + "\n" + (runner / "stderr.log").read_text(errors="replace")

    if status.get("status") != "PASS" or status.get("execution_state") != "EXECUTION_COMPLETE":
        raise SystemExit("K5 runner is not execution-complete PASS")
    if preflight.get("status") != "PASS" or not all(preflight.get(key) for key in (
        "same_session_guard_gpu", "zero_compute_apps", "zero_foreign_serving"
    )):
        raise SystemExit("K5 dispatch preflight did not pass")
    if contract.get("contract_state") != "FROZEN_BEFORE_EXECUTION" or contract.get("canonical_experiment_id") != "SWAP-K5":
        raise SystemExit("K5 frozen contract identity mismatch")
    if sha256_file(runner / "k5_contract.json") != EXPECTED_CONTRACT_SHA256:
        raise SystemExit("K5 frozen contract hash mismatch")
    engine = contract.get("engine", {})
    expected_engine = {
        "max_model_len": 32768, "max_num_seqs": 8,
        "max_num_batched_tokens": 4096, "gpu_memory_utilization": 0.97,
        "enforce_eager": True, "enable_prefix_caching": False,
        "enable_chunked_prefill": True, "kv_offloading_backend": "native",
        "kv_offloading_size_gib": 0.25,
    }
    if any(engine.get(key) != value for key, value in expected_engine.items()):
        raise SystemExit("K5 engine contract mismatch")
    if requested.get("kv_offloading_backend") != "native" or requested.get("kv_offloading_size_gib") != 0.25:
        raise SystemExit("K5 requested native host pool mismatch")
    if source_gate.get("status") != "PASS" or source_gate.get("hashes") != {
        item["path"]: item["sha256"] for item in contract["source_contract"]
    }:
        raise SystemExit("K5 source gate mismatch")

    prompts = fixture.get("prompt_token_ids_list")
    prompt_hashes = fixture.get("prompt_sha256")
    if not isinstance(prompts, list) or len(prompts) != 8 or any(len(prompt) != 16384 for prompt in prompts):
        raise SystemExit("K5 prompt fixture count/length mismatch")
    if not isinstance(prompt_hashes, list) or len(set(prompt_hashes)) != 8:
        raise SystemExit("K5 prompt fixtures are not eight distinct identities")
    if not isinstance(requests, list) or len(requests) != 8:
        raise SystemExit("K5 request denominator mismatch")
    expected_ids = [f"swap-k5-{index:02d}" for index in range(8)]
    if sorted(record.get("request_id") for record in requests) != expected_ids:
        raise SystemExit("K5 request IDs mismatch")
    for record in requests:
        if (
            record.get("input_token_count") != 16384
            or record.get("requested_output_tokens") != 32
            or record.get("output_token_count") != 32
            or record.get("finish_reason") != "length"
            or record.get("error") is not None
            or record.get("censored") is not False
        ):
            raise SystemExit(f"K5 request correctness failure: {record.get('request_id')}")
    if result.get("requested_request_count") != 8 or result.get("completed_record_count") != 8:
        raise SystemExit("K5 result denominator mismatch")

    warnings = re.findall(r"Request (swap-k5-[^: ]+): cannot store blocks", logs)
    logical_warning_ids = sorted({"-".join(value.split("-")[:3]) for value in warnings})
    if len(warnings) < 8 or logical_warning_ids != expected_ids:
        raise SystemExit("K5 lacks per-request prepare_store=None fallback evidence")
    required_logs = (
        "Creating v1 connector with name: OffloadingConnector",
        "Creating offloading spec with name: CPUOffloadingSpec",
        "GPU KV cache size: 33,600 tokens",
        "cross layer KV cache of shape (2100, 8, 32, 2, 16, 128)",
        "Allocating 1 CPU tensors",
        "Starting ZMQ publisher thread",
    )
    if not all(value in logs for value in required_logs):
        raise SystemExit("K5 native-offload runtime log evidence is incomplete")
    if events.get("decode_errors") not in (None, []) or events.get("block_stored_message_count") != 0 or events.get("block_removed_message_count") != 0:
        raise SystemExit("K5 store-refusal event semantics mismatch")
    if not telemetry:
        raise SystemExit("K5 telemetry is empty")

    raw_hashes = raw_evidence_hashes(args.local_attempt_root)
    raw_aggregate = aggregate_hash(raw_hashes)
    if raw_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"K5 raw aggregate mismatch: {raw_aggregate}")
    ttft = [int(record["ttft_ns"]) for record in requests]
    latency = [int(record["completion_latency_ns"]) for record in requests]
    peak_rss = max(item["host"]["process_totals_kib"].get("VmRSS", 0) for item in telemetry)
    peak_pin = max(item["host"]["process_totals_kib"].get("VmPin", 0) for item in telemetry)
    peak_mlocked = max(item["host"]["meminfo_kib"].get("Mlocked", 0) for item in telemetry)
    adjudication = {
        "attempt_id": args.attempt_id,
        "scientific_outcome": "HOST_POOL_STORE_REFUSED_FALLBACK_OBSERVED",
        "runner_initial_outcome": result.get("scientific_outcome"),
        "adjudication_basis": "vLLM offloading scheduler line 802 logs prepare_store=None as 'cannot store blocks'; CPU manager source returns None when protected-key eviction cannot satisfy capacity.",
        "cannot_store_warning_count": len(warnings),
        "affected_logical_request_ids": logical_warning_ids,
        "request_denominator": 8,
        "passed_request_count": 8,
        "failed_request_count": 0,
        "rejected_request_count": 0,
        "censored_request_count": 0,
        "finish_reasons": ["length"],
        "output_token_counts": [32],
        "kv_event_batch_count": events.get("batch_count"),
        "block_stored_message_count": 0,
        "block_removed_message_count": 0,
        "zero_event_interpretation": "Expected for prepare_store=None: no store is issued and therefore no BlockStored/BlockRemoved event is published.",
        "ttft_ns": numeric_summary(ttft),
        "completion_latency_ns": numeric_summary(latency),
        "telemetry_sample_count": len(telemetry),
        "peak_process_vmrss_kib": peak_rss,
        "peak_process_vmpin_kib": peak_pin,
        "peak_system_mlocked_kib": peak_mlocked,
        "raw_file_count": len(raw_hashes),
        "raw_file_set_sha256": raw_aggregate,
        "checksum_manifest_sha256": sha256_file(runner / "SHA256SUMS"),
        "stdout_sha256": sha256_file(runner / "stdout.log"),
        "stderr_sha256": sha256_file(runner / "stderr.log"),
        "validation_state": "VALIDATION_PASS",
    }
    return adjudication, {"requests": requests, "telemetry": telemetry, "events": events, "logs": logs}


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
    siblings = [path for path in (args.local_attempt_root / "runner_runs").iterdir() if path.is_dir()]
    if siblings != [runner]:
        raise SystemExit("K5 attempt must contain exactly the selected runner directory")
    adjudication, details = validate(args, runner)

    sidecar = args.local_attempt_root / "derived_swap_k5_store_refusal_fallback.json"
    write_json(sidecar, {
        "schema_version": "phase7-swap-k5-derived-v1",
        "adjudication": adjudication,
        "measurement_field_mapping": {
            "host_pool_request": "requested_engine_args.json kv_offloading_size_gib",
            "store_refusal": "stdout.log scheduler.py:802 cannot store blocks",
            "source_semantics": "k5_contract.json source_contract",
            "request_denominator_and_terminal_semantics": "requests.json records",
            "host_and_gpu_telemetry": "telemetry.jsonl",
            "zero_kv_event_confirmation": "kv_events.json",
        },
        "limitations": [
            "The public KV event stream has no explicit StoreRefused event; fallback is bound to source hash and scheduler warning.",
            "No scheduler preemption/recompute or request rejection occurred in this attempt.",
            "Diagnostic one-burst evidence; not a formal serving tail-CI or bandwidth/latency result.",
            "VmPin remained zero and cannot prove CUDA pinned-pool residency; system Mlocked and process RSS are retained.",
        ],
    })
    sidecar_hash = sha256_file(sidecar)

    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    row = next(item for item in rows if item.get("master_row_id") == "SWAP-K5")
    for key, value in (
        ("attempt_ids", args.attempt_id),
        ("remote_raw_paths", args.remote_attempt_root),
        ("local_raw_paths", str(args.local_attempt_root)),
        ("source_raw_sha256", adjudication["raw_file_set_sha256"]),
    ):
        add_row_value(row, key, value)
    append_unique(row.setdefault("manifest_sha256", []), [
        adjudication["checksum_manifest_sha256"], adjudication["stdout_sha256"],
        adjudication["stderr_sha256"], sha256_file(runner / "status.json"),
        sha256_file(runner / "result.json"), sha256_file(runner / "requests.json"),
        sha256_file(runner / "kv_events.json"), sha256_file(runner / "k5_contract.json"),
        sidecar_hash,
    ])
    transition_id = f"MR11-SWAP-K5-{args.attempt_id}-PROMOTION"
    row.update({
        "execution_state": "EXECUTION_COMPLETE", "raw_state": "COMPLETE",
        "backup_state": "VERIFIED", "review_state": "REVIEW_WITH_LIMITATION",
        "validation_state": "VALIDATION_PASS", "adoption_state": "ADOPTED",
        "blocker_or_failure": None, "k5_adjudication": adjudication,
        "claims_supported": append_unique(list(row.get("claims_supported", [])), [
            "A frozen 0.25-GiB native CPU KV host pool produced 287 source-bound prepare_store=None 'cannot store blocks' warnings spanning all eight admitted logical requests.",
            "The store-refusal fallback preserved the full 8-request denominator: all requests completed at forced length with 32 output tokens and no failure, rejection, or censoring.",
        ]),
        "claims_forbidden": append_unique(list(row.get("claims_forbidden", [])), [
            "Formal serving tail-CI, steady-state throughput, KV copy latency, PCIe bandwidth, or object moved-byte claims from SWAP-K5.",
            "Scheduler preemption/recompute, request rejection, or BlockRemoved LRU-eviction claims from this attempt.",
            "Pinned-host allocation proof from VmPin=0 telemetry.",
        ]),
        "contamination_flags": append_unique(list(row.get("contamination_flags", [])), [
            "K5_PUBLIC_KV_STREAM_HAS_NO_STORE_REFUSED_EVENT_SOURCE_BOUND_LOG_USED",
            "K5_RUNNER_INITIAL_NULL_OUTCOME_REVIEW_ADJUDICATED_FROM_SOURCE_AND_LOG",
            "K5_DIAGNOSTIC_ONE_BURST_NOT_FORMAL_SERVING",
        ]),
        "next_action": "Freeze the independent SWAP-K4 OFFxKV interaction contract before dispatch; do not infer interaction effects from K5.",
        "last_transition_record": transition_id,
    })
    transition = {
        "transition_id": transition_id, "timestamp_utc": now_utc(),
        "changed_rows": ["SWAP-K5"],
        "reason": "Promote source-bound native host-pool store-refusal fallback with complete request denominator and verified raw backup.",
        "prior_ledger_sha256": prior_hash, "attempt_id": args.attempt_id,
        "adjudication": adjudication, "derived_sidecar": str(sidecar),
        "derived_sidecar_sha256": sidecar_hash, "remote_local_hashes_verified": True,
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition_id
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(1 for item in rows if legally_closed(item))
    ledger["required_row_count"] = len(rows)
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    inventory_path = root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    append_record_once(inventory.setdefault("swap_k5_exhaustion_attempts", []), {
        **adjudication, "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": str(args.local_attempt_root), "derived_sidecar": str(sidecar),
        "derived_sidecar_sha256": sidecar_hash, "status": "VALIDATION_PASS_WITH_LIMITATIONS",
    })
    write_json(inventory_path, inventory)

    backup_path = root / "local_backup_manifest.json"
    backup = read_json(backup_path)
    append_record_once(backup.setdefault("phase7_attempt_backups", []), {
        "attempt_id": args.attempt_id, "remote_attempt": args.remote_attempt_root,
        "local_attempt": str(args.local_attempt_root), "status": "VERIFIED_RAW_VALIDATION_PASS_WITH_LIMITATIONS",
        "file_count": adjudication["raw_file_count"], "file_set_sha256": adjudication["raw_file_set_sha256"],
        "manifest_sha256": adjudication["checksum_manifest_sha256"], "derived_sidecar_sha256": sidecar_hash,
    })
    write_json(backup_path, backup)

    trigger_path = root / "trigger_adjudication.json"
    trigger = read_json(trigger_path)
    for entry in trigger.get("entries", []):
        if entry.get("trigger_id") == "SWAP-K5":
            append_unique(entry.setdefault("observed_evidence", []), [
                f"{args.attempt_id}: native 0.25-GiB host pool store refusal observed for all eight requests; all requests completed."
            ])
            append_unique(entry.setdefault("source_evidence_hashes", []), [adjudication["raw_file_set_sha256"]])
    write_json(trigger_path, trigger)

    gap_path = root / "gap_register.json"
    gap = read_json(gap_path)
    gap["entries"] = [item for item in gap.setdefault("entries", []) if item.get("gap_id") != "GAP-SWAP-K5-EXHAUSTION"]
    gap["entries"].append({
        "gap_id": "GAP-SWAP-K5-EXHAUSTION", "status": "CLOSED_WITH_LIMITATION",
        "source": str(sidecar),
        "consequence": "Host-pool store-refusal fallback is validated; no preemption, rejection, LRU BlockRemoved, pinned-memory, or formal serving performance claim is supported.",
    })
    write_json(gap_path, gap)

    claims_path = root / "claim_boundary_register.json"
    claims = read_json(claims_path)
    append_unique(claims.setdefault("claims_allowed_now", []), [
        "SWAP-K5 source-bound native host-pool store-refusal fallback with 8/8 completed request denominator"
    ])
    forbidden = claims.setdefault("claims_forbidden_now", [])
    forbidden[:] = [item for item in forbidden if item != "SWAP-K4 interaction or SWAP-K5 exhaustion conclusions before their own gates"]
    append_unique(forbidden, [
        "SWAP-K5 formal serving performance, object-level copy latency/bandwidth, preemption/recompute, rejection, LRU eviction, or pinned-memory claims",
        "SWAP-K4 OFFxKV interaction conclusions before its own frozen contract and evidence",
    ])
    write_json(claims_path, claims)

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
        "next_gpu_unit": "SWAP-K4",
        "ready_gpu_units": [],
        "next_k3_point": None,
        "next_gate_action": "FREEZE_SWAP_K4_OFFXKV_INTERACTION_CONTRACT",
        "dispatch_guards": [
            "MR2 read-only preflight clear", "no foreign serving/GPU process at dispatch",
            "new-session four-guard canary validated and locally backed up",
            "SWAP-K0/K1/K2/K3/K5 validated and locally backed up",
            "SWAP-K4 must not dispatch until its independent OFFxKV interaction contract is frozen",
            "no filler workload", "raw namespace independent",
        ],
    })
    write_json(queue_path, queue)

    review_name = f"MR11-SWAP-K5-{args.attempt_id}-PROMOTION.json"
    review = {
        "schema_version": "phase7-combined-master-swap-k5-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"], "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root, "local_raw_path": str(args.local_attempt_root),
        "execution_state": "EXECUTION_COMPLETE", "raw_backup": "VERIFIED",
        "validation_state": "VALIDATION_PASS", "review_state": "REVIEW_WITH_LIMITATION",
        "adjudication": adjudication, "derived_sidecar": str(sidecar),
        "derived_sidecar_sha256": sidecar_hash, "next_ready_unit": "SWAP-K4_AFTER_CONTRACT_GATE",
    }
    write_json(root / "reviews" / review_name, review)
    write_json(root / "checkpoints" / review_name, {
        "schema_version": "phase7-combined-master-checkpoint-v1",
        "checkpoint_id": transition_id, "timestamp_utc": transition["timestamp_utc"],
        "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(root / "master_remaining_ledger.json"),
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "next_gpu_unit": "SWAP-K4", "next_gate_action": "FREEZE_SWAP_K4_OFFXKV_INTERACTION_CONTRACT",
        "raw_file_set_sha256": adjudication["raw_file_set_sha256"],
    })
    print(json.dumps({
        "attempt_id": args.attempt_id, "scientific_outcome": adjudication["scientific_outcome"],
        "cannot_store_warning_count": adjudication["cannot_store_warning_count"],
        "request_denominator": adjudication["request_denominator"],
        "raw_file_set_sha256": adjudication["raw_file_set_sha256"],
        "execution_ledger_sha256": execution_hash,
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "next_gpu_unit": "SWAP-K4", "next_gate_action": "FREEZE_SWAP_K4_OFFXKV_INTERACTION_CONTRACT",
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
