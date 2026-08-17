"""Explicit, checked work-unit state transitions."""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    PENDING = "PENDING"
    PREFLIGHT = "PREFLIGHT"
    RUNNING = "RUNNING"
    RAW_SAVED = "RAW_SAVED"
    VALIDATING = "VALIDATING"
    COMPLETE = "COMPLETE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    SKIPPED = "SKIPPED"
    UNAVAILABLE = "UNAVAILABLE"


TERMINAL_STATES = frozenset({
    State.COMPLETE, State.FAILED_TERMINAL, State.SKIPPED, State.UNAVAILABLE,
})

TRANSITIONS: dict[State, frozenset[State]] = {
    State.PENDING: frozenset({
        State.PREFLIGHT, State.SKIPPED, State.UNAVAILABLE,
    }),
    State.PREFLIGHT: frozenset({
        State.RUNNING, State.FAILED_RETRYABLE, State.FAILED_TERMINAL,
        State.UNAVAILABLE,
    }),
    State.RUNNING: frozenset({
        State.RAW_SAVED, State.FAILED_RETRYABLE, State.FAILED_TERMINAL,
        State.UNAVAILABLE,
    }),
    State.RAW_SAVED: frozenset({
        State.VALIDATING, State.FAILED_RETRYABLE, State.FAILED_TERMINAL,
    }),
    State.VALIDATING: frozenset({
        State.COMPLETE, State.FAILED_RETRYABLE, State.FAILED_TERMINAL,
    }),
    State.FAILED_RETRYABLE: frozenset({
        State.PENDING, State.FAILED_TERMINAL, State.SKIPPED,
    }),
    State.COMPLETE: frozenset(),
    State.FAILED_TERMINAL: frozenset(),
    State.SKIPPED: frozenset(),
    State.UNAVAILABLE: frozenset(),
}


def transition_allowed(current: State, target: State) -> bool:
    return target in TRANSITIONS[current]


def require_transition(current: State, target: State) -> None:
    if not transition_allowed(current, target):
        raise ValueError(f"invalid scheduler transition: {current} -> {target}")
