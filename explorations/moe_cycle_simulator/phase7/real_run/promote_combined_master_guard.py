#!/usr/bin/env python3
"""Promote one verified new-session guard attempt into the combined successor ledger.

The promotion is deliberately conservative: the four guard rows share one raw
attempt, UM-G0 is recorded as validated negative evidence, and no runtime
offload/KV/UM performance claim is inferred from the canonical canary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


GUARD_IDS = ("MECH-G0", "KV-G0", "OS-SWAP-G0", "UM-G0")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def closed(row: dict[str, Any]) -> bool:
    return (
        row.get("validation_state") in {"VALIDATION_PASS", "NEGATIVE_EVIDENCE", "UNAVAILABLE_WITH_CONSEQUENCE"}
        and row.get("backup_state") == "VERIFIED"
        and row.get("adoption_state") in {"ADOPTED", "NOT_APPLICABLE"}
        and row.get("trigger_state") in {"NOT_CONDITIONAL", "NOT_TRIGGERED_WITH_EVIDENCE", "OWNER_WAIVED"}
    )


def relative_file_hashes(root: Path) -> dict[str, str]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    return {f"./{path.relative_to(root).as_posix()}": sha256_file(path) for path in files}


def aggregate_hash(file_hashes: dict[str, str]) -> str:
    payload = "".join(f"{file_hashes[path]}  {path}\n" for path in sorted(file_hashes)).encode("utf-8")
    return sha256_bytes(payload)


def find_runner_dir(attempt_root: Path) -> Path:
    candidates = sorted(path for path in (attempt_root / "runner_runs").glob("*") if path.is_dir())
    if len(candidates) != 1:
        raise ValueError(f"expected one runner directory, found {len(candidates)}")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    args = parser.parse_args()

    if not args.local_attempt_root.is_dir():
        raise SystemExit(f"local raw backup is missing: {args.local_attempt_root}")
    terminal = read_json(args.local_attempt_root / "guard_terminal.json")
    guard_manifest = read_json(args.local_attempt_root / "guard_manifest.json")
    if terminal.get("runner_returncode") != 0 or terminal.get("raw_capture_status") != "RAW_CAPTURED":
        raise SystemExit("guard terminal is not a successful raw capture")
    if guard_manifest.get("guard_ids") != list(GUARD_IDS):
        raise SystemExit("guard manifest does not enumerate the canonical four guards")

    file_hashes = relative_file_hashes(args.local_attempt_root)
    if len(file_hashes) != 18:
        raise SystemExit(f"expected 18 backed-up raw files, found {len(file_hashes)}")
    actual_aggregate = aggregate_hash(file_hashes)
    if actual_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"raw aggregate mismatch: {actual_aggregate} != {args.expected_aggregate_sha256}")

    runner_dir = find_runner_dir(args.local_attempt_root)
    runner_manifest = read_json(runner_dir / "manifest.json")
    requested_args = read_json(runner_dir / "requested_engine_args.json")
    resolved_runtime = read_json(runner_dir / "resolved_runtime.json")
    status = read_json(runner_dir / "status.json")
    result = read_json(runner_dir / "result.json")
    if status.get("status") != "PASS" or not isinstance(result.get("records"), list) or not result["records"]:
        raise SystemExit("canonical runner status/result is not PASS")
    if runner_manifest.get("cpu_offload_gb") != 0 or runner_manifest.get("quantization") is not None:
        raise SystemExit("canonical runner manifest violates clean guard identity")

    trace_path = args.local_attempt_root / "guard_trace.jsonl"
    trace_rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not trace_rows:
        raise SystemExit("guard trace is empty")
    if not all(
        row.get("um_probe", {}).get("status")
        == "UNAVAILABLE_UNLESS_RUNTIME_OR_PROFILER_EXPOSES_MANAGED_MEMORY"
        for row in trace_rows
    ):
        raise SystemExit("UM trace policy is not consistently recorded as unavailable")

    manifest_hash = sha256_file(args.local_attempt_root / "guard_manifest.json")
    terminal_hash = sha256_file(args.local_attempt_root / "guard_terminal.json")
    local_path = str(args.local_attempt_root)
    common = {
        "attempt_ids": [args.attempt_id],
        "remote_raw_paths": [args.remote_attempt_root],
        "local_raw_paths": [local_path],
        "manifest_sha256": [manifest_hash, terminal_hash],
        "source_raw_sha256": [actual_aggregate],
        "execution_state": "EXECUTION_COMPLETE",
        "raw_state": "COMPLETE",
        "backup_state": "VERIFIED",
        "adoption_state": "ADOPTED",
        "blocker_or_failure": None,
        "last_transition_record": "MR3-MR5-GUARD-CANARY-RAW-PROMOTION",
    }
    row_updates = {
        "MECH-G0": {
            "review_state": "REVIEW_PASS",
            "validation_state": "VALIDATION_PASS",
            "claims_supported": ["Canonical model/runtime identity and session-owned process attribution were captured with runner returncode 0."],
            "claims_forbidden": ["runtime-native dynamic expert offload or residency performance"],
            "next_action": "Proceed only to the next dependency-ready capability cell; this guard is not runtime offload evidence.",
        },
        "KV-G0": {
            "review_state": "REVIEW_PASS",
            "validation_state": "VALIDATION_PASS",
            "claims_supported": ["Canonical BF16 KV/runtime configuration and request outcome were captured; runtime KV swap was not requested in this guard."],
            "claims_forbidden": ["runtime-native KV-swap performance or block-swap absence beyond captured configuration"],
            "next_action": "Proceed to SWAP-K0 capability audit; do not infer swap performance from this guard.",
        },
        "OS-SWAP-G0": {
            "review_state": "REVIEW_PASS",
            "validation_state": "VALIDATION_PASS",
            "claims_supported": ["Process/cgroup VmSwap, major-fault and system-vmstat observations were captured in the session-local trace."],
            "claims_forbidden": ["global absence of swap based only on zero process VmSwap or system context"],
            "next_action": "Retain as OS-swap guard context for formal timing windows; do not treat it as runtime KV-swap evidence.",
        },
        "UM-G0": {
            "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "NEGATIVE_EVIDENCE",
            "claims_supported": ["Validated negative evidence: managed-memory/page-migration telemetry was unavailable in this run."],
            "claims_forbidden": ["stronger Unified Memory absence claim than the observed unavailable telemetry"],
            "next_action": "Carry the UM observability limitation into all later claims and timing rows.",
        },
    }

    ledger_path = args.master_root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    by_id = {row["master_row_id"]: row for row in rows}
    for guard_id in GUARD_IDS:
        if guard_id not in by_id:
            raise SystemExit(f"missing guard row: {guard_id}")
        by_id[guard_id].update(common)
        by_id[guard_id].update(row_updates[guard_id])
    transition = {
        "transition_id": "MR3-MR5-GUARD-CANARY-RAW-PROMOTION",
        "timestamp_utc": now_utc(),
        "changed_rows": list(GUARD_IDS),
        "reason": "New-host session-local four-guard canary completed with runner PASS, raw backup verified, and UM limitation retained.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": args.attempt_id,
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition["transition_id"]
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(1 for row in rows if closed(row))
    ledger["required_row_count"] = len(rows)
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    backup_path = args.master_root / "local_backup_manifest.json"
    backup = read_json(backup_path)
    backup["new_remote_backup"] = {
        "remote_campaign": ledger["remote_campaign_id"],
        "remote_attempt": args.remote_attempt_root,
        "local_root": str(args.master_root),
        "local_attempt": local_path,
        "status": "VERIFIED",
        "file_count": len(file_hashes),
        "file_set_sha256": actual_aggregate,
        "guard_manifest_sha256": manifest_hash,
        "guard_terminal_sha256": terminal_hash,
    }
    backup.setdefault("verified_local_sources", []).append({
        "path": local_path,
        "attempt_id": args.attempt_id,
        "file_count": len(file_hashes),
        "file_set_sha256": actual_aggregate,
        "status": "new-session guard raw backup verified",
    })
    write_json(backup_path, backup)

    inventory_path = args.master_root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    inventory["new_session_guard"] = {
        "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": local_path,
        "runner_status": "PASS",
        "guard_terminal_promotion_status": terminal.get("promotion_status"),
        "raw_file_count": len(file_hashes),
        "file_set_sha256": actual_aggregate,
        "guard_status": {key: ("NEGATIVE_EVIDENCE" if key == "UM-G0" else "VALIDATED") for key in GUARD_IDS},
    }
    write_json(inventory_path, inventory)

    gap_path = args.master_root / "gap_register.json"
    gap_register = read_json(gap_path)
    for entry in gap_register.get("entries", []):
        if entry.get("gap_id") == "GAP-NEW-SESSION-GUARD":
            entry["status"] = "CLOSED_WITH_LIMITATION"
            entry["source"] = local_path
            entry["consequence"] = "New-host guards are validated and backed up; UM remains negative evidence only."

    write_json(gap_path, gap_register)

    claim_path = args.master_root / "claim_boundary_register.json"
    claims = read_json(claim_path)
    allowed = claims.setdefault("claims_allowed_now", [])
    guard_claim = "new-host session-local MECH/KV/OS guard validated; UM validated negative evidence with telemetry limitation"
    if guard_claim not in allowed:
        allowed.append(guard_claim)
    write_json(claim_path, claims)

    remaining = [row for row in rows if not closed(row)]
    blocked = [{"id": row["master_row_id"], "reason": row["blocker_or_failure"]} for row in remaining if row.get("blocker_or_failure")]
    conditional = [row["master_row_id"] for row in remaining if row.get("trigger_state") == "PENDING"]
    remaining_ledger = {
        "schema_version": "phase7-combined-master-remaining-ledger-v1",
        "master_campaign_id": ledger["master_campaign_id"],
        "generated_from_execution_ledger_sha256": execution_hash,
        "required_total": len(rows),
        "required_legally_closed": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "required_remaining_ids": [row["master_row_id"] for row in remaining],
        "blocked_rows": blocked,
        "conditional_pending_count": len(conditional),
        "conditional_pending_ids": conditional,
        "phase7_status": ledger["status"],
    }
    write_json(args.master_root / "master_remaining_ledger.json", remaining_ledger)

    queue_path = args.master_root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue["generated_from_execution_ledger_sha256"] = execution_hash
    queue["next_gpu_unit"] = "SWAP-K0"
    queue["ready_gpu_units"] = ["SWAP-K0"]
    queue["ready_cpu_units"] = [item for item in queue.get("ready_cpu_units", []) if item not in GUARD_IDS]
    queue["dispatch_guards"] = [
        "MR2 read-only preflight clear",
        "no foreign serving/GPU process at dispatch",
        "new-session four-guard canary validated and locally backed up",
        "ADOPT-EXPERT-CATALOG remains a prerequisite for OFF-E-RT0/OFF-W0",
        "no filler workload",
        "raw namespace independent",
    ]
    write_json(queue_path, queue)

    review = {
        "schema_version": "phase7-combined-master-guard-promotion-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"],
        "attempt_id": args.attempt_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": local_path,
        "runner_returncode": terminal["runner_returncode"],
        "runner_status": status.get("status"),
        "completed_request_count": result.get("completed_request_count"),
        "guard_status": {"MECH-G0": "VALIDATED", "KV-G0": "VALIDATED", "OS-SWAP-G0": "VALIDATED", "UM-G0": "VALIDATED_NEGATIVE_EVIDENCE"},
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "guard_manifest_sha256": manifest_hash,
        "guard_terminal_sha256": terminal_hash,
        "trace_row_count": len(trace_rows),
        "promotion_status": "VALIDATION_PASS_WITH_UM_LIMITATION",
        "claims_forbidden": [
            "runtime-native dynamic expert offload",
            "runtime-native KV swap performance",
            "stronger Unified Memory absence than observed telemetry",
        ],
        "next_ready_unit": "SWAP-K0",
        "blocked_gpu_units": {"OFF-E-RT0": "ADOPT-EXPERT-CATALOG", "OFF-W0": "ADOPT-EXPERT-CATALOG"},
    }
    write_json(args.master_root / "reviews" / "MR3-MR5-GUARD-V3-MASTER.json", review)
    write_json(args.master_root / "checkpoints" / "MR3-MR5-GUARD-PROMOTION.json", {
        "schema_version": "phase7-combined-master-checkpoint-v1",
        "checkpoint_id": "MR3-MR5-GUARD-PROMOTION",
        "timestamp_utc": transition["timestamp_utc"],
        "execution_ledger_sha256": execution_hash,
        "remaining_ledger_sha256": sha256_file(args.master_root / "master_remaining_ledger.json"),
        "guard_review": str(args.master_root / "reviews" / "MR3-MR5-GUARD-V3-MASTER.json"),
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "next_ready_gpu_unit": "SWAP-K0",
    })

    print(json.dumps({
        "attempt_id": args.attempt_id,
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "execution_ledger_sha256": execution_hash,
        "required_closed_count": len(rows) - len(remaining),
        "required_remaining_count": len(remaining),
        "next_ready_gpu_unit": "SWAP-K0",
        "blocked_gpu_units": {"OFF-E-RT0": "ADOPT-EXPERT-CATALOG", "OFF-W0": "ADOPT-EXPERT-CATALOG"},
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
