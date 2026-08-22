"""Strict vLLM 0.23 serving backend boundary for TRACK_GPU probes.

This module deliberately keeps the vLLM import and worker instrumentation out
of the CPU-only probe plumbing.  A runtime adapter owns the live engine and its
worker-side hooks; this boundary owns the frozen configuration, independently
checks the installed vLLM version, and validates every measured record before
the probe may label it ``measured``.

The adapter module is explicit because vLLM executes model work in worker
processes.  A parent-process monkeypatch is not evidence that the worker hook
ran.  An adapter module must export ``create_session(target, config)``.  The
returned session must expose ``runtime_identity()``, plus ``measure_window``
for target_1 or ``measure`` for target_2.  ``runtime_identity`` must contain the
resolved engine config and all three startup-log backend markers.  Missing or
conflicting fields fail loudly; there is no fallback to mock data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from measurement.probes.mock_backend import (
    BackendError,
    KV_BYTES_PER_TOKEN,
)


EXPECTED_VLLM_VERSION = "0.23.0"
EXPECTED_MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
BACKEND_MARKERS = ("attention_backend", "fused_moe_backend", "kernel_backend")


@dataclass(frozen=True)
class DispatchRuntimeConfig:
    """Owner-frozen target_1 engine domain."""

    model_path: str
    model_revision: str = EXPECTED_MODEL_REVISION
    dtype: str = "bfloat16"
    tensor_parallel_size: int = 1
    pipeline_parallel_size: int = 1
    max_num_seqs: int = 8
    max_model_len: int = 4096
    gpu_memory_utilization: float = 0.97
    enforce_eager: bool = True
    enable_prefix_caching: bool = False
    max_num_batched_tokens: int = 1024
    cpu_offload_gb: float = 0.0
    swap_space_gb: float = 0.0

    def validate(self) -> None:
        expected = {
            "model_revision": EXPECTED_MODEL_REVISION,
            "dtype": "bfloat16",
            "tensor_parallel_size": 1,
            "pipeline_parallel_size": 1,
            "max_num_seqs": 8,
            "max_model_len": 4096,
            "gpu_memory_utilization": 0.97,
            "enforce_eager": True,
            "enable_prefix_caching": False,
            "max_num_batched_tokens": 1024,
            "cpu_offload_gb": 0.0,
            "swap_space_gb": 0.0,
        }
        _validate_frozen_config(asdict(self), expected, "target_1")
        _validate_model_path(self.model_path)


@dataclass(frozen=True)
class LongContextRuntimeConfig:
    """Owner-frozen target_2 offload-on variant.

    The amount and vLLM offload backend are intentionally required inputs.  The
    owner selected an offload-on variant but did not select those two values;
    choosing either here would silently create a new measurement domain.
    """

    model_path: str
    kv_offloading_size_gb: float
    kv_offloading_backend: str
    max_num_batched_tokens: int
    enable_prefix_caching: bool
    max_model_len: int = 1048576
    model_revision: str = EXPECTED_MODEL_REVISION
    dtype: str = "bfloat16"
    tensor_parallel_size: int = 1
    pipeline_parallel_size: int = 1
    max_num_seqs: int = 1
    gpu_memory_utilization: float = 0.97
    enforce_eager: bool = True
    cpu_offload_gb: float = 0.0
    swap_space_gb: float = 0.0

    def validate(self, requested_seq_lens: list[int]) -> None:
        expected = {
            "model_revision": EXPECTED_MODEL_REVISION,
            "dtype": "bfloat16",
            "tensor_parallel_size": 1,
            "pipeline_parallel_size": 1,
            "max_num_seqs": 1,
            "gpu_memory_utilization": 0.97,
            "enforce_eager": True,
            "cpu_offload_gb": 0.0,
            "swap_space_gb": 0.0,
        }
        _validate_frozen_config(asdict(self), expected, "target_2")
        _validate_model_path(self.model_path)
        if self.kv_offloading_size_gb <= 0:
            raise BackendError(
                "target_2 is the offload-on variant: kv_offloading_size_gb must be > 0"
            )
        if not self.kv_offloading_backend.strip():
            raise BackendError(
                "target_2 requires an explicit kv_offloading_backend; refusing to guess"
            )
        if self.max_num_batched_tokens <= 0:
            raise BackendError(
                "target_2 requires an owner-recorded max_num_batched_tokens > 0"
            )
        if not isinstance(self.enable_prefix_caching, bool):
            raise BackendError(
                "target_2 requires an explicit boolean enable_prefix_caching"
            )
        if not requested_seq_lens:
            raise BackendError("target_2 sequence-length sweep cannot be empty")
        if self.max_model_len < max(requested_seq_lens):
            raise BackendError(
                "target_2 max_model_len is smaller than the frozen sweep maximum: "
                f"{self.max_model_len} < {max(requested_seq_lens)}"
            )


def _validate_model_path(value: str) -> None:
    if not value or not Path(value).is_absolute():
        raise BackendError("model_path must be a non-empty absolute path")


def _validate_frozen_config(
    actual: Mapping[str, Any], expected: Mapping[str, Any], target: str
) -> None:
    conflicts = {
        key: {"expected": expected_value, "actual": actual.get(key)}
        for key, expected_value in expected.items()
        if actual.get(key) != expected_value
    }
    if conflicts:
        raise BackendError(
            f"{target} frozen vLLM domain conflict: "
            f"{json.dumps(conflicts, sort_keys=True)}"
        )


def _installed_vllm_version() -> str:
    try:
        return importlib.metadata.version("vllm")
    except importlib.metadata.PackageNotFoundError as exc:
        raise BackendError("vLLM is not installed in this Python environment") from exc


def _load_adapter(module_name: str) -> Any:
    if not module_name:
        raise BackendError(
            "a worker-capable --runtime-adapter-module is required for a vLLM GPU run"
        )
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise BackendError(
            f"cannot import runtime adapter {module_name!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not callable(getattr(module, "create_session", None)):
        raise BackendError(
            f"runtime adapter {module_name!r} must export create_session(target, config)"
        )
    return module


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BackendError(f"{where} must be a mapping")
    return value


def _runtime_identity(session: Any, expected_config: Mapping[str, Any]) -> dict[str, Any]:
    identity_fn = getattr(session, "runtime_identity", None)
    if not callable(identity_fn):
        raise BackendError("vLLM adapter session has no runtime_identity()")
    identity = dict(_mapping(identity_fn(), "runtime_identity"))
    if identity.get("vllm_version") != EXPECTED_VLLM_VERSION:
        raise BackendError(
            "adapter runtime identity has wrong vLLM version: "
            f"expected {EXPECTED_VLLM_VERSION}, got {identity.get('vllm_version')!r}"
        )
    if identity.get("model_revision") != EXPECTED_MODEL_REVISION:
        raise BackendError(
            "adapter runtime identity has wrong model revision: "
            f"expected {EXPECTED_MODEL_REVISION}, got {identity.get('model_revision')!r}"
        )
    resolved = _mapping(identity.get("resolved_config"), "runtime_identity.resolved_config")
    mismatches = {
        key: {"expected": value, "actual": resolved.get(key)}
        for key, value in expected_config.items()
        if key != "model_path" and resolved.get(key) != value
    }
    if mismatches:
        raise BackendError(
            "vLLM resolved config differs from requested domain: "
            f"{json.dumps(mismatches, sort_keys=True)}"
        )
    markers = _mapping(identity.get("backend_markers"), "runtime_identity.backend_markers")
    missing = [name for name in BACKEND_MARKERS if not isinstance(markers.get(name), str)
               or not markers.get(name).strip()]
    if missing:
        raise BackendError(
            "vLLM startup log is missing required backend markers: "
            + ", ".join(missing)
        )
    return identity


class _VllmBackendBase:
    name = "vllm_0_23"

    def __init__(
        self,
        *,
        target: str,
        config: DispatchRuntimeConfig | LongContextRuntimeConfig,
        runtime_adapter_module: str,
        requested_seq_lens: list[int] | None = None,
        version_lookup: Callable[[], str] = _installed_vllm_version,
        adapter_loader: Callable[[str], Any] = _load_adapter,
    ) -> None:
        if isinstance(config, DispatchRuntimeConfig):
            config.validate()
        else:
            config.validate(requested_seq_lens or [])
        installed = version_lookup()
        if installed != EXPECTED_VLLM_VERSION:
            raise BackendError(
                f"vLLM version mismatch: expected {EXPECTED_VLLM_VERSION}, got {installed}"
            )
        module = adapter_loader(runtime_adapter_module)
        config_dict = asdict(config)
        try:
            self._session = module.create_session(target=target, config=config_dict)
        except Exception as exc:
            raise BackendError(
                f"vLLM adapter failed to create {target} session: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self.runtime_identity = _runtime_identity(self._session, config_dict)
        self.runtime_identity["adapter_module"] = runtime_adapter_module
        self.runtime_identity["installed_vllm_version"] = installed
        self.runtime_config = config_dict

    def close(self) -> None:
        closer = getattr(self._session, "close", None)
        if callable(closer):
            closer()


class VllmDispatchBackend(_VllmBackendBase):
    """Live target_1 backend; all step records must come from the worker hook."""

    name = "vllm_dispatch"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(target="target_1_dispatch", **kwargs)

    def measure_window(
        self, concurrency: int, steps: int, repeat_index: int
    ) -> list[dict[str, Any]]:
        measure = getattr(self._session, "measure_window", None)
        if not callable(measure):
            raise BackendError("target_1 adapter session has no measure_window()")
        try:
            raw = measure(
                concurrency=concurrency, steps=steps, repeat_index=repeat_index
            )
        except Exception as exc:
            raise BackendError(
                f"target_1 vLLM measurement failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(raw, list) or len(raw) != steps:
            raise BackendError(
                f"target_1 adapter returned {len(raw) if isinstance(raw, list) else 'non-list'} "
                f"step records; expected exactly {steps}"
            )
        return [
            _validate_dispatch_step(record, concurrency, index, repeat_index)
            for index, record in enumerate(raw)
        ]


class VllmLongContextBackend(_VllmBackendBase):
    """Live target_2 backend for the independently named offload-on variant."""

    name = "vllm_longctx_offload_on"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(target="target_2_longctx_offload_on", **kwargs)

    def measure(self, seq_len: int) -> dict[str, Any]:
        measure = getattr(self._session, "measure", None)
        if not callable(measure):
            raise BackendError("target_2 adapter session has no measure()")
        try:
            record = measure(seq_len=seq_len)
        except Exception as exc:
            if _is_oom(exc):
                return {
                    "seq_len": seq_len,
                    "oom": True,
                    "error": f"{type(exc).__name__}: {exc}",
                    "failure_classification": "CUDA_OR_ENGINE_OOM",
                    "runtime_variant": "offload-on",
                }
            raise BackendError(
                f"target_2 vLLM measurement failed: {type(exc).__name__}: {exc}"
            ) from exc
        return _validate_longctx_record(record, seq_len)


def _require_int(record: Mapping[str, Any], key: str, *, positive: bool) -> int:
    value = record.get(key)
    floor = 1 if positive else 0
    if not isinstance(value, int) or isinstance(value, bool) or value < floor:
        relation = "> 0" if positive else ">= 0"
        raise BackendError(f"adapter record {key!r} must be an integer {relation}")
    return value


def _validate_dispatch_step(
    value: Any, concurrency: int, step_index: int, repeat_index: int
) -> dict[str, Any]:
    record = dict(_mapping(value, "dispatch step"))
    record.setdefault("step_index", step_index)
    record.setdefault("repeat_index", repeat_index)
    if record.get("concurrency") != concurrency:
        raise BackendError("dispatch adapter returned a record for the wrong concurrency")
    expert_tokens = _require_int(record, "expert_tokens", positive=True)
    if expert_tokens % concurrency:
        raise BackendError("dispatch expert_tokens is not divisible by concurrency")
    dispatch_bytes = _require_int(record, "dispatch_bytes", positive=True)
    granularity = _require_int(record, "move_granularity_bytes", positive=True)
    if dispatch_bytes != 2 * granularity:
        raise BackendError("dispatch byte accounting violates gather+scatter conservation")
    for key in (
        "dispatch_period_ns", "control_decisions", "T_prepare_ns", "T_queue_ns",
        "T_sync_ns", "T_move_ns",
    ):
        _require_int(record, key, positive=False)
    source = _mapping(record.get("measurement_source"), "dispatch measurement_source")
    if source.get("worker_hook_observed") is not True:
        raise BackendError("dispatch worker hook was not observed; refusing measured output")
    return record


def _validate_longctx_record(value: Any, seq_len: int) -> dict[str, Any]:
    record = dict(_mapping(value, "long-context record"))
    if record.get("seq_len") != seq_len:
        raise BackendError("long-context adapter returned a record for the wrong seq_len")
    if record.get("oom") is True:
        record.setdefault("failure_classification", "CUDA_OR_ENGINE_OOM")
        record.setdefault("runtime_variant", "offload-on")
        return record
    total = _require_int(record, "kv_total_bytes", positive=True)
    resident = _require_int(record, "kv_resident_bytes", positive=False)
    offloaded = _require_int(record, "kv_offloaded_bytes", positive=False)
    if total != seq_len * KV_BYTES_PER_TOKEN or resident + offloaded != total:
        raise BackendError("long-context KV byte accounting is not conservative")
    if record.get("offload_engaged") is not (offloaded > 0):
        raise BackendError("long-context offload flag disagrees with measured bytes")
    for key in (
        "kv_blocks_total", "kv_offloaded_blocks", "ttft_ns",
        "decode_per_token_ns", "kv_move_ns", "kv_move_bytes",
    ):
        _require_int(record, key, positive=False)
    source = _mapping(record.get("measurement_source"), "long-context measurement_source")
    if source.get("worker_hook_observed") is not True:
        raise BackendError("KV worker hook was not observed; refusing measured output")
    record["runtime_variant"] = "offload-on"
    return record


def _is_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "outofmemory" in text or "out of memory" in text or "oom" in text


_REGISTRY = {
    VllmDispatchBackend.name: VllmDispatchBackend,
    VllmLongContextBackend.name: VllmLongContextBackend,
}


def registered_backends() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def resolve_backend(name: str):
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise BackendError(
            f"unregistered vLLM backend {name!r}; registered: {sorted(_REGISTRY)}"
        ) from exc
