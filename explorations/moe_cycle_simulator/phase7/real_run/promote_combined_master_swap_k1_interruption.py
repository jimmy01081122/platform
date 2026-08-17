#!/usr/bin/env python3
"""Record a partial SWAP-K1 launcher interruption without scientific closure."""

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    args = parser.parse_args()

    local_root = args.local_attempt_root
    candidates = sorted(path for path in local_root.glob("*") if path.is_dir())
    if not local_root.is_dir() or len(candidates) != 1:
        raise SystemExit("partial SWAP-K1 backup must contain exactly one runner directory")
    run = candidates[0]
    if (run / "status.json").exists() or (run / "result.json").exists():
        raise SystemExit("this promotion is only for a partial runner without terminal/result artifacts")
    stdout = (run / "stdout.log").read_text(encoding="utf-8")
    stderr = (run / "stderr.log").read_text(encoding="utf-8")
    if "Creating v1 connector with name: OffloadingConnector" not in stdout:
        raise SystemExit("partial run lacks native offload initialization evidence")
    if "trigger received signal=SIGTERM" not in stdout and "trigger received signal=SIGTERM" not in stderr:
        raise SystemExit("partial run does not show the observed termination boundary")

    hashes = {
        f"./{path.relative_to(run).as_posix()}": sha256_file(path)
        for path in sorted(run.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    }
    actual_aggregate = sha256_bytes("".join(f"{hashes[path]}  {path}\n" for path in sorted(hashes)).encode("utf-8"))
    if actual_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"raw aggregate mismatch: {actual_aggregate} != {args.expected_aggregate_sha256}")

    ledger_path = args.master_root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    by_id = {row["master_row_id"]: row for row in rows}
    row = by_id.get("SWAP-K1")
    if row is None:
        raise SystemExit("missing SWAP-K1 row")
    row.setdefault("repair_lineage", []).append({
        "attempt_id": args.attempt_id,
        "failure_class": "TECHNICAL_LAUNCH_INTERRUPTION",
        "failure": "Runner was terminated after native offload initialization and before terminal/result artifacts were written.",
        "repair_required": "Use detached remote execution and a smaller 3x12288 pressure point in a new attempt namespace.",
        "source_raw_sha256": actual_aggregate,
    })
    row.update({
        "attempt_ids": [*row.get("attempt_ids", []), args.attempt_id],
        "remote_raw_paths": [*row.get("remote_raw_paths", []), args.remote_attempt_root],
        "local_raw_paths": [*row.get("local_raw_paths", []), str(local_root)],
        "manifest_sha256": [*row.get("manifest_sha256", []), sha256_file(run / "stdout.log"), sha256_file(run / "stderr.log")],
        "source_raw_sha256": [*row.get("source_raw_sha256", []), actual_aggregate],
        "execution_state": "EXECUTION_FAILED",
        "raw_state": "COMPLETE",
        "backup_state": "VERIFIED",
        "review_state": "REVIEW_WITH_LIMITATION",
        "validation_state": "UNVERIFIED",
        "adoption_state": "SUPPLEMENT_REQUIRED",
        "blocker_or_failure": "TECHNICAL_FAILURE: runner interruption after engine initialization; no terminal/result artifact and no valid SWAP-K1 gate.",
        "claims_supported": [
            "The isolated attempt captured native offload initialization and the termination boundary in stdout/stderr.",
            "No valid SWAP-K1 request or block-movement result was produced.",
        ],
        "claims_forbidden": [
            "SWAP-K1 block movement or output equivalence from this partial attempt.",
        ],
        "contamination_flags": ["TECHNICAL_LAUNCH_INTERRUPTION_NO_TERMINAL_STATUS"],
        "next_action": "Rerun only SWAP-K1 in a new detached attempt namespace with the repaired 3x12288 fixture.",
        "last_transition_record": "MR7-SWAP-K1-LAUNCH-INTERRUPTION",
    })
    transition = {
        "transition_id": "MR7-SWAP-K1-LAUNCH-INTERRUPTION",
        "timestamp_utc": now_utc(),
        "changed_rows": ["SWAP-K1"],
        "reason": "SWAP-K1 partial runner ended after native offload initialization without terminal/result artifacts; preserve and rerun in a detached new namespace.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": args.attempt_id,
        "raw_file_count": len(hashes),
        "raw_file_set_sha256": actual_aggregate,
        "failure_class": "TECHNICAL_LAUNCH_INTERRUPTION",
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition["transition_id"]
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    inventory = read_json(args.master_root / "evidence_inventory.json")
    inventory.setdefault("swap_k1_failed_attempts", []).append({
        "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": str(local_root),
        "status": "TECHNICAL_LAUNCH_INTERRUPTION",
        "failure_class": "TECHNICAL_LAUNCH_INTERRUPTION",
        "raw_file_count": len(hashes),
        "file_set_sha256": actual_aggregate,
    })
    write_json(args.master_root / "evidence_inventory.json", inventory)

    gap = read_json(args.master_root / "gap_register.json")
    gap["entries"].append({
        "gap_id": "GAP-SWAP-K1-LAUNCH-INTERRUPTION",
        "status": "SUPPLEMENT_REQUIRED",
        "source": str(local_root),
        "consequence": "No SWAP-K1 gate result; rerun in detached namespace with no PTY lifetime dependency.",
    })
    write_json(args.master_root / "gap_register.json", gap)

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
    queue = read_json(args.master_root / "master_ready_queue.json")
    queue["generated_from_execution_ledger_sha256"] = execution_hash
    queue["next_gpu_unit"] = "SWAP-K1"
    queue["ready_gpu_units"] = ["SWAP-K1"]
    queue["dispatch_guards"] = [
        "MR2 read-only preflight clear",
        "no foreign serving/GPU process at dispatch",
        "new-session four-guard canary validated and locally backed up",
        "SWAP-K0 native KV-offload capability initialized; SWAP-K1 rerun required",
        "detached remote execution required after PTY interruption",
        "ADOPT-EXPERT-CATALOG remains a prerequisite for OFF-E-RT0/OFF-W0",
        "no filler workload",
        "raw namespace independent",
    ]
    write_json(args.master_root / "master_ready_queue.json", queue)
    write_json(args.master_root / "reviews" / "MR7-SWAP-K1-LAUNCH-INTERRUPTION.json", {
        "schema_version": "phase7-combined-master-swap-k1-interruption-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"],
        "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": str(local_root),
        "failure_class": "TECHNICAL_LAUNCH_INTERRUPTION",
        "raw_file_count": len(hashes),
        "raw_file_set_sha256": actual_aggregate,
        "validation_state": "UNVERIFIED",
        "promotion_status": "REPAIR_REQUIRED",
        "next_ready_unit": "SWAP-K1",
    })
    write_json(args.master_root / "checkpoints" / "MR7-SWAP-K1-LAUNCH-INTERRUPTION.json", {
        "schema_version": "phase7-combined-master-checkpoint-v1",
        "checkpoint_id": "MR7-SWAP-K1-LAUNCH-INTERRUPTION",
        "timestamp_utc": transition["timestamp_utc"],
        "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(args.master_root / "master_remaining_ledger.json"),
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "next_ready_gpu_unit": "SWAP-K1",
    })
    print(json.dumps({
        "attempt_id": args.attempt_id,
        "failure_class": "TECHNICAL_LAUNCH_INTERRUPTION",
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
