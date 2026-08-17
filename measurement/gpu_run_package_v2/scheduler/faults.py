"""Scheduler fault taxonomy and deterministic test injection."""
from __future__ import annotations

import errno
from dataclasses import dataclass, field


class SchedulerError(RuntimeError):
    retryable = False


class RetryableFailure(SchedulerError):
    retryable = True


class CollectorUnavailable(SchedulerError):
    pass


class ValidationFailure(RetryableFailure):
    pass


class CollectorTimeout(RetryableFailure):
    pass


class TerminalCollectorFailure(SchedulerError):
    pass


class StorageExhausted(RetryableFailure):
    pass


class InjectedCrash(BaseException):
    """Simulated process death; intentionally bypasses Exception handlers."""


@dataclass
class FaultInjector:
    """Named one-shot fault points used only when explicitly configured."""

    faults: dict[str, BaseException] = field(default_factory=dict)

    def trigger(self, point: str) -> None:
        fault = self.faults.pop(point, None)
        if fault is not None:
            raise fault


def classify_os_error(error: OSError) -> SchedulerError:
    if error.errno == errno.ENOSPC:
        return StorageExhausted("disk space exhausted")
    return RetryableFailure(str(error))
