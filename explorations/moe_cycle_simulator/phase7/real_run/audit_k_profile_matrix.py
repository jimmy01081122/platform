#!/usr/bin/env python3
"""Audit K0-K5 bounded canaries and formal worker-profiler rows."""

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    batch = args.batch_dir.resolve()
    runs_root = args.runs_root.resolve()
    errors: list[str] = []
    status = read_json(batch / "status.json")
    manifest = read_json(batch / "manifest.json")
    with (batch / "progress.tsv").open("r", encoding="utf-8", newline="") as handle:
        progress = list(csv.DictReader(handle, delimiter="\t"))
    check(errors, status.get("status") == "PASS", "batch status is not PASS")
    check(errors, status.get("completed_runs") == 12, "batch completed count is not 12")
    check(errors, len(progress) == 12, "progress row count is not 12")
    check(errors, all(row.get("status") == "PASS" for row in progress), "non-PASS progress row")
    check(errors, manifest.get("sampling_mode") == "FORCED_LENGTH_CONTROLLED", "wrong sampling mode")

    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    result_summary = {}
    for row in progress:
        target = row["experiment_id"].removesuffix("-CANARY")
        run = runs_root / Path(row["run_dir"]).name
        check(errors, run.is_dir(), f"{row['experiment_id']}: run missing")
        if not run.is_dir():
            continue
        run_status = read_json(run / "status.json")
        requests = read_jsonl(run / "requests.jsonl")
        check(errors, run_status.get("status") == "PASS", f"{row['experiment_id']}: status not PASS")
        check(errors, len(requests) == 1, f"{row['experiment_id']}: expected one request")
        if len(requests) != 1:
            continue
        request = requests[0]
        profiler = request.get("profiler") or {}
        expected_output = 2 if row["experiment_id"].endswith("-CANARY") else int({
            "K0-F0": 32,
            "K1-P0-128": 32,
            "K2-P0-8192": 32,
            "K3-P1-28672": 32,
            "K4-DEC0-512": 512,
            "K5-DEC1-1024": 1024,
        }[target])
        check(errors, request.get("sampling_mode") == "FORCED_LENGTH_CONTROLLED", f"{row['experiment_id']}: wrong request mode")
        sampling = request.get("sampling", {})
        check(errors, sampling.get("ignore_eos") is True, f"{row['experiment_id']}: ignore_eos not true")
        check(errors, sampling.get("min_tokens") == expected_output, f"{row['experiment_id']}: min_tokens mismatch")
        check(errors, sampling.get("max_tokens") == expected_output, f"{row['experiment_id']}: max_tokens mismatch")
        check(errors, request.get("output_token_count") == expected_output, f"{row['experiment_id']}: output count mismatch")
        check(errors, request.get("finish_reason") == "length", f"{row['experiment_id']}: finish reason mismatch")
        check(errors, profiler.get("method") == "vllm.EngineCore worker torch.profiler", f"{row['experiment_id']}: profiler method mismatch")
        check(errors, profiler.get("kernel_event_count", 0) > 0, f"{row['experiment_id']}: no kernel events")
        check(errors, profiler.get("model_kernel_event_count", 0) > 0, f"{row['experiment_id']}: no model kernel events")
        check(errors, profiler.get("prefill_marker_count", 0) > 0, f"{row['experiment_id']}: no prefill marker")
        check(errors, profiler.get("decode_marker_count", 0) > 0, f"{row['experiment_id']}: no decode marker")
        check(errors, profiler.get("model_correlation") == "PASS_PREFILL_DECODE_SEPARABLE", f"{row['experiment_id']}: phase correlation failed")
        check(errors, profiler.get("correlation_event_count", 0) > 0, f"{row['experiment_id']}: no correlation events")
        check(errors, bool(profiler.get("stream_ids")), f"{row['experiment_id']}: no stream IDs")
        by_target[target].append({"id": row["experiment_id"], "request": request, "profiler": profiler, "run": str(run)})

    for target, rows in sorted(by_target.items()):
        check(errors, len(rows) == 2, f"{target}: expected canary and formal rows")
        if len(rows) != 2:
            continue
        canary = next((row for row in rows if row["id"].endswith("-CANARY")), None)
        formal = next((row for row in rows if not row["id"].endswith("-CANARY")), None)
        check(errors, canary is not None and formal is not None, f"{target}: canary/formal role missing")
        if canary and formal:
            check(errors, canary["request"].get("input_token_ids") == formal["request"].get("input_token_ids"), f"{target}: canary/formal input IDs differ")
            result_summary[target] = {
                "fit_role": manifest.get("fit_held_out_lock", {}).get(target),
                "input_token_count": formal["request"].get("input_token_count"),
                "output_token_count": formal["request"].get("output_token_count"),
                "kernel_event_count": formal["profiler"].get("kernel_event_count"),
                "model_kernel_event_count": formal["profiler"].get("model_kernel_event_count"),
                "prefill_marker_count": formal["profiler"].get("prefill_marker_count"),
                "decode_marker_count": formal["profiler"].get("decode_marker_count"),
                "trace_event_count": formal["profiler"].get("trace_event_count"),
                "run": formal["run"],
            }

    report = {"status": "PASS" if not errors else "FAIL", "errors": errors, "batch_status": status, "targets": result_summary, "canaries_excluded_from_fit": True}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
