#!/usr/bin/env python3
"""Record the completed SWAP-K1 event-trace attempt without over-claiming.

The V4 attempt executed the frozen 3x16384 pressure point and captured native
KV event payloads, but the trace contains BlockStored events only.  This
promotion therefore records a verified raw backup and an unverified K1 gate;
it deliberately does not close SWAP-K1 or promote swap performance claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def evidence_file_hashes(root: Path) -> dict[str, str]:
    return {
        f"./{path.relative_to(root).as_posix()}": sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    }


def aggregate_hash(file_hashes: dict[str, str]) -> str:
    payload = "".join(
        f"{file_hashes[path]}  {path}\n" for path in sorted(file_hashes)
    ).encode("utf-8")
    return sha256_bytes(payload)


def checksum_mismatches(runner: Path) -> list[str]:
    mismatches: list[str] = []
    for line in (runner / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split(None, 1)
        relative = relative.strip()
        actual = sha256_file(runner / relative)
        if expected != actual:
            mismatches.append(relative)
    return mismatches


def append_unique(values: list[str], additions: list[str]) -> list[str]:
    for value in additions:
        if value not in values:
            values.append(value)
    return values


def event_summary(event_doc: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    hash_counts: list[int] = []
    media: list[str] = []
    message_field_counts: list[int] = []
    for envelope in event_doc.get("events", []):
        decoded = envelope.get("decoded")
        if not isinstance(decoded, list) or len(decoded) < 2:
            continue
        messages = decoded[1]
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, list) or len(message) < 2:
                continue
            kind = str(message[0])
            counts[kind] = counts.get(kind, 0) + 1
            fields = message[1:]
            if not fields:
                continue
            message_field_counts.append(len(fields))
            if kind == "BlockStored":
                if isinstance(message[1], list):
                    hash_counts.append(len(message[1]))
                if len(message) > 6:
                    media.append(str(message[6]))
    return {
        "decoded_message_counts": counts,
        "decoded_block_stored_message_count": counts.get("BlockStored", 0),
        "block_stored_hash_counts": hash_counts,
        "block_stored_media": sorted(set(media)),
        "message_field_counts": sorted(set(message_field_counts)),
        "event_envelopes": len(event_doc.get("events", [])),
        "declared_block_stored_event_count": event_doc.get("block_stored_event_count"),
        "declared_block_removed_event_count": event_doc.get("block_removed_event_count"),
        "decode_errors": event_doc.get("decode_errors"),
    }


def legally_closed(row: dict[str, Any]) -> bool:
    return (
        row.get("validation_state")
        in {"VALIDATION_PASS", "NEGATIVE_EVIDENCE", "UNAVAILABLE_WITH_CONSEQUENCE"}
        and row.get("backup_state") == "VERIFIED"
        and row.get("adoption_state") in {"ADOPTED", "NOT_APPLICABLE"}
        and row.get("trigger_state")
        in {"NOT_CONDITIONAL", "NOT_TRIGGERED_WITH_EVIDENCE", "OWNER_WAIVED"}
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    parser.add_argument("--remote-checksum-manifest-sha256", required=True)
    parser.add_argument("--remote-launch-stdout-sha256", required=True)
    parser.add_argument("--remote-external-stderr-sha256", required=True)
    args = parser.parse_args()

    local_root = args.local_attempt_root
    runner = local_root / "runner_runs" / "20260814T000843Z__SWAP-K1-V4-MASTER"
    launch = local_root / "runner_runs" / "20260814T001957Z__SWAP-K1-V4-MASTER"
    external_stderr = local_root / "special_mechanism_phase7_k1_v4_stderr.log"
    required_paths = [
        runner / "status.json",
        runner / "result.json",
        runner / "requested_engine_args.json",
        runner / "kv_events.json",
        runner / "SHA256SUMS",
        launch / "stdout.log",
        external_stderr,
    ]
    if not local_root.is_dir() or not all(path.is_file() for path in required_paths):
        raise SystemExit("SWAP-K1-V4 raw backup is incomplete")

    status = read_json(runner / "status.json")
    result = read_json(runner / "result.json")
    requested = read_json(runner / "requested_engine_args.json")
    events = read_json(runner / "kv_events.json")
    stdout = (launch / "stdout.log").read_text(encoding="utf-8", errors="replace")
    stderr = external_stderr.read_text(encoding="utf-8", errors="replace")

    if status.get("status") != "PASS" or status.get("execution_state") != "EXECUTION_COMPLETE":
        raise SystemExit("SWAP-K1-V4 is not a completed PASS")
    if status.get("validation_state") != "UNVERIFIED_BLOCK_MOVEMENT":
        raise SystemExit("unexpected V4 validation state")
    if status.get("movement_gate") != "REQUIRES_OBSERVABLE_BLOCK_SWAP_EVENT_AND_LINEAGE":
        raise SystemExit("unexpected V4 movement gate")
    if requested.get("kv_offloading_size") != 2.0 or requested.get("kv_offloading_backend") != "native":
        raise SystemExit("V4 native KV-offload request is not frozen as expected")

    records = result.get("records")
    if not isinstance(records, list) or len(records) != 3:
        raise SystemExit("V4 result must contain exactly three measured records")
    if any(
        record.get("input_token_count") != 16384
        or record.get("output_token_count") != 32
        or record.get("finish_reason") != "length"
        for record in records
    ):
        raise SystemExit("V4 request correctness gate failed")
    if result.get("total_context_tokens") != 49152:
        raise SystemExit("V4 total context token count is not 3x16384")

    summary = event_summary(events)
    if summary["event_envelopes"] != 7:
        raise SystemExit("V4 event envelope count is not seven")
    if summary["decoded_block_stored_message_count"] < 7:
        raise SystemExit("V4 decoded BlockStored count is below the declared seven-event trace")
    if summary["decoded_message_counts"].get("BlockRemoved", 0) != 0:
        raise SystemExit("V4 unexpectedly contains a BlockRemoved event")
    if summary["declared_block_stored_event_count"] != 7 or summary["declared_block_removed_event_count"] != 0:
        raise SystemExit("V4 declared event counters are inconsistent")
    if summary["decode_errors"] not in (None, []):
        raise SystemExit("V4 event trace contains decode errors")
    if summary["block_stored_media"] != ["CPU"]:
        raise SystemExit(f"unexpected V4 BlockStored media: {summary['block_stored_media']}")
    if not summary["block_stored_hash_counts"] or any(
        count != 128 for count in summary["block_stored_hash_counts"]
    ):
        raise SystemExit("V4 BlockStored hash cardinality is not 128 per decoded message")

    for needle in (
        "Creating v1 connector with name: OffloadingConnector",
        "Creating offloading spec with name: CPUOffloadingSpec",
        "Allocating a cross layer KV cache",
        "Allocating 1 CPU tensors",
        "GPU KV cache size: 36,928 tokens",
    ):
        if needle not in stdout:
            raise SystemExit(f"V4 launch log lacks required evidence: {needle}")
    if not stderr.strip():
        raise SystemExit("V4 external stderr is unexpectedly empty")

    file_hashes = evidence_file_hashes(local_root)
    if len(file_hashes) != 18:
        raise SystemExit(f"expected 18 V4 raw evidence files, found {len(file_hashes)}")
    actual_aggregate = aggregate_hash(file_hashes)
    if actual_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"raw aggregate mismatch: {actual_aggregate} != {args.expected_aggregate_sha256}")
    manifest_hash = sha256_file(runner / "SHA256SUMS")
    if manifest_hash != args.remote_checksum_manifest_sha256:
        raise SystemExit("remote/local SHA256SUMS manifest hash mismatch")
    if sha256_file(launch / "stdout.log") != args.remote_launch_stdout_sha256:
        raise SystemExit("remote/local launch stdout hash mismatch")
    if sha256_file(external_stderr) != args.remote_external_stderr_sha256:
        raise SystemExit("remote/local external stderr hash mismatch")
    mismatches = checksum_mismatches(runner)
    if mismatches:
        raise SystemExit(f"V4 structured SHA256SUMS mismatch set is non-empty: {mismatches}")

    ledger_path = args.master_root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    by_id = {row["master_row_id"]: row for row in rows}
    row = by_id.get("SWAP-K1")
    if row is None:
        raise SystemExit("missing SWAP-K1 row")

    attempt_ids = row.setdefault("attempt_ids", [])
    if args.attempt_id not in attempt_ids:
        attempt_ids.append(args.attempt_id)
    for key, value in (
        ("remote_raw_paths", args.remote_attempt_root),
        ("local_raw_paths", str(local_root)),
        ("source_raw_sha256", actual_aggregate),
    ):
        values = row.setdefault(key, [])
        if value not in values:
            values.append(value)
    manifest_values = row.setdefault("manifest_sha256", [])
    append_unique(
        manifest_values,
        [manifest_hash, sha256_file(runner / "status.json"), sha256_file(runner / "kv_events.json"), sha256_file(launch / "stdout.log"), sha256_file(external_stderr)],
    )
    lineage = row.setdefault("repair_lineage", [])
    lineage.append(
        {
            "attempt_id": args.attempt_id,
            "failure_class": "SCIENTIFIC_GATE_UNVERIFIED",
            "failure": "The completed native-offload pressure run captured seven BlockStored batches and zero BlockRemoved batches; the required swap-out/in event and lineage were not observable.",
            "observed_event_summary": summary,
            "same_prompt_cache_observation": [record.get("num_cached_tokens") for record in records],
            "repair_required": "Use three distinct prompt token sequences at the same 3x16384 pressure point and require a BlockRemoved or equivalent movement event before closing K1.",
            "source_raw_sha256": actual_aggregate,
        }
    )
    row.update(
        {
            "execution_state": "EXECUTION_COMPLETE",
            "raw_state": "COMPLETE",
            "backup_state": "VERIFIED",
            "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "UNVERIFIED",
            "adoption_state": "SUPPLEMENT_REQUIRED",
            "blocker_or_failure": "SCIENTIFIC_GATE_UNVERIFIED: V4 completed the 3x16384 native KV-offload pressure run, but the event trace contains BlockStored=7 and BlockRemoved=0; no verified block swap-out/in or lineage gate is available.",
            "claims_supported": append_unique(
                list(row.get("claims_supported", [])),
                [
                    "SWAP-K1-V4 executed three real requests with 16384 input tokens and 32 generated tokens each, all finishing with finish_reason=length.",
                    "The native CPU KV-offload path initialized with a log-observed GPU KV cache size of 36928 tokens and the 49152-token pressure workload completed.",
                    "The preloaded KV event subscriber preserved seven declared event envelopes (eight decoded BlockStored messages), each carrying 128 block hashes and medium=CPU, with no decode errors.",
                ],
            ),
            "claims_forbidden": append_unique(
                list(row.get("claims_forbidden", [])),
                [
                    "A verified SWAP-K1 block swap-out/in event, moved bytes, block lineage, or swap latency from V4.",
                    "A global claim that native KV movement is impossible; V4 only lacks the required observable removal event.",
                    "K1-K5 performance, preemption, recompute, fallback, or throughput claims based on V4.",
                ],
            ),
            "contamination_flags": append_unique(
                list(row.get("contamination_flags", [])),
                [
                    "NO_BLOCK_REMOVED_EVENT_OBSERVED",
                    "SAME_PROMPT_CACHE_OBSERVED_NUM_CACHED_TOKENS_16384",
                    "RUNTIME_LOG_OBSERVATIONS_PATH_SPLIT_EXTERNAL_LAUNCH_LOG",
                    "STATUS_RESULT_CAPACITY_METADATA_MISMATCH_STATUS_36928_RESULT_38592",
                    "OBJECT_LEVEL_TRACE_BLOCK_STORED_ONLY_NO_MOVEMENT_LINEAGE",
                ],
            ),
            "next_action": "Run SWAP-K1-V5 with three distinct prompt token sequences at the same 3x16384 pressure point; require observable BlockRemoved or equivalent movement event plus lineage before K1 closure.",
            "last_transition_record": "MR7-SWAP-K1-V4-EVENT-TRACE-REVIEW",
        }
    )

    transition = {
        "transition_id": "MR7-SWAP-K1-V4-EVENT-TRACE-REVIEW",
        "timestamp_utc": now_utc(),
        "changed_rows": ["SWAP-K1"],
        "reason": "Completed V4 pressure run and verified raw backup; seven declared event envelopes (eight decoded BlockStored messages) were captured but no BlockRemoved/swap-out event or lineage was observed, so K1 remains open.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": args.attempt_id,
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": manifest_hash,
        "checksum_manifest_mismatches": [],
        "remote_local_hashes_verified": True,
        "event_summary": summary,
        "validation_state": "UNVERIFIED",
        "next_action": "SWAP-K1-V5 distinct-prompt supplement",
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition["transition_id"]
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(1 for item in rows if legally_closed(item))
    ledger["required_row_count"] = len(rows)
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    inventory_path = args.master_root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    inventory.setdefault("swap_k1_event_trace_attempts", []).append(
        {
            "attempt_id": args.attempt_id,
            "remote_raw_path": args.remote_attempt_root,
            "local_raw_path": str(local_root),
            "status": "EXECUTION_COMPLETE_GATE_UNVERIFIED",
            "request_count": len(records),
            "context_tokens_per_request": 16384,
            "total_context_tokens": 49152,
            "gpu_kv_capacity_tokens_log_observed": 36928,
            "block_stored_event_count": 7,
            "decoded_block_stored_message_count": summary["decoded_block_stored_message_count"],
            "block_removed_event_count": 0,
            "block_stored_hash_counts": [128] * 7,
            "block_stored_media": ["CPU"],
            "raw_file_count": len(file_hashes),
            "file_set_sha256": actual_aggregate,
            "checksum_manifest_sha256": manifest_hash,
            "remote_local_hashes_verified": True,
            "validation_state": "UNVERIFIED",
            "consequence": "No K1 block movement/lineage or performance claim; distinct-prompt supplement required.",
        }
    )
    write_json(inventory_path, inventory)

    gap_path = args.master_root / "gap_register.json"
    gap = read_json(gap_path)
    gap_id = "GAP-SWAP-K1-EVENT-TRACE-V4"
    entry = {
        "gap_id": gap_id,
        "status": "SUPPLEMENT_REQUIRED",
        "source": str(local_root),
        "consequence": "V4 proves a completed pressure execution and preserves BlockStored payloads, but zero BlockRemoved events prevent K1 swap movement/lineage closure; run distinct-prompt V5.",
    }
    entries = gap.setdefault("entries", [])
    entries[:] = [item for item in entries if item.get("gap_id") != gap_id]
    entries.append(entry)
    write_json(gap_path, gap)

    claim_path = args.master_root / "claim_boundary_register.json"
    claims = read_json(claim_path)
    forbidden = claims.setdefault("claims_forbidden_now", [])
    append_unique(
        forbidden,
        [
            "SWAP-K1 verified block swap-out/in or block lineage from V4",
            "SWAP-K1 movement performance from V4",
        ],
    )
    write_json(claim_path, claims)

    remaining = [item for item in rows if not legally_closed(item)]
    conditional = [item["master_row_id"] for item in remaining if item.get("trigger_state") == "PENDING"]
    blocked = [
        {"id": item["master_row_id"], "reason": item["blocker_or_failure"]}
        for item in remaining
        if item.get("blocker_or_failure")
    ]
    write_json(
        args.master_root / "master_remaining_ledger.json",
        {
            "schema_version": "phase7-combined-master-remaining-ledger-v1",
            "master_campaign_id": ledger["master_campaign_id"],
            "generated_from_execution_ledger_sha256": execution_hash,
            "required_total": len(rows),
            "required_legally_closed": len(rows) - len(remaining),
            "required_remaining_count": len(remaining),
            "required_remaining_ids": [item["master_row_id"] for item in remaining],
            "blocked_rows": blocked,
            "conditional_pending_count": len(conditional),
            "conditional_pending_ids": conditional,
            "phase7_status": ledger["status"],
        },
    )

    queue_path = args.master_root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue["generated_from_execution_ledger_sha256"] = execution_hash
    queue["next_gpu_unit"] = "SWAP-K1"
    queue["ready_gpu_units"] = ["SWAP-K1"]
    queue["dispatch_guards"] = [
        "MR2 read-only preflight clear",
        "no foreign serving/GPU process at dispatch",
        "new-session four-guard canary validated and locally backed up",
        "SWAP-K0 native KV-offload capability initialized",
        "SWAP-K1-V4 completed with raw backup verified but no BlockRemoved event; V5 distinct-prompt supplement required",
        "ADOPT-EXPERT-CATALOG remains a prerequisite for OFF-E-RT0/OFF-W0",
        "no filler workload",
        "raw namespace independent",
    ]
    write_json(queue_path, queue)

    backup_path = args.master_root / "local_backup_manifest.json"
    backup = read_json(backup_path)
    backup.setdefault("verified_local_sources", []).append(
        {
            "attempt_id": args.attempt_id,
            "path": str(local_root),
            "file_count": len(file_hashes),
            "file_set_sha256": actual_aggregate,
            "manifest_sha256": manifest_hash,
            "remote_local_hashes_verified": True,
            "status": "SWAP-K1-V4 raw backup verified; scientific gate unverified",
        }
    )
    backup.setdefault("phase7_attempt_backups", []).append(
        {
            "attempt_id": args.attempt_id,
            "remote_attempt": args.remote_attempt_root,
            "local_attempt": str(local_root),
            "status": "VERIFIED_RAW_UNVERIFIED_GATE",
            "file_count": len(file_hashes),
            "file_set_sha256": actual_aggregate,
        }
    )
    write_json(backup_path, backup)

    review = {
        "schema_version": "phase7-combined-master-swap-k1-event-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"],
        "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": str(local_root),
        "execution_state": "EXECUTION_COMPLETE",
        "request_correctness": "PASS",
        "raw_backup": "VERIFIED",
        "event_summary": summary,
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": manifest_hash,
        "remote_local_hashes_verified": True,
        "validation_state": "UNVERIFIED",
        "promotion_status": "SUPPLEMENT_REQUIRED",
        "claims_forbidden": [
            "verified block swap-out/in",
            "exact moved bytes or block lineage",
            "swap latency/throughput/performance",
        ],
        "next_ready_unit": "SWAP-K1",
    }
    write_json(args.master_root / "reviews" / "MR7-SWAP-K1-V4-EVENT-TRACE-REVIEW.json", review)
    write_json(
        args.master_root / "checkpoints" / "MR7-SWAP-K1-V4-EVENT-TRACE-REVIEW.json",
        {
            "schema_version": "phase7-combined-master-checkpoint-v1",
            "checkpoint_id": "MR7-SWAP-K1-V4-EVENT-TRACE-REVIEW",
            "timestamp_utc": transition["timestamp_utc"],
            "execution_ledger_sha256": execution_hash,
            "remaining_ledger_sha256": sha256_file(args.master_root / "master_remaining_ledger.json"),
            "required_closed_count": len(rows) - len(remaining),
            "required_remaining_count": len(remaining),
            "next_ready_gpu_unit": "SWAP-K1",
            "raw_file_set_sha256": actual_aggregate,
            "event_summary": summary,
        },
    )

    print(
        json.dumps(
            {
                "attempt_id": args.attempt_id,
                "validation_state": "UNVERIFIED",
                "promotion_status": "SUPPLEMENT_REQUIRED",
                "raw_file_count": len(file_hashes),
                "raw_file_set_sha256": actual_aggregate,
                "checksum_manifest_sha256": manifest_hash,
                "block_stored_event_count": 7,
                "decoded_block_stored_message_count": summary["decoded_block_stored_message_count"],
                "block_removed_event_count": 0,
                "execution_ledger_sha256": execution_hash,
                "required_closed_count": len(rows) - len(remaining),
                "required_remaining_count": len(remaining),
                "next_ready_gpu_unit": "SWAP-K1",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
