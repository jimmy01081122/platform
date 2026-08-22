"""CPU-only tests for the strict vLLM 0.23 backend boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from measurement.probes.mock_backend import BackendError, registered_backends, resolve_backend
from measurement.probes.vllm_backend import (
    EXPECTED_MODEL_REVISION,
    DispatchRuntimeConfig,
    LongContextRuntimeConfig,
    VllmDispatchBackend,
    VllmLongContextBackend,
)


def _identity(config, *, markers=True):
    return {
        "vllm_version": "0.23.0",
        "model_revision": EXPECTED_MODEL_REVISION,
        "resolved_config": dict(config),
        "backend_markers": ({
            "attention_backend": "FLASH_ATTN",
            "fused_moe_backend": "FLASHINFER_CUTLASS",
            "kernel_backend": "FLASHINFER_JIT",
        } if markers else {}),
    }


class DispatchSession:
    def __init__(self, config):
        self.config = config
        self.closed = False

    def runtime_identity(self):
        return _identity(self.config)

    def measure_window(self, *, concurrency, steps, repeat_index):
        return [{
            "step_index": i,
            "repeat_index": repeat_index,
            "concurrency": concurrency,
            "expert_tokens": 8 * concurrency,
            "dispatch_bytes": 2 * 8 * concurrency * 4096 * 2,
            "move_granularity_bytes": 8 * concurrency * 4096 * 2,
            "dispatch_period_ns": 20_000_000,
            "control_decisions": 16 * concurrency,
            "T_prepare_ns": 1,
            "T_queue_ns": 2,
            "T_sync_ns": 3,
            "T_move_ns": 4,
            "measurement_source": {"worker_hook_observed": True},
        } for i in range(steps)]

    def close(self):
        self.closed = True


class LongContextSession:
    def __init__(self, config):
        self.config = config

    def runtime_identity(self):
        return _identity(self.config)

    def measure(self, *, seq_len):
        total = seq_len * 131072
        offloaded = max(0, total - 8_000_000_000)
        return {
            "seq_len": seq_len,
            "oom": False,
            "kv_total_bytes": total,
            "kv_blocks_total": (seq_len + 15) // 16,
            "kv_resident_bytes": total - offloaded,
            "kv_offloaded_bytes": offloaded,
            "kv_offloaded_blocks": (offloaded + 2097151) // 2097152,
            "offload_engaged": offloaded > 0,
            "ttft_ns": 1,
            "decode_per_token_ns": 2,
            "kv_move_ns": 3 if offloaded else 0,
            "kv_move_bytes": offloaded,
            "measurement_source": {"worker_hook_observed": True},
        }


def _adapter(session_cls):
    return SimpleNamespace(
        create_session=lambda *, target, config: session_cls(config)
    )


def _dispatch_backend(**overrides):
    kwargs = {
        "config": DispatchRuntimeConfig(model_path="/workspace/model"),
        "runtime_adapter_module": "fake.dispatch",
        "version_lookup": lambda: "0.23.0",
        "adapter_loader": lambda _name: _adapter(DispatchSession),
    }
    kwargs.update(overrides)
    return VllmDispatchBackend(**kwargs)


def _longctx_backend(**overrides):
    kwargs = {
        "config": LongContextRuntimeConfig(
            model_path="/workspace/model",
            kv_offloading_size_gb=64,
            kv_offloading_backend="native",
            max_num_batched_tokens=8192,
            enable_prefix_caching=True,
        ),
        "runtime_adapter_module": "fake.longctx",
        "requested_seq_lens": [4096, 1048576],
        "version_lookup": lambda: "0.23.0",
        "adapter_loader": lambda _name: _adapter(LongContextSession),
    }
    kwargs.update(overrides)
    return VllmLongContextBackend(**kwargs)


def test_registry_resolves_explicit_vllm_backends_without_importing_vllm():
    assert "vllm_dispatch" in registered_backends()
    assert "vllm_longctx_offload_on" in registered_backends()
    assert resolve_backend("vllm_dispatch") is VllmDispatchBackend
    assert resolve_backend("vllm_longctx_offload_on") is VllmLongContextBackend


def test_target1_config_is_frozen_and_window_is_worker_hook_backed():
    backend = _dispatch_backend()
    records = backend.measure_window(concurrency=8, steps=4, repeat_index=2)
    assert len(records) == 4
    assert all(r["concurrency"] == 8 for r in records)
    assert backend.runtime_config["max_num_seqs"] == 8
    assert backend.runtime_config["max_model_len"] == 4096
    assert backend.runtime_config["max_num_batched_tokens"] == 1024
    assert backend.runtime_config["enforce_eager"] is True

    with pytest.raises(BackendError, match="frozen vLLM domain conflict"):
        _dispatch_backend(config=DispatchRuntimeConfig(
            model_path="/workspace/model", max_num_seqs=1
        ))


def test_target1_refuses_wrong_version_or_missing_startup_markers():
    with pytest.raises(BackendError, match="version mismatch"):
        _dispatch_backend(version_lookup=lambda: "0.22.0")

    class MissingMarkers(DispatchSession):
        def runtime_identity(self):
            return _identity(self.config, markers=False)

    with pytest.raises(BackendError, match="missing required backend markers"):
        _dispatch_backend(
            adapter_loader=lambda _name: _adapter(MissingMarkers)
        )


def test_target1_refuses_unobserved_worker_hook():
    class NoHook(DispatchSession):
        def measure_window(self, **kwargs):
            rows = super().measure_window(**kwargs)
            rows[0]["measurement_source"]["worker_hook_observed"] = False
            return rows

    backend = _dispatch_backend(adapter_loader=lambda _name: _adapter(NoHook))
    with pytest.raises(BackendError, match="worker hook was not observed"):
        backend.measure_window(concurrency=1, steps=1, repeat_index=0)


def test_target2_requires_explicit_positive_offload_and_batching_domain():
    with pytest.raises(BackendError, match="kv_offloading_size_gb must be > 0"):
        _longctx_backend(config=LongContextRuntimeConfig(
            model_path="/workspace/model",
            kv_offloading_size_gb=0,
            kv_offloading_backend="native",
            max_num_batched_tokens=8192,
            enable_prefix_caching=True,
        ))
    with pytest.raises(BackendError, match="explicit kv_offloading_backend"):
        _longctx_backend(config=LongContextRuntimeConfig(
            model_path="/workspace/model",
            kv_offloading_size_gb=64,
            kv_offloading_backend="",
            max_num_batched_tokens=8192,
            enable_prefix_caching=True,
        ))
    with pytest.raises(BackendError, match="max_num_batched_tokens"):
        _longctx_backend(config=LongContextRuntimeConfig(
            model_path="/workspace/model",
            kv_offloading_size_gb=64,
            kv_offloading_backend="native",
            max_num_batched_tokens=0,
            enable_prefix_caching=True,
        ))


def test_target2_preserves_full_sweep_and_labels_offload_on_variant():
    backend = _longctx_backend()
    assert backend.runtime_config["max_model_len"] == 1048576
    assert backend.runtime_config["cpu_offload_gb"] == 0
    assert backend.runtime_config["swap_space_gb"] == 0
    result = backend.measure(1048576)
    assert result["runtime_variant"] == "offload-on"
    assert result["offload_engaged"] is True

    with pytest.raises(BackendError, match="smaller than the frozen sweep maximum"):
        _longctx_backend(
            config=LongContextRuntimeConfig(
                model_path="/workspace/model",
                kv_offloading_size_gb=64,
                kv_offloading_backend="native",
                max_num_batched_tokens=8192,
                enable_prefix_caching=True,
                max_model_len=524288,
            )
        )


def test_target2_classifies_oom_as_result():
    class OomSession(LongContextSession):
        def measure(self, *, seq_len):
            raise RuntimeError("CUDA out of memory")

    backend = _longctx_backend(adapter_loader=lambda _name: _adapter(OomSession))
    result = backend.measure(1048576)
    assert result["oom"] is True
    assert result["failure_classification"] == "CUDA_OR_ENGINE_OOM"
    assert result["runtime_variant"] == "offload-on"
