#!/usr/bin/env python3
"""Promote the distinct-prompt SWAP-K1 forced movement attempt.

V5 captured native vLLM BlockStored and BlockRemoved events.  The promotion
keeps the raw evidence immutable, writes a derived identity/byte sidecar, and
closes only the forced-movement K1 gate.  It does not promote K2 capacity,
K3 serving, K5 exhaustion, or swap performance claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
        if path.is_file()
        and path.name != "SHA256SUMS"
        and path.name != "derived_swap_k1_v5_event_lineage_and_bytes.json"
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
        if expected != sha256_file(runner / relative):
            mismatches.append(relative)
    return mismatches


def append_unique(values: list[str], additions: list[str]) -> list[str]:
    for value in additions:
        if value not in values:
            values.append(value)
    return values


def event_analysis(event_doc: dict[str, Any]) -> dict[str, Any]:
    stored: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    for envelope in event_doc.get("events", []):
        decoded = envelope.get("decoded")
        if not isinstance(decoded, list) or len(decoded) < 2:
            continue
        messages = decoded[1]
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, list) or not message:
                continue
            kind = str(message[0])
            sequence = envelope.get("sequence")
            if kind == "BlockStored":
                hashes = list(message[1]) if len(message) > 1 and isinstance(message[1], list) else []
                item = {
                    "sequence": sequence,
                    "hashes": hashes,
                    "hash_count": len(hashes),
                    "parent_block_hash": message[2] if len(message) > 2 else None,
                    "token_id_count": len(message[3]) if len(message) > 3 and isinstance(message[3], list) else None,
                    "event_block_size": message[4] if len(message) > 4 else None,
                    "medium": message[6] if len(message) > 6 else None,
                }
                stored.append(item)
                timeline.append({"kind": kind, **{k: v for k, v in item.items() if k != "hashes"}})
            elif kind == "BlockRemoved":
                hashes = list(message[1]) if len(message) > 1 and isinstance(message[1], list) else []
                item = {
                    "sequence": sequence,
                    "hashes": hashes,
                    "hash_count": len(hashes),
                    "medium": message[2] if len(message) > 2 else None,
                    "group_idx": message[3] if len(message) > 3 else None,
                }
                removed.append(item)
                timeline.append({"kind": kind, **{k: v for k, v in item.items() if k != "hashes"}})

    first_stored_sequence: dict[str, Any] = {}
    for event in stored:
        for block_hash in event["hashes"]:
            first_stored_sequence.setdefault(block_hash, event["sequence"])
    first_removed_sequence: dict[str, Any] = {}
    for event in removed:
        for block_hash in event["hashes"]:
            first_removed_sequence.setdefault(block_hash, event["sequence"])

    stored_set = set(first_stored_sequence)
    removed_set = set(first_removed_sequence)
    return {
        "declared_block_stored_event_count": event_doc.get("block_stored_event_count"),
        "declared_block_removed_event_count": event_doc.get("block_removed_event_count"),
        "event_envelope_count": len(event_doc.get("events", [])),
        "decoded_block_stored_message_count": len(stored),
        "decoded_block_removed_message_count": len(removed),
        "stored_hash_record_count": sum(item["hash_count"] for item in stored),
        "removed_hash_record_count": sum(item["hash_count"] for item in removed),
        "stored_unique_hash_count": len(stored_set),
        "removed_unique_hash_count": len(removed_set),
        "removed_hashes_subset_of_stored": removed_set <= stored_set,
        "removed_hashes_not_previously_stored": sorted(removed_set - stored_set),
        "stored_hashes_without_remove": len(stored_set - removed_set),
        "stored_media": sorted({str(item["medium"]) for item in stored}),
        "removed_media": sorted({str(item["medium"]) for item in removed}),
        "stored_hash_count_groups": sorted({item["hash_count"] for item in stored}),
        "removed_hash_count_groups": sorted({item["hash_count"] for item in removed}),
        "stored_event_block_size_values": sorted({item["event_block_size"] for item in stored}),
        "stored_parent_null_count": sum(item["parent_block_hash"] is None for item in stored),
        "stored_token_id_count_values": sorted({item["token_id_count"] for item in stored}),
        "decode_errors": event_doc.get("decode_errors"),
        "first_stored_sequence_by_hash": first_stored_sequence,
        "first_removed_sequence_by_hash": first_removed_sequence,
        "timeline": timeline,
    }


def legally_closed(row: dict[str, Any]) -> bool:
    trigger_state = row.get("trigger_state")
    trigger_resolved = trigger_state in {
        "NOT_CONDITIONAL",
        "NOT_TRIGGERED_WITH_EVIDENCE",
        "OWNER_WAIVED",
    } or (
        trigger_state == "TRIGGERED"
        and row.get("validation_state")
        in {"VALIDATION_PASS", "NEGATIVE_EVIDENCE", "UNAVAILABLE_WITH_CONSEQUENCE"}
        and row.get("adoption_state") in {"ADOPTED", "NOT_APPLICABLE"}
    )
    return (
        row.get("validation_state")
        in {"VALIDATION_PASS", "NEGATIVE_EVIDENCE", "UNAVAILABLE_WITH_CONSEQUENCE"}
        and row.get("backup_state") == "VERIFIED"
        and row.get("adoption_state") in {"ADOPTED", "NOT_APPLICABLE"}
        and trigger_resolved
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    parser.add_argument("--remote-manifest-sha256", required=True)
    parser.add_argument("--remote-stdout-sha256", required=True)
    parser.add_argument("--remote-stderr-sha256", required=True)
    parser.add_argument("--event-schema-sha256", required=True)
    args = parser.parse_args()

    local_root = args.local_attempt_root
    runner = local_root / "runner_runs" / "20260814T002930Z__SWAP-K1-V5-MASTER"
    required = [
        runner / "status.json",
        runner / "result.json",
        runner / "requests.json",
        runner / "input_fixture.json",
        runner / "requested_engine_args.json",
        runner / "resolved_runtime.json",
        runner / "kv_events.json",
        runner / "SHA256SUMS",
        runner / "stdout.log",
        runner / "stderr.log",
    ]
    if not local_root.is_dir() or not all(path.is_file() for path in required):
        raise SystemExit("SWAP-K1-V5 raw backup is incomplete")

    status = read_json(runner / "status.json")
    result = read_json(runner / "result.json")
    requests = read_json(runner / "requests.json")
    fixture = read_json(runner / "input_fixture.json")
    requested = read_json(runner / "requested_engine_args.json")
    runtime = read_json(runner / "resolved_runtime.json")
    event_doc = read_json(runner / "kv_events.json")
    stdout = (runner / "stdout.log").read_text(encoding="utf-8", errors="replace")
    analysis = event_analysis(event_doc)

    if status.get("status") != "PASS" or status.get("execution_state") != "EXECUTION_COMPLETE":
        raise SystemExit("V5 runner did not complete PASS")
    if status.get("validation_state") != "UNVERIFIED_BLOCK_MOVEMENT":
        raise SystemExit("unexpected V5 runner validation state")
    if requested.get("kv_offloading_size") != 2.0 or requested.get("kv_offloading_backend") != "native":
        raise SystemExit("V5 native KV-offload request is not frozen")
    prompts = fixture.get("prompt_token_ids_list")
    if not isinstance(prompts, list) or len(prompts) != 3 or len({json.dumps(p, separators=(",", ":")) for p in prompts}) != 3:
        raise SystemExit("V5 fixture does not contain three distinct prompt sequences")
    if fixture.get("same_prompt_no_prefix_cache") is not False or fixture.get("distinct_prompt_sequences") is not True:
        raise SystemExit("V5 fixture identity does not record distinct prompts")

    records = result.get("records")
    if not isinstance(records, list) or len(records) != 3:
        raise SystemExit("V5 result must contain three measured records")
    if result.get("total_context_tokens") != 49152:
        raise SystemExit("V5 total context token count is not 49152")
    for index, record in enumerate(records):
        if (
            record.get("input_token_count") != 16384
            or record.get("input_token_ids") != prompts[index]
            or record.get("output_token_count") != 32
            or record.get("finish_reason") != "length"
            or record.get("num_cached_tokens") != 0
        ):
            raise SystemExit(f"V5 request correctness gate failed at request {index + 1}")
    if requests.get("records") != records:
        raise SystemExit("V5 requests.json and result.json records differ")

    if analysis["decoded_block_stored_message_count"] < 1 or analysis["decoded_block_removed_message_count"] < 1:
        raise SystemExit("V5 lacks both BlockStored and BlockRemoved evidence")
    if analysis["removed_hashes_not_previously_stored"]:
        raise SystemExit("V5 removed hashes are not a subset of stored hashes")
    if analysis["stored_media"] != ["CPU"] or analysis["removed_media"] != ["CPU"]:
        raise SystemExit("V5 movement medium is not CPU for both event types")
    if analysis["decode_errors"] not in (None, []):
        raise SystemExit("V5 KV event trace contains decode errors")

    shape_match = re.search(r"cross layer KV cache of shape \(([^)]+)\)", stdout)
    if not shape_match:
        raise SystemExit("V5 launch log lacks cross-layer KV cache shape")
    shape = [int(part.strip()) for part in shape_match.group(1).split(",")]
    if shape != [2308, 8, 32, 2, 16, 128]:
        raise SystemExit(f"unexpected V5 KV cache shape: {shape}")
    cache = runtime.get("vllm_config", {}).get("cache_config", {})
    block_size_tokens = cache.get("block_size")
    if block_size_tokens != 16:
        raise SystemExit(f"unexpected V5 runtime block size: {block_size_tokens}")
    required_log_evidence = (
        "dtype': 'bfloat16'",
        "GPU KV cache size: 36,928 tokens",
        "Creating v1 connector with name: OffloadingConnector",
        "Creating offloading spec with name: CPUOffloadingSpec",
        "Allocating 1 CPU tensors",
    )
    if not all(needle in stdout for needle in required_log_evidence):
        raise SystemExit("V5 native offload log evidence is incomplete")

    dtype_bytes = 2
    # Shape is (num_blocks, kv_heads, layers, K/V, block_tokens, head_dim).
    elements_per_block = shape[1] * shape[2] * shape[3] * shape[4] * shape[5]
    bytes_per_block = elements_per_block * dtype_bytes
    derived_bytes = {
        "basis": "DERIVED_FROM_RUNTIME_CACHE_SHAPE_AND_DTYPE",
        "dtype": "bfloat16",
        "dtype_bytes": dtype_bytes,
        "runtime_cache_shape": shape,
        "runtime_block_size_tokens": block_size_tokens,
        "elements_per_full_kv_block": elements_per_block,
        "bytes_per_full_kv_block": bytes_per_block,
        "stored_unique_block_count": analysis["stored_unique_hash_count"],
        "removed_unique_block_count": analysis["removed_unique_hash_count"],
        "stored_unique_block_bytes": analysis["stored_unique_hash_count"] * bytes_per_block,
        "removed_unique_block_bytes": analysis["removed_unique_hash_count"] * bytes_per_block,
        "identity_intersection_count": analysis["removed_unique_hash_count"],
        "event_reported_block_size_values": analysis["stored_event_block_size_values"],
        "event_reported_parent_null_count": analysis["stored_parent_null_count"],
        "event_reported_token_id_count_values": analysis["stored_token_id_count_values"],
        "event_schema": {
            "source_path": "/usr/local/lib/python3.12/dist-packages/vllm/distributed/kv_events.py",
            "source_sha256": args.event_schema_sha256,
            "BlockStored_fields": ["block_hashes", "parent_block_hash", "token_ids", "block_size", "lora_id", "medium", "lora_name", "extra_keys", "group_idx", "kv_cache_spec_kind", "kv_cache_spec_sliding_window"],
            "BlockRemoved_fields": ["block_hashes", "medium", "group_idx"],
        },
        "lineage": {
            "identity_subset": analysis["removed_hashes_subset_of_stored"],
            "parent_block_hash_available": analysis["stored_parent_null_count"] < analysis["decoded_block_stored_message_count"],
            "first_stored_sequence_by_hash": analysis["first_stored_sequence_by_hash"],
            "first_removed_sequence_by_hash": analysis["first_removed_sequence_by_hash"],
        },
    }
    sidecar = local_root / "derived_swap_k1_v5_event_lineage_and_bytes.json"
    write_json(sidecar, {"schema_version": "phase7-swap-k1-v5-derived-lineage-v1", "source_raw_sha256": None, "event_analysis": analysis, "derived_bytes": derived_bytes})

    # Canonical raw identity is relative to the transferred runner directory,
    # matching the remote SHA-256 namespace; the derived sidecar lives beside
    # that runner directory and is intentionally outside the raw aggregate.
    file_hashes = evidence_file_hashes(runner)
    if len(file_hashes) != 18:
        raise SystemExit(f"expected 18 V5 raw evidence files, found {len(file_hashes)}")
    actual_aggregate = aggregate_hash(file_hashes)
    if actual_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"raw aggregate mismatch: {actual_aggregate} != {args.expected_aggregate_sha256}")
    manifest_hash = sha256_file(runner / "SHA256SUMS")
    if manifest_hash != args.remote_manifest_sha256:
        raise SystemExit("remote/local V5 SHA256SUMS hash mismatch")
    if sha256_file(runner / "stdout.log") != args.remote_stdout_sha256:
        raise SystemExit("remote/local V5 stdout hash mismatch")
    if sha256_file(runner / "stderr.log") != args.remote_stderr_sha256:
        raise SystemExit("remote/local V5 stderr hash mismatch")
    mismatches = checksum_mismatches(runner)
    if mismatches:
        raise SystemExit(f"V5 declared checksum mismatch set is non-empty: {mismatches}")

    # Bind the derived sidecar to the raw aggregate after the aggregate is
    # independently checked; the sidecar is not part of the raw identity.
    sidecar_doc = read_json(sidecar)
    sidecar_doc["source_raw_sha256"] = actual_aggregate
    write_json(sidecar, sidecar_doc)
    sidecar_hash = sha256_file(sidecar)

    ledger_path = args.master_root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    by_id = {row["master_row_id"]: row for row in rows}
    row = by_id.get("SWAP-K1")
    if row is None:
        raise SystemExit("missing SWAP-K1 row")

    for key, value in (
        ("attempt_ids", args.attempt_id),
        ("remote_raw_paths", args.remote_attempt_root),
        ("local_raw_paths", str(local_root)),
        ("source_raw_sha256", actual_aggregate),
    ):
        values = row.setdefault(key, [])
        if value not in values:
            values.append(value)
    append_unique(
        row.setdefault("manifest_sha256", []),
        [manifest_hash, sha256_file(runner / "status.json"), sha256_file(runner / "kv_events.json"), sha256_file(runner / "stdout.log"), sha256_file(runner / "stderr.log"), sidecar_hash],
    )
    row.update(
        {
            "execution_state": "EXECUTION_COMPLETE",
            "raw_state": "COMPLETE",
            "backup_state": "VERIFIED",
            "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "VALIDATION_PASS",
            "adoption_state": "ADOPTED",
            "blocker_or_failure": None,
            "claims_supported": append_unique(
                list(row.get("claims_supported", [])),
                [
                    "SWAP-K1-V5 completed three distinct 16384-token requests with 32 output tokens and finish_reason=length; all requests reported num_cached_tokens=0.",
                    "The installed vLLM native CPU KV-offload path emitted both BlockStored and BlockRemoved events on medium=CPU during the forced pressure run.",
                    "All 2048 unique removed block hashes were previously observed in BlockStored events, providing identity-based movement lineage.",
                    "Derived full-block byte accounting is bound to the measured runtime cache shape, block size 16, BF16 dtype, and vLLM event schema sidecar.",
                ],
            ),
            "claims_forbidden": append_unique(
                list(row.get("claims_forbidden", [])),
                [
                    "SWAP-K2 device/host capacity fit or held-out sweep from K1.",
                    "SWAP-K3 serving tail, preemption, queue, throughput, or failure-denominator claims.",
                    "SWAP-K5 host-pool exhaustion, fallback, reject, or backpressure claims.",
                    "Swap latency, bandwidth, copy completion timing, or end-to-end KV-swap performance from K1.",
                    "A clean no-swap control output equivalence claim beyond the recorded output/finish correctness fields.",
                ],
            ),
            "contamination_flags": append_unique(
                list(row.get("contamination_flags", [])),
                [
                    "EVENT_API_BLOCK_SIZE_FIELD_ZERO_PARENT_NULL_AND_TOKEN_METADATA_EMPTY",
                    "DERIVED_BYTES_FROM_RUNTIME_CACHE_SHAPE_AND_BF16_DTYPE",
                    "V5_GPU_FINAL_SNAPSHOT_CAPTURED_BEFORE_PROCESS_EXIT",
                    "NO_K1_LATENCY_OR_COPY_COMPLETION_METRIC",
                ],
            ),
            "next_action": "Run SWAP-K2 host-capacity sweep using the V5-derived block count/bytes and frozen fit/held-out percentages; preserve full-block rounding and capacity failures.",
            "last_transition_record": "MR7-SWAP-K1-V5-PROMOTION",
        }
    )

    transition = {
        "transition_id": "MR7-SWAP-K1-V5-PROMOTION",
        "timestamp_utc": now_utc(),
        "changed_rows": ["SWAP-K1"],
        "reason": "V5 distinct-prompt pressure run passed output/finish correctness and captured CPU BlockStored/BlockRemoved events with identity-subset lineage; promote K1 forced movement while keeping K2/K3/K5 claims separate.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": args.attempt_id,
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": manifest_hash,
        "checksum_manifest_mismatches": [],
        "derived_sidecar": str(sidecar),
        "derived_sidecar_sha256": sidecar_hash,
        "event_analysis": {k: v for k, v in analysis.items() if k not in {"first_stored_sequence_by_hash", "first_removed_sequence_by_hash", "timeline"}},
        "derived_bytes": {k: v for k, v in derived_bytes.items() if k != "lineage"},
        "remote_local_hashes_verified": True,
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
            "status": "VALIDATION_PASS_FORCED_MOVEMENT",
            "request_count": 3,
            "context_tokens_per_request": 16384,
            "total_context_tokens": 49152,
            "distinct_prompt_sequences": 3,
            "num_cached_tokens": [0, 0, 0],
            "block_stored_event_count_declared": analysis["declared_block_stored_event_count"],
            "block_removed_event_count_declared": analysis["declared_block_removed_event_count"],
            "decoded_block_stored_message_count": analysis["decoded_block_stored_message_count"],
            "decoded_block_removed_message_count": analysis["decoded_block_removed_message_count"],
            "stored_unique_block_count": analysis["stored_unique_hash_count"],
            "removed_unique_block_count": analysis["removed_unique_hash_count"],
            "removed_hashes_subset_of_stored": analysis["removed_hashes_subset_of_stored"],
            "derived_bytes_per_full_block": bytes_per_block,
            "derived_removed_unique_block_bytes": derived_bytes["removed_unique_block_bytes"],
            "raw_file_count": len(file_hashes),
            "file_set_sha256": actual_aggregate,
            "checksum_manifest_sha256": manifest_hash,
            "derived_sidecar": str(sidecar),
            "derived_sidecar_sha256": sidecar_hash,
            "validation_state": "VALIDATION_PASS",
            "claims_forbidden": ["K2 capacity", "K3 serving", "K5 exhaustion", "swap performance"],
        }
    )
    write_json(inventory_path, inventory)

    trigger_path = args.master_root / "trigger_adjudication.json"
    trigger = read_json(trigger_path)
    for entry in trigger.get("entries", []):
        if entry.get("trigger_id") == "SWAP-K1":
            entry["trigger_state"] = "TRIGGERED"
            evidence = "SWAP-K1-V5 forced distinct-prompt pressure captured CPU BlockStored/BlockRemoved events with removed-hash subset lineage."
            if evidence not in entry.setdefault("observed_evidence", []):
                entry["observed_evidence"].append(evidence)
            if actual_aggregate not in entry.setdefault("source_evidence_hashes", []):
                entry["source_evidence_hashes"].append(actual_aggregate)
    write_json(trigger_path, trigger)

    gap_path = args.master_root / "gap_register.json"
    gap = read_json(gap_path)
    gap_id = "GAP-SWAP-K1-EVENT-TRACE-V4"
    for entry in gap.get("entries", []):
        if entry.get("gap_id") == gap_id:
            entry["status"] = "CLOSED_WITH_LIMITATION"
            entry["source"] = str(local_root)
            entry["consequence"] = "V5 closed the forced movement gate with CPU BlockStored/BlockRemoved identity lineage; event-level block_size/parent metadata remains incomplete and K2 must own exact capacity sweep accounting."
    gap["entries"].append(
        {
            "gap_id": "GAP-SWAP-K1-EVENT-BYTES-METADATA",
            "status": "CLOSED_WITH_LIMITATION",
            "source": str(sidecar),
            "consequence": "K1 bytes are derived from measured runtime shape/dtype; raw event block_size=0 and parent/token metadata are retained as limitations, so no K1 copy-performance claim is allowed.",
        }
    )
    write_json(gap_path, gap)

    claims = read_json(args.master_root / "claim_boundary_register.json")
    allowed = claims.setdefault("claims_allowed_now", [])
    append_unique(
        allowed,
        [
            "SWAP-K1 forced native KV movement observed through CPU BlockStored/BlockRemoved events with hash-subset lineage",
            "SWAP-K1 distinct-prompt output/finish correctness for the 3x16384 pressure workload",
        ],
    )
    forbidden = claims.setdefault("claims_forbidden_now", [])
    append_unique(
        forbidden,
        [
            "SWAP-K1 swap latency, bandwidth, copy completion, or serving benefit",
            "SWAP-K2/K3/K5 results before their own gates",
        ],
    )
    write_json(args.master_root / "claim_boundary_register.json", claims)

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
    queue["next_gpu_unit"] = "SWAP-K2"
    queue["ready_gpu_units"] = ["SWAP-K2"]
    queue["dispatch_guards"] = [
        "MR2 read-only preflight clear",
        "no foreign serving/GPU process at dispatch",
        "new-session four-guard canary validated and locally backed up",
        "SWAP-K0 native KV-offload capability initialized",
        "SWAP-K1-V5 forced movement passed with raw backup verified",
        "SWAP-K2 must use V5-derived block count/bytes and full-block rounding; no percentage-only fit claim",
        "ADOPT-EXPERT-CATALOG remains a prerequisite for OFF-E-RT0/OFF-W0",
        "no filler workload",
        "raw namespace independent",
    ]
    write_json(queue_path, queue)

    backup = read_json(args.master_root / "local_backup_manifest.json")
    backup.setdefault("verified_local_sources", []).append(
        {
            "attempt_id": args.attempt_id,
            "path": str(local_root),
            "file_count": len(file_hashes),
            "file_set_sha256": actual_aggregate,
            "manifest_sha256": manifest_hash,
            "derived_sidecar": str(sidecar),
            "derived_sidecar_sha256": sidecar_hash,
            "remote_local_hashes_verified": True,
            "status": "SWAP-K1-V5 raw backup verified; forced movement gate passed",
        }
    )
    backup.setdefault("phase7_attempt_backups", []).append(
        {
            "attempt_id": args.attempt_id,
            "remote_attempt": args.remote_attempt_root,
            "local_attempt": str(local_root),
            "status": "VERIFIED_RAW_VALIDATION_PASS_WITH_LIMITATIONS",
            "file_count": len(file_hashes),
            "file_set_sha256": actual_aggregate,
            "derived_sidecar_sha256": sidecar_hash,
        }
    )
    write_json(args.master_root / "local_backup_manifest.json", backup)

    write_json(
        args.master_root / "reviews" / "MR7-SWAP-K1-V5-PROMOTION.json",
        {
            "schema_version": "phase7-combined-master-swap-k1-v5-review-v1",
            "reviewed_at_utc": transition["timestamp_utc"],
            "attempt_id": args.attempt_id,
            "remote_raw_path": args.remote_attempt_root,
            "local_raw_path": str(local_root),
            "execution_state": "EXECUTION_COMPLETE",
            "request_correctness": "PASS",
            "forced_movement_gate": "PASS",
            "raw_backup": "VERIFIED",
            "event_analysis": {k: v for k, v in analysis.items() if k not in {"first_stored_sequence_by_hash", "first_removed_sequence_by_hash", "timeline"}},
            "derived_bytes": derived_bytes,
            "raw_file_count": len(file_hashes),
            "raw_file_set_sha256": actual_aggregate,
            "checksum_manifest_sha256": manifest_hash,
            "derived_sidecar_sha256": sidecar_hash,
            "remote_local_hashes_verified": True,
            "validation_state": "VALIDATION_PASS",
            "review_state": "REVIEW_WITH_LIMITATION",
            "claims_forbidden": ["K2/K3/K5", "swap performance", "clean-control equivalence beyond recorded fields"],
            "next_ready_unit": "SWAP-K2",
        },
    )
    write_json(
        args.master_root / "checkpoints" / "MR7-SWAP-K1-V5-PROMOTION.json",
        {
            "schema_version": "phase7-combined-master-checkpoint-v1",
            "checkpoint_id": "MR7-SWAP-K1-V5-PROMOTION",
            "timestamp_utc": transition["timestamp_utc"],
            "execution_ledger_sha256": execution_hash,
            "remaining_ledger_sha256": sha256_file(args.master_root / "master_remaining_ledger.json"),
            "required_closed_count": len(rows) - len(remaining),
            "required_remaining_count": len(remaining),
            "next_ready_gpu_unit": "SWAP-K2",
            "raw_file_set_sha256": actual_aggregate,
            "derived_sidecar_sha256": sidecar_hash,
        },
    )

    print(
        json.dumps(
            {
                "attempt_id": args.attempt_id,
                "validation_state": "VALIDATION_PASS",
                "review_state": "REVIEW_WITH_LIMITATION",
                "raw_file_count": len(file_hashes),
                "raw_file_set_sha256": actual_aggregate,
                "checksum_manifest_sha256": manifest_hash,
                "derived_sidecar_sha256": sidecar_hash,
                "decoded_block_stored_message_count": analysis["decoded_block_stored_message_count"],
                "decoded_block_removed_message_count": analysis["decoded_block_removed_message_count"],
                "removed_unique_hash_count": analysis["removed_unique_hash_count"],
                "removed_hashes_subset_of_stored": analysis["removed_hashes_subset_of_stored"],
                "derived_bytes_per_full_block": bytes_per_block,
                "derived_removed_unique_block_bytes": derived_bytes["removed_unique_block_bytes"],
                "execution_ledger_sha256": execution_hash,
                "required_closed_count": len(rows) - len(remaining),
                "required_remaining_count": len(remaining),
                "next_ready_gpu_unit": "SWAP-K2",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
