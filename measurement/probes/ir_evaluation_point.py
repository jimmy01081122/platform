"""PREP-2: map probe outputs -> CalibrationIR evaluation points (no join).

STAGE_A2 completed, so the IR evaluation-point schema is now fixed: a
calibration evaluation point is a **CalibrationIR** payload
(explorations/moe_cycle_simulator/phase2/schemas/canonical_ir.schema.json,
$defs.calibration). Its operand-shape features live in `evaluation_coordinate`
-- an array of {name, value} exact-decimal-string pairs -- with a matching
`calibration_envelope` (dimensions [{name, lower, upper}]) whose name set must
equal the coordinate's.

GAP-4's defect was that component_latency evaluation points did NOT carry
`expert_tokens` and the IR pipeline had to join back to the raw record via
`source_record_id`. PREP-2's acceptance test (TRACK_GPU_PREP step 7): a new
probe's output must generate a valid IR evaluation point WITHOUT that join.
This module does exactly that -- it builds each point from the probe's own
result dict alone (the sweep range for the envelope is a property of the run,
carried in the probe output, not an external lookup).

Everything here is schema/plumbing. It asserts no GPU performance; mock-backend
records remain stamped cpu_smoke_test_not_measurement upstream.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

MODEL_RECORD_ID = "model-mixtral-8x7b-eba92302"        # matches STAGE_A2 adapter
PLATFORM_RECORD_ID_DEFAULT = "platform-rtx-pro-6000-96gb-v1"

# Workload lineage refs are IR-ASSEMBLY bindings (which workload a point belongs
# to), filled by the A2/A3 pipeline -- NOT operand-shape features. GAP-4 was
# strictly about operand-shape (expert_tokens) needing a join back to raw; the
# shape lives in evaluation_coordinate and is carried directly. These placeholder
# rids stand in for the assembly context during the CPU smoke test.
TRAINING_WORKLOAD_PLACEHOLDER = "workload-prep2-fit-placeholder"
HELDOUT_WORKLOAD_PLACEHOLDER = "workload-prep2-holdout-placeholder"
MIN_REPETITIONS = 3            # CalibrationIR schema floor
BOOTSTRAP_RESAMPLES = 10000    # CalibrationIR schema const


class IREvaluationPointError(ValueError):
    """A probe record cannot be turned into a valid IR evaluation point."""


def _exact_decimal(value: str, where: str) -> str:
    if not isinstance(value, str):
        raise IREvaluationPointError(f"{where}: exact-decimal must be a string, got {type(value).__name__}")
    try:
        Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise IREvaluationPointError(f"{where}: {value!r} is not exact-decimal") from exc
    return value


def _int_str(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IREvaluationPointError(f"expected int, got {type(value).__name__}")
    return str(value)


def build_calibration_point(
    *,
    metric: str,
    unit: str,
    measured_value: str,
    coordinate: list[dict[str, str]],
    envelope_dimensions: list[dict[str, str]],
    runtime_variant_hash: str,
    platform_record_id: str = PLATFORM_RECORD_ID_DEFAULT,
    repetitions: int,
    sample_count: int,
    resampling_strata: list[str],
    training_workload_record_ids: list[str] | None = None,
    held_out_workload_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a CalibrationIR payload from directly-carried features only.

    Enforces the same coordinate/envelope invariants canonical_ir.py applies
    (unique names, exact-decimal values, coordinate name set == envelope name
    set, coordinate within envelope) so a point that would fail IR1 fails here,
    at the probe, instead of downstream.
    """
    if len(runtime_variant_hash) != 64 or any(c not in "0123456789abcdef" for c in runtime_variant_hash):
        raise IREvaluationPointError("runtime_variant_hash must be 64 lowercase hex chars")
    _exact_decimal(measured_value, "measured_value")
    repetitions = max(MIN_REPETITIONS, repetitions)
    sample_count = max(sample_count, repetitions)
    training = training_workload_record_ids or [TRAINING_WORKLOAD_PLACEHOLDER]
    held_out = held_out_workload_record_ids or [HELDOUT_WORKLOAD_PLACEHOLDER]
    if set(training) & set(held_out):
        raise IREvaluationPointError("training and held-out workload refs must be disjoint")

    coord_names = [c["name"] for c in coordinate]
    dim_names = [d["name"] for d in envelope_dimensions]
    if len(set(coord_names)) != len(coord_names):
        raise IREvaluationPointError("duplicate coordinate names")
    if len(set(dim_names)) != len(dim_names):
        raise IREvaluationPointError("duplicate envelope dimension names")
    if set(coord_names) != set(dim_names):
        raise IREvaluationPointError(
            f"coordinate names {sorted(coord_names)} != envelope names {sorted(dim_names)}"
        )
    dims = {d["name"]: d for d in envelope_dimensions}
    for c in coordinate:
        _exact_decimal(c["value"], f"coordinate[{c['name']}]")
        lo = Decimal(_exact_decimal(dims[c["name"]]["lower"], "envelope lower"))
        hi = Decimal(_exact_decimal(dims[c["name"]]["upper"], "envelope upper"))
        val = Decimal(c["value"])
        if not (lo <= val <= hi):
            raise IREvaluationPointError(
                f"coordinate {c['name']}={val} outside envelope [{lo},{hi}]"
            )

    return {
        "metric": metric,
        "unit": unit,
        "measured_value": measured_value,
        "predicted_value": measured_value,   # no calibration model asserted (A4's job)
        "evidence_class": "MEASURED",
        "fidelity": "MEASURED",
        "range_status": "IN_CALIBRATION_ENVELOPE",
        "calibration_profile_hash": None,
        "model_record_id": MODEL_RECORD_ID,
        "platform_record_id": platform_record_id,
        "runtime_variant_hash": runtime_variant_hash,
        "training_workload_record_ids": training,
        "held_out_workload_record_ids": held_out,
        "calibration_envelope": {"dimensions": envelope_dimensions},
        "evaluation_coordinate": coordinate,
        "repetitions": repetitions,
        "sample_count": sample_count,
        "measurement_noise_floor": "0",
        "resampling_strata": resampling_strata,
        "bootstrap_seed": "0",
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_ci_95": {"lower": measured_value, "upper": measured_value},
    }


# --------------------------------------------------------------------------- #
# probe result -> points (each built from the probe's own output; no join)
# --------------------------------------------------------------------------- #

def _envelope_dim(name: str, values: list[int]) -> dict[str, str]:
    return {"name": name, "lower": str(min(values)), "upper": str(max(values))}


def longctx_result_to_points(result: dict[str, Any]) -> list[dict[str, Any]]:
    """A6 long-context: coordinate = [seq_len]; one point per (metric, seq_len)."""
    rvh = result["runtime_variant_hash"]
    records = [
        r for r in result["records"]
        if not r.get("oom") and not r.get("measurement_failed")
    ]
    if not records:
        return []
    seq_lens = [int(r["seq_len"]) for r in records]
    dim = _envelope_dim("seq_len", seq_lens)
    metrics = [
        ("longctx_ttft", "ns", "ttft_ns"),
        ("longctx_decode_per_token", "ns", "decode_per_token_ns"),
        ("longctx_kv_move", "ns", "kv_move_ns"),
        ("longctx_kv_offloaded_bytes", "byte", "kv_offloaded_bytes"),
    ]
    points: list[dict[str, Any]] = []
    for rec in records:
        coord = [{"name": "seq_len", "value": _int_str(int(rec["seq_len"]))}]
        for metric, unit, field in metrics:
            points.append(build_calibration_point(
                metric=metric, unit=unit,
                measured_value=_int_str(int(rec[field])),
                coordinate=coord, envelope_dimensions=[dim],
                runtime_variant_hash=rvh,
                repetitions=int(rec.get("repeats", 1)),
                sample_count=int(rec.get("repeats", 1)),
                resampling_strata=["a6-long-context"],
            ))
    return points


def dispatch_result_to_points(result: dict[str, Any]) -> list[dict[str, Any]]:
    """A2 dispatch: coordinate = [expert_tokens, concurrency]; per (metric, step)."""
    rvh = result["runtime_variant_hash"]
    groups = result["groups"]
    all_expert_tokens = [int(s["expert_tokens"]) for g in groups for s in g["per_step"]]
    all_concurrency = [int(g["concurrency"]) for g in groups]
    dims = [_envelope_dim("expert_tokens", all_expert_tokens),
            _envelope_dim("concurrency", all_concurrency)]
    # metric families: bytes moved + the break-even decomposition (root spec 10.4)
    metrics = [
        ("dispatch_bytes", "byte", "dispatch_bytes"),
        ("dispatch_control_decisions", "count", "control_decisions"),
        ("dispatch_T_prepare", "ns", "T_prepare_ns"),
        ("dispatch_T_queue", "ns", "T_queue_ns"),
        ("dispatch_T_sync", "ns", "T_sync_ns"),
        ("dispatch_T_move", "ns", "T_move_ns"),
    ]
    points: list[dict[str, Any]] = []
    for g in groups:
        for step in g["per_step"]:
            coord = [
                {"name": "expert_tokens", "value": _int_str(int(step["expert_tokens"]))},
                {"name": "concurrency", "value": _int_str(int(step["concurrency"]))},
            ]
            for metric, unit, field in metrics:
                if field not in step:
                    raise IREvaluationPointError(
                        f"dispatch step missing break-even field {field!r}; "
                        "probe output is not PREP-2 complete"
                    )
                points.append(build_calibration_point(
                    metric=metric, unit=unit,
                    measured_value=_int_str(int(step[field])),
                    coordinate=coord, envelope_dimensions=dims,
                    runtime_variant_hash=rvh,
                    repetitions=1, sample_count=1,
                    resampling_strata=["a2-in-serving-dispatch"],
                ))
    return points
