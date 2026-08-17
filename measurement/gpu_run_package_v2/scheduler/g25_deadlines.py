"""Frozen G2.5 session deadline semantics with an injectable monotonic clock."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class G25DeadlineContract:
    cell_timeout_seconds: int = 480
    term_grace_seconds: int = 30
    latest_new_dispatch_elapsed_seconds: int = 5790
    execution_cutoff_elapsed_seconds: int = 6300
    session_hard_deadline_seconds: int = 7200
    outer_command_timeout_seconds: int = 7500

    def validate(self) -> None:
        if self.latest_new_dispatch_elapsed_seconds + (
            self.cell_timeout_seconds + self.term_grace_seconds
        ) != self.execution_cutoff_elapsed_seconds:
            raise ValueError("G2.5 dispatch/worker cutoff arithmetic changed")
        if self.session_hard_deadline_seconds - self.execution_cutoff_elapsed_seconds != 900:
            raise ValueError("G2.5 post-execution reserve must be exactly 900 seconds")
        if self.outer_command_timeout_seconds - self.session_hard_deadline_seconds != 300:
            raise ValueError("G2.5 outer safety reserve must be exactly 300 seconds")


class G25DeadlineTracker:
    def __init__(self, clock: Callable[[], float], *, started: float | None = None):
        self.contract = G25DeadlineContract()
        self.contract.validate()
        self.clock = clock
        self.started = clock() if started is None else float(started)

    def elapsed(self) -> float:
        value = float(self.clock()) - self.started
        if value < 0:
            raise RuntimeError("monotonic clock moved backwards")
        return value

    def may_dispatch(self) -> bool:
        return self.elapsed() < self.contract.latest_new_dispatch_elapsed_seconds

    def execution_cutoff_reached(self) -> bool:
        return self.elapsed() >= self.contract.execution_cutoff_elapsed_seconds

    def hard_deadline_reached(self) -> bool:
        return self.elapsed() >= self.contract.session_hard_deadline_seconds

    def phase(self) -> str:
        elapsed = self.elapsed()
        if elapsed < self.contract.latest_new_dispatch_elapsed_seconds:
            return "DISPATCH_ALLOWED"
        if elapsed < self.contract.execution_cutoff_elapsed_seconds:
            return "NO_NEW_DISPATCH_TERMINATE_ACTIVE_WORKER"
        if elapsed < self.contract.session_hard_deadline_seconds:
            return "FINALIZATION_AND_AUDIT_ONLY"
        if elapsed < self.contract.outer_command_timeout_seconds:
            return "INTERNAL_HARD_DEADLINE_EXPIRED"
        return "OUTER_TIMEOUT_EXPIRED"
