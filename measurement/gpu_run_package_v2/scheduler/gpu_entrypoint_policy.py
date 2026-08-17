"""Fail-closed inventory for every public GPU-capable package entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PACKAGE_ROOT / "configs/gpu_entrypoints_v1.json"
HARD_DISABLED_IDS = frozenset({
    "legacy_cuda_smoke",
    "legacy_cuda_experiment",
    "benchmark_smoke_gpu",
    "c1_model_smoke",
    "c1_run_start",
    "c1_run_resume",
    "c1_run_retry_failed",
    "c1_trace_run",
    "c1_diagnostic_run",
})
QUALIFICATION_ID = "g25_qualification_start"


class GpuEntrypointPolicyError(RuntimeError):
    pass


def load_gpu_entrypoint_policy() -> dict[str, Any]:
    try:
        value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GpuEntrypointPolicyError("GPU entrypoint policy is unreadable") from error
    expected_keys = {
        "$schema", "schema_version", "stage", "policy",
        "gpu_capable_entrypoints", "cpu_only_entrypoints", "fallback_policy",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema_version") != "g25-gpu-entrypoint-policy-v1"
        or value.get("stage") != "G2.5-S4-R6"
        or value.get("policy") != "single_attested_qualification_gpu_route"
        or value.get("fallback_policy")
        != "fail_closed_no_pgid_or_legacy_gpu_fallback"
    ):
        raise GpuEntrypointPolicyError("GPU entrypoint policy identity differs")
    rows = value.get("gpu_capable_entrypoints")
    if not isinstance(rows, list):
        raise GpuEntrypointPolicyError("GPU entrypoint inventory is not a list")
    by_id = {
        row.get("entrypoint_id"): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("entrypoint_id"), str)
    }
    if set(by_id) != HARD_DISABLED_IDS | {QUALIFICATION_ID} or len(by_id) != len(rows):
        raise GpuEntrypointPolicyError("GPU entrypoint inventory differs from code")
    if any(by_id[name].get("disposition") != "hard_disabled"
           for name in HARD_DISABLED_IDS):
        raise GpuEntrypointPolicyError("non-qualification GPU route is not disabled")
    if by_id[QUALIFICATION_ID].get("disposition") != "qualification_only":
        raise GpuEntrypointPolicyError("qualification route disposition differs")
    return value


def hard_disabled_reason(entrypoint_id: str) -> str:
    policy = load_gpu_entrypoint_policy()
    rows = {
        row["entrypoint_id"]: row for row in policy["gpu_capable_entrypoints"]
    }
    if entrypoint_id not in HARD_DISABLED_IDS:
        raise GpuEntrypointPolicyError(
            f"entrypoint is not a registered hard-disabled route: {entrypoint_id}"
        )
    if rows[entrypoint_id]["disposition"] != "hard_disabled":
        raise GpuEntrypointPolicyError("GPU entrypoint policy changed unexpectedly")
    return (
        f"S4-R6 hard-disables non-qualification GPU entrypoint: {entrypoint_id}"
    )


def assert_qualification_route() -> None:
    policy = load_gpu_entrypoint_policy()
    row = next(
        item for item in policy["gpu_capable_entrypoints"]
        if item["entrypoint_id"] == QUALIFICATION_ID
    )
    if row["disposition"] != "qualification_only":
        raise GpuEntrypointPolicyError("qualification GPU route is not exclusive")
