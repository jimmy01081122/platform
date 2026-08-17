#!/usr/bin/env python3
"""Adopt the complete V6 expert object catalog into the combined master."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from promote_combined_master_swap_k1_v5 import (
    aggregate_hash, append_unique, evidence_file_hashes, legally_closed,
    now_utc, read_json, sha256_file, write_json,
)


EXPECTED = {
    "expert_catalog.json": "9e7548cf9349972368b423a3fa8273dd30dc77c23722c99bc5e57ee99dc0a104",
    "manifest.json": "ec011bfca110bc959d3dd14b2e142da42127130264e6ed2f6b7a52869d32ff6a",
    "measurements.jsonl": "5293df9edbdc63195b4cb529e28638d25db467941a2c60693463a45561af94a7",
    "content_canary.json": "3152d8613061f0ac020b49e7c0a4bf7be44d8e5f3327d64a6d618efc241000f8",
    "topology.json": "a5585b64011d97731220ece2030cb48b8d6cf2a793d30cb96543f067f53c1699",
}
EXPECTED_AGGREGATE = "732cf965376ae0bbc20750e71d9f60649e6331c9530359a3a32f52096e11898b"
OBJECT_BYTES = 352321536
TOTAL_BYTES = 90194313216


def append_record_once(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    if not any(item.get("attempt_id") == record.get("attempt_id") for item in records):
        records.append(record)


def rebuild_remaining(root: Path, ledger: dict[str, Any], execution_hash: str) -> tuple[int, int]:
    rows = ledger["rows"]
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
    return len(rows) - len(remaining), len(remaining)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--catalog-root", type=Path, required=True)
    args = parser.parse_args()
    source = args.catalog_root
    for name, expected_hash in EXPECTED.items():
        if not (source / name).is_file() or sha256_file(source / name) != expected_hash:
            raise SystemExit(f"expert catalog source hash mismatch: {name}")
    raw_hashes = evidence_file_hashes(source)
    if aggregate_hash(raw_hashes) != EXPECTED_AGGREGATE:
        raise SystemExit("expert catalog aggregate mismatch")
    manifest = read_json(source / "manifest.json")
    catalog = read_json(source / "expert_catalog.json")
    content = read_json(source / "content_canary.json")
    objects = list(catalog.get("objects", {}).values())
    if (
        manifest.get("status") != "PASS"
        or manifest.get("runtime_catalog_schema") != "phase7-expert-catalog-v2"
        or manifest.get("measurement_class") != "GPU_TRANSFER_PROBE"
        or catalog.get("object_count") != 256
        or len(objects) != 256
        or len({item["global_object_id"] for item in objects}) != 256
    ):
        raise SystemExit("expert catalog identity/count gate failed")
    for item in objects:
        if (
            item.get("materialized_bytes") != OBJECT_BYTES
            or item.get("packed_bytes") != OBJECT_BYTES
            or item.get("aligned_bytes") != OBJECT_BYTES
            or item.get("alignment_bytes") != 256
            or item.get("object_granularity") != "layer_expert_three_tensor_bundle"
            or item.get("mutable") is not False
            or item.get("writeback_allowed") is not False
            or item.get("eviction_semantics") != "RELEASE_DISCARD_AND_RELOAD"
            or len(item.get("tensors", [])) != 3
        ):
            raise SystemExit(f"expert catalog object gate failed: {item.get('global_object_id')}")
    if any(sum(item[key] for item in objects) != TOTAL_BYTES for key in (
        "materialized_bytes", "packed_bytes", "aligned_bytes"
    )):
        raise SystemExit("expert catalog total-byte conservation failed")
    if content.get("status") != "PASS" or content.get("content_equal_after_h2d_d2h") is not True:
        raise SystemExit("expert catalog content canary failed")

    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    transition_id = "MR5-EXPERT-CATALOG-V6-ADOPTION"
    for row_id in ("ADOPT-EXPERT-CATALOG", "CATALOG-X0"):
        row = next(row for row in rows if row["master_row_id"] == row_id)
        row.update({
            "execution_state": "EXECUTION_COMPLETE", "raw_state": "COMPLETE",
            "backup_state": "VERIFIED", "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "VALIDATION_PASS", "adoption_state": "ADOPTED",
            "blocker_or_failure": None,
            "claims_supported": append_unique(list(row.get("claims_supported", [])), [
                "All 256 Mixtral layer-expert objects are catalogued as immutable BF16 three-tensor bundles with exact materialized, packed, 256-byte descriptor-aligned bytes, ownership and discard/reload semantics.",
                "Each expert object is 352,321,536 bytes and all expert objects conserve to 90,194,313,216 bytes.",
            ]),
            "claims_forbidden": append_unique(list(row.get("claims_forbidden", [])), [
                "Dynamic runtime expert residency/offload capability or performance from the catalog alone.",
                "CUDA allocator reservation, compression, quantized packing, or writeback claims.",
            ]),
            "contamination_flags": append_unique(list(row.get("contamination_flags", [])), [
                "CATALOG_ALIGNMENT_IS_TRANSFER_DESCRIPTOR_ALIGNMENT_ALLOCATOR_RESERVATION_EXCLUDED",
                "CATALOG_CHECKPOINT_IDENTITY_NO_NEW_FULL_MODEL_HASH_RESCAN",
            ]),
            "next_action": "Run OFF-E-RT0 runtime dynamic-expert-residency capability/semantics canary.",
            "last_transition_record": transition_id,
        })
        for key, value in (
            ("attempt_ids", "EXPERT-CATALOG-V6-TRANSFER-V4-EQ"),
            ("local_raw_paths", str(source)),
            ("source_raw_sha256", EXPECTED_AGGREGATE),
        ):
            values = row.setdefault(key, [])
            if value not in values:
                values.append(value)
        append_unique(row.setdefault("manifest_sha256", []), list(EXPECTED.values()))
    transition = {
        "transition_id": transition_id, "timestamp_utc": now_utc(),
        "changed_rows": ["ADOPT-EXPERT-CATALOG", "CATALOG-X0"],
        "reason": "Adopt complete expert catalog with exact materialized/packed/aligned bytes, granularity, ownership and immutable discard/reload contract.",
        "prior_ledger_sha256": prior_hash, "source_path": str(source),
        "source_raw_sha256": EXPECTED_AGGREGATE, "object_count": 256,
        "object_bytes": OBJECT_BYTES, "total_expert_bytes": TOTAL_BYTES,
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition_id
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(1 for row in rows if legally_closed(row))
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)
    closed, remaining = rebuild_remaining(root, ledger, execution_hash)

    inventory_path = root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    append_record_once(inventory.setdefault("expert_catalog_adoptions", []), {
        "attempt_id": "EXPERT-CATALOG-V6-TRANSFER-V4-EQ", "status": "VALIDATION_PASS_WITH_LIMITATIONS",
        "local_raw_path": str(source), "file_count": len(raw_hashes),
        "file_set_sha256": EXPECTED_AGGREGATE, "object_count": 256,
        "object_bytes": OBJECT_BYTES, "total_expert_bytes": TOTAL_BYTES,
    })
    write_json(inventory_path, inventory)
    backup_path = root / "local_backup_manifest.json"
    backup = read_json(backup_path)
    append_record_once(backup.setdefault("phase7_attempt_backups", []), {
        "attempt_id": "EXPERT-CATALOG-V6-TRANSFER-V4-EQ", "local_attempt": str(source),
        "status": "VERIFIED_EXISTING_RAW_ADOPTED", "file_count": len(raw_hashes),
        "file_set_sha256": EXPECTED_AGGREGATE,
    })
    write_json(backup_path, backup)
    gap_path = root / "gap_register.json"
    gap = read_json(gap_path)
    for item in gap.get("entries", []):
        if item.get("gap_id") == "GAP-CATALOG":
            item.update({
                "status": "CLOSED_WITH_LIMITATION", "source": str(source),
                "consequence": "Exact object bytes, granularity, ownership and immutable discard/reload semantics are closed; runtime dynamic residency remains separately gated by OFF-E-RT0.",
            })
    write_json(gap_path, gap)
    claims_path = root / "claim_boundary_register.json"
    claims = read_json(claims_path)
    append_unique(claims.setdefault("claims_allowed_now", []), [
        "Complete 256-object Mixtral BF16 expert catalog with exact transfer-accounting bytes and immutable ownership semantics"
    ])
    write_json(claims_path, claims)
    queue_path = root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue.update({
        "generated_from_execution_ledger_sha256": execution_hash,
        "next_gpu_unit": "OFF-E-RT0", "ready_gpu_units": ["OFF-E-RT0"],
        "next_gate_action": "FREEZE_AND_RUN_OFF_E_RT0_CAPABILITY_CANARY",
        "dispatch_guards": [
            "MR2 read-only preflight clear", "no foreign serving/GPU process at dispatch",
            "new-session four-guard canary validated and locally backed up",
            "CATALOG-X0 exact object/byte/ownership contract validated",
            "OFF-E-RT0 is a capability/semantics canary, not a performance pass",
            "cpu_offload_gb or layer-prefetch weight offload must not be relabeled dynamic expert residency",
            "no filler workload", "raw namespace independent",
        ],
    })
    write_json(queue_path, queue)
    review = {
        "schema_version": "phase7-combined-master-expert-catalog-adoption-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"],
        "attempt_id": "EXPERT-CATALOG-V6-TRANSFER-V4-EQ", "source": str(source),
        "raw_file_set_sha256": EXPECTED_AGGREGATE, "object_count": 256,
        "object_bytes": OBJECT_BYTES, "total_expert_bytes": TOTAL_BYTES,
        "validation_state": "VALIDATION_PASS", "review_state": "REVIEW_WITH_LIMITATION",
        "next_ready_unit": "OFF-E-RT0",
    }
    write_json(root / "reviews" / "MR5-EXPERT-CATALOG-V6-ADOPTION.json", review)
    write_json(root / "checkpoints" / "MR5-EXPERT-CATALOG-V6-ADOPTION.json", {
        "schema_version": "phase7-combined-master-checkpoint-v1",
        "checkpoint_id": transition_id, "timestamp_utc": transition["timestamp_utc"],
        "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(root / "master_remaining_ledger.json"),
        "required_closed_count": closed, "required_remaining_count": remaining,
        "next_ready_gpu_unit": "OFF-E-RT0", "source_raw_sha256": EXPECTED_AGGREGATE,
    })
    print(json.dumps({
        "validation_state": "VALIDATION_PASS", "object_count": 256,
        "object_bytes": OBJECT_BYTES, "total_expert_bytes": TOTAL_BYTES,
        "execution_ledger_sha256": execution_hash,
        "required_closed_count": closed, "required_remaining_count": remaining,
        "next_ready_gpu_unit": "OFF-E-RT0",
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
