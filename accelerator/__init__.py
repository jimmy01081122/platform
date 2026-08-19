"""Parametric candidate support-processor model (Stage B2).

Root spec §6. Everything in this package is ANALYTICAL or PROJECTED -- never
MEASURED_SURROGATE (guide §4.4). No accelerator benefit / break-even claim is made
here; that is C1's job (guide §6).

Public surface:
  * resource_model.ResourceModel / ResourceSweep -- the nine swept parameters (§6.1)
  * abi.AcceleratorBackend + BackendRegistry     -- the six-verb ABI (§6.3)
  * backends.default_registry()                  -- FUNCTIONAL_POLICY, CYCLE_RESOLVED_MODEL,
                                                    REFERENCE_MOCK registered; three RTL/cosim
                                                    backends reserved (unregistered => refused)
  * attachment_points.default_attachment_points() -- A1..A6 (§6.2)
  * fidelity.require_accelerator_fidelity        -- the ANALYTICAL/PROJECTED gate
"""
from __future__ import annotations

from accelerator import abi, attachment_points, backends, fidelity, resource_model

__all__ = ["abi", "attachment_points", "backends", "fidelity", "resource_model"]
