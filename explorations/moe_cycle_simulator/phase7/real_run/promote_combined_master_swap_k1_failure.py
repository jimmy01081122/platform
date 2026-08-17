#!/usr/bin/env python3
"""Record a technical SWAP-K1 failure without closing the scientific gate."""

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


def runner_dir(local_root: Path) -> Path:
    candidates = sorted(path for path in local_root.glob("*") if path.is_dir())
    if len(candidates) != 1:
        raise SystemExit(f"expected one transferred runner directory, found {len(candidates)}")
    return candidates[0]


def file_hashes(root: Path) -> dict[str, str]:
    return {
        f"./{path.relative_to(root).as_posix()}": sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    }


def aggregate_hash(hashes: dict[str, str]) -> str:
    payload = "".join(f"{hashes[path]}  {path}\n" for path in sorted(hashes)).encode("utf-8")
    return sha256_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    args = parser.parse_args()

    local_root = args.local_attempt_root
    if not local_root.is_dir():
        raise SystemExit(f"local raw backup is missing: {local_root}")
    run = runner_dir(local_root)
    status = read_json(run / "status.json")
    error = read_json(run / "error.json")
    exact = read_json(run / "exact_argv.json")
    if status.get("status") != "FAIL" or status.get("execution_state") != "EXECUTION_FAILED":
        raise SystemExit("attempt is not the recorded SWAP-K1 execution failure")
    if "Engine core initialization failed" not in error.get("message", ""):
        raise SystemExit("failure does not match the expected engine-core initialization failure")
    if exact.get("request_fixture", {}).get("base_token_count") != 153:
        raise SystemExit("unexpected fixture identity; refuse to classify this attempt")

    hashes = file_hashes(run)
    actual_aggregate = aggregate_hash(hashes)
    if actual_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"raw aggregate mismatch: {actual_aggregate} != {args.expected_aggregate_sha256}")
    checksum_hash = sha256_file(run / "SHA256SUMS")

    ledger_path = args.master_root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    by_id = {row["master_row_id"]: row for row in rows}
    row = by_id.get("SWAP-K1")
    if row is None:
        raise SystemExit("missing SWAP-K1 row")
    lineage = row.setdefault("repair_lineage", [])
    lineage.append({
        "attempt_id": args.attempt_id,
        "failure_class": "TECHNICAL_SETUP_FAILURE",
        "failure": "Engine-core CUDA OOM during FlashInfer MoE workspace initialization before valid K1 workload execution.",
        "fixture_issue": "Initial inline fixture contained 153 base tokens instead of the frozen 128-token fixture.",
        "repair_required": "New attempt namespace with exact 128-token fixture and reduced prefill workspace pressure.",
        "source_raw_sha256": actual_aggregate,
    })
    row.update({
        "attempt_ids": [*row.get("attempt_ids", []), args.attempt_id],
        "remote_raw_paths": [*row.get("remote_raw_paths", []), args.remote_attempt_root],
        "local_raw_paths": [*row.get("local_raw_paths", []), str(local_root)],
        "manifest_sha256": [*row.get("manifest_sha256", []), sha256_file(run / "exact_argv.json"), sha256_file(run / "status.json"), checksum_hash],
        "source_raw_sha256": [*row.get("source_raw_sha256", []), actual_aggregate],
        "execution_state": "EXECUTION_FAILED",
        "raw_state": "COMPLETE",
        "backup_state": "VERIFIED",
        "review_state": "REVIEW_WITH_LIMITATION",
        "validation_state": "UNVERIFIED",
        "adoption_state": "SUPPLEMENT_REQUIRED",
        "blocker_or_failure": "TECHNICAL_FAILURE: engine-core CUDA OOM during FlashInfer MoE workspace initialization; no valid block-movement gate was reached.",
        "claims_supported": [
            "The isolated attempt preserved a real engine-core CUDA OOM and its exact argv, fixture, GPU snapshots, stdout, and stderr.",
            "The attempt did not provide valid SWAP-K1 output or block-movement evidence.",
        ],
        "claims_forbidden": [
            "SWAP-K1 block swap-out/in PASS or FAIL based on this pre-workload initialization failure.",
            "Any KV movement, output, latency, or preemption claim from this attempt.",
        ],
        "contamination_flags": [
            "TECHNICAL_ENGINE_INIT_CUDA_OOM",
            "INVALID_FIXTURE_BASE_TOKEN_COUNT_153_EXPECTED_128",
        ],
        "next_action": "Repair fixture and bounded prefill configuration in a new SWAP-K1 attempt namespace, then rerun only the affected gate.",
        "last_transition_record": "MR7-SWAP-K1-TECHNICAL-FAILURE",
    })
    transition = {
        "transition_id": "MR7-SWAP-K1-TECHNICAL-FAILURE",
        "timestamp_utc": now_utc(),
        "changed_rows": ["SWAP-K1"],
        "reason": "SWAP-K1 engine initialization failed with CUDA OOM before the valid workload gate; preserve raw evidence and repair in a new namespace.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": args.attempt_id,
        "raw_file_count": len(hashes),
        "raw_file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": checksum_hash,
        "failure_class": "TECHNICAL_SETUP_FAILURE",
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition["transition_id"]
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    inventory_path = args.master_root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    inventory["swap_k1_failed_attempts"] = [
        {
            "attempt_id": args.attempt_id,
            "remote_raw_path": args.remote_attempt_root,
            "local_raw_path": str(local_root),
            "status": "TECHNICAL_FAILURE_BEFORE_VALID_WORKLOAD",
            "failure_class": "TECHNICAL_SETUP_FAILURE",
            "error": error.get("message"),
            "fixture_base_token_count": exact.get("request_fixture", {}).get("base_token_count"),
            "raw_file_count": len(hashes),
            "file_set_sha256": actual_aggregate,
        }
    ]
    write_json(inventory_path, inventory)

    gap_path = args.master_root / "gap_register.json"
    gap = read_json(gap_path)
    gap["entries"].append({
        "gap_id": "GAP-SWAP-K1-TECHNICAL-FAILURE",
        "status": "SUPPLEMENT_REQUIRED",
        "source": str(local_root),
        "consequence": "No SWAP-K1 block-movement result; repair and rerun only the affected gate with the frozen 128-token fixture.",
    })
    write_json(gap_path, gap)

    claim_path = args.master_root / "claim_boundary_register.json"
    claims = read_json(claim_path)
    forbidden = claims.setdefault("claims_forbidden_now", [])
    claim = "SWAP-K1 block-movement result from the failed engine-initialization attempt"
    if claim not in forbidden:
        forbidden.append(claim)
    write_json(claim_path, claims)

    remaining = [item for item in rows if item.get("validation_state") not in {"VALIDATION_PASS", "NEGATIVE_EVIDENCE", "UNAVAILABLE_WITH_CONSEQUENCE"} or item.get("backup_state") != "VERIFIED" or item.get("adoption_state") not in {"ADOPTED", "NOT_APPLICABLE"} or item.get("trigger_state") not in {"NOT_CONDITIONAL", "NOT_TRIGGERED_WITH_EVIDENCE", "OWNER_WAIVED"}]
    conditional = [item["master_row_id"] for item in remaining if item.get("trigger_state") == "PENDING"]
    blocked = [{"id": item["master_row_id"], "reason": item["blocker_or_failure"]} for item in remaining if item.get("blocker_or_failure")]
    write_json(args.master_root / "master_remaining_ledger.json", {
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
    })

    queue_path = args.master_root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue["generated_from_execution_ledger_sha256"] = execution_hash
    queue["next_gpu_unit"] = "SWAP-K1"
    queue["ready_gpu_units"] = ["SWAP-K1"]
    queue["dispatch_guards"] = [
        "MR2 read-only preflight clear",
        "no foreign serving/GPU process at dispatch",
        "new-session four-guard canary validated and locally backed up",
        "SWAP-K0 native KV-offload capability initialized; SWAP-K1 repair required",
        "new attempt namespace required after technical failure",
        "ADOPT-EXPERT-CATALOG remains a prerequisite for OFF-E-RT0/OFF-W0",
        "no filler workload",
        "raw namespace independent",
    ]
    write_json(queue_path, queue)

    write_json(args.master_root / "reviews" / "MR7-SWAP-K1-TECHNICAL-FAILURE.json", {
        "schema_version": "phase7-combined-master-swap-k1-failure-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"],
        "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": str(local_root),
        "failure_class": "TECHNICAL_SETUP_FAILURE",
        "failure": error.get("message"),
        "fixture_base_token_count": exact.get("request_fixture", {}).get("base_token_count"),
        "expected_fixture_base_token_count": 128,
        "raw_file_count": len(hashes),
        "raw_file_set_sha256": actual_aggregate,
        "validation_state": "UNVERIFIED",
        "promotion_status": "REPAIR_REQUIRED",
        "next_ready_unit": "SWAP-K1",
    })
    write_json(args.master_root / "checkpoints" / "MR7-SWAP-K1-TECHNICAL-FAILURE.json", {
        "schema_version": "phase7-combined-master-checkpoint-v1",
        "checkpoint_id": "MR7-SWAP-K1-TECHNICAL-FAILURE",
        "timestamp_utc": transition["timestamp_utc"],
        "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(args.master_root / "master_remaining_ledger.json"),
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "next_ready_gpu_unit": "SWAP-K1",
    })

    print(json.dumps({
        "attempt_id": args.attempt_id,
        "failure_class": "TECHNICAL_SETUP_FAILURE",
        "raw_file_count": len(hashes),
        "raw_file_set_sha256": actual_aggregate,
        "execution_ledger_sha256": execution_hash,
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "next_ready_gpu_unit": "SWAP-K1",
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
