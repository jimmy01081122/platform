"""P5_BASIC telemetry collector with per-field availability state."""
from __future__ import annotations

from typing import Any, Mapping

from .c1_contract import (
    CollectorRequest, CollectorResult, RecordCallback, TelemetryBackend,
    build_execution_alignment_key,
)


FIELDS = (
    "gpu_utilization_percent",
    "gpu_clock_mhz",
    "memory_clock_mhz",
    "power_watts",
    "temperature_celsius",
    "vram_used_bytes",
    "throttle_reason",
    "cpu_utilization_percent",
    "system_memory_used_bytes",
)


def _field(value: Any, reason: str | None) -> dict[str, Any]:
    if value is not None:
        return {"status": "observed", "value": value, "reason": None}
    return {
        "status": "unavailable_due_to_environment",
        "value": None,
        "reason": reason or "telemetry backend did not provide this field",
    }


def collect(
    backend: TelemetryBackend,
    request: CollectorRequest,
    *,
    sampling_interval_ms: int,
    emit: RecordCallback | None = None,
) -> CollectorResult:
    if sampling_interval_ms <= 0:
        raise ValueError("sampling_interval_ms must be positive")
    sample: Mapping[str, Any] = backend.sample()
    reasons = dict(sample.get("unavailable_reasons") or {})
    fields = {name: _field(sample.get(name), reasons.get(name)) for name in FIELDS}
    record = {
        "schema_version": "c1-telemetry-v1",
        "pass_id": "P5_BASIC",
        "execution_alignment_key": build_execution_alignment_key(request.execution),
        "sampling_interval_ms": sampling_interval_ms,
        "sample_index": sample.get("sample_index", 0),
        "monotonic_ns": sample.get("monotonic_ns"),
        "telemetry_start_monotonic_ns": sample.get(
            "telemetry_start_monotonic_ns", sample.get("monotonic_ns")
        ),
        "telemetry_end_monotonic_ns": sample.get(
            "telemetry_end_monotonic_ns", sample.get("monotonic_ns")
        ),
        "fields": fields,
        "sampling_failure": sample.get("sampling_failure"),
        "sample_count": sample.get("sample_count", 1),
        "failures": list(sample.get("failures") or []),
        "raw_artifact": sample.get("raw_artifact"),
    }
    result = CollectorResult("P5_BASIC")
    result.unavailable.update({
        name: state["reason"] for name, state in fields.items()
        if state["status"] != "observed"
    })
    result.add(record, emit)
    return result
