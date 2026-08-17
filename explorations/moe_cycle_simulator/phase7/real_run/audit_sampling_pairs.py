#!/usr/bin/env python3
"""Audit Phase 7 SMP1 natural-EOS clean/instrumented pairs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[Any]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def check(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch = args.batch_dir.resolve()
    runs_root = args.runs_root.resolve()
    errors: list[str] = []
    status = read_json(batch / "status.json")
    manifest = read_json(batch / "manifest.json")
    with (batch / "progress.tsv").open("r", encoding="utf-8", newline="") as handle:
        progress = list(csv.DictReader(handle, delimiter="\t"))

    check(errors, status.get("status") == "PASS", "batch status is not PASS")
    check(errors, status.get("completed_runs") == 9, "batch did not complete nine runs")
    check(errors, len(progress) == 9, f"expected nine progress rows, got {len(progress)}")
    check(errors, all(row.get("status") == "PASS" for row in progress), "non-PASS progress row")
    check(errors, manifest.get("sampling_mode") == "NATURAL_EOS_CAPPED", "manifest mode is not SMP1")
    expected_ids = {row[0] for row in manifest.get("execution_plan", [])}
    check(errors, {row.get("experiment_id") for row in progress} == expected_ids, "execution-plan coverage mismatch")

    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    runtime_identity: dict[str, dict[str, Any]] = {}
    scheduler_records: list[dict[str, Any]] = []
    telemetry_samples = 0
    routing_arrays = 0
    for row in progress:
        experiment_id = row["experiment_id"]
        run = runs_root / Path(row["run_dir"]).name
        check(errors, run.is_dir(), f"{experiment_id}: run directory missing")
        if not run.is_dir():
            continue
        run_status = read_json(run / "status.json")
        run_manifest = read_json(run / "manifest.json")
        requests = read_jsonl(run / "requests.jsonl")
        check(errors, run_status.get("status") == "PASS", f"{experiment_id}: run status not PASS")
        check(errors, len(requests) == 1, f"{experiment_id}: expected one request")
        if len(requests) != 1:
            continue
        request = requests[0]
        sampling = request.get("sampling", {})
        cap = int(run_manifest.get("output_tokens_requested"))
        check(errors, run_manifest.get("sampling_mode") == "NATURAL_EOS_CAPPED", f"{experiment_id}: wrong manifest sampling mode")
        check(errors, request.get("sampling_mode") == "NATURAL_EOS_CAPPED", f"{experiment_id}: wrong request sampling mode")
        check(errors, sampling.get("ignore_eos") is False, f"{experiment_id}: ignore_eos is not false")
        check(errors, "min_tokens" not in sampling, f"{experiment_id}: natural mode unexpectedly sets min_tokens")
        check(errors, sampling.get("max_tokens") == cap, f"{experiment_id}: max_tokens differs from cap")
        check(errors, 0 < request.get("output_token_count", 0) <= cap, f"{experiment_id}: output length outside cap")
        check(errors, request.get("finish_reason") in {"stop", "length"}, f"{experiment_id}: invalid finish reason")
        check(errors, run_manifest.get("sampling_pair_id") == row["pair_id"], f"{experiment_id}: pair ID mismatch")
        check(errors, run_manifest.get("sampling_pair_role") == row["pair_role"], f"{experiment_id}: pair role mismatch")

        runtime_class = row["runtime_class"]
        if runtime_class == "ROUTING":
            routing = request.get("routing", {})
            expected_forwarded = request["input_token_count"] + request["output_token_count"] - 1
            expected_shape = [expected_forwarded, 32, 2]
            check(errors, routing.get("validation_status") == "PASS", f"{experiment_id}: routing validation failed")
            check(errors, routing.get("expected_forwarded_token_count") == expected_forwarded, f"{experiment_id}: forwarded-token conservation failed")
            check(errors, routing.get("shape") == expected_shape, f"{experiment_id}: routing shape conservation failed")
            array_path = run / str(routing.get("array_path", ""))
            check(errors, array_path.is_file(), f"{experiment_id}: routing array missing")
            if array_path.is_file():
                import numpy as np

                array = np.load(array_path, allow_pickle=False)
                check(errors, list(array.shape) == expected_shape, f"{experiment_id}: raw routing array shape mismatch")
                check(errors, int(array.size) == expected_forwarded * 32 * 2, f"{experiment_id}: raw routing event-count mismatch")
                check(errors, int(array.min()) >= 0 and int(array.max()) < 8, f"{experiment_id}: raw expert ID out of range")
            routing_arrays += 1
        if runtime_class == "TELEMETRY":
            telemetry = read_jsonl(run / "telemetry.jsonl")
            check(errors, bool(telemetry), f"{experiment_id}: telemetry empty")
            telemetry_samples += len(telemetry)

        resolved = read_json(run / "resolved_runtime.json")
        requested = read_json(run / "requested_engine_args.json")
        vllm_config = resolved.get("llm_engine", {}).get("attributes", {}).get("vllm_config", {})
        scheduler_config = vllm_config.get("scheduler_config")
        scheduler_records.append(scheduler_config)
        runtime_identity[experiment_id] = {
            "max_model_len": requested.get("max_model_len"),
            "max_num_seqs": requested.get("max_num_seqs"),
            "max_num_batched_tokens": requested.get("max_num_batched_tokens"),
            "gpu_memory_utilization": requested.get("gpu_memory_utilization"),
            "enable_prefix_caching": requested.get("enable_prefix_caching"),
            "enforce_eager": requested.get("enforce_eager"),
            "scheduler_config": scheduler_config,
        }
        pairs[row["pair_id"]].append({"experiment_id": experiment_id, "runtime_class": runtime_class, "request": request})

    pair_report = {}
    for pair_id, members in sorted(pairs.items()):
        check(errors, len(members) == 3, f"{pair_id}: expected CLEAN/ROUTING/TELEMETRY")
        classes = {member["runtime_class"] for member in members}
        check(errors, classes == {"CLEAN", "ROUTING", "TELEMETRY"}, f"{pair_id}: runtime-class coverage mismatch")
        if not members:
            continue
        reference = members[0]["request"]
        for member in members[1:]:
            request = member["request"]
            check(errors, request.get("input_token_ids") == reference.get("input_token_ids"), f"{pair_id}: input IDs differ")
            check(errors, request.get("sampling") == reference.get("sampling"), f"{pair_id}: SamplingParams differ")
            check(errors, request.get("output_token_ids") == reference.get("output_token_ids"), f"{pair_id}: output IDs differ")
            check(errors, request.get("finish_reason") == reference.get("finish_reason"), f"{pair_id}: finish reason differs")
        pair_report[pair_id] = {
            "classes": sorted(classes),
            "input_token_count": reference.get("input_token_count"),
            "output_token_count": reference.get("output_token_count"),
            "finish_reason": reference.get("finish_reason"),
            "sampling": reference.get("sampling"),
        }

    check(errors, all(isinstance(record, dict) for record in scheduler_records), "resolved scheduler config missing")
    if scheduler_records:
        scheduler_canonical = json.dumps(scheduler_records[0], sort_keys=True)
        check(
            errors,
            all(json.dumps(record, sort_keys=True) == scheduler_canonical for record in scheduler_records[1:]),
            "resolved scheduler config differs across sampling pairs",
        )

    report = {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "batch_status": status,
        "pair_count": len(pairs),
        "pairs": pair_report,
        "routing_array_count": routing_arrays,
        "telemetry_sample_count": telemetry_samples,
        "runtime_identity": runtime_identity,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
