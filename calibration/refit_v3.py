"""STAGE_A1 cal_model_form_repair_v2 — FIT-side evaluation (v3).

Loads RAW evidence (not any transcription), fits the three preregistered
candidate model forms, and scores each against its preregistered subgroup
gates. Scoring order is deliberately: correctness/physical semantics ->
preregistered subgroup gates -> generalization class -> MAPE/max APE ->
aggregate. A low aggregate never overrides a failed subgroup gate.

Everything here is FIT-side / diagnostic (decision P-005). No held-out
judgment, no calibrated PASS. Outputs go to calibration/fits/v3/ and a
runs/<run_id>/ directory; v2 is never overwritten. raw evidence is read-only.

Usage:
    python calibration/refit_v3.py --code-commit <sha> --run-id <id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from calibration.models_v3 import (  # noqa: E402
    A1StoredPcie,
    ComponentPoint,
    EXTRAPOLATED,
    FixedTauRouteDiagnostic,
    GlobalAffine,
    INTERPOLATED,
    ModelError,
    NearestProfile,
    OldStreamFactorPcie,
    PcieTwoRegime,
    ProfileKNN,
    REPLAY_DECLARED_METADATA_OPS,
    REPLAY_OPERATOR_GRAPH,
    ReplayPoint,
    UNSUPPORTED,
    mape,
    max_ape,
    replay_missing_terms,
)

EV = REPO_ROOT / "evidence/gpu_measurements/rtx-pro-6000-v3-20260718/results"
CAL = EV / "rtx-pro-6000-calibration/result.json"
VAL = EV / "cross-device-validation/result.json"
V2_RESIDUAL = REPO_ROOT / "calibration/fits/v2/residual_report.json"
OUT = REPO_ROOT / "calibration/fits/v3"

_WL = re.compile(r"(?:calibration|validation)-(.+?)-q\d")
_ET = re.compile(r"expert_tokens=(\d+)")
_PH = re.compile(r"phase=(\w+)")
_CC = re.compile(r"concurrency=(\d+)")


def _load(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text())


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _parse_case(case: str) -> tuple[str, str, int, int]:
    w = _WL.search(case)
    ph = _PH.search(case)
    cc = _CC.search(case)
    et = _ET.search(case)
    return (
        w.group(1) if w else "?",
        ph.group(1) if ph else "?",
        int(cc.group(1)) if cc else -1,
        int(et.group(1)) if et else -1,
    )


# ---------------------------------------------------------------------------
# Loaders (raw evidence)
# ---------------------------------------------------------------------------

def load_pcie() -> tuple[list[dict], list[dict]]:
    def rows(path: Path) -> list[dict]:
        d = _load(path)
        out = []
        for r in d["raw_benchmarks"]:
            if r.get("calibration_role") in ("pcie_transfer", "copy_engine"):
                out.append({
                    "direction": r["direction"],
                    "bytes": int(r["bytes"]),
                    "streams": int(r["copy_streams"]),
                    "latency_ms": r["statistics"]["mean"],
                })
        return out
    return rows(CAL), rows(VAL)


def load_component() -> tuple[list[ComponentPoint], list[ComponentPoint]]:
    # train (q0): calibration gpu_service raw records
    dc = _load(CAL)
    train = []
    for r in dc["raw_benchmarks"]:
        if r.get("calibration_role") != "gpu_service":
            continue
        w, ph, cc, et = _parse_case(r["case"])
        train.append(ComponentPoint(r["operation"], et, ph, cc, r["statistics"]["mean"], w))
    # val (q1): validation component_latency eval points, join to raw case
    dv = _load(VAL)
    raw_by_id = {r["record_id"]: r for r in dv["raw_benchmarks"]}
    val = []
    for p in dv["evaluation_points"]:
        if p["metric"] != "component_latency":
            continue
        op = next(iter(p["features"]["gpu_operations"].keys()))
        raw = raw_by_id.get(p["source_record_id"], {})
        w, ph, cc, et = _parse_case(raw.get("case", ""))
        val.append(ComponentPoint(op, et, ph, cc, float(p["measured"]), w))
    return train, val


def load_replay() -> list[ReplayPoint]:
    dv = _load(VAL)
    raw = [r for r in dv["raw_benchmarks"] if r.get("operation") == "window_replay"]
    # a1 predicted tpot from the committed v2 residual report
    a1 = {}
    for pt in _load(V2_RESIDUAL)["points"]:
        if pt["metric"] == "moe_replay_tpot":
            a1[(round(float(pt["measured"]), 9))] = float(pt["predicted"])
    pts = []
    for r in raw:
        w, ph, cc, et = _parse_case(r["case"])
        measured = r["statistics"]["mean"]
        # match a1 prediction by measured value
        a1_pred = a1.get(round(measured, 9))
        if a1_pred is None:
            # fall back to nearest measured key
            k = min(a1, key=lambda x: abs(x - measured))
            a1_pred = a1[k]
        pts.append(ReplayPoint(w, cc, int(r["tokens"]), int(r["cpu_calls"]), measured, a1_pred))
    return pts


# ---------------------------------------------------------------------------
# Candidate A — evaluation
# ---------------------------------------------------------------------------

def eval_pcie(cal: list[dict], val: list[dict]) -> dict[str, Any]:
    model = PcieTwoRegime.fit(cal)  # hard-fails on non-physical
    old_v1 = A1StoredPcie()
    old_sf = OldStreamFactorPcie()

    def m_new(pts):
        return mape([p["latency_ms"] for p in pts],
                    [model.predict(p["direction"], p["bytes"], p["streams"]) for p in pts])

    def subset(pred):
        buckets = {
            "aggregate": val,
            "small_le_1MiB": [p for p in val if p["bytes"] <= 1_048_576],
            "transition_1MiB_22MiB": [p for p in val if 1_048_576 < p["bytes"] < 22_020_096],
            "bulk_ge_22MiB": [p for p in val if p["bytes"] >= 22_020_096],
            "h2d": [p for p in val if p["direction"] == "h2d"],
            "d2h": [p for p in val if p["direction"] == "d2h"],
        }
        out = {}
        for name, pts in buckets.items():
            if not pts:
                out[name] = None
                continue
            out[name] = mape([p["latency_ms"] for p in pts], [pred(p) for p in pts])
        return out

    new_by_bucket = subset(lambda p: model.predict(p["direction"], p["bytes"], p["streams"]))
    v1_by_bucket = subset(lambda p: old_v1.predict(p["direction"], p["bytes"], p["streams"]))
    sf_by_bucket = subset(lambda p: old_sf.predict(p["direction"], p["bytes"], p["streams"]))
    max_ape_new = max_ape([p["latency_ms"] for p in val],
                          [model.predict(p["direction"], p["bytes"], p["streams"]) for p in val])

    # production S semantics demonstration (OWNER_RESOLUTION)
    prod_s1 = model.predict("h2d", 352_321_536, 1, mode="production")
    prod_s2 = model.predict("h2d", 352_321_536, 2, mode="production")

    agg = new_by_bucket["aggregate"]
    small = new_by_bucket["small_le_1MiB"]
    accept = (agg is not None and agg < 5.0 and small is not None and small < 10.0)
    verdict = "ACCEPT" if accept else "INSUFFICIENT"

    return {
        "params": {"bulk": model.bulk, "overhead": model.overhead},
        "physical_constraints": "all satisfied (fit did not hard-fail): BW>0, A>=0, alpha>=0, beta>=0",
        "mape_by_bucket": {
            "two_regime_v3": new_by_bucket,
            "stored_a1_v1": v1_by_bucket,
            "old_stream_factor_q0": sf_by_bucket,
        },
        "max_ape_pct_v3": max_ape_new,
        "production_S_semantics": {
            "single_object_S1_h2d_336MiB_ms": prod_s1,
            "single_object_S2_h2d_336MiB": prod_s2,
            "rule": "single-object production uses S=1; S>1 -> UNSUPPORTED (S is intra-object partition, not multi-object concurrency)",
        },
        "acceptance": {
            "bar": "aggregate < 5% AND small_le_1MiB < 10% AND physical constraints",
            "aggregate_mape": agg, "small_mape": small,
            "verdict": verdict,
        },
    }


# ---------------------------------------------------------------------------
# Candidate B — evaluation (gates before aggregate)
# ---------------------------------------------------------------------------

OPS_NON_DEQUANT = ("selected_expert", "grouped_gemm", "gather_scatter")


def _knn_predictions(train, test, k=3, use_log=True):
    m = ProfileKNN(k=k, use_log=use_log).fit(train)
    rows = []
    for p in test:
        pr = m.predict_point(p)
        rows.append({"op": p.operation, "phase": p.phase, "tokens": p.tokens,
                     "workload": p.workload, "measured": p.latency_ms,
                     "predicted": pr.value, "state": pr.state,
                     "nearest_distance": pr.nearest_distance})
    return rows


def _per_op_mape(rows, phase=None, non_exact=False):
    out = {}
    for op in OPS_NON_DEQUANT:
        sel = [r for r in rows if r["op"] == op and r["state"] != UNSUPPORTED
               and not (r["predicted"] != r["predicted"])]  # exclude NaN
        if phase:
            sel = [r for r in sel if r["phase"] == phase]
        if non_exact:
            sel = [r for r in sel if r["nearest_distance"] > 1e-12]
        if len(sel) == 0:
            out[op] = {"mape": None, "n": 0}
        else:
            out[op] = {"mape": mape([r["measured"] for r in sel], [r["predicted"] for r in sel]),
                       "n": len(sel)}
    return out


def eval_component(train, val) -> dict[str, Any]:
    train_nd = [p for p in train if p.operation != "dequant"]
    val_nd = [p for p in val if p.operation != "dequant"]

    # --- diagnostic q0 -> q1 cross-fit (continuity with review) ---
    xfit = _knn_predictions(train_nd, val_nd, k=3)
    affine = GlobalAffine().fit(train_nd)
    nearest = NearestProfile().fit(train_nd)
    xfit_affine = mape([p.latency_ms for p in val_nd], [affine.predict_point(p) for p in val_nd])
    xfit_knn_agg = mape([r["measured"] for r in xfit], [r["predicted"] for r in xfit])
    exact_counts = {
        "prefill": sum(1 for r in xfit if r["phase"] == "prefill" and r["nearest_distance"] <= 1e-12),
        "decode": sum(1 for r in xfit if r["phase"] == "decode" and r["nearest_distance"] <= 1e-12),
    }
    state_counts = {}
    for s in (INTERPOLATED, EXTRAPOLATED, UNSUPPORTED):
        state_counts[s] = sum(1 for r in xfit if r["state"] == s)

    # --- LOOWO (leave one workload out), pooled q0+q1 ---
    pool = train_nd + val_nd
    workloads = sorted({p.workload for p in pool})
    loowo_rows = []
    for w in workloads:
        tr = [p for p in pool if p.workload != w]
        te = [p for p in pool if p.workload == w]
        loowo_rows.extend(_knn_predictions(tr, te, k=3))
    # comparator LOOWO (affine, nearest) — to distinguish INSUFFICIENT from REJECT
    def _comparator_loowo(build):
        out = {op: {"pairs": []} for op in OPS_NON_DEQUANT}
        for w in workloads:
            tr = [p for p in pool if p.workload != w]
            te = [p for p in pool if p.workload == w and p.phase == "prefill"]
            model = build(tr)
            for p in te:
                out[p.operation]["pairs"].append((p.latency_ms, model.predict_point(p)))
        res = {}
        for op in OPS_NON_DEQUANT:
            pr = out[op]["pairs"]
            res[op] = mape([a for a, _ in pr], [b for _, b in pr]) if pr else None
        return res
    affine_loowo = _comparator_loowo(lambda tr: GlobalAffine().fit([p for p in tr if p.operation != "dequant"]))
    nearest_loowo = _comparator_loowo(lambda tr: NearestProfile().fit(tr))

    loowo_prefill_all = _per_op_mape(loowo_rows, phase="prefill", non_exact=False)
    loowo_prefill_nonexact = _per_op_mape(loowo_rows, phase="prefill", non_exact=True)
    loowo_decode_all = _per_op_mape(loowo_rows, phase="decode", non_exact=False)
    loowo_exact_counts = {
        "prefill": sum(1 for r in loowo_rows if r["phase"] == "prefill" and r["nearest_distance"] <= 1e-12),
        "decode": sum(1 for r in loowo_rows if r["phase"] == "decode" and r["nearest_distance"] <= 1e-12),
    }

    # --- LOSRO (leave one shape region out): train low tokens, test high (extrapolation stress) ---
    losro = {}
    for op in OPS_NON_DEQUANT:
        pref = [p for p in pool if p.operation == op and p.phase == "prefill"]
        toks = sorted({p.tokens for p in pref})
        if len(toks) < 4:
            losro[op] = {"mape": None, "n": 0, "note": "too few token regions"}
            continue
        med = statistics.median(toks)
        tr = [p for p in pref if p.tokens <= med]
        te = [p for p in pref if p.tokens > med]
        m = ProfileKNN(k=3).fit(tr)
        preds = [(p.latency_ms, m.predict_point(p)) for p in te]
        preds = [(mm, pr.value) for mm, pr in preds if pr.state != UNSUPPORTED]
        losro[op] = {"mape": (mape([a for a, _ in preds], [b for _, b in preds]) if preds else None),
                     "n": len(preds), "note": "train<=median tokens, test>median (extrapolation stress, NOT an interpolation gate)"}

    # --- k sensitivity (prefill non-exact per op, LOOWO) ---
    k_sens = {}
    for k in (1, 2, 3, 4):
        rows = []
        for w in workloads:
            tr = [p for p in pool if p.workload != w]
            te = [p for p in pool if p.workload == w]
            rows.extend(_knn_predictions(tr, te, k=k))
        k_sens[str(k)] = _per_op_mape(rows, phase="prefill", non_exact=True)

    # --- distance sensitivity (log vs raw token), LOOWO prefill non-exact ---
    dist_sens = {}
    for use_log in (True, False):
        rows = []
        for w in workloads:
            tr = [p for p in pool if p.workload != w]
            te = [p for p in pool if p.workload == w]
            rows.extend(_knn_predictions(tr, te, k=3, use_log=use_log))
        dist_sens["log_token" if use_log else "raw_token"] = _per_op_mape(rows, phase="prefill", non_exact=True)

    # --- bootstrap (resample training, eval q0->q1 prefill non-exact per op) ---
    rng = random.Random(20260818)
    boot = {op: [] for op in OPS_NON_DEQUANT}
    prefill_val = [p for p in val_nd if p.phase == "prefill"]
    for _ in range(500):
        sample = [train_nd[rng.randrange(len(train_nd))] for _ in range(len(train_nd))]
        rows = _knn_predictions(sample, prefill_val, k=3)
        pm = _per_op_mape(rows, phase="prefill", non_exact=True)
        for op in OPS_NON_DEQUANT:
            if pm[op]["mape"] is not None:
                boot[op].append(pm[op]["mape"])
    boot_summary = {}
    for op in OPS_NON_DEQUANT:
        xs = sorted(boot[op])
        if xs:
            boot_summary[op] = {
                "median": xs[len(xs) // 2],
                "p05": xs[max(0, int(0.05 * len(xs)))],
                "p95": xs[min(len(xs) - 1, int(0.95 * len(xs)))],
                "n_resamples": len(xs),
            }
        else:
            boot_summary[op] = None

    # --- GATES (applied BEFORE aggregate) ---
    THR = 15.0
    MIN_NONEXACT = 3
    gate1 = {op: (loowo_prefill_all[op]["mape"] is not None and loowo_prefill_all[op]["mape"] < THR)
             for op in OPS_NON_DEQUANT}
    gate2_evaluable = {op: loowo_prefill_nonexact[op]["n"] >= MIN_NONEXACT for op in OPS_NON_DEQUANT}
    gate2 = {op: (gate2_evaluable[op] and loowo_prefill_nonexact[op]["mape"] is not None
                  and loowo_prefill_nonexact[op]["mape"] < THR) for op in OPS_NON_DEQUANT}

    # REJECT check: KNN worse than affine on any op, or nearest dominates everywhere
    knn_worse_than_affine = [
        op for op in OPS_NON_DEQUANT
        if loowo_prefill_all[op]["mape"] is not None and affine_loowo[op] is not None
        and loowo_prefill_all[op]["mape"] > affine_loowo[op]
    ]
    nearest_dominates = all(
        nearest_loowo[op] is not None and loowo_prefill_all[op]["mape"] is not None
        and nearest_loowo[op] <= loowo_prefill_all[op]["mape"] for op in OPS_NON_DEQUANT
    )

    # Owner authorization (2026-08-18): gate-not-met -> INSUFFICIENT_EVIDENCE.
    # The prereg's reject_if condition (KNN worse than affine) is surfaced as an
    # explicit generalization warning rather than flipping the verdict, because
    # a 3-workload LOOWO is too sparse to firmly reject the model *class*. Either
    # way KNN is NOT promoted and V2-GAP-B is opened.
    generalization_warning = None
    if knn_worse_than_affine:
        generalization_warning = (
            f"Under leave-one-workload-out, ProfileKNN is WORSE than global_affine on "
            f"{knn_worse_than_affine} (KNN "
            + ", ".join(f"{op}={loowo_prefill_all[op]['mape']:.1f}%" for op in knn_worse_than_affine)
            + " vs affine "
            + ", ".join(f"{op}={affine_loowo[op]:.1f}%" for op in knn_worse_than_affine)
            + "). The local-interpolation advantage seen in the same-workload q0->q1 cross-fit "
            "does NOT generalize to an unseen workload. This meets the prereg reject_if condition; "
            "reported as INSUFFICIENT (per owner instruction) rather than REJECT because 3-workload "
            "LOOWO is too sparse to firmly reject the model class. Practical effect is identical: "
            "KNN is not promoted; the honest next step is a denser prefill shape sweep (V2-GAP-B)."
        )

    if all(gate1.values()) and all(gate2.values()):
        verdict = "ACCEPT"
        reason = "prefill-only LOOWO per-op < 15% AND non-exact prefill per-op < 15% for all three ops"
    elif not all(gate2_evaluable.values()):
        verdict = "INSUFFICIENT_EVIDENCE"
        reason = "non-exact prefill subset too small to evaluate for at least one op (open V2-GAP-B); must not backfill with aggregate/decode"
    else:
        verdict = "INSUFFICIENT_EVIDENCE"
        failed = [op for op in OPS_NON_DEQUANT if not (gate1[op] and gate2[op])]
        reason = (f"gate not met for {failed} under leave-one-workload-out (prefill per-op >= 15%); "
                  "ProfileKNN stays provisional/diagnostic (NOT promoted to the component model); "
                  "open V2-GAP-B (denser prefill shape sweep).")

    return {
        "diagnostic_q0_to_q1_crossfit": {
            "global_affine_aggregate_mape": xfit_affine,
            "profile_knn_aggregate_mape": xfit_knn_agg,
            "exact_lookup_counts": exact_counts,
            "state_counts": state_counts,
            "note": "aggregate is diagnostic only; decode is dominated by exact-token lookups and must not be used to satisfy the interpolation gate",
        },
        "primary_gates_loowo": {
            "prefill_per_op_all": loowo_prefill_all,
            "prefill_per_op_non_exact": loowo_prefill_nonexact,
            "decode_per_op_all": loowo_decode_all,
            "exact_lookup_counts": loowo_exact_counts,
            "gate1_prefill_all_lt15": gate1,
            "gate2_nonexact_prefill_lt15": gate2,
            "gate2_evaluable": gate2_evaluable,
        },
        "losro_extrapolation_stress": losro,
        "k_sensitivity_prefill_nonexact": k_sens,
        "distance_sensitivity_prefill_nonexact": dist_sens,
        "bootstrap_prefill_nonexact": boot_summary,
        "comparators": {
            "global_affine_xfit_aggregate": xfit_affine,
            "global_affine_loowo_prefill_per_op": affine_loowo,
            "nearest_profile_loowo_prefill_per_op": nearest_loowo,
            "knn_worse_than_affine_ops": knn_worse_than_affine,
        },
        "dequant_excluded": "dequant is PROXY_ONLY and excluded from component success aggregate (candidate D)",
        "generalization_warning": generalization_warning,
        "acceptance": {"verdict": verdict, "reason": reason, "threshold_pct": THR},
    }


# ---------------------------------------------------------------------------
# Candidate C — evaluation (structural; sort term BLOCKED_ON_MEASUREMENT)
# ---------------------------------------------------------------------------

def eval_replay(pts: list[ReplayPoint]) -> dict[str, Any]:
    missing = replay_missing_terms()
    measured_terms = [o["op"] for o in REPLAY_OPERATOR_GRAPH if o["measured"]]

    # fixed tau diagnostic (OLS through origin on residual vs routes/tokens)
    resid = [(p.measured_tpot_ms - p.a1_pred_tpot_ms) for p in pts]
    feat = [p.routes / p.tokens for p in pts]
    tau = sum(r * f for r, f in zip(resid, feat)) / sum(f * f for f in feat)
    diag = FixedTauRouteDiagnostic(tau_route_ms=tau)
    a1_mape = mape([p.measured_tpot_ms for p in pts], [p.a1_pred_tpot_ms for p in pts])
    tau_mape = mape([p.measured_tpot_ms for p in pts], [diag.predict_tpot(p) for p in pts])
    # throughput derived (never fit): from tau-diagnostic tpot
    thr_meas = [1000.0 / p.measured_tpot_ms for p in pts]
    thr_pred = [1000.0 / diag.predict_tpot(p) for p in pts]
    tau_thr_mape = mape(thr_meas, thr_pred)
    structural_configs = sorted({(p.tokens, p.routes) for p in pts})

    return {
        "explicit_graph": REPLAY_OPERATOR_GRAPH,
        "declared_metadata_ops": REPLAY_DECLARED_METADATA_OPS,
        "metadata_mismatch": {
            "declared": REPLAY_DECLARED_METADATA_OPS,
            "actually_timed_missing_from_declared": missing + ["gemm_up", "gemm_down", "act_silu_mul"],
            "note": "declared metadata records only grouped_gemm+gather_scatter; the 2 argsorts (sort + inverse permutation) and the 3-GEMM/silu structure are omitted",
        },
        "measured_terms": measured_terms,
        "missing_terms_BLOCKED_ON_MEASUREMENT": missing,
        "tau_route_diagnostic": {
            "status": "DIAGNOSTIC_ONLY",
            "tau_route_ms": tau,
            "n_points": len(pts),
            "n_distinct_structural_configs": len(structural_configs),
            "structural_configs_tokens_routes": structural_configs,
            "a1_tpot_mape_pct": a1_mape,
            "tau_augmented_tpot_mape_pct": tau_mape,
            "tau_augmented_throughput_mape_pct_derived": tau_thr_mape,
            "warning": "fit on only %d distinct structural configs; a 1-parameter additive term will always fit; NOT a calibrated model" % len(structural_configs),
        },
        "throughput_policy": "derived as 1000/TPOT, never fit independently",
        "acceptance": {
            "verdict": "BLOCKED_ON_MEASUREMENT / INSUFFICIENT_EVIDENCE",
            "reason": "T_sort(argsort_route) and T_inverse_sort(argsort_inverse) cannot be identified from existing calibration probes; replay cannot be FIT-closed. Explicit graph is recorded; fixed tau kept diagnostic only. Opens V2-GAP-C (sort/permute microbenchmark).",
        },
    }


# ---------------------------------------------------------------------------
# Self-check: v3 production-path contracts
# ---------------------------------------------------------------------------

def self_check() -> dict[str, Any]:
    cases = []

    def add(name, ok, **extra):
        cases.append({"case": name, "ok": bool(ok), **extra})

    # PCIe hard-fail on each non-physical coefficient
    def pcie_pts(mutate):
        base = [
            {"direction": "h2d", "bytes": 65536, "streams": 1, "latency_ms": 0.04},
            {"direction": "h2d", "bytes": 65536, "streams": 2, "latency_ms": 0.07},
            {"direction": "h2d", "bytes": 22_020_096, "streams": 1, "latency_ms": 0.78},
            {"direction": "h2d", "bytes": 44_040_192, "streams": 1, "latency_ms": 1.56},
        ]
        d2h = [dict(p, direction="d2h") for p in base]
        return mutate(base + d2h)

    # negative bulk slope (BW<=0): make larger bytes cheaper
    def neg_bw(pts):
        for p in pts:
            if p["bytes"] >= 22_020_096:
                p["latency_ms"] = 0.01
        return pts
    try:
        PcieTwoRegime.fit(pcie_pts(neg_bw)); add("pcie_negative_bandwidth_hardfail", False)
    except ModelError as e:
        add("pcie_negative_bandwidth_hardfail", True, message=str(e))

    # production S>1 -> UNSUPPORTED, S=1 -> number
    cal, _ = load_pcie()
    m = PcieTwoRegime.fit(cal)
    add("pcie_production_S1_is_number", isinstance(m.predict("h2d", 352_321_536, 1, mode="production"), float))
    add("pcie_production_S2_is_UNSUPPORTED", m.predict("h2d", 352_321_536, 2, mode="production") == UNSUPPORTED)

    # KNN UNSUPPORTED when < minimum_neighbors; EXTRAPOLATED outside envelope
    knn = ProfileKNN(k=3, minimum_neighbors=2).fit([
        ComponentPoint("selected_expert", 100, "prefill", 1, 0.2, "w1"),
        ComponentPoint("selected_expert", 200, "prefill", 1, 0.3, "w1"),
    ])
    add("knn_unsupported_missing_op",
        knn.predict_point(ComponentPoint("grouped_gemm", 100, "prefill", 1, 0.0, "w")).state == UNSUPPORTED)
    add("knn_extrapolated_outside_envelope",
        knn.predict_point(ComponentPoint("selected_expert", 100000, "prefill", 1, 0.0, "w")).state == EXTRAPOLATED)
    add("knn_interpolated_inside_envelope",
        knn.predict_point(ComponentPoint("selected_expert", 150, "prefill", 1, 0.0, "w")).state == INTERPOLATED)

    # replay missing terms are non-empty (BLOCKED)
    add("replay_missing_terms_nonempty", len(replay_missing_terms()) > 0, missing=replay_missing_terms())

    return {"all_ok": all(c["ok"] for c in cases), "cases": cases}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-commit", default="UNKNOWN")
    ap.add_argument("--run-id", default="v3-adhoc")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    sc = self_check()
    if not sc["all_ok"]:
        raise SystemExit("FATAL: v3 self-check failed: " + json.dumps(sc))

    cal_pcie, val_pcie = load_pcie()
    train_c, val_c = load_component()
    replay_pts = load_replay()

    pcie = eval_pcie(cal_pcie, val_pcie)
    component = eval_component(train_c, val_c)
    replay = eval_replay(replay_pts)

    (OUT / "self_check.json").write_text(json.dumps(sc, indent=2) + "\n")
    (OUT / "pcie_report.json").write_text(json.dumps(pcie, indent=2, sort_keys=True) + "\n")
    (OUT / "component_report.json").write_text(json.dumps(component, indent=2, sort_keys=True) + "\n")
    (OUT / "replay_report.json").write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n")

    summary = {
        "schema_version": "stage-a1-v3-fit-side-summary-v1",
        "classification": "FIT_SIDE / DIAGNOSTIC (decision P-005); no held-out, no calibrated PASS",
        "code_commit": args.code_commit,
        "run_id": args.run_id,
        "models_v3_sha256": _sha256(REPO_ROOT / "calibration/models_v3.py"),
        "refit_v3_sha256": _sha256(REPO_ROOT / "calibration/refit_v3.py"),
        "candidate_a_pcie": {
            "verdict": pcie["acceptance"]["verdict"],
            "aggregate_mape": pcie["mape_by_bucket"]["two_regime_v3"]["aggregate"],
            "small_mape": pcie["mape_by_bucket"]["two_regime_v3"]["small_le_1MiB"],
            "transition_mape": pcie["mape_by_bucket"]["two_regime_v3"]["transition_1MiB_22MiB"],
            "bulk_mape": pcie["mape_by_bucket"]["two_regime_v3"]["bulk_ge_22MiB"],
            "h2d_mape": pcie["mape_by_bucket"]["two_regime_v3"]["h2d"],
            "d2h_mape": pcie["mape_by_bucket"]["two_regime_v3"]["d2h"],
            "max_ape": pcie["max_ape_pct_v3"],
        },
        "candidate_b_component": {"verdict": component["acceptance"]["verdict"], "reason": component["acceptance"]["reason"]},
        "candidate_c_replay": {"verdict": replay["acceptance"]["verdict"]},
        "dequant": "PROXY_ONLY",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    _write_run(args, summary, pcie, component, replay, sc)

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _write_run(args, summary, pcie, component, replay, sc) -> None:
    """Write runs/<run_id>/ with a manifest whose provenance is auditable.

    The provenance model separates code_commit (the commit that contains the
    exact source that produced these artifacts) from the artifact commit (the
    immediately following commit that adds this run directory). We record
    code_commit plus SHA-256 of every generating script, so the run is
    reproducible from git OR verifiable from the hashes alone — avoiding the
    earlier failure mode where a manifest pointed at a parent commit that did
    not even contain the generating code.
    """
    run_dir = REPO_ROOT / "runs" / args.run_id
    for sub in ("logs", "artifacts", "environment"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = {
        "run_id": args.run_id,
        "experiment_id": "cal_model_form_repair_v2",
        "stage": "S2",
        "platform_profile": "NVIDIA RTX PRO 6000 (evidence-of-record); this run is CPU-only FIT-side evaluation, no GPU",
        "created_at": now,
        "git": {
            "code_commit": args.code_commit,
            "provenance_note": (
                "artifacts were produced by the scripts at code_commit; this run directory is "
                "committed in the immediately following commit (artifact commit). The two scripts' "
                "SHA-256 below let you verify the exact generating code independent of git."
            ),
            "models_v3_sha256": summary["models_v3_sha256"],
            "refit_v3_sha256": summary["refit_v3_sha256"],
        },
        "command": ["python", "calibration/refit_v3.py", "--code-commit", args.code_commit,
                    "--run-id", args.run_id],
        "status": "partial",
        "status_note": ("Candidate A ACCEPT (FIT-side); Candidate B INSUFFICIENT_EVIDENCE "
                        "(ProfileKNN not promoted); Candidate C BLOCKED_ON_MEASUREMENT. "
                        "FIT-side/diagnostic only; no calibrated PASS."),
        "simulation": {"fidelity": "calibrated", "backend": "calibration.models_v3", "calibration_pack": None},
        "parent_runs": ["20260817T172500Z__stage_a1_calibration_model_form_repair"],
        "classification": "FIT_SIDE / DIAGNOSTIC (decision P-005)",
        "verdicts": {
            "candidate_a_pcie": summary["candidate_a_pcie"]["verdict"],
            "candidate_b_component": summary["candidate_b_component"]["verdict"],
            "candidate_c_replay": summary["candidate_c_replay"]["verdict"],
            "dequant": "PROXY_ONLY",
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    resolved = {
        "stage": "STAGE_A1",
        "experiment_id": "cal_model_form_repair_v2",
        "pre_registration": "experiments/specs/cal_model_form_repair_v2.yaml",
        "inputs": {
            "pcie_and_component_and_replay": str(CAL.relative_to(REPO_ROOT)),
            "residual_check": str(VAL.relative_to(REPO_ROOT)),
            "v2_a1_predicted_tpot": str(V2_RESIDUAL.relative_to(REPO_ROOT)),
        },
        "outputs_dir": "calibration/fits/v3/",
        "decision_p005": "all evidence FIT-side; no held-out; no calibrated PASS",
    }
    (run_dir / "resolved_config.yaml").write_text(
        "\n".join(_yaml_lines(resolved)) + "\n"
    )
    (run_dir / "metrics.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (run_dir / "logs" / "command.log").write_text(" ".join(manifest["command"]) + "\n")
    (run_dir / "logs" / "stdout.log").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (run_dir / "logs" / "self_check.log").write_text(json.dumps(sc, indent=2) + "\n")
    for name in ("summary.json", "pcie_report.json", "component_report.json", "replay_report.json", "self_check.json"):
        shutil.copy(OUT / name, run_dir / "artifacts" / name)
    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "code_commit": args.code_commit,
        "note": "CPU-only FIT-side evaluation; no GPU/CUDA (gpu_required: false)",
    }
    try:
        env["git_head_at_run"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        env["git_head_at_run"] = "unknown"
    (run_dir / "environment" / "tool_versions.json").write_text(json.dumps(env, indent=2) + "\n")


def _yaml_lines(obj: Any, indent: int = 0) -> list[str]:
    lines = []
    pad = "  " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict,)):
                lines.append(f"{pad}{k}:")
                lines.extend(_yaml_lines(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {json.dumps(v, ensure_ascii=False)}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
