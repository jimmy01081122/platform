"""CPU-only tests for the strict vLLM worker-control adapter."""

from __future__ import annotations

import sys
from enum import Enum
from types import ModuleType, SimpleNamespace

import pytest

from measurement.probes import vllm_runtime_adapter as adapter


def _dispatch_config(**overrides):
    config = {
        "model_path": "/workspace/models/mixtral",
        "model_revision": adapter.EXPECTED_MODEL_REVISION,
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
    config.update(overrides)
    return config


def _longctx_config(**overrides):
    config = {
        "model_path": "/workspace/models/mixtral",
        "model_revision": adapter.EXPECTED_MODEL_REVISION,
        "dtype": "bfloat16",
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "max_num_seqs": 1,
        "max_model_len": 1048576,
        "gpu_memory_utilization": 0.97,
        "enforce_eager": True,
        "enable_prefix_caching": True,
        "max_num_batched_tokens": 8192,
        "cpu_offload_gb": 0.0,
        "swap_space_gb": 0.0,
        "kv_offloading_size_gb": 16.0,
        "kv_offloading_backend": "native",
    }
    config.update(overrides)
    return config


class _FakeBridge:
    def __init__(self, identity):
        self.identity = identity
        self.close_calls = 0

    def runtime_identity(self):
        return self.identity

    def close(self):
        self.close_calls += 1


def _identity(target):
    status = (
        adapter.TARGET_1_MEASUREMENT_REFUSED
        if target == adapter.TARGET_1
        else adapter.TARGET_2_MEASUREMENT_REFUSED
    )
    return {
        "vllm_version": adapter.EXPECTED_VLLM_VERSION,
        "torch_version": adapter.EXPECTED_TORCH_VERSION,
        "model_revision": adapter.EXPECTED_MODEL_REVISION,
        "resolved_config": (
            _dispatch_config() if target == adapter.TARGET_1 else _longctx_config()
        ),
        "backend_markers": {
            "attention_backend": "Using FLASH_ATTN attention backend",
            "fused_moe_backend": (
                "Using FlashInfer CUTLASS Unquantized MoE backend"
            ),
            "kernel_backend": (
                "kernel_config=KernelConfig(enable_flashinfer_autotune=True)"
            ),
        },
        "environment": {"VLLM_USE_SIMPLE_KV_OFFLOAD": None},
        "measurement_capability": {
            "control_status": adapter.ENGINE_CONTROL_READY,
            "status": status,
            "measurement_supported": False,
            "refused_fields": ["T_move_ns"],
            "reasons": ["worker-observed field is not exposed by vLLM 0.23"],
        },
    }


def test_target1_session_proves_control_but_refuses_synthetic_timing(monkeypatch):
    bridge = _FakeBridge(_identity(adapter.TARGET_1))
    monkeypatch.setattr(adapter, "_ENGINE_FACTORY", lambda _target, _config: bridge)

    session = adapter.create_session(
        target=adapter.TARGET_1, config=_dispatch_config()
    )
    identity = session.runtime_identity()
    assert identity["measurement_capability"]["control_status"] == (
        adapter.ENGINE_CONTROL_READY
    )
    assert identity["measurement_capability"]["status"] == (
        adapter.TARGET_1_MEASUREMENT_REFUSED
    )
    identity["measurement_capability"]["status"] = "tampered"
    assert session.runtime_identity()["measurement_capability"]["status"] != "tampered"

    with pytest.raises(
        adapter.AdapterError, match=adapter.TARGET_1_MEASUREMENT_REFUSED
    ):
        session.measure_window(concurrency=8, steps=128, repeat_index=0)
    session.close()
    session.close()
    assert bridge.close_calls == 1


def test_target2_session_refuses_inferred_residency(monkeypatch):
    bridge = _FakeBridge(_identity(adapter.TARGET_2))
    monkeypatch.setattr(adapter, "_ENGINE_FACTORY", lambda _target, _config: bridge)
    monkeypatch.delenv("VLLM_USE_SIMPLE_KV_OFFLOAD", raising=False)

    session = adapter.create_session(
        target=adapter.TARGET_2, config=_longctx_config()
    )
    with pytest.raises(
        adapter.AdapterError, match=adapter.TARGET_2_MEASUREMENT_REFUSED
    ):
        session.measure(seq_len=1048576)
    session.close()


def test_target2_rejects_hidden_simple_connector_before_factory(monkeypatch):
    called = False

    def factory(_target, _config):
        nonlocal called
        called = True
        raise AssertionError("factory must not run")

    monkeypatch.setattr(adapter, "_ENGINE_FACTORY", factory)
    monkeypatch.setenv("VLLM_USE_SIMPLE_KV_OFFLOAD", "1")
    with pytest.raises(adapter.AdapterError, match="must be unset"):
        adapter.create_session(
            target=adapter.TARGET_2, config=_longctx_config()
        )
    assert called is False


@pytest.mark.parametrize(
    ("target", "config", "match"),
    [
        (adapter.TARGET_1, _dispatch_config(max_num_seqs=1), "frozen runtime"),
        (adapter.TARGET_1, _dispatch_config(max_num_seqs=8.0), "must be an integer"),
        (
            adapter.TARGET_2,
            _longctx_config(kv_offloading_backend="lmcache"),
            "frozen runtime",
        ),
        (
            adapter.TARGET_2,
            _longctx_config(kv_offloading_size_gb=64.0),
            "permits kv_offloading_size_gb",
        ),
    ],
)
def test_adapter_revalidates_owner_frozen_domain(target, config, match, monkeypatch):
    monkeypatch.delenv("VLLM_USE_SIMPLE_KV_OFFLOAD", raising=False)
    with pytest.raises(adapter.AdapterError, match=match):
        adapter.create_session(target=target, config=config)


def test_adapter_rejects_missing_and_extra_config_keys(monkeypatch):
    monkeypatch.delenv("VLLM_USE_SIMPLE_KV_OFFLOAD", raising=False)
    missing = _dispatch_config()
    missing.pop("max_num_batched_tokens")
    with pytest.raises(adapter.AdapterError, match="missing"):
        adapter.create_session(target=adapter.TARGET_1, config=missing)

    extra = _dispatch_config(unfrozen_knob=True)
    with pytest.raises(adapter.AdapterError, match="extra"):
        adapter.create_session(target=adapter.TARGET_1, config=extra)


def test_engine_kwargs_use_sanctioned_worker_extension_and_native_offload():
    dispatch = adapter._engine_kwargs(adapter.TARGET_1, _dispatch_config())
    assert dispatch["worker_extension_cls"] == adapter.WORKER_EXTENSION_CLS
    assert dispatch["max_num_seqs"] == 8
    assert dispatch["max_model_len"] == 4096
    assert dispatch["enforce_eager"] is True
    assert "kv_offloading_size" not in dispatch

    longctx = adapter._engine_kwargs(adapter.TARGET_2, _longctx_config())
    assert longctx["kv_offloading_size"] == 16.0
    assert longctx["kv_offloading_backend"] == "native"
    assert longctx["swap_space"] == 0.0


def test_backend_marker_parser_requires_all_three_canonical_lines():
    text = "\n".join(
        [
            "INFO Using FLASH_ATTN attention backend out of potential backends: (...) ",
            "INFO Using FlashInfer CUTLASS Unquantized MoE backend ",
            (
                "INFO engine kernel_config=KernelConfig(foo=1, "
                "enable_flashinfer_autotune=True)"
            ),
        ]
    )
    markers = adapter._parse_backend_markers(text)
    assert set(markers) == {
        "attention_backend",
        "fused_moe_backend",
        "kernel_backend",
    }
    with pytest.raises(adapter.AdapterError, match="kernel_backend"):
        adapter._parse_backend_markers(text.replace("enable_flashinfer_autotune=True", ""))


class _Backend(Enum):
    NATIVE = "native"


def test_resolved_config_is_read_from_vllm_config_not_echoed():
    requested = _longctx_config()
    vconfig = SimpleNamespace(
        model_config=SimpleNamespace(
            model=requested["model_path"],
            revision=adapter.EXPECTED_MODEL_REVISION,
            dtype="torch.bfloat16",
            max_model_len=1048576,
            enforce_eager=True,
        ),
        parallel_config=SimpleNamespace(
            tensor_parallel_size=1,
            pipeline_parallel_size=1,
            worker_extension_cls=adapter.WORKER_EXTENSION_CLS,
        ),
        scheduler_config=SimpleNamespace(
            max_num_seqs=1, max_num_batched_tokens=8192
        ),
        cache_config=SimpleNamespace(
            gpu_memory_utilization=0.97,
            enable_prefix_caching=True,
            kv_offloading_size=16.0,
            kv_offloading_backend=_Backend.NATIVE,
        ),
        offload_config=SimpleNamespace(uva=SimpleNamespace(cpu_offload_gb=0.0)),
    )
    llm = SimpleNamespace(llm_engine=SimpleNamespace(vllm_config=vconfig))
    resolved, provenance = adapter._resolved_config(
        llm, adapter.TARGET_2, requested
    )
    assert resolved == requested
    assert provenance["max_num_seqs"].endswith("scheduler_config.max_num_seqs")
    assert provenance["cpu_offload_gb"].endswith(
        "offload_config.uva.cpu_offload_gb"
    )
    assert "strips deprecated swap_space" in provenance["swap_space_gb"]


def test_real_bridge_shape_uses_lazy_llm_and_collective_rpc_fakes(monkeypatch):
    requested = _dispatch_config()
    captured = {}

    class Core:
        shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

    core = Core()
    vconfig = SimpleNamespace(
        model_config=SimpleNamespace(
            model=requested["model_path"],
            revision=adapter.EXPECTED_MODEL_REVISION,
            dtype="torch.bfloat16",
            max_model_len=4096,
            enforce_eager=True,
        ),
        parallel_config=SimpleNamespace(
            tensor_parallel_size=1,
            pipeline_parallel_size=1,
            worker_extension_cls=adapter.WORKER_EXTENSION_CLS,
        ),
        scheduler_config=SimpleNamespace(
            max_num_seqs=8, max_num_batched_tokens=1024
        ),
        cache_config=SimpleNamespace(
            gpu_memory_utilization=0.97,
            enable_prefix_caching=False,
        ),
        offload_config=SimpleNamespace(uva=SimpleNamespace(cpu_offload_gb=0.0)),
    )

    class LLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.llm_engine = SimpleNamespace(vllm_config=vconfig, engine_core=core)

        def collective_rpc(self, method, *, kwargs):
            assert method == "track_gpu_probe_capabilities"
            assert kwargs == {"target": adapter.TARGET_1}
            return [
                {
                    "control_status": adapter.ENGINE_CONTROL_READY,
                    "worker_extension_observed": True,
                    "worker_hook_observed": False,
                    "target_capability": {
                        "status": adapter.TARGET_1_MEASUREMENT_REFUSED,
                        "measurement_supported": False,
                        "refused_fields": ["T_move_ns"],
                        "reasons": ["FlashInferExperts.apply fused call"],
                    },
                }
            ]

    fake_vllm = ModuleType("vllm")
    fake_vllm.LLM = LLM
    fake_torch = ModuleType("torch")
    fake_torch.__version__ = adapter.EXPECTED_TORCH_VERSION
    fake_torch.version = SimpleNamespace(cuda="13.0")
    monkeypatch.setitem(sys.modules, "vllm", fake_vllm)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(adapter.importlib.metadata, "version", lambda _name: "0.23.0")
    monkeypatch.setattr(
        adapter,
        "_startup_markers",
        lambda: (_identity(adapter.TARGET_1)["backend_markers"], ["stdout.log"]),
    )
    monkeypatch.setattr(
        adapter,
        "_environment_identity",
        lambda _torch: {"VLLM_USE_SIMPLE_KV_OFFLOAD": None},
    )
    monkeypatch.setattr(adapter, "_model_identity", lambda _config: {"ok": True})

    bridge = adapter._VllmEngineBridge(adapter.TARGET_1, requested)
    identity = bridge.runtime_identity()
    assert captured["worker_extension_cls"] == adapter.WORKER_EXTENSION_CLS
    assert identity["resolved_config"] == requested
    assert identity["measurement_capability"]["status"] == (
        adapter.TARGET_1_MEASUREMENT_REFUSED
    )
    bridge.close()
    assert core.shutdown_calls == 1


def test_worker_extension_capability_is_not_mislabeled_as_measurement_hook(
    monkeypatch,
):
    monkeypatch.setattr(
        adapter,
        "_target1_worker_audit",
        lambda _worker: {
            "status": adapter.TARGET_1_MEASUREMENT_REFUSED,
            "measurement_supported": False,
            "refused_fields": ["T_move_ns"],
            "reasons": ["FlashInferExperts.apply fused call"],
        },
    )
    worker = SimpleNamespace(rank=0, local_rank=0)
    capability = adapter.TrackGpuWorkerExtension.track_gpu_probe_capabilities(
        worker, adapter.TARGET_1
    )
    assert capability["control_status"] == adapter.ENGINE_CONTROL_READY
    assert capability["worker_extension_observed"] is True
    assert capability["worker_hook_observed"] is False
    assert capability["target_capability"]["measurement_supported"] is False


def test_bad_bridge_identity_closes_engine(monkeypatch):
    bridge = _FakeBridge({"measurement_capability": {}})
    monkeypatch.setattr(adapter, "_ENGINE_FACTORY", lambda _target, _config: bridge)
    with pytest.raises(adapter.AdapterError, match="ENGINE_CONTROL_READY"):
        adapter.create_session(target=adapter.TARGET_1, config=_dispatch_config())
    assert bridge.close_calls == 1
