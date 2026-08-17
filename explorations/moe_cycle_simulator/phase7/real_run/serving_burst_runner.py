#!/usr/bin/env python3
"""Real vLLM serving burst/concurrency measurement runner.

This runner uses AsyncLLMEngine so requests overlap in the actual vLLM
continuous-batching scheduler.  It records arrival, first-yield, completion,
token identity, and raw scheduler-visible request IDs.  It is intentionally a
serving runtime and must not be mixed with canonical max_num_seqs=1 timing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import subprocess
import time
from pathlib import Path
from typing import Any


MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
MAX_MODEL_LEN = 32768
SAMPLING_BASE = {
    "n": 1,
    "temperature": 0.0,
    "top_p": 1.0,
    "top_k": 0,
    "seed": 0,
    "stop": [],
    "ignore_eos": True,
    "detokenize": True,
    "skip_special_tokens": False,
}


def sha256_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def fixture_token_ids(tokenizer: Any, length: int) -> list[int]:
    anchor = (
        "Phase 7 Mixtral GPU serving fixture. Preserve exact token positions "
        "while exercising continuous batching and decode. "
    )
    ids = list(tokenizer.encode(anchor, add_special_tokens=False))
    special_ids = {
        getattr(tokenizer, name, None)
        for name in ("bos_token_id", "eos_token_id", "unk_token_id")
    }
    ids = [int(token) for token in ids if token not in special_ids]
    if not ids or length < 1:
        raise ValueError(f"invalid serving fixture length: {length}")
    return [ids[index % len(ids)] for index in range(length)]


def load_request_plan(
    plan_path: Path | None,
    concurrency: int,
    default_input_tokens: int,
    default_output_tokens: int,
) -> list[dict[str, Any]]:
    """Load a frozen per-slot serving shape plan.

    A mixed serving burst must preserve the request mix instead of silently
    turning into a same-shape batch.  The plan therefore has exactly one
    entry per concurrent request slot and is reused unchanged for every
    measured burst.
    """
    if plan_path is None:
        return [
            {
                "slot": index,
                "class": "homogeneous",
                "input_tokens": default_input_tokens,
                "output_tokens": default_output_tokens,
            }
            for index in range(concurrency)
        ]
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    entries = payload.get("requests") if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or len(entries) != concurrency:
        raise ValueError(
            f"request plan must contain exactly {concurrency} request entries"
        )
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"request plan entry {index} is not an object")
        input_tokens = int(entry["input_tokens"])
        output_tokens = int(entry["output_tokens"])
        if input_tokens < 1 or output_tokens < 1:
            raise ValueError(f"request plan entry {index} has non-positive length")
        normalized.append({
            "slot": index,
            "class": str(entry.get("class", "unspecified")),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })
    return normalized


def nvidia_smi_snapshot() -> dict[str, Any]:
    query = (
        "timestamp,name,memory.used,memory.free,memory.total,utilization.gpu,"
        "utilization.memory,power.draw,temperature.gpu,clocks.gr,clocks.mem"
    )
    try:
        completed = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return {"raw": completed.stdout.strip(), "captured_at_ns": time.time_ns()}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "captured_at_ns": time.time_ns()}


async def collect_request(
    engine: Any,
    prompt_ids: list[int],
    sampling_params: Any,
    request_id: str,
    event_path: Path,
    sampling_mode: str,
    requested_output_tokens: int,
    client_scheduled_arrival_ns: int | None = None,
    arrival_index: int | None = None,
) -> dict[str, Any]:
    submitted_ns = time.monotonic_ns()
    first_yield_ns: int | None = None
    finished_ns: int | None = None
    latest_token_ids: list[int] = []
    latest_text = ""
    update_count = 0
    finish_reason: str | None = None
    error: str | None = None
    try:
        async for request_output in engine.generate(prompt_ids, sampling_params, request_id):
            now_ns = time.monotonic_ns()
            update_count += 1
            if first_yield_ns is None:
                first_yield_ns = now_ns
            candidate = request_output.outputs[0]
            latest_token_ids = [int(value) for value in candidate.token_ids]
            latest_text = str(candidate.text)
            finish_reason = candidate.finish_reason
            if request_output.finished:
                finished_ns = now_ns
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        finished_ns = time.monotonic_ns()

    record = {
        "schema_version": "phase7-serving-request-v1",
        "request_id": request_id,
        "input_tokens": len(prompt_ids),
        "requested_output_tokens": requested_output_tokens,
        "output_tokens": len(latest_token_ids),
        "input_ids_sha256": sha256_json(prompt_ids),
        "output_ids_sha256": sha256_json(latest_token_ids),
        "output_token_ids": latest_token_ids,
        "output_text": latest_text,
        "submitted_monotonic_ns": submitted_ns,
        "client_scheduled_arrival_monotonic_ns": (
            submitted_ns
            if client_scheduled_arrival_ns is None
            else client_scheduled_arrival_ns
        ),
        "server_observed_arrival_monotonic_ns": submitted_ns,
        "arrival_index": arrival_index,
        "first_yield_monotonic_ns": first_yield_ns,
        "completed_monotonic_ns": finished_ns,
        "ttft_ns": None if first_yield_ns is None else first_yield_ns - submitted_ns,
        "completion_latency_ns": None if finished_ns is None else finished_ns - submitted_ns,
        "decode_updates": update_count,
        "finish_reason": finish_reason,
        "error": error,
        "sampling_mode": sampling_mode,
    }
    append_jsonl(event_path, record)
    return record


async def run(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    from transformers import AutoTokenizer
    from vllm import AsyncLLMEngine, SamplingParams
    from vllm.engine.arg_utils import AsyncEngineArgs

    tokenizer = AutoTokenizer.from_pretrained(str(args.model_path), local_files_only=True)
    request_plan = load_request_plan(
        args.request_plan,
        args.concurrency,
        args.input_tokens,
        args.output_tokens,
    )
    prompt_ids_by_slot = [
        fixture_token_ids(tokenizer, int(spec["input_tokens"]))
        for spec in request_plan
    ]
    for spec in request_plan:
        if int(spec["input_tokens"]) + int(spec["output_tokens"]) > MAX_MODEL_LEN:
            raise ValueError(
                f"request plan slot {spec['slot']} exceeds max model length"
            )

    engine_args = AsyncEngineArgs(
        model=str(args.model_path),
        enable_return_routed_experts=os.environ.get("PHASE7_ENABLE_STEP_TRACE") == "1",
        dtype="bfloat16",
        load_format="safetensors",
        safetensors_prefetch_num_threads=1,
        max_model_len=MAX_MODEL_LEN,
        max_num_seqs=args.max_num_seqs,
        max_num_batched_tokens=args.max_num_batched_tokens,
        gpu_memory_utilization=args.gpu_memory_utilization,
        enforce_eager=True,
        enable_prefix_caching=False,
        disable_log_stats=True,
        enable_log_requests=True,
    )
    engine = AsyncLLMEngine.from_engine_args(engine_args)
    sampling_configs = [
        {
            **(
                SAMPLING_BASE
                if args.sampling_mode == "FORCED_LENGTH_CONTROLLED"
                else {**SAMPLING_BASE, "ignore_eos": False, "min_tokens": 0}
            ),
            "max_tokens": int(spec["output_tokens"]),
            "min_tokens": (
                int(spec["output_tokens"])
                if args.sampling_mode == "FORCED_LENGTH_CONTROLLED"
                else 0
            ),
        }
        for spec in request_plan
    ]
    sampling_params_by_slot = [
        SamplingParams(**sampling_config) for sampling_config in sampling_configs
    ]
    write_json(run_dir / "requested_engine_args.json", {
        "engine_args": str(engine_args),
        "sampling_params_by_slot": sampling_configs,
        "sampling_mode": args.sampling_mode,
        "request_plan": request_plan,
        "model_revision": MODEL_REVISION,
    })
    write_json(run_dir / "input_fixture.json", {
        "fixture_id": "phase7_mixtral_serving_repeated_anchor_v1",
        "request_plan": [
            {
                **spec,
                "token_count": len(prompt_ids_by_slot[index]),
                "token_ids": prompt_ids_by_slot[index],
                "token_ids_sha256": sha256_json(prompt_ids_by_slot[index]),
            }
            for index, spec in enumerate(request_plan)
        ],
    })

    event_path = run_dir / "requests.jsonl"
    snapshots_path = run_dir / "telemetry.jsonl"
    append_jsonl(snapshots_path, {"event": "pre_burst", "snapshot": nvidia_smi_snapshot()})

    for warmup_index in range(args.warmup_bursts):
        warmup_tasks = [
            collect_request(
                engine,
                prompt_ids_by_slot[index],
                sampling_params_by_slot[index],
                f"warmup-{warmup_index:04d}-{index:04d}",
                run_dir / "warmup_requests.jsonl",
                args.sampling_mode,
                int(request_plan[index]["output_tokens"]),
            )
            for index in range(len(request_plan))
        ]
        await asyncio.gather(*warmup_tasks)

    records: list[dict[str, Any]] = []
    request_index = 0
    if args.arrival_rate_rps is not None:
        # Deterministic Poisson open-loop schedule.  All scheduled arrivals are
        # persisted before the corresponding task is admitted to the engine so
        # the client contract remains auditable even if a request later fails.
        rng = random.Random(args.arrival_seed)
        arrival_trace_path = run_dir / "arrival_trace.jsonl"
        schedule_start_ns = time.monotonic_ns()
        scheduled_offset_ns = 0
        tasks: list[asyncio.Task[dict[str, Any]]] = []
        for arrival_index in range(args.total_requests):
            if arrival_index:
                scheduled_offset_ns += int(
                    rng.expovariate(args.arrival_rate_rps) * 1_000_000_000
                )
            scheduled_ns = schedule_start_ns + scheduled_offset_ns
            sleep_ns = scheduled_ns - time.monotonic_ns()
            if sleep_ns > 0:
                await asyncio.sleep(sleep_ns / 1_000_000_000)
            position = arrival_index % len(request_plan)
            request_id = f"open-{arrival_index:06d}"
            append_jsonl(arrival_trace_path, {
                "schema_version": "phase7-serving-arrival-v1",
                "arrival_mode": "POISSON_OPEN_LOOP",
                "arrival_seed": args.arrival_seed,
                "arrival_rate_rps": args.arrival_rate_rps,
                "arrival_index": arrival_index,
                "request_id": request_id,
                "slot": position,
                "class": request_plan[position]["class"],
                "input_tokens": request_plan[position]["input_tokens"],
                "output_tokens": request_plan[position]["output_tokens"],
                "scheduled_offset_ns": scheduled_offset_ns,
                "scheduled_monotonic_ns": scheduled_ns,
            })
            task = asyncio.create_task(
                collect_request(
                    engine,
                    prompt_ids_by_slot[position],
                    sampling_params_by_slot[position],
                    request_id,
                    event_path,
                    args.sampling_mode,
                    int(request_plan[position]["output_tokens"]),
                    scheduled_ns,
                    arrival_index,
                )
            )
            tasks.append(task)
            if (arrival_index + 1) % max(1, len(request_plan)) == 0:
                append_jsonl(snapshots_path, {
                    "event": "open_loop_arrivals_submitted",
                    "arrival_index": arrival_index,
                    "submitted_request_count": arrival_index + 1,
                    "captured_at_ns": time.time_ns(),
                    "snapshot": nvidia_smi_snapshot(),
                })
        records.extend(await asyncio.gather(*tasks))
    else:
        for burst_index in range(args.bursts):
            tasks: list[asyncio.Task[dict[str, Any]]] = []
            for position in range(args.concurrency):
                if position and args.intra_burst_gap_ms > 0:
                    await asyncio.sleep(args.intra_burst_gap_ms / 1000.0)
                request_index += 1
                request_id = f"burst-{burst_index:03d}-request-{position:03d}"
                scheduled_ns = time.monotonic_ns()
                task = asyncio.create_task(
                    collect_request(
                        engine,
                        prompt_ids_by_slot[position],
                        sampling_params_by_slot[position],
                        request_id,
                        event_path,
                        args.sampling_mode,
                        int(request_plan[position]["output_tokens"]),
                        scheduled_ns,
                        request_index - 1,
                    )
                )
                tasks.append(task)
            append_jsonl(snapshots_path, {
                "event": "burst_submitted",
                "burst_index": burst_index,
                "submitted_request_count": len(tasks),
                "captured_at_ns": time.time_ns(),
                "snapshot": nvidia_smi_snapshot(),
            })
            records.extend(await asyncio.gather(*tasks))
            if burst_index + 1 < args.bursts and args.inter_burst_gap_ms > 0:
                await asyncio.sleep(args.inter_burst_gap_ms / 1000.0)

    append_jsonl(snapshots_path, {"event": "post_burst", "snapshot": nvidia_smi_snapshot()})
    shutdown_result = engine.shutdown()
    if hasattr(shutdown_result, "__await__"):
        await shutdown_result
    return {
        "schema_version": (
            "phase7-serving-result-v2"
            if args.arrival_rate_rps is not None
            else "phase7-serving-burst-result-v1"
        ),
        "arrival_mode": (
            "POISSON_OPEN_LOOP"
            if args.arrival_rate_rps is not None
            else "CLOSED_LOOP_BURST"
        ),
        "arrival_rate_rps": args.arrival_rate_rps,
        "arrival_seed": args.arrival_seed if args.arrival_rate_rps is not None else None,
        "status": "PASS" if all(record["error"] is None for record in records) else "FAIL",
        "records": records,
        "completed_request_count": len(records),
        "requested_request_count": (
            args.total_requests
            if args.arrival_rate_rps is not None
            else args.concurrency * args.bursts
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--input-tokens", type=int, required=True)
    parser.add_argument("--output-tokens", type=int, required=True)
    parser.add_argument(
        "--sampling-mode",
        choices=("FORCED_LENGTH_CONTROLLED", "NATURAL_EOS_CAPPED"),
        default="FORCED_LENGTH_CONTROLLED",
        help="forced exact output length or natural EOS with max_tokens as cap",
    )
    parser.add_argument(
        "--request-plan",
        type=Path,
        default=None,
        help="JSON plan with exactly one input/output shape per concurrent slot",
    )
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--bursts", type=int, default=1)
    parser.add_argument("--intra-burst-gap-ms", type=float, default=0.0)
    parser.add_argument("--inter-burst-gap-ms", type=float, default=0.0)
    parser.add_argument(
        "--arrival-rate-rps",
        type=float,
        default=None,
        help="deterministic Poisson open-loop arrival rate; enables open-loop mode",
    )
    parser.add_argument(
        "--total-requests",
        type=int,
        default=None,
        help="number of open-loop requests; required with --arrival-rate-rps",
    )
    parser.add_argument(
        "--arrival-seed",
        type=int,
        default=0,
        help="seed for the frozen Poisson inter-arrival stream",
    )
    parser.add_argument("--warmup-burst", action="store_true")
    parser.add_argument(
        "--warmup-bursts",
        type=int,
        default=0,
        help="number of retained-but-excluded warmup bursts; --warmup-burst aliases one",
    )
    parser.add_argument("--max-num-seqs", type=int, required=True)
    parser.add_argument("--max-num-batched-tokens", type=int, default=512)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.97)
    args = parser.parse_args()
    if args.concurrency < 1 or args.max_num_seqs < args.concurrency:
        raise SystemExit("max-num-seqs must cover concurrency")
    if args.bursts < 1 or args.input_tokens < 1 or args.output_tokens < 1:
        raise SystemExit("burst and token lengths must be positive")
    if args.warmup_bursts < 0:
        raise SystemExit("--warmup-bursts must be non-negative")
    if args.warmup_burst:
        args.warmup_bursts = max(args.warmup_bursts, 1)
    if args.arrival_rate_rps is not None:
        if args.arrival_rate_rps <= 0 or args.total_requests is None:
            raise SystemExit(
                "open-loop mode requires positive --arrival-rate-rps and --total-requests"
            )
        if args.total_requests < 1:
            raise SystemExit("--total-requests must be positive")
    elif args.total_requests is not None:
        raise SystemExit("--total-requests requires --arrival-rate-rps")
    run_dir = args.run_root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}__{args.experiment_id}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = args.run_root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}__{args.experiment_id}__r{suffix}"
    run_dir.mkdir(parents=True)
    if os.environ.get("PHASE7_ENABLE_STEP_TRACE") == "1":
        step_trace_dir = run_dir / "routing_steps"
        step_trace_dir.mkdir()
        os.environ["PHASE7_STEP_TRACE_DIR"] = str(step_trace_dir.resolve())
        write_json(step_trace_dir / "hook_request.json", {
            "schema_version": "phase7-serving-routing-step-hook-request-v1",
            "hook_revision": os.environ.get("PHASE7_STEP_TRACE_HOOK_REVISION", "UNSPECIFIED"),
            "experiment_id": args.experiment_id,
            "runtime_class": "SERVING_VARIANT",
            "expected_kernel_correlation": False,
        })
    write_json(run_dir / "manifest.json", {
        "schema_version": (
            "phase7-serving-manifest-v2"
            if args.arrival_rate_rps is not None
            else "phase7-serving-burst-manifest-v1"
        ),
        "status": "RUNNING",
        "experiment_id": args.experiment_id,
        "runtime_class": "SERVING_VARIANT",
        "model_path": str(args.model_path),
        "model_revision": MODEL_REVISION,
        "variables": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "sampling_mode": args.sampling_mode,
        "arrival_mode": (
            "POISSON_OPEN_LOOP"
            if args.arrival_rate_rps is not None
            else "CLOSED_LOOP_BURST"
        ),
        "arrival_contract": {
            "rate_rps": args.arrival_rate_rps,
            "total_requests": args.total_requests,
            "seed": args.arrival_seed if args.arrival_rate_rps is not None else None,
        },
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    try:
        result = asyncio.run(run(args, run_dir))
        write_json(run_dir / "result.json", result)
        write_json(run_dir / "status.json", {
            "status": result["status"],
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_dir": str(run_dir),
        })
        print(json.dumps({"status": result["status"], "run_dir": str(run_dir), "completed_request_count": result["completed_request_count"]}))
        return 0 if result["status"] == "PASS" else 1
    except Exception as exc:
        write_json(run_dir / "status.json", {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_dir": str(run_dir),
        })
        raise


if __name__ == "__main__":
    raise SystemExit(main())
