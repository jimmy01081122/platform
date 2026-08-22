"""Auditable vLLM 0.23 worker boundary for the TRACK_GPU probes.

This adapter intentionally stops at the observability boundary provided by
vLLM 0.23.0.  It can create the owner-frozen engine, prove that the sanctioned
``worker_extension_cls``/``collective_rpc`` control path reached the worker,
resolve the effective engine configuration, and preserve the three canonical
startup-log markers.  It does *not* turn parent-process wall time into worker
timing evidence.

Two source-level limitations are currently terminal:

* ``FlashInferExperts.apply`` calls ``flashinfer_cutlass_fused_moe`` once.  The
  canonical kernel therefore exposes no Python boundary between activation
  movement and expert execution from which target_1's ``T_move`` could be
  isolated.
* native ``OffloadingConnectorStats`` reports completed transfer bytes/time and
  is drained/reset by ``get_kv_connector_stats``.  It exposes no auditable
  per-request GPU/CPU resident-byte gauge required by target_2.

The engine-control session remains useful evidence: its runtime identity says
``ENGINE_CONTROL_READY`` and records the exact refused measurement capability.
Calling ``measure_window`` or ``measure`` fails loudly.  There is no mock,
fallback backend, parent timing, or inferred residency path in this module.

Imports of torch and vLLM are deliberately lazy so CPU-only validation can
import and test this module.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping


EXPECTED_VLLM_VERSION = "0.23.0"
EXPECTED_TORCH_VERSION = "2.11.0+cu130"
EXPECTED_MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"

TARGET_1 = "target_1_dispatch"
TARGET_2 = "target_2_longctx_offload_on"
TARGETS = (TARGET_1, TARGET_2)

ENGINE_CONTROL_READY = "ENGINE_CONTROL_READY"
TARGET_1_MEASUREMENT_REFUSED = "TARGET_1_MEASUREMENT_REFUSED"
TARGET_2_MEASUREMENT_REFUSED = "TARGET_2_MEASUREMENT_REFUSED"

WORKER_EXTENSION_CLS = (
    "measurement.probes.vllm_runtime_adapter.TrackGpuWorkerExtension"
)

_BACKEND_MARKER_PATTERNS = {
    "attention_backend": ("Using FLASH_ATTN attention backend",),
    "fused_moe_backend": (
        "Using FlashInfer CUTLASS Unquantized MoE backend",
    ),
    "kernel_backend": (
        "kernel_config=KernelConfig(",
        "enable_flashinfer_autotune=True",
    ),
}

_COMMON_KEYS = {
    "model_path",
    "model_revision",
    "dtype",
    "tensor_parallel_size",
    "pipeline_parallel_size",
    "max_num_seqs",
    "max_model_len",
    "gpu_memory_utilization",
    "enforce_eager",
    "enable_prefix_caching",
    "max_num_batched_tokens",
    "cpu_offload_gb",
    "swap_space_gb",
}
_TARGET_2_KEYS = _COMMON_KEYS | {
    "kv_offloading_size_gb",
    "kv_offloading_backend",
}


class AdapterError(RuntimeError):
    """Raised when live evidence cannot satisfy the frozen contract."""


def _qualname(value: Any) -> str:
    cls = value if isinstance(value, type) else type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _source_evidence(value: Any, required_tokens: tuple[str, ...]) -> dict[str, Any]:
    """Return a small, auditable fingerprint without embedding source text."""

    symbol = (
        f"{value.__module__}.{value.__qualname__}"
        if callable(value) and hasattr(value, "__qualname__")
        else _qualname(value)
    )
    try:
        source = inspect.getsource(value)
        source_file = inspect.getsourcefile(value)
    except (OSError, TypeError) as exc:
        return {
            "symbol": symbol,
            "source_available": False,
            "source_error": f"{type(exc).__name__}: {exc}",
            "required_tokens": list(required_tokens),
            "required_tokens_present": False,
        }
    return {
        "symbol": symbol,
        "source_available": True,
        "source_file": source_file,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "required_tokens": list(required_tokens),
        "required_token_counts": {
            token: source.count(token) for token in required_tokens
        },
        "required_tokens_present": all(token in source for token in required_tokens),
    }


def _worker_model(worker: Any) -> Any:
    runner = getattr(worker, "model_runner", None)
    if runner is None:
        return None
    getter = getattr(runner, "get_model", None)
    if callable(getter):
        return getter()
    return getattr(runner, "model", None)


def _target1_worker_audit(worker: Any) -> dict[str, Any]:
    """Inspect the live MoE objects without installing a misleading hook."""

    expected_backend = (
        "vllm.model_executor.layers.fused_moe.experts."
        "flashinfer_cutlass_moe.FlashInferExperts"
    )
    source: dict[str, Any]
    try:
        from vllm.model_executor.layers.fused_moe.experts.flashinfer_cutlass_moe import (  # type: ignore  # noqa: E501
            FlashInferExperts,
        )

        source = _source_evidence(
            FlashInferExperts.apply, ("flashinfer_cutlass_fused_moe(",)
        )
    except Exception as exc:
        source = {
            "symbol": expected_backend + ".apply",
            "source_available": False,
            "source_error": f"{type(exc).__name__}: {exc}",
            "required_tokens": ["flashinfer_cutlass_fused_moe("],
            "required_tokens_present": False,
        }
    layers: list[dict[str, Any]] = []
    model = _worker_model(worker)
    named_modules = getattr(model, "named_modules", None)
    if callable(named_modules):
        for name, module in named_modules():
            if _qualname(module) != (
                "vllm.model_executor.layers.fused_moe.layer.FusedMoE"
            ):
                continue
            runner = getattr(module, "runner", None)
            quant_method = getattr(runner, "_quant_method", None)
            if quant_method is None:
                quant_method = getattr(module, "quant_method", None)
            kernel = getattr(quant_method, "moe_kernel", None)
            impl = getattr(kernel, "impl", None)
            experts = getattr(impl, "fused_experts", None)
            experts_name = _qualname(experts) if experts is not None else None
            evidence = (
                _source_evidence(
                    type(experts).apply,
                    ("flashinfer_cutlass_fused_moe(",),
                )
                if experts is not None
                and callable(getattr(type(experts), "apply", None))
                else None
            )
            layers.append(
                {
                    "name": name,
                    "layer_class": _qualname(module),
                    "quant_method_class": (
                        _qualname(quant_method) if quant_method is not None else None
                    ),
                    "fused_experts_class": experts_name,
                    "canonical_flashinfer_backend": experts_name == expected_backend,
                    "apply_source": evidence,
                }
            )

    canonical_layers = [row for row in layers if row["canonical_flashinfer_backend"]]
    reasons: list[str] = []
    if not layers:
        reasons.append("no live vLLM FusedMoE layer was discoverable on the worker")
    if layers and len(canonical_layers) != len(layers):
        reasons.append(
            "not every live MoE layer resolves to the owner-required FlashInferExperts"
        )
    fused_call_count = source.get("required_token_counts", {}).get(
        "flashinfer_cutlass_fused_moe("
    )
    if canonical_layers and fused_call_count == 1:
        reasons.append(
            "vllm.model_executor.layers.fused_moe.experts.flashinfer_cutlass_moe."
            "FlashInferExperts.apply invokes flashinfer_cutlass_fused_moe once; "
            "that call fuses permutation, expert execution, and unpermutation, so "
            "worker-side Python hooks cannot isolate T_move from T_execute"
        )
    elif canonical_layers:
        reasons.append(
            "the installed FlashInferExperts.apply source did not prove exactly one "
            "flashinfer_cutlass_fused_moe call; instrumentation is refused"
        )

    return {
        "status": TARGET_1_MEASUREMENT_REFUSED,
        "measurement_supported": False,
        "refused_fields": [
            "T_prepare_ns",
            "T_queue_ns",
            "T_sync_ns",
            "T_move_ns",
        ],
        "reasons": reasons,
        "live_moe_layers": layers,
        "audited_source_symbols": {
            "fused_boundary": source,
            "fused_call": "vllm.utils.flashinfer.flashinfer_cutlass_fused_moe",
        },
    }


def _target2_worker_audit(_worker: Any) -> dict[str, Any]:
    """Audit exact native-offload metrics symbols; never drain live stats."""

    source: dict[str, Any] = {}
    live_connector: dict[str, Any] = {}
    reasons: list[str] = []
    try:
        from vllm.distributed.kv_transfer import (  # type: ignore
            get_kv_transfer_group,
            has_kv_transfer_group,
        )
        from vllm.distributed.kv_transfer.kv_connector.v1.offloading.metrics import (  # type: ignore  # noqa: E501
            OffloadingConnectorStats,
        )
        from vllm.distributed.kv_transfer.kv_connector.v1.offloading.worker import (  # type: ignore  # noqa: E501
            OffloadingConnectorWorker,
        )

        source["stats_record_transfer"] = _source_evidence(
            OffloadingConnectorStats.record_transfer,
            ("op_size", "op_time", "transfer_type"),
        )
        source["stats_reduce"] = _source_evidence(
            OffloadingConnectorStats.reduce,
            ("total_bytes", "total_time"),
        )
        source["worker_stats_drain"] = _source_evidence(
            OffloadingConnectorWorker.get_kv_connector_stats,
            (
                "kv_connector_stats = self.kv_connector_stats",
                "self.kv_connector_stats = OffloadingConnectorStats()",
            ),
        )
        if has_kv_transfer_group():
            connector = get_kv_transfer_group()
            live_connector = {"class": _qualname(connector), "present": True}
        else:
            live_connector = {"present": False}
    except Exception as exc:  # capability audit failure is itself a refusal
        reasons.append(
            "native connector source audit failed: "
            f"{type(exc).__name__}: {exc}"
        )

    reasons.append(
        "vllm.distributed.kv_transfer.kv_connector.v1.offloading.metrics."
        "OffloadingConnectorStats exposes completed transfer op_size/op_time only"
    )
    reasons.append(
        "vllm.distributed.kv_transfer.kv_connector.v1.offloading.worker."
        "OffloadingConnectorWorker.get_kv_connector_stats drains and resets the "
        "transfer accumulator; vLLM 0.23 exposes no auditable per-request "
        "GPU/CPU resident-byte gauge"
    )
    return {
        "status": TARGET_2_MEASUREMENT_REFUSED,
        "measurement_supported": False,
        "refused_fields": [
            "kv_resident_bytes",
            "kv_offloaded_bytes",
            "kv_offloaded_blocks",
            "offload_engaged",
        ],
        "transfer_fields_observable_but_insufficient": [
            "gpu_to_cpu_total_bytes",
            "gpu_to_cpu_total_time",
            "cpu_to_gpu_total_bytes",
            "cpu_to_gpu_total_time",
        ],
        "reasons": reasons,
        "live_kv_transfer_group": live_connector,
        "audited_source_symbols": source,
    }


class TrackGpuWorkerExtension:
    """vLLM-sanctioned extension used only through ``collective_rpc``.

    The class intentionally adds one uniquely-prefixed method.  vLLM 0.23
    dynamically injects this class before worker construction and rejects name
    conflicts.  Seeing this method's worker PID/rank is proof that control
    reached the worker process; it is not, by itself, a measurement hook.
    """

    def track_gpu_probe_capabilities(self, target: str) -> dict[str, Any]:
        if target not in TARGETS:
            raise ValueError(f"unsupported TRACK_GPU target: {target!r}")
        target_audit = (
            _target1_worker_audit(self)
            if target == TARGET_1
            else _target2_worker_audit(self)
        )
        return {
            "control_status": ENGINE_CONTROL_READY,
            "worker_extension_observed": True,
            "worker_hook_observed": False,
            "worker_pid": os.getpid(),
            "rank": getattr(self, "rank", None),
            "local_rank": getattr(self, "local_rank", None),
            "worker_class": _qualname(self),
            "worker_extension_cls": WORKER_EXTENSION_CLS,
            "control_api": {
                "configuration": (
                    "vllm.config.parallel.ParallelConfig.worker_extension_cls"
                ),
                "rpc": "vllm.entrypoints.llm.LLM.collective_rpc",
                "method": "track_gpu_probe_capabilities",
            },
            "target_capability": target_audit,
        }


def _validate_config(target: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if target not in TARGETS:
        raise AdapterError(
            f"unsupported target {target!r}; expected one of {list(TARGETS)}"
        )
    if not isinstance(value, Mapping):
        raise AdapterError("config must be a mapping")
    config = dict(value)
    expected_keys = _COMMON_KEYS if target == TARGET_1 else _TARGET_2_KEYS
    if set(config) != expected_keys:
        raise AdapterError(
            "runtime config keys differ from the frozen adapter contract: "
            + json.dumps(
                {
                    "missing": sorted(expected_keys - set(config)),
                    "extra": sorted(set(config) - expected_keys),
                },
                sort_keys=True,
            )
        )
    model_path = config["model_path"]
    if not isinstance(model_path, str) or not Path(model_path).is_absolute():
        raise AdapterError("model_path must be a non-empty absolute path")

    for key in (
        "tensor_parallel_size",
        "pipeline_parallel_size",
        "max_num_seqs",
        "max_model_len",
        "max_num_batched_tokens",
    ):
        if not isinstance(config[key], int) or isinstance(config[key], bool):
            raise AdapterError(
                f"{key} must be an integer, not {type(config[key]).__name__}"
            )
    for key in ("enforce_eager", "enable_prefix_caching"):
        if not isinstance(config[key], bool):
            raise AdapterError(f"{key} must be boolean")
    for key in ("gpu_memory_utilization", "cpu_offload_gb", "swap_space_gb"):
        if not isinstance(config[key], (int, float)) or isinstance(
            config[key], bool
        ):
            raise AdapterError(f"{key} must be numeric")

    common_expected = {
        "model_revision": EXPECTED_MODEL_REVISION,
        "dtype": "bfloat16",
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "gpu_memory_utilization": 0.97,
        "enforce_eager": True,
        "cpu_offload_gb": 0.0,
        "swap_space_gb": 0.0,
    }
    target_expected = (
        {
            "max_num_seqs": 8,
            "max_model_len": 4096,
            "enable_prefix_caching": False,
            "max_num_batched_tokens": 1024,
        }
        if target == TARGET_1
        else {
            "max_num_seqs": 1,
            "max_model_len": 1048576,
            "kv_offloading_backend": "native",
        }
    )
    expected = {**common_expected, **target_expected}
    conflicts = {
        key: {"expected": expected_value, "actual": config.get(key)}
        for key, expected_value in expected.items()
        if config.get(key) != expected_value
    }
    if conflicts:
        raise AdapterError(
            "frozen runtime config conflict: "
            + json.dumps(conflicts, sort_keys=True)
        )
    if target == TARGET_2:
        if config["kv_offloading_size_gb"] not in (16.0, 140.0):
            raise AdapterError(
                "target_2 permits kv_offloading_size_gb=16.0 or 140.0 only"
            )
        value = config["max_num_batched_tokens"]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AdapterError("target_2 max_num_batched_tokens must be an integer > 0")
        if os.environ.get("VLLM_USE_SIMPLE_KV_OFFLOAD") is not None:
            raise AdapterError(
                "target_2 VLLM_USE_SIMPLE_KV_OFFLOAD must be unset; "
                "the hidden SimpleCPUOffloadConnector switch changes the domain"
            )
    return config


def _engine_kwargs(target: str, config: Mapping[str, Any]) -> dict[str, Any]:
    kwargs = {
        "model": config["model_path"],
        "revision": config["model_revision"],
        "dtype": config["dtype"],
        "tensor_parallel_size": config["tensor_parallel_size"],
        "pipeline_parallel_size": config["pipeline_parallel_size"],
        "max_num_seqs": config["max_num_seqs"],
        "max_model_len": config["max_model_len"],
        "gpu_memory_utilization": config["gpu_memory_utilization"],
        "enforce_eager": config["enforce_eager"],
        "enable_prefix_caching": config["enable_prefix_caching"],
        "max_num_batched_tokens": config["max_num_batched_tokens"],
        "cpu_offload_gb": config["cpu_offload_gb"],
        # vLLM 0.23's LLM.__init__ explicitly strips this deprecated field.
        # Passing the frozen zero still proves no legacy swap domain was asked for.
        "swap_space": config["swap_space_gb"],
        "worker_extension_cls": WORKER_EXTENSION_CLS,
    }
    if target == TARGET_2:
        kwargs.update(
            {
                "kv_offloading_size": config["kv_offloading_size_gb"],
                "kv_offloading_backend": config["kv_offloading_backend"],
            }
        )
    return kwargs


def _read_path(root: Any, *paths: tuple[str, ...]) -> tuple[Any, str]:
    for path in paths:
        current = root
        for name in path:
            if not hasattr(current, name):
                break
            current = getattr(current, name)
        else:
            return current, ".".join(path)
    raise AdapterError(
        "vLLM 0.23 resolved config is missing required field; tried "
        + ", ".join(".".join(path) for path in paths)
    )


def _normal_dtype(value: Any) -> str:
    text = str(value).lower()
    if text in {"bfloat16", "torch.bfloat16", "bf16"}:
        return "bfloat16"
    return text


def _normal_enum(value: Any) -> Any:
    raw = getattr(value, "value", value)
    return raw.lower() if isinstance(raw, str) else raw


def _resolved_config(
    llm: Any, target: str, requested: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    engine = getattr(llm, "llm_engine", None)
    vconfig = getattr(engine, "vllm_config", None)
    if vconfig is None:
        raise AdapterError("LLM.llm_engine.vllm_config is unavailable")

    specs: dict[str, tuple[tuple[str, ...], ...]] = {
        "model_path": (("model_config", "model"),),
        "model_revision": (("model_config", "revision"),),
        "dtype": (("model_config", "dtype"),),
        "tensor_parallel_size": (("parallel_config", "tensor_parallel_size"),),
        "pipeline_parallel_size": (("parallel_config", "pipeline_parallel_size"),),
        "max_num_seqs": (("scheduler_config", "max_num_seqs"),),
        "max_model_len": (("model_config", "max_model_len"),),
        "gpu_memory_utilization": (("cache_config", "gpu_memory_utilization"),),
        "enforce_eager": (("model_config", "enforce_eager"),),
        "enable_prefix_caching": (("cache_config", "enable_prefix_caching"),),
        "max_num_batched_tokens": (
            ("scheduler_config", "max_num_batched_tokens"),
        ),
        "cpu_offload_gb": (
            ("offload_config", "uva", "cpu_offload_gb"),
            # Keep a fail-auditable compatibility path for a locally patched
            # 0.23 wheel; provenance records which object actually resolved.
            ("cache_config", "cpu_offload_gb"),
        ),
    }
    if target == TARGET_2:
        specs.update(
            {
                "kv_offloading_size_gb": (
                    ("cache_config", "kv_offloading_size"),
                ),
                "kv_offloading_backend": (
                    ("cache_config", "kv_offloading_backend"),
                ),
            }
        )

    resolved: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for key, paths in specs.items():
        raw, path = _read_path(vconfig, *paths)
        if key == "dtype":
            raw = _normal_dtype(raw)
        elif key == "kv_offloading_backend":
            raw = _normal_enum(raw)
        resolved[key] = raw
        provenance[key] = "LLM.llm_engine.vllm_config." + path

    # LLM.__init__ in v0.23 removes deprecated swap_space before EngineArgs.
    resolved["swap_space_gb"] = requested["swap_space_gb"]
    provenance["swap_space_gb"] = (
        "vllm.entrypoints.llm.LLM.__init__ strips deprecated swap_space; "
        "adapter validated the requested value is exactly 0.0"
    )
    return resolved, provenance


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _regular_log_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("TRACK_GPU_VLLM_STARTUP_LOG_PATHS")
    candidates: list[str] = explicit.split(os.pathsep) if explicit else []
    if not candidates:
        for fd in (1, 2):
            try:
                candidates.append(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                continue
    for raw in candidates:
        path = Path(raw)
        try:
            mode = path.stat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode):
            resolved = path.resolve()
            if resolved not in paths:
                paths.append(resolved)
    return paths


def _read_tail(path: Path, limit: int = 64 * 1024 * 1024) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _parse_backend_markers(text: str) -> dict[str, str]:
    lines = [_ANSI_ESCAPE.sub("", line).strip() for line in text.splitlines()]
    markers: dict[str, str] = {}
    for name, required in _BACKEND_MARKER_PATTERNS.items():
        match = next(
            (line for line in lines if all(token in line for token in required)),
            None,
        )
        if match is None:
            raise AdapterError(
                f"vLLM startup log is missing canonical {name} marker "
                f"containing {list(required)!r}"
            )
        markers[name] = match
    return markers


def _startup_markers() -> tuple[dict[str, str], list[str]]:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    paths = _regular_log_paths()
    if not paths:
        raise AdapterError(
            "cannot audit vLLM startup markers: stdout/stderr are not regular files; "
            "run under measurement/run_gpu_attempt.py or set the absolute, regular "
            "TRACK_GPU_VLLM_STARTUP_LOG_PATHS"
        )
    combined = "\n".join(_read_tail(path) for path in paths)
    return _parse_backend_markers(combined), [str(path) for path in paths]


def _command_identity(argv: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except Exception as exc:
        return {"argv": argv, "error": f"{type(exc).__name__}: {exc}"}


def _environment_identity(torch: Any) -> dict[str, Any]:
    cudacxx = os.environ.get("CUDACXX")
    cuda_home = os.environ.get("CUDA_HOME")
    if cudacxx:
        nvcc = cudacxx
        nvcc_source = "CUDACXX"
    elif cuda_home:
        nvcc = str(Path(cuda_home) / "bin" / "nvcc")
        nvcc_source = "CUDA_HOME/bin/nvcc"
    else:
        nvcc = shutil.which("nvcc")
        nvcc_source = "PATH"
    return {
        "VLLM_USE_SIMPLE_KV_OFFLOAD": os.environ.get(
            "VLLM_USE_SIMPLE_KV_OFFLOAD"
        ),
        "CUDA_HOME": os.environ.get("CUDA_HOME"),
        "CUDA_PATH": os.environ.get("CUDA_PATH"),
        "CUDACXX": os.environ.get("CUDACXX"),
        "CPATH": os.environ.get("CPATH"),
        "LIBRARY_PATH": os.environ.get("LIBRARY_PATH"),
        "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH"),
        "nvcc": (
            _command_identity([nvcc, "--version"])
            if nvcc
            else {"error": "nvcc not found on PATH"}
        ),
        "nvcc_resolution_source": nvcc_source,
        "nvidia_smi_driver": _command_identity(
            [
                "nvidia-smi",
                "--query-gpu=driver_version",
                "--format=csv,noheader",
            ]
        ),
        "torch_cuda_version": getattr(getattr(torch, "version", None), "cuda", None),
    }


def _model_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    """Capture a lightweight identity; never pretend file sizes are content hashes."""

    root = Path(config["model_path"])
    config_path = root / "config.json"
    config_sha256 = None
    if config_path.is_file():
        config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
    try:
        shards = sorted(root.glob("*.safetensors"))
        inventory = [{"name": p.name, "bytes": p.stat().st_size} for p in shards]
    except OSError:
        inventory = []
    inventory_blob = json.dumps(
        inventory, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "model_path": str(root),
        "model_revision": config["model_revision"],
        "config_sha256": config_sha256,
        "safetensors_shard_count": len(inventory),
        "safetensors_total_bytes": sum(row["bytes"] for row in inventory),
        "safetensors_size_inventory_sha256": hashlib.sha256(inventory_blob).hexdigest(),
        "weight_content_hash_computed_by_adapter": False,
    }


def _summarize_capability(target: str, rows: Any) -> dict[str, Any]:
    if not isinstance(rows, list) or len(rows) != 1:
        raise AdapterError(
            "TP=1/PP=1 requires exactly one worker capability result; got "
            f"{len(rows) if isinstance(rows, list) else type(rows).__name__}"
        )
    row = rows[0]
    if not isinstance(row, Mapping):
        raise AdapterError("worker capability result must be a mapping")
    if row.get("control_status") != ENGINE_CONTROL_READY:
        raise AdapterError("worker extension did not report ENGINE_CONTROL_READY")
    if row.get("worker_extension_observed") is not True:
        raise AdapterError("worker extension RPC was not observed on the worker")
    if row.get("worker_hook_observed") is not False:
        raise AdapterError(
            "worker control capability is ambiguously labeled as a measurement hook"
        )
    target_capability = row.get("target_capability")
    if not isinstance(target_capability, Mapping):
        raise AdapterError("worker target capability is missing")
    expected_status = (
        TARGET_1_MEASUREMENT_REFUSED
        if target == TARGET_1
        else TARGET_2_MEASUREMENT_REFUSED
    )
    if target_capability.get("status") != expected_status:
        raise AdapterError(
            "unexpected worker measurement status: "
            f"expected {expected_status}, got {target_capability.get('status')!r}"
        )
    if target_capability.get("measurement_supported") is not False:
        raise AdapterError("refused target capability must set measurement_supported=false")
    return {
        "control_status": ENGINE_CONTROL_READY,
        "status": expected_status,
        "measurement_supported": False,
        "supported_capabilities": [
            "frozen_engine_construction",
            "worker_extension_cls_injection",
            "collective_rpc_worker_control",
            "resolved_engine_config_audit",
            "startup_backend_marker_audit",
        ],
        "refused_fields": list(target_capability.get("refused_fields", [])),
        "reasons": list(target_capability.get("reasons", [])),
    }


class _VllmEngineBridge:
    def __init__(self, target: str, config: Mapping[str, Any]) -> None:
        self._llm: Any = None
        self._identity: dict[str, Any] | None = None
        try:
            installed = importlib.metadata.version("vllm")
        except importlib.metadata.PackageNotFoundError as exc:
            raise AdapterError("vLLM is not installed") from exc
        if installed != EXPECTED_VLLM_VERSION:
            raise AdapterError(
                "vLLM version mismatch: expected "
                f"{EXPECTED_VLLM_VERSION}, got {installed}"
            )

        import torch  # type: ignore

        if str(torch.__version__) != EXPECTED_TORCH_VERSION:
            raise AdapterError(
                f"torch version mismatch: expected {EXPECTED_TORCH_VERSION}, "
                f"got {torch.__version__}"
            )
        from vllm import LLM  # type: ignore

        try:
            self._llm = LLM(**_engine_kwargs(target, config))
            resolved, provenance = _resolved_config(self._llm, target, config)
            extension, extension_path = _read_path(
                self._llm.llm_engine.vllm_config,
                ("parallel_config", "worker_extension_cls"),
            )
            if extension != WORKER_EXTENSION_CLS:
                raise AdapterError(
                    "resolved worker_extension_cls differs from the sanctioned "
                    f"adapter: {extension!r}"
                )
            mismatches = {
                key: {"expected": expected, "actual": resolved.get(key)}
                for key, expected in config.items()
                if key != "model_path" and resolved.get(key) != expected
            }
            if mismatches:
                raise AdapterError(
                    "vLLM resolved config differs from frozen request: "
                    + json.dumps(mismatches, sort_keys=True, default=str)
                )
            markers, marker_sources = _startup_markers()
            worker_rows = self._llm.collective_rpc(
                "track_gpu_probe_capabilities",
                kwargs={"target": target},
            )
            capability = _summarize_capability(target, worker_rows)
            self._identity = {
                "adapter_schema": "track-gpu-vllm-runtime-adapter-v1",
                "control_status": ENGINE_CONTROL_READY,
                "vllm_version": installed,
                "torch_version": str(torch.__version__),
                "model_revision": resolved["model_revision"],
                "resolved_config": resolved,
                "resolved_config_provenance": provenance,
                "requested_engine_kwargs": _engine_kwargs(target, config),
                "backend_markers": markers,
                "backend_marker_log_sources": marker_sources,
                "worker_extension_cls": WORKER_EXTENSION_CLS,
                "worker_extension_cls_provenance": (
                    "LLM.llm_engine.vllm_config." + extension_path
                ),
                "adapter_process": {
                    "pid": os.getpid(),
                    "python_executable": sys.executable,
                },
                "worker_capabilities": worker_rows,
                "measurement_capability": capability,
                "environment": _environment_identity(torch),
                "model_identity": _model_identity(config),
            }
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def runtime_identity(self) -> dict[str, Any]:
        if self._identity is None:
            raise AdapterError("vLLM bridge identity is unavailable")
        return copy.deepcopy(self._identity)

    def close(self) -> None:
        llm, self._llm = self._llm, None
        if llm is None:
            return
        engine = getattr(llm, "llm_engine", None)
        core = getattr(engine, "engine_core", None)
        shutdown = getattr(core, "shutdown", None)
        if callable(shutdown):
            shutdown()


_ENGINE_FACTORY: Callable[[str, Mapping[str, Any]], Any] = _VllmEngineBridge


class _StrictSession:
    def __init__(self, target: str, config: dict[str, Any], bridge: Any) -> None:
        self._target = target
        self._config = config
        self._bridge = bridge
        self._closed = False
        identity_fn = getattr(bridge, "runtime_identity", None)
        if not callable(identity_fn):
            self.close()
            raise AdapterError("engine bridge has no runtime_identity()")
        try:
            identity = identity_fn()
            if not isinstance(identity, Mapping):
                raise AdapterError("engine bridge runtime identity must be a mapping")
            self._identity = copy.deepcopy(dict(identity))
            capability = self._identity.get("measurement_capability")
            if not isinstance(capability, Mapping):
                raise AdapterError("runtime identity has no measurement_capability")
            expected = (
                TARGET_1_MEASUREMENT_REFUSED
                if target == TARGET_1
                else TARGET_2_MEASUREMENT_REFUSED
            )
            if capability.get("control_status") != ENGINE_CONTROL_READY:
                raise AdapterError("runtime identity is not ENGINE_CONTROL_READY")
            if capability.get("status") != expected:
                raise AdapterError(
                    f"runtime identity must explicitly report {expected}"
                )
        except Exception:
            self.close()
            raise

    def runtime_identity(self) -> dict[str, Any]:
        return copy.deepcopy(self._identity)

    def _refuse(self) -> None:
        capability = self._identity["measurement_capability"]
        detail = {
            "status": capability["status"],
            "refused_fields": capability.get("refused_fields", []),
            "reasons": capability.get("reasons", []),
        }
        raise AdapterError(
            f"{capability['status']}: worker control is ready, but vLLM 0.23 "
            "cannot supply the frozen measured fields without inference or "
            "backend substitution; "
            + json.dumps(detail, sort_keys=True)
        )

    def measure_window(
        self, *, concurrency: int, steps: int, repeat_index: int
    ) -> list[dict[str, Any]]:
        if self._target != TARGET_1:
            raise AdapterError("measure_window is only valid for target_1")
        del concurrency, steps, repeat_index
        self._refuse()

    def measure(self, *, seq_len: int) -> dict[str, Any]:
        if self._target != TARGET_2:
            raise AdapterError("measure is only valid for target_2")
        del seq_len
        self._refuse()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._bridge, "close", None)
        if callable(close):
            close()


def create_session(*, target: str, config: Mapping[str, Any]) -> _StrictSession:
    """Create one strict engine-control session for the requested target."""

    validated = _validate_config(target, config)
    bridge = _ENGINE_FACTORY(target, validated)
    return _StrictSession(target, validated, bridge)
