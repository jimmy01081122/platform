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
import math
import random
import statistics
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
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise IREvaluationPointError(f"{where}: {value!r} is not exact-decimal") from exc
    if not parsed.is_finite():
        raise IREvaluationPointError(f"{where}: {value!r} must be finite")
    return value


def _int_str(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IREvaluationPointError(f"expected int, got {type(value).__name__}")
    return str(value)


def _number_str(value: int | float) -> str:
    """Serialize an already-aggregated numeric primary without truncation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IREvaluationPointError(
            f"expected finite number, got {type(value).__name__}"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise IREvaluationPointError("expected finite number")
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
    evidence_class: str,
    fidelity: str,
    measurement_noise_floor: str,
    bootstrap_ci_95: dict[str, str],
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
    measured_decimal = Decimal(_exact_decimal(measured_value, "measured_value"))
    if (
        isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or repetitions < MIN_REPETITIONS
    ):
        raise IREvaluationPointError(
            f"repetitions must be an observed integer >= {MIN_REPETITIONS}; "
            "the IR builder must never inflate it"
        )
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < repetitions
    ):
        raise IREvaluationPointError(
            "sample_count must be an observed integer >= repetitions"
        )
    if evidence_class not in {"SYNTHETIC", "MEASURED", "DERIVED"}:
        raise IREvaluationPointError(f"unsupported evidence_class {evidence_class!r}")
    if fidelity not in {
        "MEASURED",
        "CALIBRATED_SURROGATE",
        "ANALYTIC_FIRST_ORDER",
        "FUNCTIONAL_ONLY",
        "UNAVAILABLE",
    }:
        raise IREvaluationPointError(f"unsupported fidelity {fidelity!r}")
    if evidence_class == "SYNTHETIC" and fidelity == "MEASURED":
        raise IREvaluationPointError("synthetic evidence cannot claim MEASURED fidelity")
    noise_floor = Decimal(
        _exact_decimal(measurement_noise_floor, "measurement_noise_floor")
    )
    if noise_floor < 0:
        raise IREvaluationPointError("measurement_noise_floor must be non-negative")
    if not isinstance(bootstrap_ci_95, dict):
        raise IREvaluationPointError("bootstrap_ci_95 must be a mapping")
    try:
        ci_lower = Decimal(
            _exact_decimal(bootstrap_ci_95["lower"], "bootstrap_ci_95.lower")
        )
        ci_upper = Decimal(
            _exact_decimal(bootstrap_ci_95["upper"], "bootstrap_ci_95.upper")
        )
    except KeyError as exc:
        raise IREvaluationPointError(
            f"bootstrap_ci_95 missing {exc.args[0]!r}"
        ) from exc
    if ci_lower > ci_upper or not (ci_lower <= measured_decimal <= ci_upper):
        raise IREvaluationPointError(
            "bootstrap_ci_95 must be ordered and contain measured_value"
        )
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
        "evidence_class": evidence_class,
        "fidelity": fidelity,
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
        "measurement_noise_floor": measurement_noise_floor,
        "resampling_strata": resampling_strata,
        "bootstrap_seed": "0",
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_ci_95": dict(bootstrap_ci_95),
    }


# --------------------------------------------------------------------------- #
# probe result -> points (each built from the probe's own output; no join)
# --------------------------------------------------------------------------- #

def _envelope_dim(name: str, values: list[int]) -> dict[str, str]:
    return {"name": name, "lower": str(min(values)), "upper": str(max(values))}


def _linear_percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bootstrap_mean_ci(
    samples: list[float], *, seed: int = 0, resamples: int = BOOTSTRAP_RESAMPLES
) -> dict[str, str]:
    """Compute the declared deterministic bootstrap CI instead of fabricating one."""
    if len(samples) < MIN_REPETITIONS:
        raise IREvaluationPointError(
            f"bootstrap requires at least {MIN_REPETITIONS} observed repetitions"
        )
    if any(not math.isfinite(value) for value in samples):
        raise IREvaluationPointError("bootstrap samples must be finite")
    rng = random.Random(seed)
    count = len(samples)
    bootstrap_means = [
        statistics.fmean(samples[rng.randrange(count)] for _ in range(count))
        for _ in range(resamples)
    ]
    return {
        "lower": _number_str(_linear_percentile(bootstrap_means, 0.025)),
        "upper": _number_str(_linear_percentile(bootstrap_means, 0.975)),
    }


def longctx_result_to_points(result: dict[str, Any]) -> list[dict[str, Any]]:
    """A6 long-context: coordinate = [seq_len]; one point per (metric, seq_len)."""
    if result.get("primary_statistic") != "arithmetic_mean":
        raise IREvaluationPointError(
            "longctx IR requires arithmetic_mean primary values"
        )
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
    evidence = result.get("evidence")
    if evidence == "measured":
        evidence_class, fidelity = "MEASURED", "MEASURED"
    elif evidence == "cpu_smoke_test_not_measurement":
        evidence_class, fidelity = "SYNTHETIC", "FUNCTIONAL_ONLY"
    else:
        raise IREvaluationPointError(
            f"longctx result has unsupported evidence label {evidence!r}"
        )
    points: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("primary_statistic") != "arithmetic_mean":
            raise IREvaluationPointError(
                "longctx record has no arithmetic_mean primary"
        )
        coord = [{"name": "seq_len", "value": _int_str(int(rec["seq_len"]))}]
        for metric, unit, field in metrics:
            samples = [float(value) for value in rec[f"{field}_repeats"]]
            if len(samples) != int(rec["repeats"]):
                raise IREvaluationPointError(
                    f"longctx {field} sample count disagrees with repeats"
                )
            noise_floor = statistics.stdev(samples) if len(samples) > 1 else 0.0
            points.append(build_calibration_point(
                metric=metric, unit=unit,
                measured_value=_number_str(rec[field]),
                coordinate=coord, envelope_dimensions=[dim],
                runtime_variant_hash=rvh,
                repetitions=len(samples),
                sample_count=len(samples),
                resampling_strata=["a6-long-context"],
                evidence_class=evidence_class,
                fidelity=fidelity,
                measurement_noise_floor=_number_str(noise_floor),
                bootstrap_ci_95=_bootstrap_mean_ci(samples),
            ))
    return points


def dispatch_result_to_points(result: dict[str, Any]) -> list[dict[str, Any]]:
    """A2 dispatch: one honest aggregate per metric/concurrency.

    Each serving window is an independent repetition.  Steps within a window are
    samples, not extra repetitions.  This grouping prevents the prior behavior
    where every single step was mislabeled as three repetitions.
    """
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
    evidence = result.get("evidence")
    if evidence == "measured":
        evidence_class, fidelity = "MEASURED", "MEASURED"
    elif evidence == "cpu_smoke_test_not_measurement":
        evidence_class, fidelity = "SYNTHETIC", "FUNCTIONAL_ONLY"
    else:
        raise IREvaluationPointError(
            f"dispatch result has unsupported evidence label {evidence!r}"
        )
    points: list[dict[str, Any]] = []
    for g in groups:
        steps = g["per_step"]
        repeat_indices = sorted({int(step["repeat_index"]) for step in steps})
        if len(repeat_indices) < MIN_REPETITIONS:
            raise IREvaluationPointError(
                "dispatch IR requires at least three observed serving windows"
            )
        expert_tokens_values = {int(step["expert_tokens"]) for step in steps}
        concurrency_values = {int(step["concurrency"]) for step in steps}
        if len(expert_tokens_values) != 1 or concurrency_values != {int(g["concurrency"])}:
            raise IREvaluationPointError(
                "dispatch group does not have one stable operand-shape coordinate"
            )
        coord = [
            {"name": "expert_tokens", "value": _int_str(expert_tokens_values.pop())},
            {"name": "concurrency", "value": _int_str(int(g["concurrency"]))},
        ]
        for metric, unit, field in metrics:
            if any(field not in step for step in steps):
                raise IREvaluationPointError(
                    f"dispatch step missing break-even field {field!r}; "
                    "probe output is not PREP-2 complete"
                )
            per_window: list[float] = []
            for repeat_index in repeat_indices:
                window_samples = [
                    float(step[field])
                    for step in steps
                    if int(step["repeat_index"]) == repeat_index
                ]
                if not window_samples:
                    raise IREvaluationPointError(
                        f"dispatch repetition {repeat_index} has no {field} samples"
                    )
                per_window.append(statistics.fmean(window_samples))
            primary = statistics.fmean(per_window)
            noise_floor = (
                statistics.stdev(per_window) if len(per_window) > 1 else 0.0
            )
            points.append(build_calibration_point(
                metric=metric, unit=unit,
                measured_value=_number_str(primary),
                coordinate=coord, envelope_dimensions=dims,
                runtime_variant_hash=rvh,
                repetitions=len(per_window), sample_count=len(steps),
                resampling_strata=["a2-in-serving-dispatch-window"],
                evidence_class=evidence_class,
                fidelity=fidelity,
                measurement_noise_floor=_number_str(noise_floor),
                bootstrap_ci_95=_bootstrap_mean_ci(per_window),
            ))
    return points
