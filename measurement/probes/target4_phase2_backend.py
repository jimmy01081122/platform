"""CPU-smoke and live-CUDA backends for target_4 Phase 2 microbenchmarks.

This module imports no GPU library at module import time.  ``TorchTarget4Backend``
loads torch only when explicitly selected and raises ``BackendError`` if CUDA or
BF16 is unavailable; it never substitutes the deterministic mock backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Callable

try:
    from measurement.probes.mock_backend import BackendError
except ImportError:  # pragma: no cover - direct script execution fallback
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes.mock_backend import BackendError


def _synthetic_samples(mean_ms: float, repeats: int) -> list[float]:
    """Deterministic centered jitter for plumbing tests, never performance."""
    centre = (repeats - 1) / 2.0
    return [round(mean_ms * (1.0 + (index - centre) * 0.0005), 9)
            for index in range(repeats)]


@dataclass
class MockTarget4Backend:
    """Pure-CPU structural smoke backend with deliberately synthetic timings."""

    name: str = "mock"

    @property
    def evidence(self) -> str:
        return "cpu_smoke_test_not_measurement"

    @property
    def runtime_identity(self) -> dict[str, Any]:
        return {"backend": self.name, "runtime": "stdlib deterministic mock"}

    def measure_component(
        self, operation: str, expert_tokens: int, phase: str, repeats: int,
    ) -> dict[str, Any]:
        base = {
            "selected_expert": 0.080,
            "grouped_gemm": 0.085,
            "gather_scatter": 0.012,
            "dequant": 0.020,
        }[operation]
        phase_factor = 1.02 if phase == "prefill" else 1.0
        mean = phase_factor * (base + expert_tokens * 0.00002)
        return {
            "samples_ms": _synthetic_samples(mean, repeats),
            "inner_iterations": 1,
            "implementation": "deterministic_cpu_smoke_not_measurement",
        }

    def measure_sort_permute(
        self, operation: str, expert_tokens: int, repeats: int,
    ) -> dict[str, Any]:
        base = {
            "argsort_route": 0.008,
            "argsort_inverse": 0.009,
            "index_select_pack": 0.010,
            "index_select_unpack": 0.0105,
        }[operation]
        mean = base + expert_tokens * 0.00001
        return {
            "samples_ms": _synthetic_samples(mean, repeats),
            "inner_iterations": 1,
            "implementation": "deterministic_cpu_smoke_not_measurement",
        }

    def measure_dequant(
        self, weight_bytes: int, fixed_expert_tokens: int, repeats: int,
    ) -> dict[str, Any]:
        del fixed_expert_tokens
        mean = 0.015 + weight_bytes / 8.0e9
        return {
            "samples_ms": _synthetic_samples(mean, repeats),
            "inner_iterations": 1,
            "implementation": "deterministic_cpu_smoke_not_measurement",
        }


class _CudaTimer:
    def __init__(
        self,
        torch_mod: Any,
        *,
        warmup: int,
        repeats: int,
        minimum_inner_seconds: float,
    ) -> None:
        self.torch = torch_mod
        self.warmup = warmup
        self.repeats = repeats
        self.minimum_inner_seconds = minimum_inner_seconds

    def _elapsed_ms(self, fn: Callable[[], Any], iterations: int) -> float:
        start = self.torch.cuda.Event(enable_timing=True)
        end = self.torch.cuda.Event(enable_timing=True)
        start.record()
        retained = None
        for _ in range(iterations):
            retained = fn()
        end.record()
        end.synchronize()
        # Keep the final output alive through synchronization.
        if retained is None:  # pragma: no cover - all registered ops return tensors
            raise BackendError("CUDA microbenchmark operation returned no output")
        return float(start.elapsed_time(end))

    def run(self, fn: Callable[[], Any]) -> tuple[list[float], int]:
        try:
            for _ in range(self.warmup):
                fn()
            self.torch.cuda.synchronize()
            probe_ms = max(self._elapsed_ms(fn, 1), 1e-6)
            inner_iterations = max(
                1,
                min(
                    1_000_000,
                    math.ceil(self.minimum_inner_seconds * 1000.0 / probe_ms),
                ),
            )
            samples = [
                self._elapsed_ms(fn, inner_iterations) / inner_iterations
                for _ in range(self.repeats)
            ]
        except RuntimeError as exc:
            raise BackendError(f"CUDA target_4 microbenchmark failed: {exc}") from exc
        if any(not math.isfinite(value) or value <= 0 for value in samples):
            raise BackendError(f"CUDA emitted non-positive/non-finite latency: {samples}")
        return samples, inner_iterations


@dataclass
class TorchTarget4Backend:
    """Live CUDA implementation matching the frozen benchmark tensor geometry."""

    warmup: int = 10
    repeats: int = 5
    minimum_inner_seconds: float = 1.0
    hidden_size: int = 7168
    intermediate_size: int = 2048
    num_experts: int = 256
    seed: int = 20260718
    name: str = "gpu"
    _torch: Any = field(default=None, repr=False)
    _weights: tuple[Any, Any, Any] | None = field(default=None, init=False, repr=False)
    _scale: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self._torch is None:
            try:
                import torch
            except ImportError as exc:  # pragma: no cover - host dependent
                raise BackendError("torch is required for target_4 GPU probes") from exc
            self._torch = torch
        if not self._torch.cuda.is_available():
            raise BackendError("CUDA unavailable; refusing CPU/mock substitution")
        if not self._torch.cuda.is_bf16_supported():
            raise BackendError("BF16 unavailable; refusing to change the frozen dtype")
        if self.repeats <= 0 or self.warmup < 0 or self.minimum_inner_seconds <= 0:
            raise BackendError("warmup/repeats/minimum-inner-seconds are invalid")
        self._torch.manual_seed(self.seed)
        self._torch.cuda.manual_seed_all(self.seed)
        self._scale = self._torch.tensor(
            0.03125, device="cuda", dtype=self._torch.float16
        )

    @property
    def evidence(self) -> str:
        return "measured"

    @property
    def runtime_identity(self) -> dict[str, Any]:
        torch = self._torch
        props = torch.cuda.get_device_properties(0)
        return {
            "backend": self.name,
            "torch_version": str(torch.__version__),
            "torch_cuda_version": str(torch.version.cuda),
            "device_name": torch.cuda.get_device_name(0),
            "device_total_memory_bytes": int(props.total_memory),
            "activation_dtype": "bfloat16",
            "seed": self.seed,
        }

    def _timer(self) -> _CudaTimer:
        return _CudaTimer(
            self._torch,
            warmup=self.warmup,
            repeats=self.repeats,
            minimum_inner_seconds=self.minimum_inner_seconds,
        )

    def _ensure_weights(self) -> tuple[Any, Any, Any]:
        if self._weights is None:
            torch = self._torch
            self._weights = (
                torch.randn(
                    self.hidden_size,
                    self.intermediate_size,
                    device="cuda",
                    dtype=torch.bfloat16,
                ),
                torch.randn(
                    self.hidden_size,
                    self.intermediate_size,
                    device="cuda",
                    dtype=torch.bfloat16,
                ),
                torch.randn(
                    self.intermediate_size,
                    self.hidden_size,
                    device="cuda",
                    dtype=torch.bfloat16,
                ),
            )
        return self._weights

    def _route_and_order(self, expert_tokens: int) -> tuple[Any, Any, Any]:
        torch = self._torch
        route = torch.randint(
            0, self.num_experts, (expert_tokens,), device="cuda", dtype=torch.long
        )
        order = torch.argsort(route)
        inverse = torch.argsort(order)
        return route, order, inverse

    def measure_component(
        self, operation: str, expert_tokens: int, phase: str, repeats: int,
    ) -> dict[str, Any]:
        if repeats != self.repeats:
            raise BackendError("probe/backend repeats mismatch")
        if phase not in ("prefill", "decode") or expert_tokens <= 0:
            raise BackendError("invalid component shape axes")
        torch = self._torch
        gate, up, down = self._ensure_weights()
        _route, order, inverse = self._route_and_order(expert_tokens)
        activations = torch.randn(
            expert_tokens, self.hidden_size, device="cuda", dtype=torch.bfloat16
        )

        if operation == "selected_expert":
            def fn() -> Any:
                y = torch.nn.functional.silu(activations @ gate) * (activations @ up)
                return y @ down
            implementation = "shape_faithful_dense_expert_gemm"
        elif operation == "grouped_gemm":
            def fn() -> Any:
                grouped = activations.index_select(0, order)
                y = torch.nn.functional.silu(grouped @ gate) * (grouped @ up)
                return y @ down
            implementation = "route_ordered_dense_expert_gemm"
        elif operation == "gather_scatter":
            def fn() -> Any:
                return activations.index_select(0, order).index_select(0, inverse)
            implementation = "two_index_select_pack_unpack"
        elif operation == "dequant":
            packed = torch.randint(
                0,
                255,
                (expert_tokens, (self.hidden_size + 1) // 2),
                device="cuda",
                dtype=torch.uint8,
            )

            def fn() -> Any:
                low = (packed & 0x0F).to(torch.int8)
                high = (packed >> 4).to(torch.int8)
                low = torch.where(low >= 8, low - 16, low)
                high = torch.where(high >= 8, high - 16, high)
                return torch.stack((low, high), dim=-1).flatten(-2)[
                    ..., : self.hidden_size
                ].to(torch.float16) * self._scale
            implementation = "synthetic_symmetric_int4_proxy_not_checkpoint_awq"
        else:
            raise BackendError(f"unsupported component operation {operation!r}")

        samples, inner_iterations = self._timer().run(fn)
        return {
            "samples_ms": samples,
            "inner_iterations": inner_iterations,
            "implementation": implementation,
        }

    def measure_sort_permute(
        self, operation: str, expert_tokens: int, repeats: int,
    ) -> dict[str, Any]:
        if repeats != self.repeats or expert_tokens <= 0:
            raise BackendError("invalid sort/permute measurement axes")
        torch = self._torch
        route, order, inverse = self._route_and_order(expert_tokens)
        activations = torch.randn(
            expert_tokens, self.hidden_size, device="cuda", dtype=torch.bfloat16
        )
        if operation == "argsort_route":
            fn = lambda: torch.argsort(route)
            implementation = "torch_argsort_route_expert_ids"
        elif operation == "argsort_inverse":
            fn = lambda: torch.argsort(order)
            implementation = "torch_argsort_route_order_inverse"
        elif operation == "index_select_pack":
            fn = lambda: activations.index_select(0, order)
            implementation = "torch_index_select_route_pack"
        elif operation == "index_select_unpack":
            fn = lambda: activations.index_select(0, inverse)
            implementation = "torch_index_select_inverse_unpack"
        else:
            raise BackendError(f"unsupported sort/permute operation {operation!r}")
        samples, inner_iterations = self._timer().run(fn)
        return {
            "samples_ms": samples,
            "inner_iterations": inner_iterations,
            "implementation": implementation,
        }

    def measure_dequant(
        self, weight_bytes: int, fixed_expert_tokens: int, repeats: int,
    ) -> dict[str, Any]:
        if repeats != self.repeats or weight_bytes <= 0 or fixed_expert_tokens <= 0:
            raise BackendError("invalid dequant weight/token axes")
        torch = self._torch
        # The weight-byte axis is exact: uint8 has one byte per packed element.
        # Token count remains a separately recorded fixed control and does not
        # alter this proxy's packed-weight allocation.
        packed = torch.randint(
            0, 255, (weight_bytes,), device="cuda", dtype=torch.uint8
        )

        def fn() -> Any:
            low = (packed & 0x0F).to(torch.int8)
            high = (packed >> 4).to(torch.int8)
            low = torch.where(low >= 8, low - 16, low)
            high = torch.where(high >= 8, high - 16, high)
            return torch.stack((low, high), dim=-1).flatten().to(torch.float16) * self._scale

        samples, inner_iterations = self._timer().run(fn)
        return {
            "samples_ms": samples,
            "inner_iterations": inner_iterations,
            "implementation": "synthetic_symmetric_int4_proxy_weight_bytes_sweep",
        }


_REGISTRY = {"mock": MockTarget4Backend, "gpu": TorchTarget4Backend}


def registered_backends() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def resolve_backend(name: str):
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise BackendError(
            f"unregistered target_4 backend {name!r}; registered: {sorted(_REGISTRY)}"
        ) from exc

