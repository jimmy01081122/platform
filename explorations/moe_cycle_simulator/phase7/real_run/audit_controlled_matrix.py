#!/usr/bin/env python3
"""Audit a locally backed-up Phase 7 controlled GPU matrix."""

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


def add_error(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def verify_run_checksums(run_dir: Path, errors: list[str]) -> int:
    checksum_path = run_dir / "SHA256SUMS"
    if not checksum_path.is_file():
        errors.append(f"{run_dir.name}: missing SHA256SUMS")
        return 0
    verified = 0
    for line_number, line in enumerate(checksum_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            expected, relative = line.split("  ", 1)
        except ValueError:
            errors.append(f"{run_dir.name}: malformed SHA256SUMS line {line_number}")
            continue
        target = run_dir / relative
        if not target.is_file():
            errors.append(f"{run_dir.name}: checksum target missing: {relative}")
            continue
        actual = sha256_file(target)
        if actual != expected:
            errors.append(f"{run_dir.name}: checksum mismatch: {relative}")
            continue
        verified += 1
    return verified


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--batch-id", default="controlled-v1-20260811T1400Z")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    backup = args.backup_root.resolve()
    runs_root = backup / "controlled_runs"
    batch_dir = backup / "remote_batch" / args.batch_id
    errors: list[str] = []
    warnings: list[str] = []

    add_error(errors, runs_root.is_dir(), f"missing runs root: {runs_root}")
    add_error(errors, batch_dir.is_dir(), f"missing batch directory: {batch_dir}")
    if errors:
        report = {"status": "FAIL", "errors": errors, "warnings": warnings}
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 1

    batch_status = read_json(batch_dir / "status.json")
    batch_manifest = read_json(batch_dir / "manifest.json")
    add_error(errors, batch_status.get("status") == "PASS", "batch status is not PASS")
    add_error(errors, batch_status.get("completed_runs") == 90, "batch completed_runs is not 90")
    add_error(errors, batch_status.get("total_runs") == 90, "batch total_runs is not 90")

    with (batch_dir / "progress.tsv").open("r", encoding="utf-8", newline="") as handle:
        progress = list(csv.DictReader(handle, delimiter="\t"))
    add_error(errors, len(progress) == 90, f"expected 90 progress rows, got {len(progress)}")
    progress_ids = [row["experiment_id"] for row in progress]
    add_error(errors, len(set(progress_ids)) == len(progress_ids), "duplicate experiment IDs in progress")
    add_error(errors, all(row["status"] == "PASS" for row in progress), "non-PASS progress row found")

    expected_experiments = {
        f"CTRL-{point['point_id']}-{runtime.lower()}"
        for point in batch_manifest["points"]
        for runtime in ("CLEAN", "ROUTING", "TELEMETRY")
    }
    add_error(errors, len(batch_manifest["points"]) == 30, "batch manifest does not contain 30 points")
    add_error(errors, set(progress_ids) == expected_experiments, "progress coverage differs from frozen manifest")

    run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    add_error(errors, len(run_dirs) == 90, f"expected 90 backed-up run directories, got {len(run_dirs)}")
    run_by_experiment: dict[str, Path] = {}
    checksum_files_verified = 0
    request_count_by_class: Counter[str] = Counter()
    telemetry_sample_count = 0
    routing_array_count = 0
    routing_event_count = 0
    outputs_by_point: dict[str, list[list[int]]] = defaultdict(list)
    durations_by_class: dict[str, list[int]] = defaultdict(list)

    for progress_row in progress:
        experiment_id = progress_row["experiment_id"]
        remote_basename = Path(progress_row["run_dir"]).name
        run_dir = runs_root / remote_basename
        if not run_dir.is_dir():
            errors.append(f"{experiment_id}: backed-up run directory missing: {remote_basename}")
            continue
        run_status = read_json(run_dir / "status.json")
        manifest = read_json(run_dir / "manifest.json")
        model = read_json(run_dir / "model_identity.json")
        engine_args = read_json(run_dir / "requested_engine_args.json")
        requests = read_jsonl(run_dir / "requests.jsonl")
        runtime_class = manifest.get("runtime_class")
        point_id = experiment_id.removeprefix("CTRL-").removesuffix(f"-{runtime_class.lower()}")
        run_by_experiment[experiment_id] = run_dir

        add_error(errors, run_status.get("status") == "PASS", f"{experiment_id}: run status is not PASS")
        add_error(errors, manifest.get("experiment_id") == experiment_id, f"{experiment_id}: manifest ID mismatch")
        add_error(errors, manifest.get("model_revision") == MODEL_REVISION, f"{experiment_id}: model revision mismatch")
        config = model.get("config", {})
        add_error(errors, config.get("num_hidden_layers") == 32, f"{experiment_id}: layer count is not 32")
        add_error(errors, config.get("num_local_experts") == 8, f"{experiment_id}: expert count is not 8")
        add_error(errors, config.get("num_experts_per_tok") == 2, f"{experiment_id}: top-k is not 2")
        add_error(errors, config.get("torch_dtype") == "bfloat16", f"{experiment_id}: model dtype is not BF16")
        add_error(errors, engine_args.get("dtype") == "bfloat16", f"{experiment_id}: runtime dtype is not BF16")
        add_error(errors, engine_args.get("quantization") is None, f"{experiment_id}: quantization is enabled")
        add_error(errors, engine_args.get("cpu_offload_gb") == 0, f"{experiment_id}: CPU offload is enabled")
        add_error(errors, engine_args.get("max_num_seqs") == 1, f"{experiment_id}: max_num_seqs is not 1")
        add_error(errors, engine_args.get("enable_prefix_caching") is False, f"{experiment_id}: prefix cache is enabled")

        expected_repetitions = 4 if runtime_class == "CLEAN" else 1
        add_error(
            errors,
            len(requests) == expected_repetitions,
            f"{experiment_id}: expected {expected_repetitions} requests, got {len(requests)}",
        )
        request_count_by_class[runtime_class] += len(requests)
        if runtime_class == "CLEAN":
            roles = Counter(request.get("repetition_role") for request in requests)
            add_error(errors, roles == Counter({"warmup": 1, "measured": 3}), f"{experiment_id}: clean roles invalid")
        else:
            roles = Counter(request.get("repetition_role") for request in requests)
            add_error(errors, roles == Counter({"measured": 1}), f"{experiment_id}: instrumented roles invalid")

        expected_input = manifest.get("input_tokens_requested")
        expected_output = manifest.get("output_tokens_requested")
        for request in requests:
            add_error(errors, request.get("input_token_count") == expected_input, f"{experiment_id}: input length mismatch")
            add_error(errors, request.get("output_token_count") == expected_output, f"{experiment_id}: output length mismatch")
            add_error(errors, len(request.get("input_token_ids", [])) == expected_input, f"{experiment_id}: input IDs truncated")
            add_error(errors, len(request.get("output_token_ids", [])) == expected_output, f"{experiment_id}: output IDs truncated")
            add_error(errors, request.get("finish_reason") == "length", f"{experiment_id}: finish reason is not length")
            add_error(errors, request.get("num_cached_tokens") == 0, f"{experiment_id}: unexpected cached tokens")
            outputs_by_point[point_id].append(request.get("output_token_ids", []))
            durations_by_class[runtime_class].append(request.get("wall_duration_ns", 0))

        if runtime_class == "ROUTING" and requests:
            routing = requests[0].get("routing", {})
            expected_shape = [expected_input + expected_output - 1, 32, 2]
            add_error(errors, routing.get("validation_status") == "PASS", f"{experiment_id}: routing validation failed")
            add_error(errors, routing.get("shape") == expected_shape, f"{experiment_id}: routing shape mismatch")
            add_error(errors, routing.get("minimum_expert_id", -1) >= 0, f"{experiment_id}: negative expert ID")
            add_error(errors, routing.get("maximum_expert_id", 8) < 8, f"{experiment_id}: expert ID out of range")
            route_path = run_dir / routing.get("array_path", "")
            add_error(errors, route_path.is_file() and route_path.stat().st_size > 0, f"{experiment_id}: routing array missing")
            routing_array_count += 1
            routing_event_count += expected_shape[0] * expected_shape[1] * expected_shape[2]

        if runtime_class == "TELEMETRY" and requests:
            telemetry_path = run_dir / "telemetry.jsonl"
            add_error(errors, telemetry_path.is_file() and telemetry_path.stat().st_size > 0, f"{experiment_id}: telemetry missing")
            telemetry_rows = read_jsonl(telemetry_path) if telemetry_path.is_file() else []
            telemetry_sample_count += len(telemetry_rows)
            telemetry = requests[0].get("telemetry") or {}
            add_error(errors, telemetry.get("sample_count", 0) > 0, f"{experiment_id}: telemetry sample_count is zero")
            add_error(errors, not telemetry.get("errors"), f"{experiment_id}: telemetry sampler errors present")

        checksum_files_verified += verify_run_checksums(run_dir, errors)

    for point_id, outputs in outputs_by_point.items():
        if outputs:
            reference = outputs[0]
            add_error(
                errors,
                all(output == reference for output in outputs[1:]),
                f"{point_id}: output-token IDs differ across clean/routing/telemetry repetitions",
            )

    monitor_v1_status = read_json(batch_dir / "model_residency_monitor" / "status.json")
    monitor_v2_dir = batch_dir / "model_residency_monitor_v2"
    monitor_v2_status = read_json(monitor_v2_dir / "status.json")
    monitor_v2_summary = read_json(monitor_v2_dir / "per_run_summary.json")
    monitor_v2_samples = read_jsonl(monitor_v2_dir / "samples.jsonl")
    reconciled = read_json(monitor_v2_dir / "reconciled_load_times.json")
    add_error(errors, monitor_v1_status.get("status") == "STOPPED", "monitor v1 calibration run not preserved as STOPPED")
    add_error(errors, monitor_v2_status.get("status") == "PASS", "monitor v2 status is not PASS")
    add_error(errors, reconciled.get("status") == "PASS", "monitor reconciliation status is not PASS")
    add_error(errors, reconciled.get("record_count") == 90, "monitor reconciliation does not contain 90 records")
    reconciled_records = reconciled.get("records", [])
    add_error(
        errors,
        all(record.get("vllm_reported_model_loading_seconds") is not None for record in reconciled_records),
        "reconciled model-load time missing",
    )
    add_error(
        errors,
        all(record.get("vllm_reported_weight_loading_seconds") is not None for record in reconciled_records),
        "reconciled weight-load time missing",
    )
    historical = read_json(monitor_v2_dir / "historical_load_backfill.json").get("records", [])
    live = monitor_v2_summary.get("records", [])
    historical_ids = {record["experiment_id"] for record in historical}
    live_ids = {record["experiment_id"] for record in live}
    add_error(errors, len(historical) == 47, f"expected 47 historical monitor records, got {len(historical)}")
    add_error(errors, len(live) == 43, f"expected 43 live monitor records, got {len(live)}")
    add_error(errors, not historical_ids.intersection(live_ids), "historical/live monitor coverage overlaps")
    add_error(errors, historical_ids.union(live_ids) == expected_experiments, "monitor coverage does not span 90 experiments")
    left_censored = [record for record in live if record.get("load_transition_left_censored")]
    add_error(errors, len(left_censored) == 1, f"expected one left-censored live run, got {len(left_censored)}")
    for record in live:
        experiment_id = record["experiment_id"]
        add_error(errors, record.get("resident_wall_time_ns") is not None, f"{experiment_id}: no resident timestamp")
        add_error(errors, record.get("idle_wall_time_ns") is not None, f"{experiment_id}: no idle timestamp")
        add_error(errors, record.get("peak_used_memory_bytes", 0) >= 80_000 * 1024 * 1024, f"{experiment_id}: resident threshold not reached")
        add_error(errors, record.get("resident_to_idle_transition_upper_bound_ns") is not None, f"{experiment_id}: no eviction bound")
        if not record.get("load_transition_left_censored"):
            add_error(errors, record.get("dispatch_to_resident_ns", 0) > 0, f"{experiment_id}: invalid load duration")

    # The batch-level checksum was created concurrently with the sidecar's
    # final writes and is intentionally not used as an integrity root.  Each
    # immutable run checksum is verified above; the backup receives a new
    # post-download checksum after this audit.
    warnings.append(
        "Batch-level SHA256SUMS is not authoritative for sidecar files because the monitor finalized after batch PASS; "
        "per-run checksums and the post-download backup checksum are authoritative."
    )

    report = {
        "status": "PASS" if not errors else "FAIL",
        "batch_id": args.batch_id,
        "batch_status": batch_status,
        "point_count": len(batch_manifest["points"]),
        "run_count": len(progress),
        "request_count_by_class": dict(request_count_by_class),
        "routing_array_count": routing_array_count,
        "routing_event_count": routing_event_count,
        "telemetry_sample_count": telemetry_sample_count,
        "run_checksum_files_verified": checksum_files_verified,
        "monitor": {
            "v1_status": monitor_v1_status.get("status"),
            "v2_status": monitor_v2_status.get("status"),
            "historical_log_only_count": len(historical),
            "live_nvml_count": len(live),
            "left_censored_live_count": len(left_censored),
            "raw_sample_count": len(monitor_v2_samples),
            "reconciled_load_count": len(reconciled_records),
        },
        "errors": errors,
        "warnings": warnings,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
