"""Reference mock backend: the minimal correct six-verb datapath.

Its sole job (root spec §6.3, guide §5) is to VALIDATE the five ABI paths --
transaction adapter, clock stepping, backpressure, completion, counter -- so the
two analytical backends and any downstream RTL/cosim backend can be checked
against a known-good reference. It makes no performance claim.

Service model: one cycle of pipeline latency per transaction plus op time, i.e.
the base QueuePipelineBackend model unchanged. Fidelity: ANALYTICAL.
"""
from __future__ import annotations

from accelerator.backends._core import QueuePipelineBackend
from accelerator.fidelity import Fidelity
from accelerator.resource_model import ResourceModel

BACKEND_NAME = "REFERENCE_MOCK"


class ReferenceMockBackend(QueuePipelineBackend):
    name = BACKEND_NAME

    def __init__(self, resources: ResourceModel, fidelity: Fidelity | str = Fidelity.ANALYTICAL) -> None:
        super().__init__(resources, fidelity)


def factory(resources: ResourceModel, fidelity: Fidelity | str = Fidelity.ANALYTICAL) -> ReferenceMockBackend:
    return ReferenceMockBackend(resources, fidelity)
