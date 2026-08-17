#!/usr/bin/env python3
"""Validate and promote one frozen SWAP-K3 serving-pressure point.

The K3 row is a four-point diagnostic matrix.  Every invocation validates one
actual AsyncLLMEngine burst, binds its immutable raw files to a checksum
manifest, records any supplied technical-failure lineage, and keeps K3 open
until long-prefill, decode-heavy, mixed, and burst points all pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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


POINTS: dict[str, dict[str, Any]] = {
    "LONG-PREFILL": {
        "concurrency": 8,
        "bursts": 1,
        "input_tokens": 8192,
        "output_tokens": 32,
        "max_num_seqs": 8,
        "max_num_batched_tokens": 4096,
        "request_plan": None,
    },
    "DECODE-HEAVY": {
        "concurrency": 8,
        "bursts": 1,
        "input_tokens": 512,
        "output_tokens": 512,
        "max_num_seqs": 8,
        "max_num_batched_tokens": 4096,
        "request_plan": None,
    },
    "MIXED": {
        "concurrency": 4,
        "bursts": 1,
        "input_tokens": 128,
        "output_tokens": 32,
        "max_num_seqs": 8,
        "max_num_batched_tokens": 4096,
        "request_plan": "serving_s4_mixed_plan_v1.json",
    },
    "BURST": {
        "concurrency": 32,
        "bursts": 1,
        "input_tokens": 128,
        "output_tokens": 32,
        "max_num_seqs": 32,
        "max_num_batched_tokens": 4096,
        "request_plan": None,
    },
}

REQUIRED_RUN_FILES = (
    "status.json",
    "result.json",
    "manifest.json",
    "requested_engine_args.json",
    "input_fixture.json",
    "requests.jsonl",
    "telemetry.jsonl",
    "k3_contract.json",
    "stdout.log",
    "stderr.log",
    "SHA256SUMS",
)

EXPECTED_MODEL = "/vault/flow/moe_simulator_phase7/models/mistralai__Mixtral-8x7B-Instruct-v0.1__eba92302__bf16_safetensors"
EXPECTED_MIXED_PLAN_SHA256 = "10358ce5bd41ca62fcdc2a5c7e472b68807ae31c4a8f328a8b443f8657a17962"


def append_record_once(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    attempt_id = record.get("attempt_id")
    if not any(item.get("attempt_id") == attempt_id for item in records):
        records.append(record)


def add_attempt_value(row: dict[str, Any], key: str, value: str) -> None:
    values = row.setdefault(key, [])
    if value and value not in values:
        values.append(value)


def json_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise SystemExit(f"{path.name}:{line_number} is not a JSON object")
        records.append(value)
    return records


def runner_from_root(root: Path, runner_name: str) -> Path:
    runner = root / "runner_runs" / runner_name
    if not runner.is_dir():
        raise SystemExit(f"missing runner directory: {runner}")
    siblings = sorted(path for path in (root / "runner_runs").glob("*") if path.is_dir())
    if siblings != [runner]:
        raise SystemExit(f"expected exactly one runner directory under {root}")
    return runner


def raw_tree_hashes(root: Path) -> dict[str, str]:
    return {
        f"./{path.relative_to(root).as_posix()}": sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.name != "SHA256SUMS"
        and not path.name.startswith("derived_")
    }


def numeric_summary(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def expected_specs(args: argparse.Namespace, point: dict[str, Any]) -> list[dict[str, Any]]:
    if point["request_plan"] is None:
        return [
            {
                "slot": index,
                "class": "homogeneous",
                "input_tokens": point["input_tokens"],
                "output_tokens": point["output_tokens"],
            }
            for index in range(point["concurrency"])
        ]
    if args.mixed_plan is None or not args.mixed_plan.is_file():
        raise SystemExit("--mixed-plan is required for the MIXED point")
    if sha256_file(args.mixed_plan) != EXPECTED_MIXED_PLAN_SHA256:
        raise SystemExit("frozen mixed-plan SHA-256 mismatch")
    payload = read_json(args.mixed_plan)
    requests = payload.get("requests")
    if not isinstance(requests, list) or len(requests) != point["concurrency"]:
        raise SystemExit("frozen mixed plan has the wrong request count")
    return [
        {
            "slot": index,
            "class": str(spec["class"]),
            "input_tokens": int(spec["input_tokens"]),
            "output_tokens": int(spec["output_tokens"]),
        }
        for index, spec in enumerate(requests)
    ]


def validate_point(args: argparse.Namespace, runner: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    point = POINTS[args.point_id]
    missing = [name for name in REQUIRED_RUN_FILES if not (runner / name).is_file()]
    if missing:
        raise SystemExit(f"K3 raw backup is incomplete: {missing}")

    status = read_json(runner / "status.json")
    result = read_json(runner / "result.json")
    manifest = read_json(runner / "manifest.json")
    requested = read_json(runner / "requested_engine_args.json")
    fixture = read_json(runner / "input_fixture.json")
    contract = read_json(runner / "k3_contract.json")
    events = read_jsonl(runner / "requests.jsonl")
    telemetry = read_jsonl(runner / "telemetry.jsonl")
    stdout = (runner / "stdout.log").read_text(encoding="utf-8", errors="replace")
    stderr = (runner / "stderr.log").read_text(encoding="utf-8", errors="replace")
    logs = stdout + "\n" + stderr

    expected_count = point["concurrency"] * point["bursts"]
    if status.get("status") != "PASS" or result.get("status") != "PASS":
        raise SystemExit("K3 point did not complete PASS")
    if result.get("requested_request_count") != expected_count or result.get("completed_request_count") != expected_count:
        raise SystemExit("K3 request denominator/count mismatch")
    if result.get("arrival_mode") != "CLOSED_LOOP_BURST":
        raise SystemExit("K3 point did not use the frozen closed-loop burst mode")
    if manifest.get("runtime_class") != "SERVING_VARIANT" or manifest.get("arrival_mode") != "CLOSED_LOOP_BURST":
        raise SystemExit("K3 manifest runtime/arrival class mismatch")
    if manifest.get("sampling_mode") != "FORCED_LENGTH_CONTROLLED":
        raise SystemExit("K3 sampling mode is not frozen")

    if contract.get("contract_id") != "SWAP-K3-SERVING-PRESSURE-V1":
        raise SystemExit("K3 contract identity mismatch")
    if contract.get("status") != "FROZEN_BEFORE_NEW_RESULTS" or contract.get("selected_point_id") != args.point_id:
        raise SystemExit("K3 selected point was not frozen before execution")
    runtime = contract.get("runtime", {})
    expected_runtime = {
        "model": EXPECTED_MODEL,
        "max_model_len": 32768,
        "dtype": "bfloat16",
        "enforce_eager": True,
        "enable_prefix_caching": False,
        "kv_offloading_size_gib": 2.0,
        "kv_offloading_backend": "native",
        "gpu_memory_utilization": 0.97,
        "max_num_batched_tokens": 4096,
    }
    for key, value in expected_runtime.items():
        if runtime.get(key) != value:
            raise SystemExit(f"K3 runtime contract mismatch for {key}")
    workload = next((item for item in contract.get("workloads", []) if item.get("point_id") == args.point_id), None)
    if workload is None:
        raise SystemExit("selected K3 workload is absent from the contract")
    for key in ("concurrency", "bursts", "input_tokens", "output_tokens", "max_num_seqs", "max_num_batched_tokens", "request_plan"):
        if workload.get(key) != point[key]:
            raise SystemExit(f"K3 point contract mismatch for {key}")

    variables = manifest.get("variables", {})
    for key in ("concurrency", "bursts", "input_tokens", "output_tokens", "max_num_seqs", "max_num_batched_tokens"):
        if variables.get(key) != point[key]:
            raise SystemExit(f"K3 manifest variable mismatch for {key}")
    if manifest.get("model_path") != EXPECTED_MODEL or variables.get("gpu_memory_utilization") != 0.97:
        raise SystemExit("K3 model/GPU-memory contract mismatch")

    engine_args = str(requested.get("engine_args", ""))
    required_engine_markers = (
        "dtype='bfloat16'",
        "max_model_len=32768",
        "enable_prefix_caching=False",
        "gpu_memory_utilization=0.97",
        "max_num_batched_tokens=4096",
        f"max_num_seqs={point['max_num_seqs']}",
        "enforce_eager=True",
        "kv_offloading_size=2.0",
        "kv_offloading_backend='native'",
    )
    if not all(marker in engine_args for marker in required_engine_markers):
        raise SystemExit("K3 requested AsyncEngineArgs are incomplete")
    if requested.get("sampling_mode") != "FORCED_LENGTH_CONTROLLED":
        raise SystemExit("K3 requested sampling mode mismatch")

    specs = expected_specs(args, point)
    requested_plan = requested.get("request_plan")
    fixture_plan = fixture.get("request_plan")
    if not isinstance(requested_plan, list) or not isinstance(fixture_plan, list):
        raise SystemExit("K3 request-plan evidence is missing")
    if len(requested_plan) != expected_count or len(fixture_plan) != expected_count:
        raise SystemExit("K3 request-plan evidence count mismatch")
    for index, spec in enumerate(specs):
        if {key: requested_plan[index].get(key) for key in spec} != spec:
            raise SystemExit(f"K3 requested plan mismatch at slot {index}")
        if {key: fixture_plan[index].get(key) for key in spec} != spec:
            raise SystemExit(f"K3 fixture plan mismatch at slot {index}")
        token_ids = fixture_plan[index].get("token_ids")
        if not isinstance(token_ids, list) or len(token_ids) != spec["input_tokens"]:
            raise SystemExit(f"K3 fixture token count mismatch at slot {index}")
        if fixture_plan[index].get("token_count") != spec["input_tokens"] or fixture_plan[index].get("token_ids_sha256") != json_sha256(token_ids):
            raise SystemExit(f"K3 fixture token identity mismatch at slot {index}")

    result_records = result.get("records")
    if not isinstance(result_records, list) or len(result_records) != expected_count or len(events) != expected_count:
        raise SystemExit("K3 full request-event count mismatch")
    result_by_id = {record.get("request_id"): record for record in result_records}
    event_by_id = {record.get("request_id"): record for record in events}
    if len(result_by_id) != expected_count or result_by_id.keys() != event_by_id.keys():
        raise SystemExit("K3 request IDs are not unique/consistent")

    expected_arrivals = set(range(expected_count))
    if {record.get("arrival_index") for record in events} != expected_arrivals:
        raise SystemExit("K3 arrival-index denominator is incomplete")
    failures = 0
    censored = 0
    arrival_to_completion: list[int] = []
    ttft_values: list[int] = []
    completion_values: list[int] = []
    for record in events:
        request_id = str(record["request_id"])
        if result_by_id[request_id] != record:
            raise SystemExit(f"K3 result/JSONL event mismatch for {request_id}")
        index = int(record["arrival_index"])
        spec = specs[index]
        if record.get("input_tokens") != spec["input_tokens"] or record.get("requested_output_tokens") != spec["output_tokens"]:
            raise SystemExit(f"K3 request shape mismatch for {request_id}")
        if record.get("output_tokens") != spec["output_tokens"] or record.get("finish_reason") != "length":
            raise SystemExit(f"K3 output/finish correctness mismatch for {request_id}")
        if record.get("error") is not None:
            failures += 1
        submitted = record.get("submitted_monotonic_ns")
        scheduled = record.get("client_scheduled_arrival_monotonic_ns")
        first = record.get("first_yield_monotonic_ns")
        completed = record.get("completed_monotonic_ns")
        if not all(isinstance(value, int) for value in (submitted, scheduled, first, completed)):
            censored += 1
            continue
        if not (scheduled <= completed and submitted <= first <= completed):
            raise SystemExit(f"K3 monotonic timestamp ordering failed for {request_id}")
        if record.get("ttft_ns") != first - submitted or record.get("completion_latency_ns") != completed - submitted:
            raise SystemExit(f"K3 derived timing mismatch for {request_id}")
        if record.get("input_ids_sha256") != fixture_plan[index]["token_ids_sha256"]:
            raise SystemExit(f"K3 input identity mismatch for {request_id}")
        output_ids = record.get("output_token_ids")
        if not isinstance(output_ids, list) or record.get("output_ids_sha256") != json_sha256(output_ids):
            raise SystemExit(f"K3 output identity mismatch for {request_id}")
        arrival_to_completion.append(completed - scheduled)
        ttft_values.append(record["ttft_ns"])
        completion_values.append(record["completion_latency_ns"])
    if failures or censored:
        raise SystemExit(f"K3 PASS point has failures={failures}, censored={censored}")

    telemetry_events = [record.get("event") for record in telemetry]
    for required_event in ("pre_burst", "burst_submitted", "post_burst"):
        if required_event not in telemetry_events:
            raise SystemExit(f"K3 telemetry lacks {required_event}")
    host_snapshots: list[dict[str, int]] = []
    for record in telemetry:
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("raw"):
            raise SystemExit("K3 GPU telemetry snapshot is incomplete")
        host_memory = snapshot.get("host_memory")
        if not isinstance(host_memory, dict) or not all(key in host_memory for key in ("MemTotal", "MemAvailable", "MemFree")):
            raise SystemExit("K3 host-memory telemetry is incomplete")
        host_snapshots.append(host_memory)

    required_log_markers = (
        "Creating v1 connector with name: OffloadingConnector",
        "Creating offloading spec with name: CPUOffloadingSpec",
        "GPU KV cache size:",
        "Allocating a cross layer KV cache",
    )
    if not all(marker in logs for marker in required_log_markers):
        raise SystemExit("K3 native KV-offload log evidence is incomplete")

    file_hashes = evidence_file_hashes(runner)
    if len(file_hashes) < 10:
        raise SystemExit(f"expected at least 10 K3 raw files, found {len(file_hashes)}")
    raw_aggregate = aggregate_hash(file_hashes)
    if raw_aggregate != args.expected_aggregate_sha256:
        raise SystemExit(f"K3 raw aggregate mismatch: {raw_aggregate}")
    manifest_hash = sha256_file(runner / "SHA256SUMS")
    if manifest_hash != args.remote_manifest_sha256:
        raise SystemExit("K3 remote/local SHA256SUMS mismatch")
    mismatches = checksum_mismatches(runner)
    if mismatches:
        raise SystemExit(f"K3 checksum mismatch set is non-empty: {mismatches}")
    if sha256_file(runner / "stdout.log") != args.remote_stdout_sha256:
        raise SystemExit("K3 remote/local stdout mismatch")
    if sha256_file(runner / "stderr.log") != args.remote_stderr_sha256:
        raise SystemExit("K3 remote/local stderr mismatch")

    point_result = {
        "attempt_id": args.attempt_id,
        "point_id": args.point_id,
        "request_count_denominator": expected_count,
        "completed_request_count": expected_count,
        "failure_count": failures,
        "rejected_count": 0,
        "censored_count": censored,
        "request_shapes": specs,
        "arrival_to_completion_ns": numeric_summary(arrival_to_completion),
        "ttft_ns": numeric_summary(ttft_values),
        "completion_latency_ns": numeric_summary(completion_values),
        "telemetry_event_count": len(telemetry),
        "host_memory_snapshot_count": len(host_snapshots),
        "manifest_terminal_status_file": status.get("status"),
        "manifest_initial_status": manifest.get("status"),
        "native_kv_offload_log_evidence": list(required_log_markers),
        "kv_observation": "runtime configuration and logs only; object-level KV event trace is not exposed by this serving runner",
        "claim_boundary": contract.get("measurement_contract", {}).get("claim_boundary"),
        "raw_file_count": len(file_hashes),
        "raw_file_set_sha256": raw_aggregate,
        "checksum_manifest_sha256": manifest_hash,
        "stdout_sha256": sha256_file(runner / "stdout.log"),
        "stderr_sha256": sha256_file(runner / "stderr.log"),
        "validation_state": "VALIDATION_PASS",
    }
    return point_result, {
        "raw_file_hashes": file_hashes,
        "raw_aggregate": raw_aggregate,
        "events": events,
        "telemetry": telemetry,
        "host_snapshots": host_snapshots,
        "contract": contract,
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
    if failed_root is None or not failed_root.is_dir():
        raise SystemExit("--failed-local-attempt-root is required with --failed-attempt-id")
    file_hashes = raw_tree_hashes(failed_root)
    raw_aggregate = aggregate_hash(file_hashes)
    if args.failed_expected_aggregate_sha256 and raw_aggregate != args.failed_expected_aggregate_sha256:
        raise SystemExit("failed K3 attempt raw aggregate mismatch")
    stdout_path = failed_root / "stdout.log"
    stderr_path = failed_root / "stderr.log"
    if not stdout_path.is_file() or not stderr_path.is_file():
        raise SystemExit("failed K3 attempt lacks outer stdout/stderr")
    stdout_hash = sha256_file(stdout_path)
    stderr_hash = sha256_file(stderr_path)
    if args.failed_stdout_sha256 and stdout_hash != args.failed_stdout_sha256:
        raise SystemExit("failed K3 stdout mismatch")
    if args.failed_stderr_sha256 and stderr_hash != args.failed_stderr_sha256:
        raise SystemExit("failed K3 stderr mismatch")
    failure = {
        "attempt_id": args.failed_attempt_id,
        "failure_class": args.failed_failure_class,
        "failure": args.failed_failure,
        "repair_required": args.failed_repair_required,
        "remote_raw_path": args.failed_remote_attempt_root,
        "local_raw_path": str(failed_root),
        "raw_file_count": len(file_hashes),
        "file_set_sha256": raw_aggregate,
        "stdout_sha256": stdout_hash,
        "stderr_sha256": stderr_hash,
        "remote_local_hashes_verified": True,
        "status": args.failed_status,
    }
    row.setdefault("repair_lineage", []).append(failure)
    add_attempt_value(row, "attempt_ids", args.failed_attempt_id)
    add_attempt_value(row, "remote_raw_paths", args.failed_remote_attempt_root)
    add_attempt_value(row, "local_raw_paths", str(failed_root))
    add_attempt_value(row, "source_raw_sha256", raw_aggregate)
    append_unique(row.setdefault("manifest_sha256", []), [stdout_hash, stderr_hash])
    append_record_once(inventory.setdefault("swap_k3_failed_attempts", []), failure)
    append_record_once(
        backup.setdefault("phase7_attempt_backups", []),
        {
            "attempt_id": args.failed_attempt_id,
            "remote_attempt": args.failed_remote_attempt_root,
            "local_attempt": str(failed_root),
            "status": "VERIFIED_RAW_TECHNICAL_FAILURE",
            "file_count": len(file_hashes),
            "file_set_sha256": raw_aggregate,
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
    parser.add_argument("--point-id", choices=list(POINTS), required=True)
    parser.add_argument("--mixed-plan", type=Path)
    parser.add_argument("--expected-aggregate-sha256", required=True)
    parser.add_argument("--remote-manifest-sha256", required=True)
    parser.add_argument("--remote-stdout-sha256", required=True)
    parser.add_argument("--remote-stderr-sha256", required=True)
    parser.add_argument("--failed-attempt-id")
    parser.add_argument("--failed-remote-attempt-root", default="")
    parser.add_argument("--failed-local-attempt-root", type=Path)
    parser.add_argument("--failed-expected-aggregate-sha256", default="")
    parser.add_argument("--failed-stdout-sha256", default="")
    parser.add_argument("--failed-stderr-sha256", default="")
    parser.add_argument("--failed-failure-class", default="TECHNICAL_SETUP_FAILURE")
    parser.add_argument("--failed-failure", default="K3 technical launch/setup failure; no result is admitted.")
    parser.add_argument("--failed-repair-required", default="Use a fresh namespace with compile-checked frozen K3 runner injection.")
    parser.add_argument("--failed-status", default="TECHNICAL_FAILURE_NOT_ADMITTED")
    args = parser.parse_args()

    runner = runner_from_root(args.local_attempt_root, args.runner_dir_name)
    point_result, details = validate_point(args, runner)
    safe_point = args.point_id.lower().replace("-", "_")
    safe_attempt = args.attempt_id.lower().replace("-", "_")
    sidecar = args.local_attempt_root / f"derived_swap_k3_{safe_point}_{safe_attempt}_serving_events.json"
    write_json(
        sidecar,
        {
            "schema_version": "phase7-swap-k3-serving-point-derived-v1",
            "source_raw_sha256": details["raw_aggregate"],
            "point": point_result,
            "request_events": details["events"],
            "telemetry_events": details["telemetry"],
            "measurement_field_mapping": {
                "arrival_to_completion": "requests.jsonl client_scheduled_arrival_monotonic_ns -> completed_monotonic_ns",
                "gpu_telemetry": "telemetry.jsonl snapshot.raw",
                "host_memory": "telemetry.jsonl snapshot.host_memory",
                "failure_denominator": point_result["request_count_denominator"],
            },
            "limitations": [
                "Diagnostic serving-pressure evidence only; not formal SERV tail-CI closure.",
                "GPU and host-memory telemetry are burst-level snapshots, not per-request samples.",
                "No object-level KV issue/completion event trace is exposed by this serving runner.",
                "manifest.json retains its initial RUNNING field; status.json is the terminal PASS authority.",
            ],
        },
    )
    sidecar_hash = sha256_file(sidecar)

    root = args.master_root
    ledger_path = root / "master_execution_ledger.json"
    ledger = read_json(ledger_path)
    prior_hash = sha256_file(ledger_path)
    rows = ledger["rows"]
    row = next((item for item in rows if item.get("master_row_id") == "SWAP-K3"), None)
    if row is None:
        raise SystemExit("missing SWAP-K3 master row")
    inventory_path = root / "evidence_inventory.json"
    backup_path = root / "local_backup_manifest.json"
    inventory = read_json(inventory_path)
    backup = read_json(backup_path)

    record_failed_attempt(args, row, inventory, backup)
    add_attempt_value(row, "attempt_ids", args.attempt_id)
    add_attempt_value(row, "remote_raw_paths", args.remote_attempt_root)
    add_attempt_value(row, "local_raw_paths", str(args.local_attempt_root))
    add_attempt_value(row, "source_raw_sha256", details["raw_aggregate"])
    append_unique(
        row.setdefault("manifest_sha256", []),
        [
            point_result["checksum_manifest_sha256"],
            sha256_file(runner / "status.json"),
            sha256_file(runner / "result.json"),
            sha256_file(runner / "requests.jsonl"),
            sha256_file(runner / "telemetry.jsonl"),
            sha256_file(runner / "k3_contract.json"),
            point_result["stdout_sha256"],
            point_result["stderr_sha256"],
            sidecar_hash,
        ],
    )
    append_record_once(
        row.setdefault("k3_point_results", []),
        {**point_result, "derived_sidecar": str(sidecar), "derived_sidecar_sha256": sidecar_hash},
    )
    completed_points = [
        point_id
        for point_id in POINTS
        if any(item.get("point_id") == point_id and item.get("validation_state") == "VALIDATION_PASS" for item in row.get("k3_point_results", []))
    ]
    missing_points = [point_id for point_id in POINTS if point_id not in completed_points]
    matrix_complete = not missing_points
    next_gpu_unit = "SWAP-K3" if missing_points else "SWAP-K5"
    transition_id = f"MR7-SWAP-K3-{args.point_id}-{args.attempt_id}-PROMOTION"
    row.update(
        {
            "execution_state": "EXECUTION_COMPLETE",
            "raw_state": "COMPLETE",
            "backup_state": "VERIFIED",
            "review_state": "REVIEW_WITH_LIMITATION",
            "validation_state": "VALIDATION_PASS" if matrix_complete else "UNVERIFIED",
            "adoption_state": "ADOPTED" if matrix_complete else "SUPPLEMENT_REQUIRED",
            "blocker_or_failure": None if matrix_complete else f"K3_MATRIX_INCOMPLETE: missing {', '.join(missing_points)}",
            "k3_required_points": list(POINTS),
            "k3_completed_points": completed_points,
            "k3_missing_points": missing_points,
            "claims_supported": append_unique(
                list(row.get("claims_supported", [])),
                [
                    f"SWAP-K3 {args.point_id} completed {point_result['request_count_denominator']} frozen serving requests with full arrival-to-completion events, output-ID hashes, finish semantics, and zero failed/rejected/censored requests.",
                    f"SWAP-K3 {args.point_id} captured burst-level GPU and host-memory telemetry while native CPU KV offload was active in runtime configuration and logs.",
                ],
            ),
            "claims_forbidden": append_unique(
                list(row.get("claims_forbidden", [])),
                [
                    "Formal SERV tail-CI, 1000-request, throughput, p95/p99 stability, or calibration closure from SWAP-K3 diagnostics.",
                    "Object-level KV swap issue/completion, moved-byte, latency, or PCIe-bandwidth claims from SWAP-K3.",
                    "SWAP-K4 OFFxKV interaction or SWAP-K5 exhaustion/fallback conclusions from SWAP-K3.",
                ],
            ),
            "contamination_flags": append_unique(
                list(row.get("contamination_flags", [])),
                [
                    "K3_MANIFEST_INITIAL_STATUS_REMAINS_RUNNING_STATUS_JSON_IS_TERMINAL",
                    "K3_GPU_HOST_TELEMETRY_IS_BURST_LEVEL_NOT_PER_REQUEST",
                    "K3_NO_OBJECT_LEVEL_KV_EVENT_TRACE",
                ],
            ),
            "next_action": (
                f"Proceed to frozen SWAP-K3 point {missing_points[0]}."
                if missing_points
                else "Proceed to ready SWAP-K5; SWAP-K4 remains a separate required interaction row and must not dispatch without its frozen OFFxKV contract."
            ),
            "last_transition_record": transition_id,
        }
    )

    transition = {
        "transition_id": transition_id,
        "timestamp_utc": now_utc(),
        "changed_rows": ["SWAP-K3"],
        "reason": f"Promote verified SWAP-K3 {args.point_id} diagnostic serving-pressure point; retain row open until all four frozen points complete.",
        "prior_ledger_sha256": prior_hash,
        "attempt_id": args.attempt_id,
        "point_id": args.point_id,
        "point_result": point_result,
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
        inventory.setdefault("swap_k3_serving_pressure_attempts", []),
        {
            **point_result,
            "remote_raw_path": args.remote_attempt_root,
            "local_raw_path": str(args.local_attempt_root),
            "derived_sidecar": str(sidecar),
            "derived_sidecar_sha256": sidecar_hash,
            "status": "VALIDATION_PASS_POINT_MATRIX_COMPLETE" if matrix_complete else "VALIDATION_PASS_POINT_MATRIX_PARTIAL",
            "claims_forbidden": ["formal SERV tail-CI", "object-level KV performance", "SWAP-K4/K5"],
        },
    )
    write_json(inventory_path, inventory)

    trigger_path = root / "trigger_adjudication.json"
    trigger = read_json(trigger_path)
    for entry in trigger.get("entries", []):
        if entry.get("trigger_id") == "SWAP-K3":
            evidence = f"{args.attempt_id} validated {args.point_id} serving-pressure events under native KV offload; matrix complete={matrix_complete}."
            append_unique(entry.setdefault("observed_evidence", []), [evidence])
            append_unique(entry.setdefault("source_evidence_hashes", []), [details["raw_aggregate"]])
            entry["trigger_state"] = "TRIGGERED"
    write_json(trigger_path, trigger)

    gap_path = root / "gap_register.json"
    gap = read_json(gap_path)
    gap_entries = gap.setdefault("entries", [])
    gap_entries[:] = [entry for entry in gap_entries if entry.get("gap_id") != "GAP-SWAP-K3-SERVING-PRESSURE-MATRIX"]
    gap_entries.append(
        {
            "gap_id": "GAP-SWAP-K3-SERVING-PRESSURE-MATRIX",
            "status": "CLOSED_WITH_LIMITATION" if matrix_complete else "SUPPLEMENT_REQUIRED",
            "source": str(sidecar),
            "consequence": "All four K3 serving-pressure points are required; evidence remains diagnostic and cannot replace formal SERV tail-CI or object-level KV performance evidence.",
            "completed_points": completed_points,
            "missing_points": missing_points,
        }
    )
    write_json(gap_path, gap)

    claims_path = root / "claim_boundary_register.json"
    claims = read_json(claims_path)
    append_unique(claims.setdefault("claims_allowed_now", []), [f"SWAP-K3 {args.point_id} diagnostic serving-pressure event and output-correctness evidence"])
    forbidden_now = claims.setdefault("claims_forbidden_now", [])
    if matrix_complete:
        forbidden_now[:] = [item for item in forbidden_now if item != "SWAP-K2/K3/K5 results before their own gates"]
    append_unique(
        forbidden_now,
        [
            "SWAP-K3 formal SERV tail-CI or calibrated performance closure",
            "SWAP-K3 object-level KV latency/bandwidth/copy-completion claims",
            "SWAP-K4 interaction or SWAP-K5 exhaustion conclusions before their own gates",
        ],
    )
    write_json(claims_path, claims)

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
    queue["next_gpu_unit"] = next_gpu_unit
    queue["ready_gpu_units"] = [next_gpu_unit]
    queue["next_k3_point"] = missing_points[0] if missing_points else None
    queue["dispatch_guards"] = [
        "MR2 read-only preflight clear",
        "no foreign serving/GPU process at dispatch",
        "new-session four-guard canary validated and locally backed up",
        "SWAP-K0/K1/K2 gates and local backups verified",
        f"SWAP-K3 completed points: {', '.join(completed_points) if completed_points else 'none'}",
        "SWAP-K3 uses frozen diagnostic contract; do not promote to formal SERV tail-CI",
        "SWAP-K4 requires its own frozen OFFxKV interaction contract and remains non-dispatchable until then",
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
            "status": "SWAP-K3 point raw backup verified",
        },
    )
    append_record_once(
        backup.setdefault("phase7_attempt_backups", []),
        {
            "attempt_id": args.attempt_id,
            "remote_attempt": args.remote_attempt_root,
            "local_attempt": str(args.local_attempt_root),
            "status": "VERIFIED_RAW_VALIDATION_PASS_WITH_LIMITATIONS",
            "file_count": point_result["raw_file_count"],
            "file_set_sha256": point_result["raw_file_set_sha256"],
            "manifest_sha256": point_result["checksum_manifest_sha256"],
            "derived_sidecar_sha256": sidecar_hash,
        },
    )
    write_json(backup_path, backup)

    review_name = f"MR7-SWAP-K3-{args.point_id}-{args.attempt_id}-PROMOTION.json"
    write_json(
        root / "reviews" / review_name,
        {
            "schema_version": "phase7-combined-master-swap-k3-point-review-v1",
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
            "claims_forbidden": ["formal SERV tail-CI", "object-level KV performance", "SWAP-K4/K5"],
            "next_ready_unit": next_gpu_unit,
        },
    )
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
            "next_ready_gpu_unit": next_gpu_unit,
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
                "request_count_denominator": point_result["request_count_denominator"],
                "failure_count": point_result["failure_count"],
                "censored_count": point_result["censored_count"],
                "matrix_completed_points": completed_points,
                "matrix_missing_points": missing_points,
                "execution_ledger_sha256": execution_hash,
                "required_closed_count": len(rows) - len(remaining),
                "required_remaining_count": len(remaining),
                "next_ready_gpu_unit": next_gpu_unit,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
