#!/usr/bin/env python3
"""Promote one measured SWAP-K2 capacity point into the combined ledger.

K2 is a seven-point host-capacity matrix.  Each invocation records one
complete, independently backed-up point while keeping the master row open
until all fit and held-out points are present.  Technical launch failures are
preserved as repair lineage and never converted into scientific evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from promote_combined_master_swap_k1_v5 import (
    aggregate_hash,
    append_unique,
    checksum_mismatches,
    event_analysis,
    evidence_file_hashes,
    legally_closed,
    now_utc,
    read_json,
    sha256_file,
    write_json,
)


POINTS: dict[str, dict[str, Any]] = {
    "FIT-25": {"role": "FIT", "fraction": 0.25, "host_capacity_gib": 1.0, "expected_host_blocks": 512},
    "FIT-50": {"role": "FIT", "fraction": 0.50, "host_capacity_gib": 2.0, "expected_host_blocks": 1024},
    "FIT-75": {"role": "FIT", "fraction": 0.75, "host_capacity_gib": 3.0, "expected_host_blocks": 1536},
    "FIT-100": {"role": "FIT", "fraction": 1.00, "host_capacity_gib": 4.0, "expected_host_blocks": 2048},
    "HELDOUT-37.5": {"role": "HELDOUT", "fraction": 0.375, "host_capacity_gib": 1.5, "expected_host_blocks": 768},
    "HELDOUT-62.5": {"role": "HELDOUT", "fraction": 0.625, "host_capacity_gib": 2.5, "expected_host_blocks": 1280},
    "HELDOUT-87.5": {"role": "HELDOUT", "fraction": 0.875, "host_capacity_gib": 3.5, "expected_host_blocks": 1792},
}
REFERENCE_WORKING_SET_BYTES = 4 * 1024 * 1024 * 1024
REFERENCE_FULL_BLOCK_BYTES = 2 * 1024 * 1024
REQUIRED_RUN_FILES = (
    "status.json",
    "result.json",
    "requests.json",
    "input_fixture.json",
    "requested_engine_args.json",
    "resolved_runtime.json",
    "kv_events.json",
    "k2_contract.json",
    "resolved_runtime.json",
    "SHA256SUMS",
    "stdout.log",
    "stderr.log",
)


def add_attempt_value(row: dict[str, Any], key: str, value: str) -> None:
    values = row.setdefault(key, [])
    if value not in values:
        values.append(value)


def append_record_once(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    attempt_id = record.get("attempt_id")
    if not any(item.get("attempt_id") == attempt_id for item in records):
        records.append(record)


def runner_from_root(root: Path) -> Path:
    runners = sorted((root / "runner_runs").glob("*"))
    if len(runners) != 1 or not runners[0].is_dir():
        raise SystemExit(f"expected exactly one runner directory under {root}")
    return runners[0]


def validate_manifest(runner: Path, expected_manifest: str) -> None:
    actual_manifest = sha256_file(runner / "SHA256SUMS")
    if actual_manifest != expected_manifest:
        raise SystemExit(f"remote/local SHA256SUMS mismatch: {actual_manifest} != {expected_manifest}")
    mismatches = checksum_mismatches(runner)
    if mismatches:
        raise SystemExit(f"declared checksum mismatch set is non-empty: {mismatches}")


def validate_point(args: argparse.Namespace, runner: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = POINTS[args.point_id]
    required = [runner / name for name in REQUIRED_RUN_FILES]
    if not all(path.is_file() for path in required):
        missing = [str(path.name) for path in required if not path.is_file()]
        raise SystemExit(f"K2 raw backup is incomplete: {missing}")

    status = read_json(runner / "status.json")
    result = read_json(runner / "result.json")
    requests = read_json(runner / "requests.json")
    fixture = read_json(runner / "input_fixture.json")
    requested = read_json(runner / "requested_engine_args.json")
    runtime = read_json(runner / "resolved_runtime.json")
    contract = read_json(runner / "k2_contract.json")
    events = read_json(runner / "kv_events.json")
    stdout = (runner / "stdout.log").read_text(encoding="utf-8", errors="replace")
    stderr = (runner / "stderr.log").read_text(encoding="utf-8", errors="replace")
    logs = stdout + "\n" + stderr

    if status.get("status") != "PASS" or status.get("execution_state") != "EXECUTION_COMPLETE":
        raise SystemExit("K2 runner did not complete PASS")
    if status.get("requested_kv_offloading_size_gib") != expected["host_capacity_gib"]:
        raise SystemExit("K2 status host budget does not match the point contract")
    if requested.get("kv_offloading_size") != expected["host_capacity_gib"] or requested.get("kv_offloading_backend") != "native":
        raise SystemExit("K2 native host-capacity request is not frozen")

    for key, value in (
        ("point_id", args.point_id),
        ("role", expected["role"]),
        ("host_capacity_gib", expected["host_capacity_gib"]),
        ("expected_host_blocks", expected["expected_host_blocks"]),
        ("expected_rounded_bytes", expected["expected_host_blocks"] * REFERENCE_FULL_BLOCK_BYTES),
        ("reference_working_set_bytes", REFERENCE_WORKING_SET_BYTES),
        ("reference_full_block_bytes", REFERENCE_FULL_BLOCK_BYTES),
    ):
        if contract.get(key) != value:
            raise SystemExit(f"K2 contract mismatch for {key}: {contract.get(key)!r} != {value!r}")
    if contract.get("rounding_policy") != "FLOOR_TO_COMPLETE_BLOCK":
        raise SystemExit("K2 full-block rounding policy is not frozen")

    records = result.get("records")
    prompts = fixture.get("prompt_token_ids_list")
    if not isinstance(records, list) or len(records) != 3:
        raise SystemExit("K2 result must contain exactly three measured records")
    if not isinstance(prompts, list) or len(prompts) != 3 or len({json.dumps(p, separators=(",", ":")) for p in prompts}) != 3:
        raise SystemExit("K2 fixture must contain three distinct prompt sequences")
    if fixture.get("same_prompt_no_prefix_cache") is not False or fixture.get("distinct_prompt_sequences") is not True:
        raise SystemExit("K2 fixture identity flags are not frozen")
    for index, record in enumerate(records):
        if (
            record.get("input_token_count") != 16384
            or record.get("input_token_ids") != prompts[index]
            or record.get("output_token_count") != 32
            or record.get("finish_reason") != "length"
            or record.get("num_cached_tokens") != 0
        ):
            raise SystemExit(f"K2 request correctness gate failed at request {index + 1}")
    if requests.get("records") != records or result.get("total_context_tokens") != 49152:
        raise SystemExit("K2 requests/result consistency gate failed")

    shape_match = re.search(r"cross layer KV cache of shape \(([^)]+)\)", logs)
    if not shape_match:
        raise SystemExit("K2 logs lack cross-layer KV cache shape")
    shape = [int(part.strip()) for part in shape_match.group(1).split(",")]
    if shape != [2308, 8, 32, 2, 16, 128]:
        raise SystemExit(f"unexpected K2 KV cache shape: {shape}")
    cache = runtime.get("vllm_config", {}).get("cache_config", {})
    block_size = cache.get("block_size")
    if block_size != 16:
        raise SystemExit(f"unexpected K2 runtime block size: {block_size}")
    required_log_evidence = (
        "Creating v1 connector with name: OffloadingConnector",
        "Creating offloading spec with name: CPUOffloadingSpec",
        "GPU KV cache size: 36,928 tokens",
        "Allocating 1 CPU tensors",
    )
    if not all(needle in logs for needle in required_log_evidence):
        raise SystemExit("K2 native offload log evidence is incomplete")

    analysis = event_analysis(events)
    if analysis["decode_errors"] not in (None, []):
        raise SystemExit("K2 KV event trace contains decode errors")
    dtype_bytes = 2
    elements_per_block = shape[1] * shape[2] * shape[3] * shape[4] * shape[5]
    bytes_per_block = elements_per_block * dtype_bytes
    if bytes_per_block != REFERENCE_FULL_BLOCK_BYTES:
        raise SystemExit(f"derived full block bytes mismatch: {bytes_per_block}")
    if not analysis["removed_hashes_subset_of_stored"]:
        raise SystemExit("K2 removed block hashes are not a subset of stored hashes")

    file_hashes = evidence_file_hashes(runner)
    if len(file_hashes) < 19:
        raise SystemExit(f"expected at least 19 K2 raw evidence files, found {len(file_hashes)}")
    actual_aggregate = aggregate_hash(file_hashes)
    if actual_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"raw aggregate mismatch: {actual_aggregate} != {args.expected_aggregate_sha256}")
    validate_manifest(runner, args.remote_manifest_sha256)
    if sha256_file(runner / "stdout.log") != args.remote_stdout_sha256:
        raise SystemExit("remote/local K2 stdout hash mismatch")
    if sha256_file(runner / "stderr.log") != args.remote_stderr_sha256:
        raise SystemExit("remote/local K2 stderr hash mismatch")

    point_result = {
        "attempt_id": args.attempt_id,
        "point_id": args.point_id,
        "role": expected["role"],
        "fraction": expected["fraction"],
        "host_capacity_gib": expected["host_capacity_gib"],
        "host_capacity_bytes": int(expected["host_capacity_gib"] * 1024**3),
        "expected_host_blocks": expected["expected_host_blocks"],
        "expected_rounded_bytes": expected["expected_host_blocks"] * REFERENCE_FULL_BLOCK_BYTES,
        "reference_working_set_bytes": REFERENCE_WORKING_SET_BYTES,
        "reference_full_block_bytes": REFERENCE_FULL_BLOCK_BYTES,
        "rounding_policy": "FLOOR_TO_COMPLETE_BLOCK",
        "runtime_cache_shape": shape,
        "runtime_block_size_tokens": block_size,
        "dtype": "bfloat16",
        "derived_bytes_per_full_block": bytes_per_block,
        "event_analysis": {key: value for key, value in analysis.items() if key not in {"first_stored_sequence_by_hash", "first_removed_sequence_by_hash", "timeline"}},
        "movement_observed": analysis["decoded_block_removed_message_count"] > 0,
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": actual_aggregate,
        "checksum_manifest_sha256": sha256_file(runner / "SHA256SUMS"),
        "stdout_sha256": sha256_file(runner / "stdout.log"),
        "stderr_sha256": sha256_file(runner / "stderr.log"),
        "validation_state": "VALIDATION_PASS",
    }
    return point_result, {
        "status": status,
        "result": result,
        "contract": contract,
        "analysis": analysis,
        "shape": shape,
        "bytes_per_block": bytes_per_block,
        "actual_aggregate": actual_aggregate,
        "file_hashes": file_hashes,
    }


def record_failed_attempt(
    args: argparse.Namespace,
    row: dict[str, Any],
    inventory: dict[str, Any],
    backup: dict[str, Any],
) -> None:
    if not args.failed_attempt_id or args.failed_attempt_id in row.setdefault("attempt_ids", []):
        return
    failed_root = args.failed_local_attempt_root
    if failed_root is None:
        raise SystemExit("--failed-local-attempt-root is required with --failed-attempt-id")
    runner = runner_from_root(failed_root)
    file_hashes = evidence_file_hashes(runner)
    aggregate = aggregate_hash(file_hashes)
    if args.failed_expected_aggregate_sha256 and aggregate != args.failed_expected_aggregate_sha256:
        raise SystemExit("failed-attempt raw aggregate mismatch")
    stdout_hash = sha256_file(runner / "stdout.log")
    stderr_hash = sha256_file(runner / "stderr.log")
    if args.failed_stdout_sha256 and stdout_hash != args.failed_stdout_sha256:
        raise SystemExit("failed-attempt stdout hash mismatch")
    if args.failed_stderr_sha256 and stderr_hash != args.failed_stderr_sha256:
        raise SystemExit("failed-attempt stderr hash mismatch")
    manifest_hash = None
    if (runner / "SHA256SUMS").is_file():
        manifest_hash = sha256_file(runner / "SHA256SUMS")
        if args.failed_remote_manifest_sha256 and manifest_hash != args.failed_remote_manifest_sha256:
            raise SystemExit("failed-attempt remote/local manifest hash mismatch")
        mismatches = checksum_mismatches(runner)
        if mismatches:
            raise SystemExit(f"failed-attempt checksum mismatch set is non-empty: {mismatches}")
    failure = {
        "attempt_id": args.failed_attempt_id,
        "failure_class": args.failed_failure_class,
        "failure": args.failed_failure,
        "repair_required": args.failed_repair_required,
        "remote_raw_path": args.failed_remote_attempt_root,
        "local_raw_path": str(failed_root),
        "raw_file_count": len(file_hashes),
        "file_set_sha256": aggregate,
        "stdout_sha256": stdout_hash,
        "stderr_sha256": stderr_hash,
        "remote_local_hashes_verified": True,
        "status": args.failed_status,
    }
    if manifest_hash is not None:
        failure["manifest_sha256"] = manifest_hash
    row.setdefault("repair_lineage", []).append(failure)
    add_attempt_value(row, "attempt_ids", args.failed_attempt_id)
    add_attempt_value(row, "remote_raw_paths", args.failed_remote_attempt_root)
    add_attempt_value(row, "local_raw_paths", str(failed_root))
    add_attempt_value(row, "source_raw_sha256", aggregate)
    append_unique(row.setdefault("manifest_sha256", []), [value for value in [manifest_hash, stdout_hash, stderr_hash] if value])
    append_record_once(inventory.setdefault("swap_k2_failed_attempts", []), failure)
    append_record_once(
        backup.setdefault("phase7_attempt_backups", []),
        {
            "attempt_id": args.failed_attempt_id,
            "remote_attempt": args.failed_remote_attempt_root,
            "local_attempt": str(failed_root),
            "status": "VERIFIED_RAW_TECHNICAL_FAILURE",
            "file_count": len(file_hashes),
            "file_set_sha256": aggregate,
            "stdout_sha256": stdout_hash,
            "stderr_sha256": stderr_hash,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-root", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--remote-attempt-root", required=True)
    parser.add_argument("--local-attempt-root", type=Path, required=True)
    parser.add_argument("--runner-dir-name", required=True)
    parser.add_argument("--point-id", choices=sorted(POINTS), required=True)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    parser.add_argument("--remote-manifest-sha256", required=True)
    parser.add_argument("--remote-stdout-sha256", required=True)
    parser.add_argument("--remote-stderr-sha256", required=True)
    parser.add_argument("--failed-attempt-id")
    parser.add_argument("--failed-remote-attempt-root", default="")
    parser.add_argument("--failed-local-attempt-root", type=Path)
    parser.add_argument("--failed-expected-aggregate-sha256", default="")
    parser.add_argument("--failed-remote-manifest-sha256", default="")
    parser.add_argument("--failed-stdout-sha256", default="")
    parser.add_argument("--failed-stderr-sha256", default="")
    parser.add_argument("--failed-failure-class", default="TECHNICAL_SETUP_FAILURE")
    parser.add_argument(
        "--failed-failure",
        default="Python IndentationError occurred while inserting the K2 contract before engine import; no GPU workload or scientific result was produced.",
    )
    parser.add_argument("--failed-repair-required", default="Use a new attempt namespace with compile-checked K2 runner source.")
    parser.add_argument("--failed-status", default="TECHNICAL_FAILURE_BEFORE_VALID_WORKLOAD")
    args = parser.parse_args()

    runner = args.local_attempt_root / "runner_runs" / args.runner_dir_name
    point_result, details = validate_point(args, runner)
    sidecar = args.local_attempt_root / f"derived_swap_k2_{args.point_id.lower().replace('.', 'p')}_{args.attempt_id.lower()}_event_lineage_and_capacity.json"
    sidecar_doc = {
        "schema_version": "phase7-swap-k2-point-derived-v1",
        "source_raw_sha256": details["actual_aggregate"],
        "point": point_result,
        "event_analysis_full": details["analysis"],
        "capacity_derivation": {
            "basis": "V5_DERIVED_RUNTIME_CACHE_SHAPE_AND_DTYPE",
            "reference_working_set_bytes": REFERENCE_WORKING_SET_BYTES,
            "reference_full_block_bytes": REFERENCE_FULL_BLOCK_BYTES,
            "expected_host_blocks": POINTS[args.point_id]["expected_host_blocks"],
            "expected_rounded_bytes": POINTS[args.point_id]["expected_host_blocks"] * REFERENCE_FULL_BLOCK_BYTES,
            "runtime_cache_shape": details["shape"],
            "runtime_block_size_tokens": 16,
            "dtype": "bfloat16",
            "bytes_per_full_block": details["bytes_per_block"],
        },
    }
    write_json(sidecar, sidecar_doc)
    sidecar_hash = sha256_file(sidecar)

    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    row = next((item for item in rows if item["master_row_id"] == "SWAP-K2"), None)
    if row is None:
        raise SystemExit("missing SWAP-K2 row")
    inventory_path = root / "evidence_inventory.json"
    inventory = read_json(inventory_path)
    backup_path = root / "local_backup_manifest.json"
    backup = read_json(backup_path)

    record_failed_attempt(args, row, inventory, backup)
    add_attempt_value(row, "attempt_ids", args.attempt_id)
    add_attempt_value(row, "remote_raw_paths", args.remote_attempt_root)
    add_attempt_value(row, "local_raw_paths", str(args.local_attempt_root))
    add_attempt_value(row, "source_raw_sha256", details["actual_aggregate"])
    append_unique(
        row.setdefault("manifest_sha256", []),
        [
            point_result["checksum_manifest_sha256"],
            sha256_file(runner / "status.json"),
            sha256_file(runner / "kv_events.json"),
            point_result["stdout_sha256"],
            point_result["stderr_sha256"],
            sha256_file(runner / "k2_contract.json"),
            sidecar_hash,
        ],
    )
    append_record_once(row.setdefault("k2_point_results", []), {**point_result, "derived_sidecar": str(sidecar), "derived_sidecar_sha256": sidecar_hash})

    completed_points = sorted(
        {
            item["point_id"]
            for item in row.get("k2_point_results", [])
            if item.get("validation_state") == "VALIDATION_PASS"
        },
        key=lambda value: list(POINTS).index(value),
    )
    missing_points = [point_id for point_id in POINTS if point_id not in completed_points]
    matrix_complete = not missing_points
    row.update(
        {
            "execution_state": "EXECUTION_COMPLETE",
            "raw_state": "COMPLETE",
            "backup_state": "VERIFIED",
            "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "VALIDATION_PASS" if matrix_complete else "UNVERIFIED",
            "adoption_state": "ADOPTED" if matrix_complete else "SUPPLEMENT_REQUIRED",
            "blocker_or_failure": None if matrix_complete else f"K2_MATRIX_INCOMPLETE: missing {', '.join(missing_points)}",
            "k2_required_points": list(POINTS),
            "k2_completed_points": completed_points,
            "k2_missing_points": missing_points,
            "claims_supported": append_unique(
                list(row.get("claims_supported", [])),
                [
                    f"SWAP-K2 {args.point_id} completed the frozen {POINTS[args.point_id]['host_capacity_gib']} GiB host-capacity point with 3x16384 input tokens, 32 output tokens, and finish_reason=length.",
                    f"SWAP-K2 {args.point_id} uses floor-to-complete-block rounding: {POINTS[args.point_id]['expected_host_blocks']} full blocks and {POINTS[args.point_id]['expected_host_blocks'] * REFERENCE_FULL_BLOCK_BYTES} bytes from the V5-derived 2 MiB full-block size.",
                ],
            ),
            "claims_forbidden": append_unique(
                list(row.get("claims_forbidden", [])),
                [
                    "A complete SWAP-K2 fit/held-out matrix conclusion before all seven points are executed and reviewed.",
                    "SWAP-K2 latency, PCIe bandwidth, copy completion, serving tail, or throughput claims.",
                    "SWAP-K3 serving-pressure or SWAP-K5 exhaustion claims.",
                ],
            ),
            "contamination_flags": append_unique(
                list(row.get("contamination_flags", [])),
                [
                    "K2_FIT_25_V1_TECHNICAL_INDENTATION_FAILURE_NO_GPU_WORKLOAD",
                    "EVENT_API_BLOCK_SIZE_FIELD_ZERO_PARENT_NULL_AND_TOKEN_METADATA_EMPTY",
                    "K2_MATRIX_REQUIRES_ALL_FIT_AND_HELDOUT_POINTS",
                ],
            ),
            "next_action": "Proceed to the next frozen SWAP-K2 point: " + (missing_points[0] if missing_points else "SWAP-K3 dispatch after matrix review"),
            "last_transition_record": f"MR7-SWAP-K2-{args.point_id}-{args.attempt_id}-PROMOTION",
        }
    )

    transition_id = f"MR7-SWAP-K2-{args.point_id}-{args.attempt_id}-PROMOTION"
    transition = {
        "transition_id": transition_id,
        "timestamp_utc": now_utc(),
        "changed_rows": ["SWAP-K2"],
        "reason": f"Promote the verified {args.point_id} K2 capacity point; retain the row open until the complete fit/held-out matrix is present.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": args.attempt_id,
        "point_id": args.point_id,
        "point_result": {key: value for key, value in point_result.items() if key != "event_analysis"},
        "derived_sidecar": str(sidecar),
        "derived_sidecar_sha256": sidecar_hash,
        "matrix_completed_points": completed_points,
        "matrix_missing_points": missing_points,
        "validation_state": row["validation_state"],
        "review_state": row["review_state"],
        "remote_local_hashes_verified": True,
    }
    ledger.setdefault("transitions", []).append(transition)
    ledger["latest_transition_id"] = transition_id
    ledger["updated_at_utc"] = transition["timestamp_utc"]
    ledger["required_closed_count"] = sum(1 for item in rows if legally_closed(item))
    ledger["required_row_count"] = len(rows)
    write_json(ledger_path, ledger)
    execution_hash = sha256_file(ledger_path)

    append_record_once(
        inventory.setdefault("swap_k2_capacity_attempts", []),
        {
            **point_result,
            "remote_raw_path": args.remote_attempt_root,
            "local_raw_path": str(args.local_attempt_root),
            "derived_sidecar": str(sidecar),
            "derived_sidecar_sha256": sidecar_hash,
            "status": "VALIDATION_PASS_POINT_MATRIX_COMPLETE" if matrix_complete else "VALIDATION_PASS_POINT_MATRIX_PARTIAL",
            "claims_forbidden": ["complete K2 matrix until remaining points are executed", "K2/K3/K5 performance or serving claims"],
        },
    )
    write_json(inventory_path, inventory)

    gap_path = root / "gap_register.json"
    gap = read_json(gap_path)
    gap_entries = gap.setdefault("entries", [])
    gap_entries[:] = [entry for entry in gap_entries if entry.get("gap_id") != "GAP-SWAP-K2-CAPACITY-MATRIX"]
    gap_entries.append(
        {
            "gap_id": "GAP-SWAP-K2-CAPACITY-MATRIX",
            "status": "CLOSED_WITH_LIMITATION" if matrix_complete else "SUPPLEMENT_REQUIRED",
            "source": str(args.local_attempt_root),
            "consequence": "All seven complete-block fit/held-out points are required before K2 closure; raw event metadata limitations remain bound to the per-point sidecars.",
            "completed_points": completed_points,
            "missing_points": missing_points,
        }
    )
    write_json(gap_path, gap)

    claims = read_json(root / "claim_boundary_register.json")
    append_unique(
        claims.setdefault("claims_allowed_now", []),
        [f"SWAP-K2 {args.point_id} point-level host-capacity execution and output/finish correctness"],
    )
    append_unique(
        claims.setdefault("claims_forbidden_now", []),
        ["SWAP-K2 complete matrix conclusion before all seven points", "SWAP-K2/K3/K5 performance or serving benefit"],
    )
    write_json(root / "claim_boundary_register.json", claims)

    remaining = [item for item in rows if not legally_closed(item)]
    conditional = [item["master_row_id"] for item in remaining if item.get("trigger_state") == "PENDING"]
    blocked = [
        {"id": item["master_row_id"], "reason": item["blocker_or_failure"]}
        for item in remaining
        if item.get("blocker_or_failure")
    ]
    write_json(
        root / "master_remaining_ledger.json",
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

    queue_path = root / "master_ready_queue.json"
    queue = read_json(queue_path)
    queue["generated_from_execution_ledger_sha256"] = execution_hash
    queue["next_gpu_unit"] = "SWAP-K2" if missing_points else "SWAP-K3"
    queue["ready_gpu_units"] = ["SWAP-K2"] if missing_points else ["SWAP-K3"]
    queue["next_k2_point"] = missing_points[0] if missing_points else None
    queue["dispatch_guards"] = [
        "MR2 read-only preflight clear",
        "no foreign serving/GPU process at dispatch",
        "new-session four-guard canary validated and locally backed up",
        "SWAP-K0 native KV-offload capability initialized",
        "SWAP-K1-V5 forced movement passed with raw backup verified",
        "SWAP-K2 uses V5-derived 2 MiB full-block bytes and floor-to-complete-block rounding",
        f"SWAP-K2 completed points: {', '.join(completed_points) if completed_points else 'none'}",
        "no filler workload",
        "raw namespace independent",
    ]
    write_json(queue_path, queue)

    append_record_once(
        backup.setdefault("verified_local_sources", []),
        {
            "attempt_id": args.attempt_id,
            "path": str(args.local_attempt_root),
            "file_count": point_result["raw_file_count"],
            "file_set_sha256": point_result["raw_file_set_sha256"],
            "manifest_sha256": point_result["checksum_manifest_sha256"],
            "derived_sidecar": str(sidecar),
            "derived_sidecar_sha256": sidecar_hash,
            "remote_local_hashes_verified": True,
            "status": "SWAP-K2 point raw backup verified",
        },
    )
    write_json(backup_path, backup)

    review_name = f"MR7-SWAP-K2-{args.point_id}-{args.attempt_id}-PROMOTION.json"
    review = {
        "schema_version": "phase7-combined-master-swap-k2-point-review-v1",
        "reviewed_at_utc": transition["timestamp_utc"],
        "attempt_id": args.attempt_id,
        "point_id": args.point_id,
        "remote_raw_path": args.remote_attempt_root,
        "local_raw_path": str(args.local_attempt_root),
        "execution_state": "EXECUTION_COMPLETE",
        "request_correctness": "PASS",
        "raw_backup": "VERIFIED",
        "point_result": point_result,
        "derived_sidecar": str(sidecar),
        "derived_sidecar_sha256": sidecar_hash,
        "validation_state": "VALIDATION_PASS",
        "review_state": "REVIEW_WITH_LIMITATION",
        "matrix_complete": matrix_complete,
        "missing_points": missing_points,
        "claims_forbidden": ["complete K2 matrix", "K2/K3/K5 performance or serving benefit"],
        "next_ready_unit": queue["next_gpu_unit"],
    }
    write_json(root / "reviews" / review_name, review)
    write_json(
        root / "checkpoints" / review_name,
        {
            "schema_version": "phase7-combined-master-checkpoint-v1",
            "checkpoint_id": transition_id,
            "timestamp_utc": transition["timestamp_utc"],
            "execution_ledger_sha256": execution_hash,
            "remaining_ledger_sha256": sha256_file(root / "master_remaining_ledger.json"),
            "required_closed_count": len(rows) - len(remaining),
            "required_remaining_count": len(remaining),
            "next_ready_gpu_unit": queue["next_gpu_unit"],
            "point_id": args.point_id,
            "raw_file_set_sha256": point_result["raw_file_set_sha256"],
            "matrix_completed_points": completed_points,
            "matrix_missing_points": missing_points,
        },
    )

    print(
        json.dumps(
            {
                "attempt_id": args.attempt_id,
                "point_id": args.point_id,
                "validation_state": "VALIDATION_PASS",
                "review_state": "REVIEW_WITH_LIMITATION",
                "raw_file_count": point_result["raw_file_count"],
                "raw_file_set_sha256": point_result["raw_file_set_sha256"],
                "checksum_manifest_sha256": point_result["checksum_manifest_sha256"],
                "decoded_block_stored_message_count": point_result["event_analysis"]["decoded_block_stored_message_count"],
                "decoded_block_removed_message_count": point_result["event_analysis"]["decoded_block_removed_message_count"],
                "removed_unique_hash_count": point_result["event_analysis"]["removed_unique_hash_count"],
                "removed_hashes_subset_of_stored": point_result["event_analysis"]["removed_hashes_subset_of_stored"],
                "matrix_completed_points": completed_points,
                "matrix_missing_points": missing_points,
                "execution_ledger_sha256": execution_hash,
                "required_closed_count": len(rows) - len(remaining),
                "required_remaining_count": len(remaining),
                "next_ready_gpu_unit": queue["next_gpu_unit"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
