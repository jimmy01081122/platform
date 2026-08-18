"""STAGE_A1 refit: apply the corrected model-form functions in
``src/edgeflow/calibrated_backend.py`` to real evidence and report FIT-side
residuals.

This script does not invent a new calibration pipeline. It reuses
``fit_parameters()`` (unchanged fitting entry point) against the same
calibration-role raw benchmarks that produced the failed
``rtx-q0-fitted-parameters.json``, then:

  1. augments the PCIe parameters with a ``floor_ms`` term fit from the
     Aug-11 small-size transfer microbenchmark (defect 4);
  2. runs a self-check that the non-physical-fit rejection is still live;
  3. scores the corrected model against the same points that were scored in
     ``rtx-q1-validation-report.json`` (joined back to their raw records to
     recover the operand-shape features the original evaluation-point schema
     dropped), and reports MAPE/APE as FIT-side residuals only.

Per decision P-005 (see experiments/specs/cal_model_form_repair_v1.yaml),
every number produced here is FIT-side. Nothing here is a held-out judgment.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from edgeflow.calibrated_backend import (  # noqa: E402
    CalibrationError,
    METRICS,
    SHAPE_INSENSITIVE_OPERATIONS,
    _line_fit,
    fit_parameters,
    predict,
)

EVIDENCE = REPO_ROOT / "evidence"
CALIBRATION_RESULT = (
    EVIDENCE
    / "gpu_measurements/rtx-pro-6000-v3-20260718/results/rtx-pro-6000-calibration/result.json"
)
RESIDUAL_CHECK_RESULT = (
    EVIDENCE
    / "gpu_measurements/rtx-pro-6000-v3-20260718/results/cross-device-validation/result.json"
)
TRANSFER_BACKUP = (
    EVIDENCE
    / "measurement_backups/20260811T171000Z__phase7_remote_transfer_backup/raw/transfers"
)
OLD_Q1_REPORT = EVIDENCE / "gpu_measurements/rtx-pro-6000-v3-20260718/rtx-q1-validation-report.json"

OUT_DIR = REPO_ROOT / "calibration" / "fits" / "v2"

_EXPERT_TOKENS_RE = re.compile(r"expert_tokens=(\d+)")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Step 1: fit parameters from the calibration-role raw benchmarks (defects
# 2 and 3 are already handled by the updated fit_parameters()/predict() in
# calibrated_backend.py; this script does not reimplement that logic).
# ---------------------------------------------------------------------------


def fit_base_parameters() -> dict[str, Any]:
    calibration_result = _load(CALIBRATION_RESULT)
    return fit_parameters(calibration_result)


# ---------------------------------------------------------------------------
# Step 2: defect 4 — floor_ms from the Aug-11 small-size transfer sweep.
# Only XFER-L0 (local_pinned), 4KiB records with ci_rule_stable=True on
# gpu_duration_ns are used (guide §4.3: cpu_enqueue_ns is frequently unstable
# and must not be treated as equally trustworthy).
# ---------------------------------------------------------------------------


def fit_small_transfer_floor() -> dict[str, Any]:
    per_direction: dict[str, list[float]] = {"H2D": [], "D2H": []}
    unstable_excluded: dict[str, int] = {"H2D": 0, "D2H": 0}
    sources: list[str] = []
    for measurements_path in sorted(TRANSFER_BACKUP.glob("*-L/measurements.jsonl")):
        sources.append(str(measurements_path.relative_to(REPO_ROOT)))
        for line in measurements_path.read_text().splitlines():
            record = json.loads(line)
            if record.get("family") != "XFER-L0" or record.get("bytes_label") != "4KiB":
                continue
            direction = record.get("direction")
            if direction not in per_direction:
                continue
            gpu_ns = record["gpu_duration_ns"]
            if not gpu_ns.get("ci_rule_stable", False):
                unstable_excluded[direction] += 1
                continue
            per_direction[direction].append(gpu_ns["mean"] / 1e6)
    floor_ms = {
        direction: statistics.fmean(values) for direction, values in per_direction.items()
    }
    return {
        "floor_ms": {"h2d": floor_ms["H2D"], "d2h": floor_ms["D2H"]},
        "provenance": {
            "sources": sources,
            "family": "XFER-L0",
            "bytes_label": "4KiB",
            "host_memory": "local_pinned",
            "samples_used": {d: len(v) for d, v in per_direction.items()},
            "samples_excluded_unstable_ci": unstable_excluded,
            "selection_rule": "gpu_duration_ns.ci_rule_stable == true only; cpu_enqueue_ns not used (frequently unstable per guide 4.3)",
        },
    }


def apply_defect4(params: dict[str, Any], floor_fit: dict[str, Any]) -> dict[str, Any]:
    params = json.loads(json.dumps(params))  # deep copy
    for direction, floor_ms in floor_fit["floor_ms"].items():
        params["pcie"][direction]["floor_ms"] = floor_ms
    return params


# ---------------------------------------------------------------------------
# Step 3: self-check that non-physical fits are rejected. Two layers:
#   (a) the low-level _line_fit() primitive rejects flat/negative-slope/
#       negative-intercept fits; and
#   (b) the PRODUCTION entry point fit_parameters() actually propagates that
#       rejection for a non-excluded gpu_service operation instead of silently
#       falling back to a constant.
# Layer (b) was added after a Principal-Reviewer finding (2026-08-18): the
# earlier self-check only exercised _line_fit(), so it could not prove the
# production path rejected non-physical fits — and in fact it did not, because
# fit_parameters() caught the CalibrationError and substituted a flat constant.
# That fallback has been removed from calibrated_backend.py; this check guards
# against its regression. A pytest under tests/ would be the ideal home but is
# outside this stage's authorized edit scope; this runtime check is the
# auditable substitute, written to calibration/fits/v2/self_check.json.
# ---------------------------------------------------------------------------


def _minimal_calibration_result(gpu_service_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Smallest raw_benchmarks payload that satisfies fit_parameters()'s
    required-role checks, with a caller-supplied gpu_service probe set."""

    def row(rid: str, role: str, val: float, **fields: Any) -> dict[str, Any]:
        return {"record_id": rid, "calibration_role": role, "repeats_ms": [val, val], **fields}

    rows = [
        row("sc-cpu", "cpu_runtime", 0.1),
        row("sc-h2d-1", "pcie_transfer", 0.3, direction="h2d", bytes=100, copy_streams=1),
        row("sc-h2d-2", "pcie_transfer", 0.4, direction="h2d", bytes=200, copy_streams=1),
        row("sc-d2h-1", "pcie_transfer", 0.35, direction="d2h", bytes=100, copy_streams=1),
        row("sc-d2h-2", "pcie_transfer", 0.5, direction="d2h", bytes=200, copy_streams=1),
        row("sc-ce", "copy_engine", 0.24, direction="h2d", bytes=100, copy_streams=2),
        row("sc-mem-1", "memory", 0.1, bytes=100),
        row("sc-mem-2", "memory", 0.2, bytes=300),
        row("sc-q-1", "queueing", 0.05, queue_depth=1),
        row("sc-q-2", "queueing", 0.11, queue_depth=3),
        row("sc-ct", "contention", 1.1, concurrency=2, base_service_ms=1.0),
    ]
    rows.extend(gpu_service_rows)
    return {"raw_benchmarks": rows}


def self_check_non_physical_rejection() -> dict[str, Any]:
    checks = []

    # --- Layer (a): the _line_fit primitive itself ---
    try:
        _line_fit([(1.0, 5.0), (2.0, 5.0)], "self-check-flat")
        checks.append({"case": "line_fit_flat_slope", "rejected": False})
    except CalibrationError as exc:
        checks.append({"case": "line_fit_flat_slope", "rejected": True, "message": str(exc)})

    try:
        _line_fit([(1.0, 5.0), (2.0, 1.0)], "self-check-negative-slope")
        checks.append({"case": "line_fit_negative_slope", "rejected": False})
    except CalibrationError as exc:
        checks.append({"case": "line_fit_negative_slope", "rejected": True, "message": str(exc)})

    try:
        _line_fit([(10.0, 1.0), (20.0, 21.0)], "self-check-negative-intercept")
        checks.append({"case": "line_fit_negative_intercept", "rejected": False})
    except CalibrationError as exc:
        checks.append(
            {"case": "line_fit_negative_intercept", "rejected": True, "message": str(exc)}
        )

    # --- Layer (b): the production fit_parameters() path ---
    def gs(rid: str, val: float, operation: str, expert_tokens: int) -> dict[str, Any]:
        return {
            "record_id": rid,
            "calibration_role": "gpu_service",
            "repeats_ms": [val, val],
            "operation": operation,
            "case": f"self-check,expert_tokens={expert_tokens}",
        }

    # A non-excluded operation whose latency DECREASES with more tokens
    # (negative slope) must make the whole production fit raise.
    non_physical = _minimal_calibration_result(
        [gs("sc-gs-a", 1.0, "selected_expert", 100), gs("sc-gs-b", 0.1, "selected_expert", 200)]
    )
    try:
        fit_parameters(non_physical)
        checks.append({"case": "fit_parameters_non_physical_non_excluded", "rejected": False})
    except CalibrationError as exc:
        checks.append(
            {"case": "fit_parameters_non_physical_non_excluded", "rejected": True, "message": str(exc)}
        )

    # A non-excluded operation with only ONE distinct shape-axis value cannot
    # identify an affine fit. The production path must raise, not silently fall
    # back to a flat constant (P-012 follow-up: removed flat_fallback_single_
    # shape_group). Two rows at the same expert_tokens = a single shape group.
    single_shape = _minimal_calibration_result(
        [gs("sc-gs-e", 1.0, "selected_expert", 100), gs("sc-gs-f", 1.0, "selected_expert", 100)]
    )
    try:
        fit_parameters(single_shape)
        checks.append({"case": "fit_parameters_single_shape_non_excluded", "rejected": False})
    except CalibrationError as exc:
        checks.append(
            {"case": "fit_parameters_single_shape_non_excluded", "rejected": True, "message": str(exc)}
        )

    # Control: the SAME non-physical latencies, but for an excluded operation
    # (dequant), must NOT raise — it is a labeled flat-by-exclusion model, not a
    # failed affine fit. This confirms the exclusion is by-name, not by-catch.
    excluded_operation = next(iter(SHAPE_INSENSITIVE_OPERATIONS))
    excluded = _minimal_calibration_result(
        [
            gs("sc-gs-c", 1.0, excluded_operation, 100),
            gs("sc-gs-d", 0.1, excluded_operation, 200),
        ]
    )
    try:
        params = fit_parameters(excluded)
        form = params["gpu_service"]["operation_ms"][excluded_operation]["model_form"]
        checks.append(
            {
                "case": "fit_parameters_excluded_operation_is_flat_not_error",
                "rejected": False,
                "expected_no_raise": True,
                "model_form": form,
                "ok": form == "flat_by_registered_exclusion",
            }
        )
    except CalibrationError as exc:
        checks.append(
            {
                "case": "fit_parameters_excluded_operation_is_flat_not_error",
                "rejected": True,
                "expected_no_raise": True,
                "ok": False,
                "message": str(exc),
            }
        )

    rejection_cases = [c for c in checks if c["case"] != "fit_parameters_excluded_operation_is_flat_not_error"]
    control = next(c for c in checks if c["case"] == "fit_parameters_excluded_operation_is_flat_not_error")
    all_rejected = all(c["rejected"] for c in rejection_cases) and control.get("ok", False)
    return {
        "all_non_physical_cases_rejected": all_rejected,
        "production_path_covered": True,
        "cases": checks,
    }


# ---------------------------------------------------------------------------
# Step 4: residual analysis. Join the cross-device-validation raw_benchmarks
# back to its evaluation_points via record_id to recover expert_tokens /
# case-string shape info that the stripped evaluation-point schema does not
# carry, then score with the corrected predict(). Per decision P-005 this
# entire dataset is FIT-side, despite its original "validation" label.
# ---------------------------------------------------------------------------


def _augment_features(point: dict[str, Any], raw_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    features = dict(point["features"])
    if point["metric"] != "component_latency":
        return features
    raw = raw_by_id.get(point["source_record_id"])
    if raw is None:
        return features
    case = raw.get("case", "")
    match = _EXPERT_TOKENS_RE.search(case) if isinstance(case, str) else None
    if match:
        features = dict(features)
        features["tokens"] = float(match.group(1))
    return features


def score_residuals() -> dict[str, Any]:
    result = _load(RESIDUAL_CHECK_RESULT)
    raw_by_id = {r["record_id"]: r for r in result["raw_benchmarks"]}
    points = result["evaluation_points"]

    params = apply_defect4(fit_base_parameters(), fit_small_transfer_floor())

    rows = []
    for point in points:
        metric = point["metric"]
        features = _augment_features(point, raw_by_id)
        measured = point["measured"]
        try:
            predicted = predict(metric, features, params)
            error = None
        except CalibrationError as exc:
            predicted = None
            error = str(exc)
        raw = raw_by_id.get(point["source_record_id"], {})
        ape_pct = None
        if predicted is not None:
            ape_pct = abs(predicted - measured) / measured * 100.0
        rows.append(
            {
                "point_id": point["point_id"],
                "metric": metric,
                "measured": measured,
                "predicted": predicted,
                "ape_pct": ape_pct,
                "error": error,
                "domain": point.get("domain", {}),
                "case": raw.get("case"),
                "shape_augmented": features.get("tokens") if metric == "component_latency" else None,
            }
        )

    per_metric = {}
    for metric in METRICS:
        metric_rows = [r for r in rows if r["metric"] == metric and r["ape_pct"] is not None]
        errored_rows = [r for r in rows if r["metric"] == metric and r["error"] is not None]
        mape = statistics.fmean(r["ape_pct"] for r in metric_rows) if metric_rows else None
        old_mape = {
            "component_latency": 304.418,
            "moe_replay_tpot": 293.936,
            "pcie_transfer_latency": 66.879,
            "moe_replay_throughput": 60.658,
        }[metric]
        per_metric[metric] = {
            "fit_side_mape_pct": mape,
            "n_scored": len(metric_rows),
            "n_errored": len(errored_rows),
            "errors": [r["error"] for r in errored_rows],
            "old_model_validation_mape_pct_for_reference": old_mape,
            "note": "FIT-side residual only; old_model value is the immutable evidence-of-record from rtx-q1-validation-report.json, not a held-out comparison.",
        }

    return {
        "schema_version": "stage-a1-fit-side-residual-report-v1",
        "decision_p005": "all points below are FIT-side per decision P-005; none are held-out judgments",
        "parameters_used": params,
        "per_metric": per_metric,
        "points": rows,
    }


# ---------------------------------------------------------------------------
# Step 5: non-convergence / measurement-gap notes (honest reporting, guide
# step 5). gpu_service model_form is inspected directly from the fitted
# parameters to detect which operations fell back to a flat model.
# ---------------------------------------------------------------------------


def build_notes(params: dict[str, Any]) -> dict[str, Any]:
    non_converged = []
    for operation, model in params["gpu_service"]["operation_ms"].items():
        if model["model_form"] == "affine_per_token":
            continue
        if model["model_form"] == "flat_by_registered_exclusion":
            reason = (
                "deliberately excluded from the affine-in-tokens attempt (a-priori "
                "shape-insensitive). dequant's benchmark is an explicit synthetic proxy "
                "(synthetic_symmetric_int4_proxy_not_checkpoint_awq); its true driver is "
                "expert weight byte size, not token count. Modeled as a flat labeled "
                "PROXY_ONLY constant, NOT a caught non-physical fit. See GAP-1."
            )
        else:
            reason = "insufficient distinct shape groups in calibration-role evidence"
        non_converged.append(
            {
                "operation": operation,
                "model_form": model["model_form"],
                "reason": reason,
            }
        )

    measurement_gaps = [
        {
            "id": "GAP-1-DEQUANT-WEIGHT-BYTES",
            "description": (
                "dequant latency does not correlate with expert_tokens in the available "
                "calibration-role probes; its physical driver is expert weight byte size, "
                "which is not varied independently of expert_tokens in any evidence family "
                "used this stage. A dedicated weight-bytes sweep at fixed token count is "
                "needed to fit a true shape-aware dequant model."
            ),
            "feeds": "TRACK_GPU measurement_priority #4 (component service model operand-shape gaps)",
        },
        {
            "id": "GAP-2-AGGREGATE-CONTENTION-UNVALIDATED",
            "description": (
                "copy_engine.stream_latency_factors (reinterpreted as aggregate completion "
                "time / occupancy ratio) is still fit from the calibration-role copy_engine "
                "probes, but no evaluation point in either evidence file has a non-empty "
                "features.transfers list, so the aggregate model is never exercised end to "
                "end. Its correctness under defect 1's corrected interpretation is untested "
                "against real multi-stream aggregate-completion measurements."
            ),
            "feeds": "TRACK_GPU measurement_priority #4",
        },
        {
            "id": "GAP-3-SMALL-TRANSFER-FLOOR-SAMPLE-SIZE",
            "description": (
                "floor_ms is fit from 2-3 stable 4KiB samples per direction (XFER-L0, "
                "local_pinned) across three attempt directories. This is enough to remove "
                "the systematic 55-59% underestimate at small sizes but leaves floor_ms's "
                "own uncertainty unquantified (no CI reported here). Additional repeated "
                "small-size probes would narrow this."
            ),
            "feeds": "TRACK_GPU measurement_priority #4",
        },
        {
            "id": "GAP-5-MOE-REPLAY-TOKEN-NORMALIZATION-MISMATCH",
            "description": (
                "moe_replay_tpot improved from 293.936% (old) to ~43% FIT-side MAPE "
                "after the batching fix, but predicted tpot is still systematically "
                "~43% too fast (predicted total_ms is too low). Root cause appears to "
                "be a units mismatch, not a sign/direction error: the moe_replay "
                "evaluation points derive tokens_per_launch = tokens/cpu_calls (1 at "
                "concurrency=1, 4 at concurrency=4), while the gpu_service calibration "
                "probes' decode-phase expert_tokens are 8 at concurrency=1 and 32 at "
                "concurrency=4 -- an exactly 8x normalization difference between the "
                "two measurement campaigns' conventions for 'tokens per launch'. Even "
                "after accounting for the 8x count difference (which barely moves the "
                "affine prediction since per_token_ms is small relative to intercept), "
                "the measured per-call-pair cost implied by moe_replay's total_ms "
                "(~0.171 ms per grouped_gemm+gather_scatter pair at concurrency=1) is "
                "about 1.87x higher than what the calibration-role decode probes alone "
                "predict at matching expert_tokens=8 (~0.092 ms). This suggests "
                "moe_replay's 'cpu_calls' does not correspond 1:1 to a single "
                "grouped_gemm/gather_scatter launch pair in the calibration-role "
                "convention (possibly a different layer/expert grouping). Not "
                "resolved this stage; needs either explicit documentation of the "
                "cpu_calls<->launch-granularity mapping from the measurement harness, "
                "or a dedicated moe_replay-style probe that varies tokens_per_launch "
                "directly instead of composing two separately-calibrated probe "
                "families."
            ),
            "feeds": "TRACK_GPU measurement_priority #4",
        },
        {
            "id": "GAP-6-SMALL-TRANSFER-STREAM-INTERACTION-UNMODELED",
            "description": (
                "defect 1's fix (single-transfer latency independent of copy_streams) "
                "is strongly confirmed at the large-transfer scale the original "
                "diagnosis used (bytes=88080384: streams 1/2/4 all predict within 0.2% "
                "APE of measured, matching the guide's worked example almost exactly). "
                "But at bytes=65536 (small transfers), measured single-transfer latency "
                "*does* grow with copy_streams (streams=4 measured ~0.137ms vs the "
                "flat/floor prediction of ~0.028ms, APE ~79%) -- the opposite of the "
                "large-size behavior. This is a genuine, newly observed regime "
                "difference (small transfers appear overhead/launch-bound, where "
                "concurrent streams compete for launch/queue resources, vs large "
                "transfers which are bandwidth-bound and largely insensitive to stream "
                "count). This interaction is not modeled by the current fix -- the "
                "pre-registered defect 1 model form (stream-independent single-transfer "
                "latency) is only validated at large size; a size-dependent stream "
                "interaction term would be needed to close this gap and was not in "
                "scope for this stage's four registered defects."
            ),
            "feeds": "TRACK_GPU measurement_priority #4",
        },
        {
            "id": "GAP-4-COMPONENT-LATENCY-SHAPE-FEATURE-MISSING-AT-SOURCE",
            "description": (
                "Standalone component_latency evaluation points do not carry expert_tokens "
                "in their features dict at all (only gpu_operations counts); this script "
                "recovers it by joining back to the raw calibration-role record via "
                "source_record_id and parsing the case string. Any future evaluation-point "
                "generator (A2/A3 IR pipeline) should carry operand-shape features directly "
                "in the evaluation point rather than requiring this join."
            ),
            "feeds": "STAGE_A2 IR evaluation-point schema design",
        },
    ]

    return {"non_converged_operations": non_converged, "measurement_gaps": measurement_gaps}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    base_params = fit_base_parameters()
    floor_fit = fit_small_transfer_floor()
    params = apply_defect4(base_params, floor_fit)

    self_check = self_check_non_physical_rejection()
    if not self_check["all_non_physical_cases_rejected"]:
        raise SystemExit(
            "FATAL: non-physical fit rejection self-check failed; "
            "refusing to write fit outputs. " + json.dumps(self_check)
        )

    residuals = score_residuals()
    notes = build_notes(params)

    (OUT_DIR / "parameters.json").write_text(
        json.dumps(
            {
                "schema_version": "stage-a1-calibrated-backend-parameters-v2",
                "fit_split": "calibration (defects 1-3) + small-transfer floor sweep (defect 4)",
                "pre_registration": "experiments/specs/cal_model_form_repair_v1.yaml",
                "floor_fit_provenance": floor_fit["provenance"],
                **params,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    (OUT_DIR / "self_check.json").write_text(json.dumps(self_check, indent=2) + "\n")
    (OUT_DIR / "residual_report.json").write_text(
        json.dumps(residuals, indent=2, sort_keys=True) + "\n"
    )
    (OUT_DIR / "measurement_gaps.json").write_text(json.dumps(notes, indent=2) + "\n")

    print("wrote:", OUT_DIR / "parameters.json")
    print("wrote:", OUT_DIR / "self_check.json")
    print("wrote:", OUT_DIR / "residual_report.json")
    print("wrote:", OUT_DIR / "measurement_gaps.json")
    print()
    print("FIT-side MAPE (defect fix, vs old-model evidence-of-record):")
    for metric, report in residuals["per_metric"].items():
        print(
            f"  {metric}: new={report['fit_side_mape_pct']!r} "
            f"(n={report['n_scored']}, errored={report['n_errored']}) "
            f"old={report['old_model_validation_mape_pct_for_reference']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
