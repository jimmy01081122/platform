#!/usr/bin/env python3
"""Cross-stage lease validation for the prepaid six-hour GPU allocation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from explorations.moe_cycle_simulator.phase7.application.executor.common import (
    M0Error,
)


TOTAL_ALLOCATION_SECONDS = 21_600
RELEASE_RESERVE_SECONDS = 900
D0_STAGE_SECONDS = 300
MATERIALIZATION_STAGE_SECONDS = 5_400
M0_STAGE_SECONDS = 14_400
D0_WORK_SECONDS = 240
MATERIALIZATION_WORK_SECONDS = 4_800
M0_WORK_SECONDS = 13_200


def validate_stage_envelopes() -> None:
    if (
        D0_STAGE_SECONDS
        + MATERIALIZATION_STAGE_SECONDS
        + M0_STAGE_SECONDS
        + RELEASE_RESERVE_SECONDS
        > TOTAL_ALLOCATION_SECONDS
    ):
        raise M0Error("stage envelopes do not fit the prepaid allocation")
    for stage, work in (
        (D0_STAGE_SECONDS, D0_WORK_SECONDS),
        (MATERIALIZATION_STAGE_SECONDS, MATERIALIZATION_WORK_SECONDS),
        (M0_STAGE_SECONDS, M0_WORK_SECONDS),
    ):
        if work <= 0 or work >= stage:
            raise M0Error("stage work deadline must leave a positive terminal reserve")


def parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise M0Error(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise M0Error(f"{field} is not a valid UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc or parsed.microsecond != 0:
        raise M0Error(f"{field} must have whole-second UTC precision")
    return parsed


def validate_allocation_window(window: Mapping[str, Any]) -> tuple[datetime, datetime]:
    validate_stage_envelopes()
    expected_keys = {
        "start_trigger",
        "lease_start_utc",
        "lease_deadline_utc",
        "total_seconds",
        "billing_mode",
        "extension_allowed",
        "additional_cost_allowed",
        "maximum_additional_spend_amount",
        "maximum_additional_spend_currency",
        "release_reserve_seconds",
    }
    if set(window) != expected_keys:
        raise M0Error("allocation-window key closure mismatch")
    if (
        window["start_trigger"] != "OWNER_RELEASES_FRESH_SSH_HANDOFF"
        or window["total_seconds"] != TOTAL_ALLOCATION_SECONDS
        or window["billing_mode"] != "PREPAID_FIXED_WINDOW"
        or window["extension_allowed"] is not False
        or window["additional_cost_allowed"] is not False
        or window["maximum_additional_spend_amount"] != "0"
        or window["maximum_additional_spend_currency"] != "TWD"
        or window["release_reserve_seconds"] != RELEASE_RESERVE_SECONDS
    ):
        raise M0Error("allocation-window authority differs from the owner decision")
    start = parse_utc(window["lease_start_utc"], "lease_start_utc")
    deadline = parse_utc(window["lease_deadline_utc"], "lease_deadline_utc")
    if int((deadline - start).total_seconds()) != TOTAL_ALLOCATION_SECONDS:
        raise M0Error("lease deadline must be exactly six hours after handoff")
    return start, deadline


def require_remaining_budget(
    window: Mapping[str, Any],
    *,
    stage_outer_seconds: int,
    downstream_reserve_seconds: int,
    now: datetime | None = None,
) -> int:
    if (
        isinstance(stage_outer_seconds, bool)
        or not isinstance(stage_outer_seconds, int)
        or stage_outer_seconds <= 0
        or isinstance(downstream_reserve_seconds, bool)
        or not isinstance(downstream_reserve_seconds, int)
        or downstream_reserve_seconds < 0
    ):
        raise M0Error("stage/reserve budget must be nonnegative whole seconds")
    start, deadline = validate_allocation_window(window)
    observed = now or datetime.now(timezone.utc).replace(microsecond=0)
    if observed.tzinfo != timezone.utc:
        raise M0Error("authoritative lease check must use UTC")
    if observed < start:
        raise M0Error("allocation lease has not started")
    remaining = int((deadline - observed).total_seconds())
    required = (
        stage_outer_seconds
        + downstream_reserve_seconds
        + RELEASE_RESERVE_SECONDS
    )
    if remaining < required:
        raise M0Error(
            "insufficient prepaid lease remains for the bounded stage: "
            f"remaining={remaining}, required={required}"
        )
    return remaining
