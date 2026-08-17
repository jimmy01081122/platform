#!/usr/bin/env python3
"""Independent Mixtral BF16 GPU campaign runner.

This file deliberately has no dependency on the legacy Granite/C1 package or on
the CPU/mock Phase 7 adapters.  Each invocation creates one fresh, immutable
run directory.  A failure is written before the exception is re-raised, so the
calling shell can stop the affected gate without losing partial evidence.

The runner is intended to be copied into the approved GPU environment and run
with the model already materialized under /vault.  It records the actual vLLM
constructor signature/configuration rather than silently dropping unsupported
arguments.  Routing is collected through vLLM 0.23's
CompletionOutput.routed_experts field when the routing runtime class is used;
there is no synthetic routing fallback.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import hashlib
import importlib.metadata
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Iterable


MODEL_ID = "mistralai/Mixtral-8x7B-Instruct-v0.1"
MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
EXPECTED_VLLM_VERSION = "0.23.0"
MAX_MODEL_LEN = 32768
DEFAULT_MAX_BATCHED_TOKENS = 256
DEFAULT_GPU_MEMORY_UTILIZATION = 0.97
SAMPLING = {
    "temperature": 0.0,
    "top_p": 1.0,
    "top_k": 0,
    "seed": 0,
    "ignore_eos": True,
    "stop": [],
}
RUNTIME_CLASSES = {
    "CLEAN",
    "TELEMETRY",
    "ROUTING",
    "KERNEL_PROFILE",
    "MEMORY_PROFILE",
    "SERVING_VARIANT",
}


class CampaignFailure(RuntimeError):
    """A technical failure that must invalidate the affected experiment."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def compact_utc() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_safe(value: Any, depth: int = 0) -> Any:
    """Convert runtime objects to bounded JSON without hiding their type."""

    if depth > 8:
        return {"type": type(value).__name__, "truncated": True, "repr": repr(value)[:1000]}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"type": "bytes", "sha256": sha256_bytes(value), "bytes": len(value)}
    if dataclasses.is_dataclass(value):
        return json_safe(dataclasses.asdict(value), depth + 1)
    if isinstance(value, dict):
        return {str(k): json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v, depth + 1) for v in value]
    # numpy arrays are used by routed_experts.  They are handled separately
    # before request records are serialized; this branch is for metadata only.
    if hasattr(value, "tolist") and hasattr(value, "shape"):
        try:
            return {
                "type": type(value).__name__,
                "shape": list(value.shape),
                "dtype": str(value.dtype),
            }
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return {
                "type": type(value).__name__,
                "attributes": json_safe(vars(value), depth + 1),
            }
        except Exception:
            pass
    return {"type": type(value).__name__, "repr": repr(value)[:2000]}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def run_command(argv: list[str], timeout: float = 30.0) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_ns": time.monotonic_ns() - started,
        }
    except Exception as exc:
        return {
            "argv": argv,
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "duration_ns": time.monotonic_ns() - started,
        }


def nvidia_smi_snapshot() -> dict[str, Any]:
    query = (
        "timestamp,index,uuid,name,driver_version,memory.total,memory.used,memory.free,"
        "utilization.gpu,utilization.memory,power.draw,clocks.gr,clocks.mem,temperature.gpu"
    )
    return run_command(
        ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"], timeout=20
    )


def parse_smi_line(raw_line: str) -> dict[str, Any]:
    fields = [part.strip() for part in raw_line.strip().split(",")]
    names = [
        "timestamp",
        "index",
        "uuid",
        "name",
        "driver_version",
        "memory_total_mib",
        "memory_used_mib",
        "memory_free_mib",
        "gpu_utilization_pct",
        "memory_utilization_pct",
        "power_draw_w",
        "graphics_clock_mhz",
        "memory_clock_mhz",
        "temperature_c",
    ]
    row: dict[str, Any] = {"raw": raw_line}
    for index, name in enumerate(names):
        value = fields[index] if index < len(fields) else ""
        if name in {"timestamp", "uuid", "name", "driver_version"}:
            row[name] = value
        else:
            try:
                row[name] = float(value) if "." in value else int(value)
            except (TypeError, ValueError):
                row[name] = None if value in {"", "N/A", "[Not Supported]"} else value
    return row


class TelemetrySampler:
    """Low-interference nvidia-smi sampler; the raw line is retained."""

    def __init__(self, output_path: Path, interval_seconds: float = 0.25) -> None:
        self.output_path = output_path
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sample_count = 0
        self.errors: list[dict[str, Any]] = []
        self.started_monotonic_ns: int | None = None
        self.ended_monotonic_ns: int | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("telemetry sampler already started")
        self.started_monotonic_ns = time.monotonic_ns()
        self._thread = threading.Thread(target=self._loop, name="phase7-nvidia-smi", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        query = (
            "timestamp,index,uuid,name,driver_version,memory.total,memory.used,memory.free,"
            "utilization.gpu,utilization.memory,power.draw,clocks.gr,clocks.mem,temperature.gpu"
        )
        while not self._stop.is_set():
            sample_monotonic_ns = time.monotonic_ns()
            result = run_command(
                ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"], timeout=10
            )
            if result["returncode"] == 0:
                lines = [line for line in result["stdout"].splitlines() if line.strip()]
                for line in lines:
                    append_jsonl(
                        self.output_path,
                        {
                            "sample_monotonic_ns": sample_monotonic_ns,
                            "sample_wall_time_ns": time.time_ns(),
                            "query_duration_ns": result["duration_ns"],
                            "gpu": parse_smi_line(line),
                        },
                    )
                    self.sample_count += 1
            else:
                error = {
                    "sample_monotonic_ns": sample_monotonic_ns,
                    "returncode": result["returncode"],
                    "stderr": result["stderr"],
                }
                self.errors.append(error)
                append_jsonl(self.output_path, {"telemetry_error": error})
            self._stop.wait(self.interval_seconds)
        self.ended_monotonic_ns = time.monotonic_ns()

    def stop(self) -> dict[str, Any]:
        if self._thread is None:
            return {"started": False, "sample_count": 0, "errors": []}
        self._stop.set()
        self._thread.join(timeout=15)
        self.ended_monotonic_ns = self.ended_monotonic_ns or time.monotonic_ns()
        return {
            "started": True,
            "sample_count": self.sample_count,
            "errors": self.errors,
            "started_monotonic_ns": self.started_monotonic_ns,
            "ended_monotonic_ns": self.ended_monotonic_ns,
            "duration_ns": (self.ended_monotonic_ns - self.started_monotonic_ns)
            if self.started_monotonic_ns is not None
            else None,
            "interval_seconds": self.interval_seconds,
        }


def capture_torch_memory(torch_module: Any, label: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "label": label,
        "wall_time_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
    }
    try:
        row["cuda_available"] = bool(torch_module.cuda.is_available())
        if not row["cuda_available"]:
            return row
        row["device_count"] = int(torch_module.cuda.device_count())
        row["device"] = int(torch_module.cuda.current_device())
        row["memory_allocated_bytes"] = int(torch_module.cuda.memory_allocated())
        row["memory_reserved_bytes"] = int(torch_module.cuda.memory_reserved())
        row["max_memory_allocated_bytes"] = int(torch_module.cuda.max_memory_allocated())
        row["max_memory_reserved_bytes"] = int(torch_module.cuda.max_memory_reserved())
        free_bytes, total_bytes = torch_module.cuda.mem_get_info()
        row["cuda_free_bytes"] = int(free_bytes)
        row["cuda_total_bytes"] = int(total_bytes)
    except Exception as exc:
        row["error"] = {"type": type(exc).__name__, "message": str(exc)}
    return row


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception as exc:
        return f"ERROR:{type(exc).__name__}:{exc}"


def environment_record() -> dict[str, Any]:
    result = {
        "captured_at_utc": utc_now(),
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "executable": sys.executable,
        "python_version": sys.version,
        "platform": platform.platform(),
        "uname": json_safe(platform.uname()),
        "env_selected": {
            key: os.environ.get(key)
            for key in sorted(os.environ)
            if key.startswith(("CUDA", "NVIDIA", "VLLM", "TORCH", "HF_", "TRANSFORMERS"))
        },
        "packages": {
            name: package_version(name)
            for name in ("vllm", "torch", "transformers", "numpy", "safetensors")
        },
        "nvidia_smi": nvidia_smi_snapshot(),
    }
    try:
        import torch

        result["torch"] = {
            "version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "cuda_available": bool(torch.cuda.is_available()),
            "device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
        }
        if torch.cuda.is_available():
            result["torch"]["device_properties"] = json_safe(
                torch.cuda.get_device_properties(torch.cuda.current_device())
            )
    except Exception as exc:
        result["torch_error"] = {"type": type(exc).__name__, "message": str(exc)}
    return result


def model_record(model_path: Path, vault_hash_file: Path | None = None) -> dict[str, Any]:
    if not model_path.is_dir():
        raise CampaignFailure(f"model path does not exist or is not a directory: {model_path}")
    config_path = model_path / "config.json"
    if not config_path.is_file():
        raise CampaignFailure(f"model config is missing: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    required = {
        "model_type": "mixtral",
        "torch_dtype": "bfloat16",
        "num_hidden_layers": 32,
        "num_local_experts": 8,
        "num_experts_per_tok": 2,
        "max_position_embeddings": 32768,
    }
    mismatches = {
        key: {"expected": expected, "actual": config.get(key)}
        for key, expected in required.items()
        if config.get(key) != expected
    }
    if mismatches:
        raise CampaignFailure(f"Mixtral config identity mismatch: {mismatches}")
    files = []
    for path in sorted(model_path.iterdir()):
        if path.is_file():
            files.append({"name": path.name, "bytes": path.stat().st_size})
    shards = [item for item in files if item["name"].endswith(".safetensors")]
    if len(shards) != 19:
        raise CampaignFailure(f"expected 19 safetensor shards, found {len(shards)}")
    record: dict[str, Any] = {
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_path": str(model_path),
        "config": config,
        "config_sha256": sha256_file(config_path),
        "files": files,
        "safetensor_shard_count": len(shards),
        "safetensor_bytes": sum(item["bytes"] for item in shards),
        "vault_hash_file": str(vault_hash_file) if vault_hash_file else None,
    }
    if vault_hash_file is not None:
        record["vault_hash_status"] = (
            vault_hash_file.with_suffix(vault_hash_file.suffix + ".status").read_text(encoding="utf-8")
            if vault_hash_file.with_suffix(vault_hash_file.suffix + ".status").is_file()
            else "MISSING_STATUS"
        )
        record["vault_hash_manifest_present"] = vault_hash_file.is_file()
        if vault_hash_file.is_file():
            record["vault_hash_manifest_sha256"] = sha256_file(vault_hash_file)
            record["vault_hash_manifest_text"] = vault_hash_file.read_text(encoding="utf-8")
    return record


def fixture_token_ids(tokenizer: Any, length: int) -> list[int]:
    if length <= 0:
        raise CampaignFailure(f"input token length must be positive, got {length}")
    anchor = (
        "Phase 7 Mixtral GPU measurement fixture. Preserve exact token positions "
        "while exercising prefill and decode execution. "
    )
    ids = list(tokenizer.encode(anchor, add_special_tokens=False))
    special_ids = {getattr(tokenizer, name, None) for name in ("bos_token_id", "eos_token_id", "unk_token_id")}
    ids = [int(token) for token in ids if token not in special_ids]
    if not ids:
        raise CampaignFailure("tokenizer produced no non-special fixture token IDs")
    return [ids[index % len(ids)] for index in range(length)]


def load_text_fixture(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except Exception as exc:
        raise CampaignFailure(f"cannot read UTF-8 prompt fixture {path}: {type(exc).__name__}: {exc}") from exc
    if not text.strip():
        raise CampaignFailure(f"prompt fixture is empty: {path}")
    return text, {
        "source_path": str(path),
        "bytes": len(raw),
        "sha256": sha256_bytes(raw),
        "encoding": "utf-8",
    }


def load_request_plan(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    try:
        raw = path.read_bytes()
        plan = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CampaignFailure(f"cannot read request plan {path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema_version") != "phase7-request-plan-v1":
        raise CampaignFailure("request plan must be a phase7-request-plan-v1 JSON object")
    repeat_count = plan.get("repeat_count")
    if not isinstance(repeat_count, int) or isinstance(repeat_count, bool) or repeat_count < 1:
        raise CampaignFailure("request plan repeat_count must be an integer >= 1")
    requests = plan.get("requests")
    if not isinstance(requests, list) or not requests:
        raise CampaignFailure("request plan requests must be a non-empty list")

    materialized: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for position, request in enumerate(requests, start=1):
        if not isinstance(request, dict):
            raise CampaignFailure(f"request plan entry {position} must be an object")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise CampaignFailure(f"request plan entry {position} has no request_id")
        if request_id in seen_ids:
            raise CampaignFailure(f"duplicate request_id in request plan: {request_id}")
        if any(not (char.isalnum() or char in "-_.") for char in request_id):
            raise CampaignFailure(f"request_id contains unsafe filename characters: {request_id!r}")
        seen_ids.add(request_id)

        output_tokens = request.get("output_tokens")
        if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 1:
            raise CampaignFailure(f"request {request_id} output_tokens must be an integer >= 1")
        source_keys = [key for key in ("prompt_text", "prompt_file", "input_tokens") if request.get(key) is not None]
        if len(source_keys) != 1:
            raise CampaignFailure(
                f"request {request_id} must provide exactly one of prompt_text, prompt_file, or input_tokens"
            )

        prompt_text: str | None = None
        input_tokens: int | None = None
        source_record: dict[str, Any]
        if source_keys[0] == "prompt_text":
            prompt_text = request["prompt_text"]
            if not isinstance(prompt_text, str) or not prompt_text.strip():
                raise CampaignFailure(f"request {request_id} prompt_text must be a non-empty string")
            encoded = prompt_text.encode("utf-8")
            source_record = {
                "request_id": request_id,
                "kind": "inline_prompt_text",
                "bytes": len(encoded),
                "sha256": sha256_bytes(encoded),
            }
        elif source_keys[0] == "prompt_file":
            prompt_file_value = request["prompt_file"]
            if not isinstance(prompt_file_value, str) or not prompt_file_value:
                raise CampaignFailure(f"request {request_id} prompt_file must be a non-empty string")
            prompt_path = Path(prompt_file_value)
            if not prompt_path.is_absolute():
                prompt_path = path.parent / prompt_path
            prompt_text, fixture_record = load_text_fixture(prompt_path)
            expected_sha256 = request.get("prompt_sha256")
            if expected_sha256 is not None and expected_sha256 != fixture_record["sha256"]:
                raise CampaignFailure(
                    f"request {request_id} prompt SHA mismatch: expected {expected_sha256}, got {fixture_record['sha256']}"
                )
            source_record = {"request_id": request_id, "kind": "prompt_file", **fixture_record}
        else:
            input_tokens = request["input_tokens"]
            if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 1:
                raise CampaignFailure(f"request {request_id} input_tokens must be an integer >= 1")
            if input_tokens + output_tokens > MAX_MODEL_LEN:
                raise CampaignFailure(
                    f"request {request_id} exceeds max model length: {input_tokens} + {output_tokens} > {MAX_MODEL_LEN}"
                )
            source_record = {
                "request_id": request_id,
                "kind": "exact_token_fixture",
                "input_tokens": input_tokens,
            }

        materialized.append(
            {
                "request_id": request_id,
                "prompt_text": prompt_text,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "source": source_record,
            }
        )
        source_records.append(source_record)

    attestation = {
        "source_path": str(path),
        "bytes": len(raw),
        "sha256": sha256_bytes(raw),
        "schema_version": plan["schema_version"],
        "plan_id": plan.get("plan_id"),
        "repeat_count": repeat_count,
        "request_count_per_sequence": len(materialized),
        "sources": source_records,
        "plan": plan,
    }
    return plan, materialized, attestation


def request_prompt(prompt_text: str | None, prompt_token_ids: list[int] | None) -> Any:
    if prompt_token_ids is not None:
        return {"prompt_token_ids": prompt_token_ids}
    if prompt_text is None:
        raise CampaignFailure("either prompt_text or prompt_token_ids is required")
    return prompt_text


def sampling_params(vllm_module: Any, output_tokens: int) -> Any:
    if output_tokens <= 0:
        raise CampaignFailure(f"output token count must be positive, got {output_tokens}")
    params = dict(
        n=1,
        temperature=SAMPLING["temperature"],
        top_p=SAMPLING["top_p"],
        top_k=SAMPLING["top_k"],
        seed=SAMPLING["seed"],
        ignore_eos=SAMPLING["ignore_eos"],
        stop=SAMPLING["stop"],
        min_tokens=output_tokens,
        max_tokens=output_tokens,
        detokenize=True,
        skip_special_tokens=False,
    )
    return vllm_module.SamplingParams(**params)


def runtime_constructor_args(runtime_class: str, max_num_seqs: int, max_num_batched_tokens: int, gpu_memory_utilization: float) -> dict[str, Any]:
    if runtime_class not in RUNTIME_CLASSES:
        raise CampaignFailure(f"unsupported runtime class: {runtime_class}")
    args: dict[str, Any] = {
        "model": None,  # filled after model identity is captured
        "dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "enable_expert_parallel": False,
        "enforce_eager": True,
        "max_model_len": MAX_MODEL_LEN,
        "max_num_seqs": max_num_seqs,
        "max_num_batched_tokens": max_num_batched_tokens,
        "gpu_memory_utilization": gpu_memory_utilization,
        "cpu_offload_gb": 0,
        "quantization": None,
        "load_format": "safetensors",
        # The canonical model is on an NFS-backed /vault.  One prefetch thread
        # preserves the checkpoint and runtime semantics while avoiding the
        # multi-reader page-cache wait observed in the first attempt.
        "safetensors_prefetch_num_threads": 1,
        "safetensors_prefetch_block_size": 16777216,
        "seed": 0,
        "enable_prefix_caching": False,
        "trust_remote_code": False,
    }
    if runtime_class == "ROUTING":
        args["enable_return_routed_experts"] = True
    return args


def import_runtime(expected_vllm_version: str) -> tuple[Any, Any, Any]:
    try:
        import torch
        import vllm
        from vllm import LLM, SamplingParams
    except Exception as exc:
        raise CampaignFailure(f"failed to import torch/vllm: {type(exc).__name__}: {exc}") from exc
    actual = getattr(vllm, "__version__", None) or package_version("vllm")
    if expected_vllm_version and actual != expected_vllm_version:
        raise CampaignFailure(f"vLLM version mismatch: expected {expected_vllm_version}, got {actual}")
    return torch, vllm, LLM


def resolved_runtime_record(llm: Any, constructor_args: dict[str, Any], signature: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "constructor_args": constructor_args,
        "llm_constructor_signature": signature,
        "vllm_version": package_version("vllm"),
    }
    for root_name in ("llm_engine", "engine"):
        root = getattr(llm, root_name, None)
        if root is None:
            continue
        record[root_name] = {
            "type": type(root).__name__,
            "attributes": {
                name: json_safe(getattr(root, name))
                for name in ("model_config", "cache_config", "parallel_config", "scheduler_config", "vllm_config")
                if hasattr(root, name)
            },
        }
    return record


def save_memory(torch_module: Any, run_dir: Path, label: str) -> None:
    append_jsonl(run_dir / "memory.jsonl", capture_torch_memory(torch_module, label))


def routed_array(completion: Any) -> Any:
    array = getattr(completion, "routed_experts", None)
    if array is None:
        raise CampaignFailure("routing runtime returned no CompletionOutput.routed_experts")
    try:
        import numpy as np

        converted = np.asarray(array)
    except Exception as exc:
        raise CampaignFailure(f"cannot materialize routed_experts array: {type(exc).__name__}: {exc}") from exc
    if converted.size == 0:
        raise CampaignFailure("routing runtime returned an empty routed_experts array")
    return converted


def save_routing(run_dir: Path, logical_id: str, completion: Any, input_count: int, output_count: int, model_config: dict[str, Any]) -> dict[str, Any]:
    array = routed_array(completion)
    expected_layers = int(model_config["num_hidden_layers"])
    expected_experts = int(model_config["num_local_experts"])
    expected_top_k = int(model_config["num_experts_per_tok"])
    route_dir = run_dir / "routing"
    route_dir.mkdir(exist_ok=True)
    array_path = route_dir / f"{logical_id}.npy"
    import numpy as np

    # Preserve the raw runtime payload before validating its semantics.  A
    # schema mismatch is itself useful evidence and must not erase the only
    # artifact needed to diagnose a vLLM routing API change.
    np.save(array_path, array, allow_pickle=False)
    minimum = int(array.min())
    maximum = int(array.max())
    # vLLM returns routing only for tokens that have gone through a
    # forward pass.  For autoregressive generation the final sampled token
    # is returned to the caller but is not forwarded again, so the expected
    # coverage is prompt + completion - 1.  This is the contract documented
    # by RoutedExpertsManager.get() in vLLM 0.23.0.
    expected_forwarded_tokens = input_count + output_count - 1
    validation_errors: list[str] = []
    if array.ndim != 3:
        validation_errors.append(
            f"routing array must be rank-3 [tokens,layers,topk], got shape {array.shape}"
        )
    elif int(array.shape[1]) != expected_layers or int(array.shape[2]) != expected_top_k:
        validation_errors.append(
            "routing dimensions disagree with materialized model config: "
            f"shape={tuple(array.shape)}, expected_layers={expected_layers}, expected_top_k={expected_top_k}"
        )
    if array.ndim >= 1 and int(array.shape[0]) != expected_forwarded_tokens:
        validation_errors.append(
            f"routing forward-token coverage mismatch: got {array.shape[0]}, "
            f"expected {expected_forwarded_tokens} (= input + output - 1) with prefix caching disabled"
        )
    if minimum < 0 or maximum >= expected_experts:
        validation_errors.append(
            f"routing expert ID out of range: min={minimum}, max={maximum}, experts={expected_experts}"
        )
    metadata = {
        "logical_request_id": logical_id,
        "source_field": "vllm.CompletionOutput.routed_experts",
        "semantics": (
            "per token that completed a model forward pass, actual model layer, top-k expert IDs; "
            "the final sampled output token is excluded because it is not forwarded again"
        ),
        "shape": [int(x) for x in array.shape],
        "dtype": str(array.dtype),
        "input_token_count": input_count,
        "output_token_count": output_count,
        "expected_forwarded_token_count": expected_forwarded_tokens,
        "excluded_trailing_sampled_token_count": 1,
        "layers_from_model_config": expected_layers,
        "experts_from_model_config": expected_experts,
        "top_k_from_model_config": expected_top_k,
        "minimum_expert_id": minimum,
        "maximum_expert_id": maximum,
        "array_path": str(array_path.relative_to(run_dir)),
        "array_sha256": sha256_file(array_path),
        "validation_status": "PASS" if not validation_errors else "FAIL",
        "validation_errors": validation_errors,
    }
    write_json(route_dir / f"{logical_id}.json", metadata)
    if validation_errors:
        raise CampaignFailure("; ".join(validation_errors))
    return metadata


def profiler_run(torch_module: Any, invoke: Any, trace_path: Path, table_path: Path) -> tuple[Any, dict[str, Any]]:
    try:
        activities = [torch_module.profiler.ProfilerActivity.CPU]
        if torch_module.cuda.is_available():
            activities.append(torch_module.profiler.ProfilerActivity.CUDA)
        with torch_module.profiler.profile(
            activities=activities,
            record_shapes=False,
            profile_memory=True,
            with_stack=False,
        ) as profile:
            value = invoke()
        profile.export_chrome_trace(str(trace_path))
        table = profile.key_averages().table(sort_by="self_cpu_time_total", row_limit=200)
        table_path.write_text(table, encoding="utf-8")
        event_count = len(profile.key_averages())
        if event_count <= 0:
            raise CampaignFailure("PyTorch profiler returned no events")
        return value, {
            "method": "torch.profiler",
            "activities": [str(x) for x in activities],
            "event_count": event_count,
            "trace_path": str(trace_path.name),
            "table_path": str(table_path.name),
        }
    except Exception as exc:
        write_json(
            trace_path.with_suffix(".failure.json"),
            {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()},
        )
        raise CampaignFailure(f"profiler failed: {type(exc).__name__}: {exc}") from exc


def worker_profiler_run(llm: Any, invoke: Any, run_dir: Path, logical_id: str) -> tuple[Any, dict[str, Any]]:
    trace_dir = run_dir / "profiler" / "worker"
    trace_dir.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in trace_dir.rglob("*") if path.is_file()}
    started = False
    request_failed = False
    try:
        llm.start_profile(logical_id)
        started = True
        value = invoke()
    except Exception as exc:
        request_failed = True
        write_json(
            run_dir / "profiler" / f"{logical_id}.worker.failure.json",
            {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()},
        )
        raise CampaignFailure(f"worker profiler request failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if started:
            try:
                llm.stop_profile()
            except Exception as exc:
                write_json(
                    run_dir / "profiler" / f"{logical_id}.worker.stop_failure.json",
                    {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()},
                )
                if not request_failed:
                    raise CampaignFailure(f"cannot stop worker profiler: {type(exc).__name__}: {exc}") from exc

    files = sorted(path for path in trace_dir.rglob("*") if path.is_file() and path.resolve() not in before)
    trace_files = [path for path in files if path.name.endswith((".json", ".json.gz"))]
    if not trace_files:
        raise CampaignFailure("vLLM worker profiler produced no JSON trace")
    total_trace_events = 0
    kernel_events = 0
    model_kernel_events = 0
    cuda_related_events = 0
    copy_events = 0
    copy_bytes_by_direction: dict[str, int] = {}
    copy_event_count_by_direction: dict[str, int] = {}
    stream_ids: set[int] = set()
    correlation_event_count = 0
    prefill_markers = 0
    decode_markers = 0
    attention_markers = 0
    moe_markers = 0
    trace_records = []
    phase_pattern = re.compile(r"execute_context_(\d+)\((\d+)\)_generation_(\d+)\((\d+)\)")
    model_kernel_markers = (
        "vllm::",
        "flash_fwd",
        "flashattn",
        "paged_attention",
        "rotary_embedding",
        "reshape_and_cache",
        "fused_moe",
        "moefcgemm",
        "cublaslt",
        "cutlass",
    )
    for path in trace_files:
        try:
            if path.name.endswith(".gz"):
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    trace = json.load(handle)
            else:
                with path.open("r", encoding="utf-8") as handle:
                    trace = json.load(handle)
        except Exception as exc:
            raise CampaignFailure(f"cannot parse worker profiler trace {path}: {type(exc).__name__}: {exc}") from exc
        events = trace.get("traceEvents", []) if isinstance(trace, dict) else []
        if not isinstance(events, list):
            raise CampaignFailure(f"worker profiler traceEvents is not a list: {path}")
        trace_kernel_events = 0
        trace_model_kernel_events = 0
        trace_cuda_events = 0
        # gpu_user_annotation is the device-timeline projection of the same
        # user annotation.  Prefer it so phase counts are not double-counted.
        has_gpu_phase_annotations = any(
            isinstance(event, dict)
            and str(event.get("cat", "")).lower() == "gpu_user_annotation"
            and phase_pattern.fullmatch(str(event.get("name", "")))
            for event in events
        )
        for event in events:
            if not isinstance(event, dict):
                continue
            category = str(event.get("cat", "")).lower()
            name = str(event.get("name", "")).lower()
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            if category == "kernel":
                trace_kernel_events += 1
                if any(marker in name for marker in model_kernel_markers):
                    trace_model_kernel_events += 1
            if any(marker in category or marker in name for marker in ("cuda", "kernel", "gpu_memcpy", "gpu_memset")):
                trace_cuda_events += 1
            if category == "gpu_memcpy":
                direction = str(event.get("name", "UNKNOWN"))
                event_bytes = int(args.get("bytes", 0) or 0)
                copy_event_count_by_direction[direction] = copy_event_count_by_direction.get(direction, 0) + 1
                copy_bytes_by_direction[direction] = copy_bytes_by_direction.get(direction, 0) + event_bytes
                copy_events += 1
            if isinstance(args.get("stream"), int):
                stream_ids.add(int(args["stream"]))
            if args.get("correlation") is not None:
                correlation_event_count += 1
            if category in ({"gpu_user_annotation"} if has_gpu_phase_annotations else {"user_annotation"}):
                match = phase_pattern.fullmatch(str(event.get("name", "")))
                if match:
                    context_sequences, context_tokens, generation_sequences, generation_tokens = (
                        int(value) for value in match.groups()
                    )
                    if context_sequences > 0 or context_tokens > 0:
                        prefill_markers += 1
                    if generation_sequences > 0 or generation_tokens > 0:
                        decode_markers += 1
            if category in {"cpu_op", "user_annotation", "gpu_user_annotation", "kernel"}:
                if any(marker in name for marker in ("attention", "flash_fwd", "flashattn", "paged_attention")):
                    attention_markers += 1
                if any(marker in name for marker in ("moe_forward", "fused_moe", "moefcgemm", "topkgating")):
                    moe_markers += 1
        total_trace_events += len(events)
        kernel_events += trace_kernel_events
        model_kernel_events += trace_model_kernel_events
        cuda_related_events += trace_cuda_events
        trace_records.append(
            {
                "path": str(path.relative_to(run_dir)),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "trace_event_count": len(events),
                "kernel_event_count": trace_kernel_events,
                "model_kernel_event_count": trace_model_kernel_events,
                "cuda_related_event_count": trace_cuda_events,
            }
        )
    if kernel_events <= 0:
        raise CampaignFailure(
            f"vLLM worker profiler trace has no model kernel events: files={len(trace_files)}, events={total_trace_events}"
        )
    if model_kernel_events <= 0:
        raise CampaignFailure("vLLM worker profiler trace has GPU kernels but no recognizable model kernel")
    if prefill_markers <= 0 or decode_markers <= 0:
        raise CampaignFailure(
            "vLLM worker profiler cannot separate prefill/decode: "
            f"prefill_markers={prefill_markers}, decode_markers={decode_markers}"
        )
    other_files = [
        {"path": str(path.relative_to(run_dir)), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in files
        if path not in trace_files
    ]
    return value, {
        "method": "vllm.EngineCore worker torch.profiler",
        "profile_prefix": logical_id,
        "trace_files": trace_records,
        "other_files": other_files,
        "trace_event_count": total_trace_events,
        "kernel_event_count": kernel_events,
        "model_kernel_event_count": model_kernel_events,
        "cuda_related_event_count": cuda_related_events,
        "copy_event_count": copy_events,
        "copy_event_count_by_direction": copy_event_count_by_direction,
        "copy_bytes_by_direction": copy_bytes_by_direction,
        "stream_ids": sorted(stream_ids),
        "correlation_event_count": correlation_event_count,
        "prefill_marker_count": prefill_markers,
        "decode_marker_count": decode_markers,
        "attention_marker_count": attention_markers,
        "moe_marker_count": moe_markers,
        "trace_timestamp_unit": "microseconds (PyTorch Chrome trace ts/dur)",
        "model_correlation": "PASS_PREFILL_DECODE_SEPARABLE",
        "validation_status": "PASS",
    }


def output_metrics(output: Any) -> Any:
    return json_safe(getattr(output, "metrics", None))


def run_one_request(
    llm: Any,
    torch_module: Any,
    vllm_module: Any,
    run_dir: Path,
    runtime_class: str,
    logical_id: str,
    prompt_text: str | None,
    prompt_token_ids: list[int] | None,
    output_tokens: int,
    model_config: dict[str, Any],
    exact_input_tokens: int | None,
    exact_output_tokens: int | None,
    repetition_role: str | None = None,
    repetition_index: int | None = None,
    sequence_index: int | None = None,
    sequence_position: int | None = None,
    plan_request_id: str | None = None,
) -> dict[str, Any]:
    if prompt_token_ids is not None:
        input_count = len(prompt_token_ids)
    else:
        input_count = None
    sampler = (
        TelemetrySampler(run_dir / "telemetry.jsonl")
        if runtime_class in {"TELEMETRY", "MEMORY_PROFILE"}
        else None
    )
    save_memory(torch_module, run_dir, f"{logical_id}:before")
    if sampler is not None:
        sampler.start()
    params = sampling_params(vllm_module, output_tokens)
    prompt = request_prompt(prompt_text, prompt_token_ids)
    started_wall_ns = time.time_ns()
    started_monotonic_ns = time.monotonic_ns()
    profiler_info: dict[str, Any] | None = None
    try:
        def invoke() -> Any:
            return llm.generate([prompt], sampling_params=params, use_tqdm=False)

        if runtime_class == "KERNEL_PROFILE":
            results, profiler_info = worker_profiler_run(llm, invoke, run_dir, logical_id)
        else:
            results = invoke()
    finally:
        ended_monotonic_ns = time.monotonic_ns()
        ended_wall_ns = time.time_ns()
        telemetry_info = sampler.stop() if sampler is not None else None
        save_memory(torch_module, run_dir, f"{logical_id}:after")
    if not isinstance(results, list) or len(results) != 1:
        raise CampaignFailure(f"expected one RequestOutput, got {type(results).__name__} length={len(results) if isinstance(results, list) else 'n/a'}")
    request_output = results[0]
    if not getattr(request_output, "finished", False):
        raise CampaignFailure(f"request did not finish: {logical_id}")
    if not getattr(request_output, "outputs", None):
        raise CampaignFailure(f"request returned no completion output: {logical_id}")
    completion = request_output.outputs[0]
    actual_input_ids = [int(x) for x in (getattr(request_output, "prompt_token_ids", None) or [])]
    actual_output_ids = [int(x) for x in (getattr(completion, "token_ids", None) or [])]
    actual_input_count = len(actual_input_ids)
    actual_output_count = len(actual_output_ids)
    record: dict[str, Any] = {
        "logical_request_id": logical_id,
        "repetition_role": repetition_role,
        "repetition_index": repetition_index,
        "sequence_index": sequence_index,
        "sequence_position": sequence_position,
        "plan_request_id": plan_request_id,
        "engine_request_id": str(getattr(request_output, "request_id", "")),
        "runtime_class": runtime_class,
        "started_wall_time_ns": started_wall_ns,
        "ended_wall_time_ns": ended_wall_ns,
        "started_monotonic_ns": started_monotonic_ns,
        "ended_monotonic_ns": ended_monotonic_ns,
        "wall_duration_ns": ended_monotonic_ns - started_monotonic_ns,
        "prompt_text": prompt_text,
        "input_token_ids": actual_input_ids,
        "input_token_count": actual_input_count,
        "output_text": str(getattr(completion, "text", "")),
        "output_token_ids": actual_output_ids,
        "output_token_count": actual_output_count,
        "finish_reason": getattr(completion, "finish_reason", None),
        "stop_reason": json_safe(getattr(completion, "stop_reason", None)),
        "metrics": output_metrics(request_output),
        "num_cached_tokens": json_safe(getattr(request_output, "num_cached_tokens", None)),
        "telemetry": telemetry_info,
        "profiler": profiler_info,
        "sampling": {**SAMPLING, "min_tokens": output_tokens, "max_tokens": output_tokens},
    }
    if exact_input_tokens is not None and actual_input_count != exact_input_tokens:
        raise CampaignFailure(
            f"exact input token mismatch for {logical_id}: expected {exact_input_tokens}, got {actual_input_count}"
        )
    if exact_output_tokens is not None:
        if actual_output_count != exact_output_tokens:
            raise CampaignFailure(
                f"exact output token mismatch for {logical_id}: expected {exact_output_tokens}, got {actual_output_count}"
            )
        if getattr(completion, "finish_reason", None) != "length":
            raise CampaignFailure(
                f"forced-length request did not stop by length for {logical_id}: "
                f"{getattr(completion, 'finish_reason', None)!r}"
            )
    if runtime_class == "ROUTING":
        if input_count is None:
            input_count = actual_input_count
        try:
            routing = save_routing(run_dir, logical_id, completion, input_count, actual_output_count, model_config)
            record["routing"] = routing
        except CampaignFailure as exc:
            record["routing_validation"] = {"status": "FAIL", "message": str(exc)}
            append_jsonl(run_dir / "requests.jsonl", record)
            write_json(run_dir / "routing_validation_failure_request.json", record)
            raise
    append_jsonl(run_dir / "requests.jsonl", record)
    return record


def create_run_dir(root: Path, experiment_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in experiment_id)
    for suffix in range(1000):
        run_id = f"{compact_utc()}__{slug}" if suffix == 0 else f"{compact_utc()}__{slug}__{suffix:03d}"
        path = root / run_id
        try:
            path.mkdir()
            return path
        except FileExistsError:
            time.sleep(0.01)
    raise CampaignFailure(f"cannot allocate a unique run directory for {experiment_id}")


def finalize_run(run_dir: Path, status: str, extra: dict[str, Any] | None = None) -> None:
    state = {"status": status, "finished_at_utc": utc_now()}
    if extra:
        state.update(extra)
    write_json(run_dir / "status.json", state)
    checksum_path = run_dir / "SHA256SUMS"
    rows = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path == checksum_path:
            continue
        rows.append(f"{sha256_file(path)}  {path.relative_to(run_dir)}")
    checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def prepare_profiler_dir(run_dir: Path) -> None:
    (run_dir / "profiler").mkdir(exist_ok=True)


def load_engine(
    model_path: Path,
    runtime_class: str,
    max_num_seqs: int,
    max_num_batched_tokens: int,
    gpu_memory_utilization: float,
    expected_vllm_version: str,
    run_dir: Path,
    profiler_max_iterations: int,
) -> tuple[Any, Any, Any, dict[str, Any], dict[str, Any]]:
    torch_module, vllm_module, llm_class = import_runtime(expected_vllm_version)
    args = runtime_constructor_args(runtime_class, max_num_seqs, max_num_batched_tokens, gpu_memory_utilization)
    args["model"] = str(model_path)
    if runtime_class == "KERNEL_PROFILE":
        worker_trace_dir = (run_dir / "profiler" / "worker").resolve()
        worker_trace_dir.mkdir(parents=True, exist_ok=True)
        args["profiler_config"] = {
            "profiler": "torch",
            "torch_profiler_dir": str(worker_trace_dir),
            "torch_profiler_with_stack": False,
            "torch_profiler_with_flops": False,
            "torch_profiler_use_gzip": True,
            "torch_profiler_dump_cuda_time_total": True,
            "torch_profiler_record_shapes": False,
            "torch_profiler_with_memory": True,
            "ignore_frontend": True,
            "delay_iterations": 0,
            "max_iterations": profiler_max_iterations,
            "warmup_iterations": 0,
            "active_iterations": 5,
            "wait_iterations": 0,
        }
    llm_signature = inspect.signature(llm_class.__init__)
    # vLLM exposes most EngineArgs through LLM.__init__(..., **kwargs).  Check
    # the actual EngineArgs dataclass as well as the public LLM wrapper; a
    # direct-signature-only check falsely rejected the supported v0.23 API.
    try:
        from vllm.engine.arg_utils import EngineArgs

        engine_signature = inspect.signature(EngineArgs)
        accepted_parameters = set(llm_signature.parameters) | set(engine_signature.parameters)
        signature = {
            "llm_constructor": str(llm_signature),
            "engine_args": str(engine_signature),
        }
    except Exception as exc:
        raise CampaignFailure(f"cannot inspect vLLM EngineArgs: {type(exc).__name__}: {exc}") from exc
    missing = sorted(name for name in args if name != "model" and name not in accepted_parameters)
    if missing:
        raise CampaignFailure(f"runtime API cannot represent frozen arguments; unsupported fields: {missing}")
    # swap_space is intentionally absent.  Passing an old vLLM-only argument
    # would make the runtime contract version-dependent and could mask a setup bug.
    write_json(run_dir / "requested_engine_args.json", args)
    llm = llm_class(**args)
    resolved = resolved_runtime_record(llm, args, signature)
    write_json(run_dir / "resolved_runtime.json", resolved)
    return torch_module, vllm_module, llm, args, resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--runtime-class", choices=sorted(RUNTIME_CLASSES), default="CLEAN")
    parser.add_argument("--prompt-text")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--request-plan", type=Path)
    parser.add_argument("--output-tokens", type=int)
    parser.add_argument("--logical-request-id", default="request-0001")
    parser.add_argument("--warmup-count", type=int, default=0)
    parser.add_argument("--measured-count", type=int, default=1)
    parser.add_argument("--max-num-seqs", type=int, default=1)
    parser.add_argument("--max-num-batched-tokens", type=int, default=DEFAULT_MAX_BATCHED_TOKENS)
    parser.add_argument("--gpu-memory-utilization", type=float, default=DEFAULT_GPU_MEMORY_UTILIZATION)
    parser.add_argument("--expected-vllm-version", default=EXPECTED_VLLM_VERSION)
    parser.add_argument("--profiler-max-iterations", type=int, default=96)
    parser.add_argument("--vault-hash-file", type=Path)
    parser.add_argument("--require-vault-hash", action="store_true")
    parser.add_argument("--allow-unverified-vault-hash", action="store_true")
    parser.add_argument("--no-exact-output", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = create_run_dir(args.run_root, args.experiment_id)
    phase = "initialization"
    try:
        if args.max_num_seqs < 1:
            raise CampaignFailure("max-num-seqs must be >= 1")
        if args.warmup_count < 0 or args.measured_count < 1:
            raise CampaignFailure("warmup-count must be >= 0 and measured-count must be >= 1")
        if args.profiler_max_iterations < 1:
            raise CampaignFailure("profiler-max-iterations must be >= 1")
        if args.runtime_class != "SERVING_VARIANT" and args.max_num_seqs != 1:
            raise CampaignFailure("canonical/non-serving runtime classes require max-num-seqs=1")
        selected_sources = sum(
            source is not None
            for source in (args.input_tokens, args.prompt_text, args.prompt_file, args.request_plan)
        )
        if selected_sources != 1:
            raise CampaignFailure(
                "provide exactly one of --input-tokens, --prompt-text, --prompt-file, or --request-plan"
            )
        if args.request_plan is None and args.output_tokens is None:
            raise CampaignFailure("--output-tokens is required unless --request-plan is used")
        if args.request_plan is not None and args.output_tokens is not None:
            raise CampaignFailure("request-plan output lengths come from the plan; do not pass --output-tokens")
        if args.request_plan is not None and (args.warmup_count != 0 or args.measured_count != 1):
            raise CampaignFailure("request-plan repetitions come from plan.repeat_count; keep warmup=0 and measured=1")
        prompt_file_text = None
        prompt_file_attestation = None
        request_plan = None
        plan_requests = None
        request_plan_attestation = None
        if args.prompt_file is not None:
            prompt_file_text, prompt_file_attestation = load_text_fixture(args.prompt_file)
        if args.request_plan is not None:
            request_plan, plan_requests, request_plan_attestation = load_request_plan(args.request_plan)
        phase = "environment_and_model_identity"
        write_json(run_dir / "environment.json", environment_record())
        hash_status = None
        if args.vault_hash_file is not None:
            status_path = args.vault_hash_file.with_suffix(args.vault_hash_file.suffix + ".status")
            hash_status = status_path.read_text(encoding="utf-8") if status_path.is_file() else "MISSING_STATUS"
            if args.require_vault_hash and "DONE" not in hash_status:
                raise CampaignFailure(f"required full /vault hash is not complete: {hash_status!r}")
            if args.vault_hash_file.is_file():
                write_json(
                    run_dir / "vault_hash_attestation.json",
                    {
                        "source_path": str(args.vault_hash_file),
                        "status_path": str(status_path),
                        "status_text": hash_status,
                        "manifest_sha256": sha256_file(args.vault_hash_file),
                        "manifest_text": args.vault_hash_file.read_text(encoding="utf-8"),
                    },
                )
        model = model_record(args.model_path, args.vault_hash_file)
        write_json(run_dir / "model_identity.json", model)
        if prompt_file_attestation is not None:
            write_json(run_dir / "prompt_file_attestation.json", prompt_file_attestation)
        if request_plan_attestation is not None:
            write_json(run_dir / "request_plan_attestation.json", request_plan_attestation)
        write_json(
            run_dir / "manifest.json",
            {
                "schema_version": "phase7-real-gpu-run-v1",
                "experiment_id": args.experiment_id,
                "created_at_utc": utc_now(),
                "model_id": MODEL_ID,
                "model_revision": MODEL_REVISION,
                "model_path": str(args.model_path),
                "runtime_class": args.runtime_class,
                "sampling": SAMPLING,
                "input_tokens_requested": args.input_tokens,
                "output_tokens_requested": args.output_tokens,
                "prompt_file": prompt_file_attestation,
                "request_plan": (
                    {
                        "source_path": request_plan_attestation["source_path"],
                        "sha256": request_plan_attestation["sha256"],
                        "plan_id": request_plan_attestation["plan_id"],
                        "repeat_count": request_plan_attestation["repeat_count"],
                        "request_count_per_sequence": request_plan_attestation["request_count_per_sequence"],
                    }
                    if request_plan_attestation is not None
                    else None
                ),
                "exact_output_required": not args.no_exact_output,
                "max_num_seqs": args.max_num_seqs,
                "max_num_batched_tokens": args.max_num_batched_tokens,
                "gpu_memory_utilization": args.gpu_memory_utilization,
                "profiler_max_iterations": args.profiler_max_iterations,
                "cpu_offload_gb": 0,
                "quantization": None,
                "swap_space": "not_passed_to_vllm_api",
                "vault_hash_status": hash_status,
            },
        )
        phase = "engine_load"
        prepare_profiler_dir(run_dir)
        torch_module, vllm_module, llm, constructor_args, resolved = load_engine(
            args.model_path,
            args.runtime_class,
            args.max_num_seqs,
            args.max_num_batched_tokens,
            args.gpu_memory_utilization,
            args.expected_vllm_version,
            run_dir,
            args.profiler_max_iterations,
        )
        save_memory(torch_module, run_dir, "post_load")
        postload_smi = nvidia_smi_snapshot()
        write_json(run_dir / "nvidia-smi-post-load.json", postload_smi)
        tokenizer = llm.get_tokenizer()
        if args.request_plan is not None:
            phase = "plan_fixture_materialization"
            assert request_plan is not None
            assert plan_requests is not None
            materialized_plan_requests = []
            fixture_dir = run_dir / "input_fixtures"
            for plan_request in plan_requests:
                if plan_request["input_tokens"] is not None:
                    fixture_dir.mkdir(exist_ok=True)
                    plan_token_ids = fixture_token_ids(tokenizer, plan_request["input_tokens"])
                    fixture_record = {
                        "fixture_id": "phase7_mixtral_repeated_anchor_v1",
                        "plan_request_id": plan_request["request_id"],
                        "anchor": "Phase 7 Mixtral GPU measurement fixture",
                        "tokenizer_type": type(tokenizer).__name__,
                        "token_count": len(plan_token_ids),
                        "token_ids_sha256": sha256_bytes(canonical_json(plan_token_ids).encode("utf-8")),
                        "token_ids": plan_token_ids,
                        "construction": (
                            "repeat tokenizer.encode(anchor, add_special_tokens=False) after removing "
                            "BOS/EOS/UNK until exact length"
                        ),
                    }
                    write_json(fixture_dir / f"{plan_request['request_id']}.json", fixture_record)
                else:
                    plan_token_ids = None
                materialized_plan_requests.append({**plan_request, "prompt_token_ids": plan_token_ids})
            write_json(
                run_dir / "materialized_request_plan.json",
                {
                    "plan_id": request_plan.get("plan_id"),
                    "repeat_count": request_plan["repeat_count"],
                    "request_count_per_sequence": len(materialized_plan_requests),
                    "execution_order": [request["request_id"] for request in materialized_plan_requests],
                    "source_attestations": [request["source"] for request in materialized_plan_requests],
                },
            )
            token_ids = None
            prompt_text = None
        elif args.input_tokens is not None:
            phase = "fixture_materialization"
            token_ids = fixture_token_ids(tokenizer, args.input_tokens)
            write_json(
                run_dir / "input_fixture.json",
                {
                    "fixture_id": "phase7_mixtral_repeated_anchor_v1",
                    "anchor": "Phase 7 Mixtral GPU measurement fixture",
                    "tokenizer_type": type(tokenizer).__name__,
                    "token_count": len(token_ids),
                    "token_ids_sha256": sha256_bytes(canonical_json(token_ids).encode("utf-8")),
                    "token_ids": token_ids,
                    "construction": "repeat tokenizer.encode(anchor, add_special_tokens=False) after removing BOS/EOS/UNK until exact length",
                },
            )
            prompt_text = None
        else:
            token_ids = None
            prompt_text = prompt_file_text if prompt_file_text is not None else args.prompt_text
        phase = "generation"
        records: list[dict[str, Any]] = []
        if args.request_plan is not None:
            assert request_plan is not None
            for sequence_index in range(1, request_plan["repeat_count"] + 1):
                sequence_started_wall_ns = time.time_ns()
                sequence_started_monotonic_ns = time.monotonic_ns()
                append_jsonl(
                    run_dir / "sequence_events.jsonl",
                    {
                        "event": "sequence_start",
                        "sequence_index": sequence_index,
                        "wall_time_ns": sequence_started_wall_ns,
                        "monotonic_ns": sequence_started_monotonic_ns,
                    },
                )
                for sequence_position, plan_request in enumerate(materialized_plan_requests, start=1):
                    logical_id = (
                        f"{plan_request['request_id']}__sequence-{sequence_index:02d}"
                        f"__position-{sequence_position:02d}"
                    )
                    record = run_one_request(
                        llm=llm,
                        torch_module=torch_module,
                        vllm_module=vllm_module,
                        run_dir=run_dir,
                        runtime_class=args.runtime_class,
                        logical_id=logical_id,
                        prompt_text=plan_request["prompt_text"],
                        prompt_token_ids=plan_request["prompt_token_ids"],
                        output_tokens=plan_request["output_tokens"],
                        model_config=model["config"],
                        exact_input_tokens=plan_request["input_tokens"],
                        exact_output_tokens=None if args.no_exact_output else plan_request["output_tokens"],
                        repetition_role="sequence",
                        repetition_index=sequence_index,
                        sequence_index=sequence_index,
                        sequence_position=sequence_position,
                        plan_request_id=plan_request["request_id"],
                    )
                    records.append(record)
                sequence_ended_monotonic_ns = time.monotonic_ns()
                append_jsonl(
                    run_dir / "sequence_events.jsonl",
                    {
                        "event": "sequence_complete",
                        "sequence_index": sequence_index,
                        "wall_time_ns": time.time_ns(),
                        "monotonic_ns": sequence_ended_monotonic_ns,
                        "duration_ns": sequence_ended_monotonic_ns - sequence_started_monotonic_ns,
                        "completed_request_count": len(materialized_plan_requests),
                    },
                )
            write_json(
                run_dir / "result.json",
                {
                    "plan_id": request_plan.get("plan_id"),
                    "repeat_count": request_plan["repeat_count"],
                    "request_count_per_sequence": len(materialized_plan_requests),
                    "total_completed_requests": len(records),
                    "records": records,
                },
            )
        else:
            total_repetitions = args.warmup_count + args.measured_count
            for repetition_index in range(total_repetitions):
                if repetition_index < args.warmup_count:
                    role = "warmup"
                    role_index = repetition_index + 1
                else:
                    role = "measured"
                    role_index = repetition_index - args.warmup_count + 1
                logical_id = f"{args.logical_request_id}__{role}-{role_index:02d}"
                record = run_one_request(
                    llm=llm,
                    torch_module=torch_module,
                    vllm_module=vllm_module,
                    run_dir=run_dir,
                    runtime_class=args.runtime_class,
                    logical_id=logical_id,
                    prompt_text=prompt_text,
                    prompt_token_ids=token_ids,
                    output_tokens=args.output_tokens,
                    model_config=model["config"],
                    exact_input_tokens=args.input_tokens,
                    exact_output_tokens=None if args.no_exact_output else args.output_tokens,
                    repetition_role=role,
                    repetition_index=repetition_index + 1,
                )
                records.append(record)
            if len(records) == 1:
                write_json(run_dir / "result.json", records[0])
            else:
                write_json(
                    run_dir / "result.json",
                    {
                        "warmup_count": args.warmup_count,
                        "measured_count": args.measured_count,
                        "records": records,
                    },
                )
        write_json(run_dir / "nvidia-smi-post-request.json", nvidia_smi_snapshot())
        terminal_extra = {
            "phase": "complete",
            "logical_request_id": args.logical_request_id if args.request_plan is None else None,
            "warmup_count": args.warmup_count if args.request_plan is None else None,
            "measured_count": args.measured_count if args.request_plan is None else None,
            "request_plan_id": request_plan.get("plan_id") if request_plan is not None else None,
            "sequence_count": request_plan["repeat_count"] if request_plan is not None else None,
            "request_count_per_sequence": len(plan_requests) if plan_requests is not None else None,
            "total_completed_requests": len(records),
        }
        finalize_run(
            run_dir,
            "PASS",
            terminal_extra,
        )
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "run_dir": str(run_dir),
                    "completed_request_count": len(records),
                    "request_plan_id": terminal_extra["request_plan_id"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except BaseException as exc:
        failure = {
            "status": "FAIL",
            "phase": phase,
            "failed_at_utc": utc_now(),
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        write_json(run_dir / "failure.json", failure)
        try:
            write_json(run_dir / "nvidia-smi-failure.json", nvidia_smi_snapshot())
        except Exception:
            pass
        finalize_run(run_dir, "FAIL", {"phase": phase, "exception_type": type(exc).__name__})
        print(json.dumps({"status": "FAIL", "run_dir": str(run_dir), "failure": failure}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
