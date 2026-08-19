"""Shared queue+pipeline core implementing the six-verb ABI once.

The reference mock and both analytical backends are the SAME datapath semantics
with different service-time models -- exactly the discipline root spec §14.4
demands ("simulator/software/firmware/RTL must not use different algorithm
semantics while claiming cross-layer consistency"). So the queue, backpressure,
issue, in-flight pipeline, completion ordering, and counters live here once; a
subclass only overrides ``service_cycles`` (how long one transaction occupies the
datapath) and may override ``advance`` bandwidth accounting.

All timing is integer femtoseconds derived from the ResourceModel's rational
clock (root spec §4). Fidelity is ANALYTICAL/PROJECTED (enforced by the base
AcceleratorBackend); nothing here is a measured surrogate.
"""
from __future__ import annotations

import heapq
import math
from collections import deque

from accelerator.abi import (
    AcceleratorBackend,
    Backpressure,
    Completion,
    Counters,
    Transaction,
)
from accelerator.fidelity import Fidelity
from accelerator.resource_model import ResourceModel


class QueuePipelineBackend(AcceleratorBackend):
    """A cycle-stepped inbound-queue + fixed-latency pipeline.

    State:
      * ``_queue``     : inbound transactions awaiting issue (bounded, backpressure)
      * ``_inflight``  : min-heap of (complete_cycle, seq, Completion) issued not-yet-done
      * ``_done``      : completions ready to be polled (FIFO by completion then submit)
      * ``_cycle``     : current cycle index (integer)
    """

    def __init__(self, resources: ResourceModel, fidelity: Fidelity | str) -> None:
        super().__init__(resources, fidelity)
        self.reset()

    # --- overridable service model ----------------------------------------
    def service_cycles(self, txn: Transaction) -> int:
        """Cycles this transaction occupies the datapath (issue -> completion).

        Base model: fixed pipeline latency plus datapath-op time. Subclasses add
        bandwidth/queueing terms. Always >= 1 so a transaction cannot complete in
        the same instant it is issued.
        """
        op_cycles = math.ceil(txn.op_count / self.resources.operations_per_cycle)
        return max(1, self.resources.pipeline_latency_cycles + op_cycles)

    # --- the six verbs -----------------------------------------------------
    def reset(self) -> None:
        self._queue: deque[Transaction] = deque()
        self._inflight: list[tuple[int, int, Completion]] = []
        self._done: list[Completion] = []
        self._cycle: int = 0
        self._seq: int = 0
        self._counters = Counters()

    def can_accept(self, txn: Transaction) -> bool:
        return len(self._queue) < self.resources.queue_depth

    def submit(self, txn: Transaction) -> None:
        self._counters.submitted += 1
        if not self.can_accept(txn):
            self._counters.backpressure_events += 1
            raise Backpressure(
                f"queue full (depth={self.resources.queue_depth}); txn {txn.txn_id} refused"
            )
        self._counters.accepted += 1
        self._queue.append(txn)

    def advance(self, cycles: int) -> None:
        if cycles < 0:
            raise ValueError("cycles must be non-negative")
        for _ in range(cycles):
            self._step_one_cycle()
        self._counters.cycles_advanced += cycles
        self._counters.time_fs = self.resources.cycles_to_fs(self._cycle)

    def poll_completions(self) -> list[Completion]:
        # Drain everything already retired as of the current time, in submit order.
        self._retire_up_to_now()
        out = sorted(self._done, key=lambda c: (c.complete_time_fs, c.txn_id))
        self._done = []
        return out

    def snapshot_counters(self) -> Counters:
        return Counters(**self._counters.to_dict())

    # --- internals ---------------------------------------------------------
    def _step_one_cycle(self) -> None:
        # Issue up to issue_width transactions from the head of the queue.
        issued = 0
        while self._queue and issued < self.resources.issue_width:
            txn = self._queue.popleft()
            svc = self.service_cycles(txn)
            complete_cycle = self._cycle + svc
            submit_fs = self.resources.cycles_to_fs(self._cycle)
            complete_fs = self.resources.cycles_to_fs(complete_cycle)
            comp = Completion(
                txn_id=txn.txn_id,
                attachment_point=txn.attachment_point,
                submit_time_fs=submit_fs,
                complete_time_fs=complete_fs,
            )
            heapq.heappush(self._inflight, (complete_cycle, self._seq, comp))
            self._seq += 1
            issued += 1
            self._counters.busy_cycles += 1
        self._cycle += 1
        self._retire_up_to_now()

    def _retire_up_to_now(self) -> None:
        while self._inflight and self._inflight[0][0] <= self._cycle:
            _, _, comp = heapq.heappop(self._inflight)
            self._done.append(comp)
            self._counters.completed += 1
