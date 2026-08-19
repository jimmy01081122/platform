"""CYCLE_RESOLVED_MODEL backend [ANALYTICAL].

Adds the terms FUNCTIONAL_POLICY omits: a memory-bandwidth service term and
issue-width/queue-limited occupancy, so the timeline reflects transfer time and
datapath throughput, not just structural latency. "Cycle-resolved" here means it
has a clock domain, a bounded queue (backpressure), and per-cycle issue/occupancy
-- matching the root-spec §3.1 CYCLE_RESOLVED *engine layer* meaning, but the
candidate-processor numbers are still ANALYTICAL (closed-form, no on-device fit).

Explicitly NOT cycle-accurate and NOT a measured surrogate. The bandwidth figure
is a swept ResourceModel parameter, never a fitted device constant.
"""
from __future__ import annotations

import math

from accelerator.abi import Transaction
from accelerator.backends._core import QueuePipelineBackend
from accelerator.fidelity import Fidelity
from accelerator.resource_model import ResourceModel

BACKEND_NAME = "CYCLE_RESOLVED_MODEL"


class CycleResolvedModelBackend(QueuePipelineBackend):
    """Pipeline latency + datapath ops + memory-bandwidth transfer time."""

    name = BACKEND_NAME

    def __init__(self, resources: ResourceModel, fidelity: Fidelity | str = Fidelity.ANALYTICAL) -> None:
        super().__init__(resources, fidelity)

    def service_cycles(self, txn: Transaction) -> int:
        # Structural latency + datapath op time (base terms) ...
        base = super().service_cycles(txn)
        # ... plus the bandwidth term: bytes / bandwidth converted to cycles.
        if txn.work_bytes > 0:
            transfer_fs = (txn.work_bytes * (10**15)) // self.resources.memory_bandwidth_bytes_per_s
            period = self.resources.cycle_period_fs
            transfer_cycles = math.ceil(transfer_fs / period) if period > 0 else 0
        else:
            transfer_cycles = 0
        return max(1, base + transfer_cycles)


def factory(resources: ResourceModel, fidelity: Fidelity | str = Fidelity.ANALYTICAL) -> CycleResolvedModelBackend:
    return CycleResolvedModelBackend(resources, fidelity)
