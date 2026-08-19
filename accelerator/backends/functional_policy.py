"""FUNCTIONAL_POLICY backend [ANALYTICAL].

Answers the functional question -- what work the candidate processor accepts, in
what order it completes, and what the resulting counters are -- WITHOUT claiming a
cycle-resolved timeline. It models only structural latency (pipeline latency +
datapath ops, issue-width limited), and deliberately ignores memory-bandwidth
contention. Use it for policy/ordering/counter correctness and as the coarse
lower-effort tier below CYCLE_RESOLVED_MODEL.

Fidelity: ANALYTICAL. It is NOT a measured surrogate and produces no benefit
claim; its timeline is coarse by construction and must not be read as calibrated.
"""
from __future__ import annotations

from accelerator.backends._core import QueuePipelineBackend
from accelerator.fidelity import Fidelity
from accelerator.resource_model import ResourceModel

BACKEND_NAME = "FUNCTIONAL_POLICY"


class FunctionalPolicyBackend(QueuePipelineBackend):
    """Structural-latency-only backend; bandwidth contention is intentionally omitted."""

    name = BACKEND_NAME

    def __init__(self, resources: ResourceModel, fidelity: Fidelity | str = Fidelity.ANALYTICAL) -> None:
        super().__init__(resources, fidelity)

    # service_cycles: base model (pipeline latency + op time). No bandwidth term:
    # FUNCTIONAL_POLICY does not model transfer contention -- that is the explicit
    # distinction from CYCLE_RESOLVED_MODEL below.


def factory(resources: ResourceModel, fidelity: Fidelity | str = Fidelity.ANALYTICAL) -> FunctionalPolicyBackend:
    return FunctionalPolicyBackend(resources, fidelity)
