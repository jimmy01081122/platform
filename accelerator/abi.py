"""Six-verb candidate-processor backend ABI and its anti-forgery registry.

Root spec §6.3 fixes a stable backend ABI of exactly six verbs:

    reset   can_accept   submit   advance   poll_completions   snapshot_counters

and one hard rule, restated verbatim in the guide and AGENTS.md §3:

    未註冊的 backend 必須直接拒絕執行，不得靜默替換為較低 fidelity 的實作。
    (An un-registered backend must be refused outright; it must not be silently
     substituted by a lower-fidelity implementation. This is a deliberate
     anti-forgery design.)

This module supplies:

* transaction / completion / counter data types (the transaction adapter path);
* ``AcceleratorBackend``: the six-verb interface every backend implements;
* ``BackendRegistry``: dispatch that refuses un-registered backend names, mirroring
  the guard in src/edgeflow/multifidelity.py (which stays untouched).

The three downstream backends (RTL_TRACE_REPLAY, VERILATOR_COSIM,
RTL_CALIBRATED_SURROGATE) are declared as interface-only stubs in
``accelerator.backends`` and are deliberately NOT registered here, so asking for
one raises -- that is exactly the behaviour the guard test asserts.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from accelerator.fidelity import Fidelity, require_accelerator_fidelity
from accelerator.resource_model import ResourceModel


class BackendNotRegistered(ValueError):
    """A backend name was dispatched that is not in the registry (anti-forgery)."""


class Backpressure(RuntimeError):
    """submit() was called while the backend could not accept the transaction."""


@dataclass(frozen=True)
class Transaction:
    """One unit of work handed to the candidate processor (transaction adapter).

    ``attachment_point`` names which of A1..A6 this work unit belongs to.
    ``work_bytes`` and ``op_count`` are the sizing the cost models consume; both
    are ANALYTICAL inputs, never measured device counters.
    """

    txn_id: int
    attachment_point: str            # "A1".."A6"
    work_bytes: int                  # bytes to move for this unit (may be 0)
    op_count: int                    # datapath operations for this unit (may be 0)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Completion:
    """A completed transaction, tagged so the caller can match it to submission."""

    txn_id: int
    attachment_point: str
    submit_time_fs: int
    complete_time_fs: int

    @property
    def latency_fs(self) -> int:
        return self.complete_time_fs - self.submit_time_fs


@dataclass
class Counters:
    """Cumulative backend counters (the snapshot_counters path).

    These are model bookkeeping (submitted/completed/cycles/backpressure), NOT
    on-silicon performance counters. Fidelity is ANALYTICAL by construction.
    """

    submitted: int = 0
    accepted: int = 0
    completed: int = 0
    backpressure_events: int = 0
    cycles_advanced: int = 0
    time_fs: int = 0
    busy_cycles: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class AcceleratorBackend(abc.ABC):
    """The six-verb ABI. Every candidate-processor backend implements exactly this.

    A backend binds a ResourceModel (the nine swept parameters) and declares a
    fidelity label that MUST be ANALYTICAL or PROJECTED (enforced in __init__).
    """

    #: Stable backend name used for registration/dispatch.
    name: str = ""

    def __init__(self, resources: ResourceModel, fidelity: Fidelity | str) -> None:
        self.resources = resources
        # Hard isolation: reject MEASURED_SURROGATE (and every on-device label).
        self.fidelity = require_accelerator_fidelity(fidelity)

    # --- the six verbs -----------------------------------------------------
    @abc.abstractmethod
    def reset(self) -> None:
        """Return to initial state: clear queue, completions, counters, clock."""

    @abc.abstractmethod
    def can_accept(self, txn: Transaction) -> bool:
        """Backpressure: True iff submit(txn) would be accepted right now."""

    @abc.abstractmethod
    def submit(self, txn: Transaction) -> None:
        """Enqueue a transaction. Raise Backpressure if not can_accept(txn)."""

    @abc.abstractmethod
    def advance(self, cycles: int) -> None:
        """Step the clock by ``cycles`` accelerator cycles, doing pipeline work."""

    @abc.abstractmethod
    def poll_completions(self) -> list[Completion]:
        """Return and remove all completions ready as of the current time."""

    @abc.abstractmethod
    def snapshot_counters(self) -> Counters:
        """Return a copy of the cumulative counters."""


class BackendRegistry:
    """Dispatch only to explicitly registered backend factories (anti-forgery).

    Mirrors src/edgeflow/multifidelity.MultiFidelityDispatcher: an un-registered
    name raises BackendNotRegistered rather than falling back to any other
    implementation. The three downstream RTL/cosim backends are intentionally not
    registered, so requesting them is refused -- proving the guard holds.
    """

    def __init__(self) -> None:
        # name -> factory(resources, fidelity) -> AcceleratorBackend
        self._factories: dict[str, Any] = {}
        self._declared_unregistered: dict[str, str] = {}

    def register(self, name: str, factory: Any) -> None:
        if name in self._factories:
            raise ValueError(f"backend already registered: {name}")
        self._factories[name] = factory

    def declare_reserved(self, name: str, reason: str) -> None:
        """Record an interface-only backend that is deliberately NOT executable."""
        self._declared_unregistered[name] = reason

    def is_registered(self, name: str) -> bool:
        return name in self._factories

    def registered_names(self) -> list[str]:
        return sorted(self._factories)

    def reserved_names(self) -> list[str]:
        return sorted(self._declared_unregistered)

    def create(
        self, name: str, resources: ResourceModel, fidelity: Fidelity | str
    ) -> AcceleratorBackend:
        factory = self._factories.get(name)
        if factory is None:
            reserved = self._declared_unregistered.get(name)
            hint = (
                f" (reserved interface-only: {reserved})"
                if reserved is not None
                else ""
            )
            raise BackendNotRegistered(
                f"backend {name!r} is not registered; an unavailable "
                f"detailed/RTL/cosim backend cannot be silently substituted{hint}. "
                f"registered: {self.registered_names()}"
            )
        return factory(resources, fidelity)
