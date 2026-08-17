#!/usr/bin/env python3
"""Audit a locally backed-up Phase 7 W0-W3 natural GPU campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[Any]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def verify_checksums(run_dir: Path, errors: list[str]) -> int:
    path = run_dir / "SHA256SUMS"
    if not path.is_file():
        errors.append(f"{run_dir.name}: missing SHA256SUMS")
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        expected, relative = line.split("  ", 1)
        target = run_dir / relative
        if not target.is_file():
            errors.append(f"{run_dir.name}: checksum target missing: {relative}")
        elif sha256_file(target) != expected:
            errors.append(f"{run_dir.name}: checksum mismatch: {relative}")
        else:
            count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--batch-id", default="natural-v1-20260811T1541Z")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.backup_root.resolve()
    batch = root / "remote_batch" / args.batch_id
    runs_root = root / "natural_runs"
    fixtures_root = root / "fixtures" / "natural-v1-20260811T1530Z"
    repairs_root = root / "profiler_repairs"
    errors: list[str] = []
    warnings: list[str] = []

    for path in (batch, runs_root, fixtures_root):
        check(errors, path.is_dir(), f"missing required directory: {path}")
    if errors:
        report = {"status": "FAIL", "errors": errors, "warnings": warnings}
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 1

    status = read_json(batch / "status.json")
    manifest = read_json(batch / "manifest.json")
    fixture_manifest = read_json(fixtures_root / "manifest.json")
    w3_plan = read_json(fixtures_root / "W3_request_plan.json")
    check(errors, status.get("status") == "PASS", "natural batch status is not PASS")
    check(errors, status.get("completed_runs") == 15, "natural batch completed_runs is not 15")
    check(errors, status.get("total_runs") == 15, "natural batch total_runs is not 15")
    check(errors, manifest.get("fixture_manifest_sha256") == sha256_file(fixtures_root / "manifest.json"), "fixture manifest SHA mismatch")

    with (batch / "progress.tsv").open("r", encoding="utf-8", newline="") as handle:
        progress = list(csv.DictReader(handle, delimiter="\t"))
    check(errors, len(progress) == 15, f"expected 15 progress rows, got {len(progress)}")
    check(errors, len({row["experiment_id"] for row in progress}) == 15, "duplicate natural experiment ID")
    check(errors, all(row["status"] == "PASS" for row in progress), "non-PASS natural progress row")
    expected_ids = {row[0] for row in manifest["execution_plan"]}
    check(errors, {row["experiment_id"] for row in progress} == expected_ids, "natural execution-plan coverage mismatch")

    fixture_by_workload = {row["workload_id"]: row for row in fixture_manifest["fixtures"]}
    w3_by_id = {row["request_id"]: row for row in w3_plan["requests"]}
    run_count = 0
    checksum_count = 0
    request_count = Counter()
    routing_count = 0
    telemetry_samples = 0
    output_groups: dict[str, list[list[int]]] = defaultdict(list)
    invalid_parent_profiles: list[str] = []

    for row in progress:
        experiment_id = row["experiment_id"]
        workload_id = row["workload_id"]
        runtime_class = row["runtime_class"]
        run_dir = runs_root / Path(row["run_dir"]).name
        if not run_dir.is_dir():
            errors.append(f"{experiment_id}: missing backed-up run")
            continue
        run_count += 1
        run_status = read_json(run_dir / "status.json")
        run_manifest = read_json(run_dir / "manifest.json")
        model = read_json(run_dir / "model_identity.json")
        engine = read_json(run_dir / "requested_engine_args.json")
        requests = read_jsonl(run_dir / "requests.jsonl")
        check(errors, run_status.get("status") == "PASS", f"{experiment_id}: run status not PASS")
        check(errors, run_manifest.get("experiment_id") == experiment_id, f"{experiment_id}: manifest ID mismatch")
        check(errors, run_manifest.get("model_revision") == MODEL_REVISION, f"{experiment_id}: model revision mismatch")
        config = model.get("config", {})
        check(errors, (config.get("num_hidden_layers"), config.get("num_local_experts"), config.get("num_experts_per_tok")) == (32, 8, 2), f"{experiment_id}: MoE dimensions mismatch")
        check(errors, engine.get("dtype") == "bfloat16", f"{experiment_id}: runtime is not BF16")
        check(errors, engine.get("quantization") is None and engine.get("cpu_offload_gb") == 0, f"{experiment_id}: quant/offload contract violated")
        check(errors, engine.get("max_num_seqs") == 1, f"{experiment_id}: max_num_seqs is not one")
        request_count[runtime_class] += len(requests)

        if workload_id in {"W0", "W1", "W2"}:
            expected_input = fixture_by_workload[workload_id]["input_token_count"]
            expected_output = fixture_by_workload[workload_id]["forced_output_tokens"]
            expected_requests = 4 if runtime_class == "CLEAN" else 1
            check(errors, len(requests) == expected_requests, f"{experiment_id}: request count mismatch")
            if runtime_class == "CLEAN":
                check(errors, Counter(req.get("repetition_role") for req in requests) == Counter({"warmup": 1, "measured": 3}), f"{experiment_id}: clean role mismatch")
            for request in requests:
                check(errors, request.get("input_token_count") == expected_input, f"{experiment_id}: input count mismatch")
                check(errors, request.get("output_token_count") == expected_output, f"{experiment_id}: output count mismatch")
                check(errors, request.get("finish_reason") == "length", f"{experiment_id}: finish reason mismatch")
                check(errors, len(request.get("input_token_ids", [])) == expected_input, f"{experiment_id}: input IDs truncated")
                check(errors, len(request.get("output_token_ids", [])) == expected_output, f"{experiment_id}: output IDs truncated")
                output_groups[workload_id].append(request.get("output_token_ids", []))
            if runtime_class == "ROUTING" and requests:
                routing = requests[0].get("routing", {})
                check(errors, routing.get("validation_status") == "PASS", f"{experiment_id}: routing validation failed")
                check(errors, routing.get("shape") == [expected_input + expected_output - 1, 32, 2], f"{experiment_id}: routing shape mismatch")
                check(errors, 0 <= routing.get("minimum_expert_id", -1) <= routing.get("maximum_expert_id", 8) < 8, f"{experiment_id}: expert IDs invalid")
                routing_count += 1
            if runtime_class in {"TELEMETRY", "MEMORY_PROFILE"}:
                telemetry_path = run_dir / "telemetry.jsonl"
                check(errors, telemetry_path.is_file() and telemetry_path.stat().st_size > 0, f"{experiment_id}: telemetry missing")
                telemetry_samples += len(read_jsonl(telemetry_path)) if telemetry_path.is_file() else 0
            if runtime_class == "KERNEL_PROFILE" and requests:
                profiler = requests[0].get("profiler") or {}
                if profiler.get("method") != "vllm.EngineCore worker torch.profiler" or profiler.get("kernel_event_count", 0) <= 0:
                    invalid_parent_profiles.append(experiment_id)
        else:
            check(errors, len(requests) == 96, f"{experiment_id}: W3 must contain 96 requests")
            events = read_jsonl(run_dir / "sequence_events.jsonl")
            check(errors, len(events) == 6, f"{experiment_id}: W3 sequence event count mismatch")
            check(errors, [event["event"] for event in events] == ["sequence_start", "sequence_complete"] * 3, f"{experiment_id}: W3 sequence event order mismatch")
            for request in requests:
                plan_id = request.get("plan_request_id")
                plan_row = w3_by_id.get(plan_id)
                if not plan_row:
                    errors.append(f"{experiment_id}: unknown W3 plan request {plan_id}")
                    continue
                check(errors, request.get("input_token_count") == plan_row["preflight_input_token_count"], f"{experiment_id}/{plan_id}: input count differs from preflight")
                check(errors, request.get("output_token_count") == plan_row["output_tokens"], f"{experiment_id}/{plan_id}: output count mismatch")
                check(errors, request.get("finish_reason") == "length", f"{experiment_id}/{plan_id}: finish reason mismatch")
                check(errors, 1 <= request.get("sequence_index", 0) <= 3, f"{experiment_id}/{plan_id}: sequence index invalid")
                check(errors, 1 <= request.get("sequence_position", 0) <= 32, f"{experiment_id}/{plan_id}: sequence position invalid")
                output_groups[f"W3/{plan_id}"].append(request.get("output_token_ids", []))
            if runtime_class == "TELEMETRY":
                telemetry_path = run_dir / "telemetry.jsonl"
                check(errors, telemetry_path.is_file() and telemetry_path.stat().st_size > 0, "W3 telemetry raw file missing")
                telemetry_samples += len(read_jsonl(telemetry_path)) if telemetry_path.is_file() else 0

        checksum_count += verify_checksums(run_dir, errors)

    for key, outputs in output_groups.items():
        if outputs:
            check(errors, all(output == outputs[0] for output in outputs[1:]), f"{key}: output IDs differ across repetitions/classes")

    repair_results = {}
    accepted_repairs = (
        "K6-W1-WORKER-PROFILE-V3",
        "K7-W2-WORKER-PROFILE-V2",
    )
    for repair_id in accepted_repairs:
        candidates = list(repairs_root.glob(f"*__{repair_id}")) if repairs_root.is_dir() else []
        if len(candidates) != 1:
            errors.append(f"missing unique profiler repair run: {repair_id}")
            continue
        repair = candidates[0]
        repair_status = read_json(repair / "status.json")
        repair_request = read_jsonl(repair / "requests.jsonl")[0]
        profiler = repair_request.get("profiler") or {}
        check(errors, repair_status.get("status") == "PASS", f"{repair_id}: status not PASS")
        check(errors, profiler.get("method") == "vllm.EngineCore worker torch.profiler", f"{repair_id}: wrong profiler method")
        check(errors, profiler.get("kernel_event_count", 0) > 0, f"{repair_id}: no worker kernel events")
        check(errors, profiler.get("model_kernel_event_count", 0) > 0, f"{repair_id}: no recognizable model kernel events")
        check(errors, profiler.get("prefill_marker_count", 0) > 0, f"{repair_id}: no prefill markers")
        check(errors, profiler.get("decode_marker_count", 0) > 0, f"{repair_id}: no decode markers")
        check(errors, profiler.get("model_correlation") == "PASS_PREFILL_DECODE_SEPARABLE", f"{repair_id}: model phase correlation failed")
        check(errors, profiler.get("correlation_event_count", 0) > 0, f"{repair_id}: no launch/completion correlation")
        check(errors, bool(profiler.get("stream_ids")), f"{repair_id}: no stream identity")
        check(errors, profiler.get("validation_status") == "PASS", f"{repair_id}: profiler validation failed")
        checksum_count += verify_checksums(repair, errors)
        repair_results[repair_id] = {
            "kernel_event_count": profiler.get("kernel_event_count"),
            "model_kernel_event_count": profiler.get("model_kernel_event_count"),
            "prefill_marker_count": profiler.get("prefill_marker_count"),
            "decode_marker_count": profiler.get("decode_marker_count"),
            "copy_event_count_by_direction": profiler.get("copy_event_count_by_direction"),
            "copy_bytes_by_direction": profiler.get("copy_bytes_by_direction"),
            "stream_ids": profiler.get("stream_ids"),
            "correlation_event_count": profiler.get("correlation_event_count"),
            "trace_event_count": profiler.get("trace_event_count"),
            "trace_files": profiler.get("trace_files"),
        }

    repair_history = []
    if repairs_root.is_dir():
        for repair in sorted(path for path in repairs_root.iterdir() if path.is_dir()):
            status_path = repair / "status.json"
            failure_path = repair / "failure.json"
            repair_history.append(
                {
                    "run": repair.name,
                    "status": read_json(status_path).get("status") if status_path.is_file() else "MISSING",
                    "failure_preserved": failure_path.is_file(),
                }
            )

    monitor = batch / "model_residency_monitor_v2"
    monitor_status = read_json(monitor / "status.json")
    monitor_summary = read_json(monitor / "per_run_summary.json")
    monitor_history = read_json(monitor / "historical_load_backfill.json").get("records", [])
    monitor_live = monitor_summary.get("records", [])
    monitor_samples = read_jsonl(monitor / "samples.jsonl")
    check(errors, monitor_status.get("status") == "PASS", "natural residency monitor status not PASS")
    check(errors, len(monitor_history) == 1 and len(monitor_live) == 14, "natural residency coverage must be 1 historical + 14 live")
    check(errors, sum(bool(row.get("load_transition_left_censored")) for row in monitor_live) == 0, "natural residency live data is left-censored")

    warnings.append(
        "Original W1/W2 parent-process torch.profiler runs are preserved but are not accepted as K6/K7; "
        "only worker-profiler repair runs satisfy deep kernel evidence."
    )
    report = {
        "status": "PASS" if not errors else "FAIL",
        "batch_status": status,
        "run_count": run_count,
        "request_count_by_class": dict(request_count),
        "routing_array_count": routing_count,
        "telemetry_sample_count": telemetry_samples,
        "checksum_files_verified": checksum_count,
        "invalid_parent_profiler_runs_preserved": invalid_parent_profiles,
        "worker_profiler_repairs": repair_results,
        "worker_profiler_repair_history": repair_history,
        "residency_monitor": {
            "status": monitor_status.get("status"),
            "historical_count": len(monitor_history),
            "live_count": len(monitor_live),
            "raw_sample_count": len(monitor_samples),
        },
        "errors": errors,
        "warnings": warnings,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
