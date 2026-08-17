#!/usr/bin/env python3
"""SWAP-K5 native KV host-pool exhaustion/fallback evidence runner."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
EXPECTED_SOURCE_HASHES = {
    "/usr/local/lib/python3.12/dist-packages/vllm/v1/kv_offload/cpu/manager.py": "fd89bb49d46a01fdf6acfdf6c3c1867e5cb3116a0b24bc47859cc83de34841bd",
    "/usr/local/lib/python3.12/dist-packages/vllm/v1/core/sched/scheduler.py": "9ae1491f57a6294e32416b93ef2ea68107fa0d65c3a4d7f745f1261012f735e9",
    "/usr/local/lib/python3.12/dist-packages/vllm/distributed/kv_events.py": "24841685ad6db3543a19b9639eea24e1992ab37c4773c775da9f4140bb813322",
    "/usr/local/lib/python3.12/dist-packages/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py": "42dddd1dd728622d6f6488eae58b26c51c76d6b80c20874233cda2a87f75b7c6",
}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return value


def source_gate() -> dict[str, str]:
    actual = {path: sha256_file(Path(path)) for path in EXPECTED_SOURCE_HASHES}
    if actual != EXPECTED_SOURCE_HASHES:
        raise RuntimeError(f"source contract mismatch: {actual!r}")
    return actual


def dispatch_preflight() -> dict[str, Any]:
    identity = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=uuid,name,driver_version,memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    compute_apps = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    process_table = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,etimes=,comm=,args="],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    foreign_serving = []
    for line in process_table.splitlines():
        lowered = line.lower()
        if not any(marker in lowered for marker in (
            "vllm serve", "api_server", "openai.api_server", "serving_burst_runner.py"
        )):
            continue
        try:
            pid = int(line.split(None, 1)[0])
        except (ValueError, IndexError):
            pid = -1
        if pid != os.getpid():
            foreign_serving.append(line.strip())
    expected_uuid = "GPU-177cc8e4-ff4d-a649-ac29-a3807141b521"
    result = {
        "captured_at_utc": utc_now(),
        "expected_gpu_uuid": expected_uuid,
        "gpu_identity": identity,
        "compute_apps": compute_apps.splitlines() if compute_apps else [],
        "foreign_serving_processes": foreign_serving,
        "same_session_guard_gpu": expected_uuid in identity,
        "zero_compute_apps": not bool(compute_apps),
        "zero_foreign_serving": not bool(foreign_serving),
    }
    result["status"] = "PASS" if all((
        result["same_session_guard_gpu"],
        result["zero_compute_apps"],
        result["zero_foreign_serving"],
    )) else "BLOCKED"
    if result["status"] != "PASS":
        raise RuntimeError(f"dispatch preflight blocked: {result!r}")
    return result


def prompt_fixture(tokenizer: Any, length: int, slot: int) -> list[int]:
    anchor = (
        f"Phase 7 SWAP K5 host exhaustion request slot {slot}. "
        "Preserve token identity while applying native KV offload pressure. "
    )
    ids = [int(v) for v in tokenizer.encode(anchor, add_special_tokens=False)]
    forbidden = {
        getattr(tokenizer, name, None)
        for name in ("bos_token_id", "eos_token_id", "unk_token_id")
    }
    ids = [value for value in ids if value not in forbidden]
    if not ids:
        raise RuntimeError("empty prompt fixture anchor")
    rotated = ids[slot % len(ids):] + ids[:slot % len(ids)]
    return [rotated[index % len(rotated)] for index in range(length)]


def parse_status_kib(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            match = re.match(r"^(VmRSS|VmPin|VmSize):\s+(\d+)\s+kB$", line)
            if match:
                result[match.group(1)] = int(match.group(2))
    except (OSError, PermissionError):
        pass
    return result


def host_process_snapshot() -> dict[str, Any]:
    totals = {"VmRSS": 0, "VmPin": 0, "VmSize": 0}
    processes: list[dict[str, Any]] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, PermissionError):
            continue
        if not any(marker in command for marker in ("vllm", "swap_k5_exhaustion_runner", "Mixtral-8x7B")):
            continue
        values = parse_status_kib(proc / "status")
        for key in totals:
            totals[key] += values.get(key, 0)
        processes.append({"pid": int(proc.name), "command": command[:512], **values})
    meminfo: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        match = re.match(r"^(MemTotal|MemAvailable|Mlocked|Unevictable):\s+(\d+)\s+kB$", line)
        if match:
            meminfo[match.group(1)] = int(match.group(2))
    return {"process_totals_kib": totals, "processes": processes, "meminfo_kib": meminfo}


def gpu_snapshot() -> dict[str, Any]:
    fields = "timestamp,uuid,name,memory.used,memory.free,memory.total,utilization.gpu,utilization.memory,power.draw"
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return {"raw": result.stdout.strip()}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


class TelemetryCollector:
    def __init__(self, path: Path, interval_s: float = 0.5) -> None:
        self.path = path
        self.interval_s = interval_s
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=10)

    def _run(self) -> None:
        sequence = 0
        while not self.stop_event.is_set():
            append_jsonl(self.path, {
                "sequence": sequence,
                "captured_at_utc": utc_now(),
                "monotonic_ns": time.monotonic_ns(),
                "gpu": gpu_snapshot(),
                "host": host_process_snapshot(),
            })
            sequence += 1
            self.stop_event.wait(self.interval_s)


class KVEventCollector:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.records: list[dict[str, Any]] = []
        self.decode_errors: list[str] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(timeout=10):
            raise RuntimeError("KV event subscriber did not become ready")

    def stop(self) -> None:
        time.sleep(1.0)
        self.stop_event.set()
        self.thread.join(timeout=10)

    def _run(self) -> None:
        import msgspec
        import zmq

        context = zmq.Context.instance()
        socket = context.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, b"")
        socket.setsockopt(zmq.RCVHWM, 100000)
        socket.connect(self.endpoint)
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        self.ready.set()
        try:
            while not self.stop_event.is_set():
                if socket not in dict(poller.poll(100)):
                    continue
                frames = socket.recv_multipart()
                if len(frames) != 3:
                    self.decode_errors.append(f"unexpected frame count {len(frames)}")
                    continue
                topic, seq_raw, payload = frames
                try:
                    decoded = msgspec.msgpack.decode(payload)
                    sequence = int.from_bytes(seq_raw, "big")
                    self.records.append({
                        "sequence": sequence,
                        "topic": topic.decode(errors="replace"),
                        "payload_bytes": len(payload),
                        "payload_sha256": hashlib.sha256(payload).hexdigest(),
                        "captured_at_utc": utc_now(),
                        "decoded": jsonable(decoded),
                    })
                except Exception as exc:
                    self.decode_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            socket.close(linger=0)


def event_summary(records: list[dict[str, Any]], errors: list[str]) -> dict[str, Any]:
    stored: list[str] = []
    removed: list[str] = []
    stored_messages = 0
    removed_messages = 0
    for record in records:
        try:
            events = record["decoded"][1]
        except (KeyError, IndexError, TypeError):
            continue
        for event in events:
            if not isinstance(event, list) or len(event) < 2:
                continue
            if event[0] == "BlockStored":
                stored_messages += 1
                stored.extend(str(value) for value in event[1][0])
            elif event[0] == "BlockRemoved":
                removed_messages += 1
                removed.extend(str(value) for value in event[1][0])
    stored_set = set(stored)
    occupancy: set[str] = set()
    peak = 0
    for record in sorted(records, key=lambda item: item["sequence"]):
        try:
            events = record["decoded"][1]
        except (KeyError, IndexError, TypeError):
            continue
        for event in events:
            if event[0] == "BlockStored":
                occupancy.update(str(value) for value in event[1][0])
                peak = max(peak, len(occupancy))
            elif event[0] == "BlockRemoved":
                occupancy.difference_update(str(value) for value in event[1][0])
    return {
        "batch_count": len(records),
        "decode_errors": errors,
        "block_stored_message_count": stored_messages,
        "block_removed_message_count": removed_messages,
        "stored_block_occurrence_count": len(stored),
        "stored_unique_block_count": len(stored_set),
        "removed_block_occurrence_count": len(removed),
        "removed_unique_block_count": len(set(removed)),
        "removed_hashes_subset_of_stored": set(removed).issubset(stored_set),
        "reconstructed_peak_host_block_count": peak,
        "reconstructed_terminal_host_block_count": len(occupancy),
    }


async def collect_request(engine: Any, prompt_ids: list[int], sampling: Any, request_id: str) -> dict[str, Any]:
    submitted = time.monotonic_ns()
    first_yield = None
    completed = None
    output_ids: list[int] = []
    finish_reason = None
    error = None
    updates = 0
    try:
        async for output in engine.generate(prompt_ids, sampling, request_id):
            now = time.monotonic_ns()
            updates += 1
            if first_yield is None:
                first_yield = now
            candidate = output.outputs[0]
            output_ids = [int(value) for value in candidate.token_ids]
            finish_reason = candidate.finish_reason
            if output.finished:
                completed = now
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        completed = time.monotonic_ns()
    record = {
        "request_id": request_id,
        "input_token_count": len(prompt_ids),
        "input_ids_sha256": sha256_json(prompt_ids),
        "requested_output_tokens": 32,
        "output_token_count": len(output_ids),
        "output_token_ids": output_ids,
        "output_ids_sha256": sha256_json(output_ids),
        "submitted_monotonic_ns": submitted,
        "first_yield_monotonic_ns": first_yield,
        "completed_monotonic_ns": completed,
        "ttft_ns": None if first_yield is None else first_yield - submitted,
        "completion_latency_ns": None if completed is None else completed - submitted,
        "decode_updates": updates,
        "finish_reason": finish_reason,
        "error": error,
        "censored": completed is None,
    }
    return record


async def execute(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    from transformers import AutoTokenizer
    from vllm import AsyncLLMEngine, SamplingParams
    from vllm.config.kv_events import KVEventsConfig
    from vllm.engine.arg_utils import AsyncEngineArgs

    preflight = dispatch_preflight()
    write_json(run_dir / "dispatch_preflight.json", preflight)
    source_hashes = source_gate()
    tokenizer = AutoTokenizer.from_pretrained(str(args.model_path), local_files_only=True)
    prompts = [prompt_fixture(tokenizer, 16384, slot) for slot in range(8)]
    if len({sha256_json(prompt) for prompt in prompts}) != 8:
        raise RuntimeError("prompt fixtures are not distinct")
    write_json(run_dir / "input_fixture.json", {
        "fixture_id": "phase7_swap_k5_distinct_slot_anchor_v1",
        "distinct_prompt_sequences": True,
        "prompt_token_ids_list": prompts,
        "prompt_sha256": [sha256_json(prompt) for prompt in prompts],
    })
    write_json(run_dir / "source_gate.json", {"status": "PASS", "hashes": source_hashes})

    event_collector = KVEventCollector("tcp://127.0.0.1:5625")
    telemetry = TelemetryCollector(run_dir / "telemetry.jsonl")
    event_collector.start()
    telemetry.start()
    engine = None
    records: list[dict[str, Any]] = []
    try:
        event_config = KVEventsConfig(
            enable_kv_cache_events=True,
            publisher="zmq",
            endpoint="tcp://*:5625",
            buffer_steps=10000,
            hwm=100000,
            max_queue_size=100000,
        )
        engine_args = AsyncEngineArgs(
            model=str(args.model_path), dtype="bfloat16", load_format="safetensors",
            safetensors_prefetch_num_threads=1, max_model_len=32768,
            max_num_seqs=8, max_num_batched_tokens=4096,
            gpu_memory_utilization=0.97, enforce_eager=True,
            enable_prefix_caching=False, enable_chunked_prefill=True,
            disable_log_stats=False, kv_cache_metrics=True,
            enable_logging_iteration_details=True, enable_log_requests=True,
            kv_offloading_backend="native", kv_offloading_size=0.25,
            kv_events_config=event_config,
        )
        sampling_config = {
            "n": 1, "temperature": 0.0, "top_p": 1.0, "top_k": 0,
            "seed": 0, "stop": [], "ignore_eos": True,
            "min_tokens": 32, "max_tokens": 32,
            "detokenize": True, "skip_special_tokens": False,
        }
        write_json(run_dir / "requested_engine_args.json", {
            "engine_args": str(engine_args),
            "sampling_params": sampling_config,
            "kv_offloading_backend": "native",
            "kv_offloading_size_gib": 0.25,
            "kv_event_endpoint": "tcp://*:5625",
        })
        engine = AsyncLLMEngine.from_engine_args(engine_args)
        sampling = SamplingParams(**sampling_config)
        admission_ns = time.monotonic_ns()
        write_json(run_dir / "admission.json", {
            "mode": "CLOSED_LOOP_SIMULTANEOUS_BURST",
            "request_ids": [f"swap-k5-{slot:02d}" for slot in range(8)],
            "admission_monotonic_ns": admission_ns,
        })
        tasks = [asyncio.create_task(collect_request(
            engine, prompts[slot], sampling, f"swap-k5-{slot:02d}"
        )) for slot in range(8)]
        records = list(await asyncio.gather(*tasks))
    finally:
        if engine is not None:
            shutdown_result = engine.shutdown()
            if hasattr(shutdown_result, "__await__"):
                await shutdown_result
        event_collector.stop()
        telemetry.stop()

    summary = event_summary(event_collector.records, event_collector.decode_errors)
    write_json(run_dir / "kv_events.json", {
        "endpoint": "tcp://127.0.0.1:5625",
        "events": event_collector.records,
        **summary,
    })
    write_json(run_dir / "requests.json", {"records": records})
    failed = sum(record["error"] is not None for record in records)
    censored = sum(bool(record["censored"]) for record in records)
    rejected = sum(
        record["error"] is not None and "reject" in record["error"].lower()
        for record in records
    )
    if summary["block_removed_message_count"] > 0:
        outcome = "CPU_HOST_POOL_LRU_EVICTION_OBSERVED"
    elif failed or censored:
        outcome = "REQUEST_FAILURE_OR_CENSORING_OBSERVED"
    else:
        outcome = "NO_EXHAUSTION_RESPONSE_OBSERVED"
    return {
        "schema_version": "phase7-swap-k5-result-v1",
        "execution_state": "EXECUTION_COMPLETE",
        "scientific_outcome": outcome,
        "requested_request_count": 8,
        "completed_record_count": len(records),
        "passed_request_count": 8 - failed - censored,
        "failed_request_count": failed,
        "rejected_request_count": rejected,
        "censored_request_count": censored,
        "denominator_preserved": len(records) == 8,
        "records": records,
        "kv_event_summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--experiment-id", default="SWAP-K5")
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text())
    if contract.get("contract_state") != "FROZEN_BEFORE_EXECUTION":
        raise SystemExit("K5 contract is not frozen")
    run_dir = args.run_root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}__{args.experiment_id}"
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "exact_argv.json", {"argv": sys.argv, "cwd": os.getcwd()})
    (run_dir / "k5_contract.json").write_bytes(args.contract.read_bytes())
    write_json(run_dir / "manifest.json", {
        "schema_version": "phase7-swap-k5-manifest-v1",
        "status": "RUNNING",
        "experiment_id": args.experiment_id,
        "runtime_class": "SERVING_VARIANT_DIAGNOSTIC",
        "model_path": str(args.model_path),
        "model_revision": MODEL_REVISION,
        "contract_sha256": sha256_file(args.contract),
        "started_at_utc": utc_now(),
    })
    try:
        result = asyncio.run(execute(args, run_dir))
        write_json(run_dir / "result.json", result)
        write_json(run_dir / "status.json", {
            "status": "PASS",
            "execution_state": "EXECUTION_COMPLETE",
            "scientific_outcome": result["scientific_outcome"],
            "run_dir": str(run_dir),
            "finished_at_utc": utc_now(),
        })
        print(json.dumps({"status": "PASS", "run_dir": str(run_dir), "scientific_outcome": result["scientific_outcome"]}))
        return 0
    except Exception as exc:
        write_json(run_dir / "status.json", {
            "status": "INVALID_TECHNICAL_FAILURE",
            "execution_state": "EXECUTION_INCOMPLETE",
            "error": f"{type(exc).__name__}: {exc}",
            "run_dir": str(run_dir),
            "finished_at_utc": utc_now(),
        })
        raise


if __name__ == "__main__":
    raise SystemExit(main())
