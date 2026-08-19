"""Interface-only downstream backends -- declared, deliberately NOT registered.

Root spec §6.3: RTL_TRACE_REPLAY, VERILATOR_COSIM, and RTL_CALIBRATED_SURROGATE
"only keep the interface, left for downstream". They exist so the ABI shape is
fixed now, but they are NOT executable in this platform: there is no RTL trace, no
Verilator build, and no calibrated surrogate here.

They are registered with the registry as *reserved* (declare_reserved), NOT as
factories. Dispatching one therefore raises BackendNotRegistered -- the
anti-forgery guard (root spec §6.3, §14.2). Instantiating one raises too, so a
stub can never masquerade as a working backend.
"""
from __future__ import annotations

from accelerator.abi import AcceleratorBackend, Completion, Counters, Transaction
from accelerator.resource_model import ResourceModel

RESERVED = {
    "RTL_TRACE_REPLAY": "replays a captured RTL trace; no trace exists in this platform",
    "VERILATOR_COSIM": "co-simulates against a Verilator build; not built here",
    "RTL_CALIBRATED_SURROGATE": "surrogate calibrated to RTL activity; no RTL activity captured",
}


class _ReservedBackend(AcceleratorBackend):
    """Never usable: every verb raises. Kept only to pin the interface shape."""

    def __init__(self, *_args, **_kwargs) -> None:  # noqa: D401 - not constructible
        raise NotImplementedError(
            f"{type(self).__name__} is an interface-only reserved backend; it is "
            "not implemented in this platform and cannot execute (root spec §6.3)."
        )

    def reset(self) -> None:  # pragma: no cover - unreachable, ctor raises
        raise NotImplementedError

    def can_accept(self, txn: Transaction) -> bool:  # pragma: no cover
        raise NotImplementedError

    def submit(self, txn: Transaction) -> None:  # pragma: no cover
        raise NotImplementedError

    def advance(self, cycles: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def poll_completions(self) -> list[Completion]:  # pragma: no cover
        raise NotImplementedError

    def snapshot_counters(self) -> Counters:  # pragma: no cover
        raise NotImplementedError


class RtlTraceReplayBackend(_ReservedBackend):
    name = "RTL_TRACE_REPLAY"


class VerilatorCosimBackend(_ReservedBackend):
    name = "VERILATOR_COSIM"


class RtlCalibratedSurrogateBackend(_ReservedBackend):
    name = "RTL_CALIBRATED_SURROGATE"
