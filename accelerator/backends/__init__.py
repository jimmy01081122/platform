"""Backend package: builds the default registry with the anti-forgery guard.

Registered (executable this stage): FUNCTIONAL_POLICY, CYCLE_RESOLVED_MODEL,
REFERENCE_MOCK. Reserved (interface-only, NOT executable): RTL_TRACE_REPLAY,
VERILATOR_COSIM, RTL_CALIBRATED_SURROGATE -- declared reserved so dispatching them
raises BackendNotRegistered (root spec §6.3).
"""
from __future__ import annotations

from accelerator.abi import BackendRegistry
from accelerator.backends import (
    cycle_resolved_model,
    functional_policy,
    reference_mock,
    reserved,
)


def default_registry() -> BackendRegistry:
    reg = BackendRegistry()
    reg.register(functional_policy.BACKEND_NAME, functional_policy.factory)
    reg.register(cycle_resolved_model.BACKEND_NAME, cycle_resolved_model.factory)
    reg.register(reference_mock.BACKEND_NAME, reference_mock.factory)
    for name, reason in reserved.RESERVED.items():
        reg.declare_reserved(name, reason)
    return reg


__all__ = [
    "default_registry",
    "functional_policy",
    "cycle_resolved_model",
    "reference_mock",
    "reserved",
]
