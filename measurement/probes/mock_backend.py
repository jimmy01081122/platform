"""Deterministic CPU mock backend for GPU-prep probes.

Purpose: let the long-context KV runner and the in-serving dispatch probe walk
their *entire* code path on a pure-CPU host. The mock returns deterministic,
clearly-synthetic timing and byte-accounting values so that the probe's output
serialization and the downstream parser/validator can be exercised end to end.

It is NOT a performance model. Numbers are derived from simple closed forms
(byte counts from the frozen KV / expert facts in PLATFORM_FLOW_SPECIFICATION
§2.2, latencies from an arbitrary fixed clock) purely so the plumbing has
something structurally valid to serialize. Any result produced through this
backend is stamped ``evidence = "cpu_smoke_test_not_measurement"`` upstream.

The real GPU backend (vLLM / serving) is intentionally NOT implemented in this
track: TRACK_GPU_PREP is pure CPU and must never dispatch to a GPU, SSH, server
or serving process. The probes select the backend by name; the GPU backend is a
registered-but-unimplemented stub that raises if invoked here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Frozen platform facts (PLATFORM_FLOW_SPECIFICATION §2.2, SWAP-K2). These are
# used only to make the mock's byte accounting structurally consistent; they are
# NOT re-measured here.
KV_BYTES_PER_TOKEN = 131072  # 128 KiB/token; = 2,097,152 B block / 16 tokens
KV_BLOCK_BYTES = 2097152     # bytes_per_full_block (SWAP-K2)
KV_BLOCK_TOKENS = 16         # runtime_block_size_tokens (SWAP-K2)
EXPERT_OBJECT_BYTES = 352321536  # 336 MiB (OFF-E-PR3)
VRAM_BYTES = 96 * 1000**3    # 96 GB nominal; real free budget is measured, not assumed


class BackendError(RuntimeError):
    """Raised when a backend is asked to do something it must not do here."""


@dataclass
class MockClock:
    """Deterministic monotonic clock in nanoseconds (no wall-clock, no sleep)."""

    _now_ns: int = 0

    def advance(self, ns: int) -> int:
        if ns < 0:
            raise BackendError("clock cannot move backwards")
        self._now_ns += int(ns)
        return self._now_ns

    def now(self) -> int:
        return self._now_ns


@dataclass
class MockLongContextBackend:
    """CPU mock of a long-context serving backend with KV offload.

    Emits deterministic per-sequence-length outputs: TTFT, per-token latency,
    KV bytes resident vs offloaded, and offload hit/miss. The offload decision
    uses a configurable KV budget so the smoke test can exercise BOTH the
    "fits in VRAM" and the "must offload" branches without a GPU.
    """

    kv_budget_bytes: int
    # fixed synthetic clock rates -- arbitrary, only to produce ordered numbers
    ttft_base_ns: int = 90_000_000            # 90 ms floor
    ttft_per_token_ns: int = 40_000           # +40 us/prompt-token (synthetic)
    decode_base_ns: int = 20_000_000          # 20 ms/token floor
    offload_penalty_ns_per_block: int = 250_000  # synthetic per-offloaded-block cost
    kv_move_bandwidth_bytes_per_ns: float = 28.0  # ~28 GB/s (H2D order-of-magnitude)

    name: str = "mock_longctx"

    def measure(self, seq_len: int) -> dict[str, Any]:
        if seq_len <= 0:
            raise BackendError(f"seq_len must be positive, got {seq_len}")
        kv_total_bytes = seq_len * KV_BYTES_PER_TOKEN
        blocks_total = (seq_len + KV_BLOCK_TOKENS - 1) // KV_BLOCK_TOKENS
        resident_bytes = min(kv_total_bytes, self.kv_budget_bytes)
        offloaded_bytes = kv_total_bytes - resident_bytes
        offloaded_blocks = (offloaded_bytes + KV_BLOCK_BYTES - 1) // KV_BLOCK_BYTES
        must_offload = offloaded_bytes > 0

        ttft_ns = self.ttft_base_ns + seq_len * self.ttft_per_token_ns
        decode_ns = self.decode_base_ns + offloaded_blocks * self.offload_penalty_ns_per_block
        kv_move_ns = (
            int(offloaded_bytes / self.kv_move_bandwidth_bytes_per_ns)
            if offloaded_bytes
            else 0
        )
        return {
            "seq_len": seq_len,
            "kv_total_bytes": kv_total_bytes,
            "kv_blocks_total": blocks_total,
            "kv_resident_bytes": resident_bytes,
            "kv_offloaded_bytes": offloaded_bytes,
            "kv_offloaded_blocks": offloaded_blocks,
            "offload_engaged": must_offload,
            "ttft_ns": ttft_ns,
            "decode_per_token_ns": decode_ns,
            "kv_move_ns": kv_move_ns,
            "kv_move_bytes": offloaded_bytes,
            "oom": False,
        }


@dataclass
class MockDispatchBackend:
    """CPU mock of an in-serving MoE dispatch instrumentation point.

    Reports, per decode step, the *system-level* movement and control structure
    that the existing same-device gather_scatter proxy (benchmark.py:463) does
    NOT capture: bytes moved per dispatch, granularity, dispatch frequency, and
    the number of control decisions.
    """

    hidden: int = 4096            # activation width used for byte accounting
    routing_width: int = 8        # numel(flatten(decode selected_experts)); trace fact
    dtype_bytes: int = 2          # bf16 activations
    step_period_ns: int = 20_000_000  # synthetic decode step period
    name: str = "mock_dispatch"

    def measure_step(self, step_index: int, concurrency: int) -> dict[str, Any]:
        if concurrency <= 0:
            raise BackendError(f"concurrency must be positive, got {concurrency}")
        expert_tokens = self.routing_width * concurrency
        # gather (permute in) + scatter (permute out) => two moves of the
        # activation matrix per dispatch.
        bytes_per_move = expert_tokens * self.hidden * self.dtype_bytes
        dispatch_bytes = 2 * bytes_per_move
        # one routing/permutation control decision per expert-token, per direction
        control_decisions = 2 * expert_tokens
        return {
            "step_index": step_index,
            "concurrency": concurrency,
            "expert_tokens": expert_tokens,
            "dispatch_bytes": dispatch_bytes,
            "move_granularity_bytes": bytes_per_move,
            "dispatch_period_ns": self.step_period_ns,
            "control_decisions": control_decisions,
        }


class GpuBackendStub:
    """Registered-but-unimplemented real GPU backend.

    Mirrors PLATFORM_FLOW_SPECIFICATION §6.3 anti-spoofing: an unavailable
    backend must refuse loudly, never silently degrade to a lower-fidelity path.
    In TRACK_GPU_PREP invoking it is a hard error -- this track is pure CPU.
    """

    name = "gpu"

    def __getattr__(self, _name: str):  # pragma: no cover - defensive
        raise BackendError(
            "GPU backend is not runnable in TRACK_GPU_PREP (pure CPU). "
            "Real measurement belongs to TRACK_GPU."
        )


_REGISTRY = {
    "mock_longctx": MockLongContextBackend,
    "mock_dispatch": MockDispatchBackend,
    "gpu": GpuBackendStub,
}


def registered_backends() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def resolve_backend(name: str):
    if name not in _REGISTRY:
        raise BackendError(
            f"unregistered backend {name!r}; registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]
