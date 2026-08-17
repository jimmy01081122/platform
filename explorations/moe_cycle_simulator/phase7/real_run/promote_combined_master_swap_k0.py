#!/usr/bin/env python3
"""Promote the bounded native-KV-offload SWAP-K0 capability canary.

SWAP-K0 is closed only for the capability question.  The canary does not
promote a block-level swap event, swap performance, or a negative claim about
runtime movement.  Those remain in the triggered SWAP-K1/K2/K3/K5 branch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


TRIGGERED_CHILDREN = ("SWAP-K1", "SWAP-K2", "SWAP-K3", "SWAP-K5")


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


def find_runner_dir(attempt_root: Path) -> Path:
    runner_root = attempt_root / "runner_runs"
    if runner_root.is_dir():
        candidates = sorted(path for path in runner_root.glob("*") if path.is_dir())
    else:
        # scp of the remote runner directory is intentionally stored as a
        # local attempt root whose sole child is the timestamped runner dir.
        candidates = sorted(path for path in attempt_root.glob("*") if path.is_dir())
    if len(candidates) != 1:
        raise ValueError(f"expected one runner directory, found {len(candidates)}")
    return candidates[0]


def declared_checksum_mismatches(runner_dir: Path) -> list[str]:
    checksum_path = runner_dir / "SHA256SUMS"
    mismatches: list[str] = []
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split(None, 1)
        path = runner_dir / relative.strip()
        actual = sha256_file(path)
        if actual != expected:
            mismatches.append(relative.strip())
    return mismatches


def contains(text: str, needle: str) -> bool:
    return needle in text


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
    runner_dir = find_runner_dir(local_root)
    status = read_json(runner_dir / "status.json")
    result = read_json(runner_dir / "result.json")
    requested = read_json(runner_dir / "requested_engine_args.json")
    capability = read_json(local_root / "" / runner_dir.name / "capability_manifest.json")
    stdout = (runner_dir / "stdout.log").read_text(encoding="utf-8")
    stderr = (runner_dir / "stderr.log").read_text(encoding="utf-8")

    if status.get("status") != "PASS" or status.get("execution_state") != "EXECUTION_COMPLETE":
        raise SystemExit("SWAP-K0 status is not a completed PASS")
    records = result.get("records")
    if not isinstance(records, list) or len(records) != 1:
        raise SystemExit("SWAP-K0 result must contain exactly one measured record")
    record = records[0]
    if (
        record.get("input_token_count") != 128
        or record.get("output_token_count") != 32
        or record.get("finish_reason") != "length"
    ):
        raise SystemExit("SWAP-K0 request correctness gate failed")
    if requested.get("kv_offloading_size") != 1.0 or requested.get("kv_offloading_backend") != "native":
        raise SystemExit("SWAP-K0 native KV-offload request is not frozen as expected")
    if requested.get("cpu_offload_gb") != 0 or requested.get("quantization") is not None:
        raise SystemExit("SWAP-K0 clean weight/offload identity is invalid")
    required_log_evidence = (
        "Creating v1 connector with name: OffloadingConnector",
        "Creating offloading spec with name: CPUOffloadingSpec",
        "Allocating a cross layer KV cache",
        "Allocating 1 CPU tensors",
    )
    if not all(contains(stdout, needle) for needle in required_log_evidence):
        raise SystemExit("native KV-offload initialization evidence is incomplete")
    if status.get("object_level_block_trace") != "UNAVAILABLE_UNLESS_RUNTIME_EXPOSES":
        raise SystemExit("unexpected block-trace boundary")

    # Canonicalize the transferred file set at the runner directory, matching
    # the remote per-file hash namespace and avoiding a local scp wrapper name
    # becoming part of the evidence identity.
    file_hashes = evidence_file_hashes(runner_dir)
    if len(file_hashes) != 15:
        raise SystemExit(f"expected 15 SWAP-K0 evidence files, found {len(file_hashes)}")
    actual_aggregate = aggregate_hash(file_hashes)
    if actual_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"raw aggregate mismatch: {actual_aggregate} != {args.expected_aggregate_sha256}")
    checksum_manifest_hash = sha256_file(runner_dir / "SHA256SUMS")
    checksum_mismatches = declared_checksum_mismatches(runner_dir)
    if checksum_mismatches != ["stdout.log"]:
        raise SystemExit(f"unexpected SHA256SUMS mismatch set: {checksum_mismatches}")

    ledger_path = args.master_root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    by_id = {row["master_row_id"]: row for row in rows}
    if "SWAP-K0" not in by_id:
        raise SystemExit("missing SWAP-K0 row")
    row = by_id["SWAP-K0"]
    row.update(
        {
            "attempt_ids": [args.attempt_id],
            "remote_raw_paths": [args.remote_attempt_root],
            "local_raw_paths": [str(local_root)],
            "manifest_sha256": [
                sha256_file(local_root / runner_dir.name / "capability_manifest.json"),
                sha256_file(runner_dir / "status.json"),
                checksum_manifest_hash,
            ],
            "source_raw_sha256": [actual_aggregate],
            "execution_state": "EXECUTION_COMPLETE",
            "raw_state": "COMPLETE",
            "backup_state": "VERIFIED",
            "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "VALIDATION_PASS",
            "adoption_state": "ADOPTED",
            "blocker_or_failure": None,
            "claims_supported": [
                "vLLM 0.23.0 accepted and initialized the native CPU KV-offload path with a 1.0 GiB offloading buffer.",
                "The bounded real Mixtral request completed with 128 input tokens, 32 output tokens, and finish_reason=length.",
                "Runtime logs captured OffloadingConnector, CPUOffloadingSpec, cross-layer KV allocation, and one CPU KV tensor allocation.",
            ],
            "claims_forbidden": [
                "A verified block-level swap-out/in event, block identity/lineage, or exact moved bytes from this bounded request.",
                "Runtime KV-swap latency, throughput, tail, preemption, recompute, or fallback performance.",
                "A global negative claim that runtime KV movement cannot occur.",
            ],
            "contamination_flags": [
                "REMOTE_SHA256SUMS_STALE_FOR_STDOUT_FINAL_FLUSH; CURRENT_REMOTE_LOCAL_EVIDENCE_HASHES_MATCH",
                "OBJECT_LEVEL_KV_BLOCK_TRACE_UNAVAILABLE",
            ],
            "next_action": "Run triggered SWAP-K1 with frozen multi-sequence pressure and an explicit block movement observation gate.",
            "last_transition_record": "MR7-SWAP-K0-CAPABILITY-PROMOTION",
        }
    )

    transition = {
        "transition_id": "MR7-SWAP-K0-CAPABILITY-PROMOTION",
        "timestamp_utc": now_utc(),
        "changed_rows": ["SWAP-K0", *TRIGGERED_CHILDREN],
        "reason": "Native vLLM KV-offload capability initialized and bounded real request passed; block-level movement remains unobserved and is not claimed.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": args.attempt_id,
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": checksum_manifest_hash,
        "checksum_manifest_mismatches": checksum_mismatches,
    }
    for child_id in TRIGGERED_CHILDREN:
        child = by_id.get(child_id)
        if child is None:
            raise SystemExit(f"missing triggered child row: {child_id}")
        child["trigger_state"] = "TRIGGERED"
        child["prerequisite_row_ids"] = ["SWAP-K0"]
        child["blocker_or_failure"] = None
        child["next_action"] = f"Run capability-triggered {child_id} under its frozen source contract."
        child["last_transition_record"] = transition["transition_id"]

    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition["transition_id"]
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    inventory_path = args.master_root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    inventory["swap_k0"] = {
        "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": str(local_root),
        "status": "CAPABILITY_VALIDATED_WITH_TRACE_LIMITATION",
        "requested_kv_offloading_size_gib": requested.get("kv_offloading_size"),
        "requested_kv_offloading_backend": requested.get("kv_offloading_backend"),
        "measured_input_tokens": record.get("input_token_count"),
        "measured_output_tokens": record.get("output_token_count"),
        "finish_reason": record.get("finish_reason"),
        "raw_file_count": len(file_hashes),
        "file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": checksum_manifest_hash,
        "checksum_manifest_mismatches": checksum_mismatches,
        "block_trace": "UNAVAILABLE_UNLESS_RUNTIME_EXPOSES",
    }
    write_json(inventory_path, inventory)

    trigger_path = args.master_root / "trigger_adjudication.json"
    trigger = read_json(trigger_path)
    for entry in trigger.get("entries", []):
        if entry.get("trigger_id") in TRIGGERED_CHILDREN:
            entry["trigger_state"] = "TRIGGERED"
            evidence = "SWAP-K0 native offload capability initialized; no block event promoted from bounded canary."
            if evidence not in entry.setdefault("observed_evidence", []):
                entry["observed_evidence"].append(evidence)
            if actual_aggregate not in entry.setdefault("source_evidence_hashes", []):
                entry["source_evidence_hashes"].append(actual_aggregate)
    write_json(trigger_path, trigger)

    gap_path = args.master_root / "gap_register.json"
    gap = read_json(gap_path)
    gap["entries"].append(
        {
            "gap_id": "GAP-SWAP-K0-BLOCK-TRACE",
            "status": "CLOSED_WITH_LIMITATION",
            "source": str(local_root),
            "consequence": "Capability is established; exact block identity, moved bytes, and swap event remain unobserved and block K1-K5 performance claims until their gates run.",
        }
    )
    write_json(gap_path, gap)

    claim_path = args.master_root / "claim_boundary_register.json"
    claims = read_json(claim_path)
    allowed = claims.setdefault("claims_allowed_now", [])
    allowed_claim = "vLLM 0.23.0 native CPU KV-offload capability initialized in a bounded real Mixtral canary"
    if allowed_claim not in allowed:
        allowed.append(allowed_claim)
    forbidden = claims.setdefault("claims_forbidden_now", [])
    for claim in (
        "exact runtime KV block swap-out/in event from SWAP-K0",
        "runtime KV-swap performance or block-lineage claim from SWAP-K0",
    ):
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
        "SWAP-K0 native KV-offload capability initialized; K1 must capture block movement or retain scientific limitation",
        "ADOPT-EXPERT-CATALOG remains a prerequisite for OFF-E-RT0/OFF-W0",
        "no filler workload",
        "raw namespace independent",
    ]
    write_json(queue_path, queue)

    review = {
        "schema_version": "phase7-combined-master-swap-k0-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"],
        "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": str(local_root),
        "runner_status": status.get("status"),
        "request_correctness": "PASS",
        "native_kv_offload_initialization": "OBSERVED",
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": checksum_manifest_hash,
        "checksum_manifest_mismatches": checksum_mismatches,
        "validation_state": "VALIDATION_PASS_FOR_CAPABILITY_ONLY",
        "review_state": "REVIEW_WITH_LIMITATION",
        "claims_forbidden": [
            "verified block-level swap-out/in event",
            "exact moved KV bytes or block lineage",
            "runtime KV-swap performance, preemption, recompute, or fallback claim",
        ],
        "triggered_children": list(TRIGGERED_CHILDREN),
        "next_ready_unit": "SWAP-K1",
        "blocked_gpu_units": {"OFF-E-RT0": "ADOPT-EXPERT-CATALOG", "OFF-W0": "ADOPT-EXPERT-CATALOG"},
    }
    write_json(args.master_root / "reviews" / "MR7-SWAP-K0-MASTER.json", review)
    write_json(args.master_root / "checkpoints" / "MR7-SWAP-K0-PROMOTION.json", {
        "schema_version": "phase7-combined-master-checkpoint-v1",
        "checkpoint_id": "MR7-SWAP-K0-PROMOTION",
        "timestamp_utc": transition["timestamp_utc"],
        "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(args.master_root / "master_remaining_ledger.json"),
        "review": str(args.master_root / "reviews" / "MR7-SWAP-K0-MASTER.json"),
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "next_ready_gpu_unit": "SWAP-K1",
    })

    print(json.dumps({
        "attempt_id": args.attempt_id,
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": checksum_manifest_hash,
        "checksum_manifest_mismatches": checksum_mismatches,
        "execution_ledger_sha256": execution_hash,
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "triggered_children": list(TRIGGERED_CHILDREN),
        "next_ready_gpu_unit": "SWAP-K1",
        "blocked_gpu_units": {"OFF-E-RT0": "ADOPT-EXPERT-CATALOG", "OFF-W0": "ADOPT-EXPERT-CATALOG"},
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
