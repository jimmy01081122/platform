"""Injectable monotonic clocks and session time-budget policy."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """A manually advanced clock; tests never sleep."""

    def __init__(self, initial: float = 0.0) -> None:
        self._now = float(initial)

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self._now += seconds


@dataclass(frozen=True)
class TimeBudget:
    session_minutes: float = 120.0
    stop_dispatch_before_end_minutes: float = 15.0
    packaging_reserve_minutes: float = 15.0

    def __post_init__(self) -> None:
        values = (
            self.session_minutes,
            self.stop_dispatch_before_end_minutes,
            self.packaging_reserve_minutes,
        )
        if any(value < 0 for value in values):
            raise ValueError("time-budget values cannot be negative")
        if self.stop_dispatch_before_end_minutes > self.session_minutes:
            raise ValueError("stop-dispatch reserve exceeds session")
        if self.packaging_reserve_minutes > self.session_minutes:
            raise ValueError("packaging reserve exceeds session")

    @property
    def dispatch_deadline_seconds(self) -> float:
        return (
            self.session_minutes - self.stop_dispatch_before_end_minutes
        ) * 60.0

    def can_dispatch(self, elapsed_seconds: float) -> bool:
        return elapsed_seconds < self.dispatch_deadline_seconds

    def remaining_seconds(self, elapsed_seconds: float) -> float:
        return max(0.0, self.session_minutes * 60.0 - elapsed_seconds)
