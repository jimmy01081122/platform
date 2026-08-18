"""Regression tests for the cal_model_form_repair_v2 (v3) production-path
contracts. Focus: the NEW behaviors that must not silently regress —
two-regime hard-fail, production stream semantics, KNN interpolation states,
and the replay BLOCKED_ON_MEASUREMENT structural fact.

These guard preregistered contracts; they do not assert any calibrated PASS.
"""
import math

import pytest

from calibration.models_v3 import (
    EXTRAPOLATED,
    FixedTauRouteDiagnostic,
    INTERPOLATED,
    ComponentPoint,
    ModelError,
    PcieTwoRegime,
    ProfileKNN,
    ReplayPoint,
    UNSUPPORTED,
    replay_missing_terms,
)


def _physical_pcie_points():
    base = [
        {"direction": "h2d", "bytes": 65536, "streams": 1, "latency_ms": 0.040},
        {"direction": "h2d", "bytes": 65536, "streams": 2, "latency_ms": 0.074},
        {"direction": "h2d", "bytes": 65536, "streams": 4, "latency_ms": 0.137},
        {"direction": "h2d", "bytes": 22_020_096, "streams": 1, "latency_ms": 0.779},
        {"direction": "h2d", "bytes": 44_040_192, "streams": 1, "latency_ms": 1.557},
        {"direction": "h2d", "bytes": 88_080_384, "streams": 1, "latency_ms": 3.113},
    ]
    return base + [dict(p, direction="d2h") for p in base]


def test_pcie_two_regime_fits_and_is_physical():
    m = PcieTwoRegime.fit(_physical_pcie_points())
    for d in ("h2d", "d2h"):
        assert m.bulk[d]["bandwidth_bytes_per_ms"] > 0
        assert m.bulk[d]["intercept_ms"] >= 0
        assert m.overhead[d]["alpha_ms"] >= 0
        assert m.overhead[d]["beta_ms_per_extra_stream"] >= 0


def test_pcie_two_regime_hardfails_on_negative_bandwidth():
    pts = _physical_pcie_points()
    for p in pts:
        if p["bytes"] >= 22_020_096:
            p["latency_ms"] = 0.01  # larger transfers cheaper -> negative slope
    with pytest.raises(ModelError):
        PcieTwoRegime.fit(pts)


def test_pcie_two_regime_hardfails_on_negative_overhead_slope():
    pts = _physical_pcie_points()
    # make more streams cheaper -> beta < 0
    for p in pts:
        if p["bytes"] == 65536:
            p["latency_ms"] = {1: 0.14, 2: 0.07, 4: 0.04}[p["streams"]]
    with pytest.raises(ModelError):
        PcieTwoRegime.fit(pts)


def test_pcie_production_single_object_uses_s1_number():
    m = PcieTwoRegime.fit(_physical_pcie_points())
    val = m.predict("h2d", 352_321_536, 1, mode="production")
    assert isinstance(val, float) and val > 0


def test_pcie_production_s_gt_1_is_unsupported():
    m = PcieTwoRegime.fit(_physical_pcie_points())
    assert m.predict("h2d", 352_321_536, 2, mode="production") == UNSUPPORTED
    assert m.predict("h2d", 352_321_536, 4, mode="production") == UNSUPPORTED


def test_pcie_evaluation_mode_bulk_is_stream_invariant():
    m = PcieTwoRegime.fit(_physical_pcie_points())
    vals = [m.predict("h2d", 88_080_384, s, mode="evaluation") for s in (1, 2, 4)]
    assert (max(vals) - min(vals)) / min(vals) < 0.01


def _knn():
    pts = [
        ComponentPoint("selected_expert", 100, "prefill", 1, 0.20, "w1"),
        ComponentPoint("selected_expert", 200, "prefill", 1, 0.30, "w1"),
        ComponentPoint("selected_expert", 400, "prefill", 1, 0.50, "w1"),
    ]
    return ProfileKNN(k=3, minimum_neighbors=2).fit(pts)


def test_knn_unsupported_when_operation_absent():
    pr = _knn().predict_point(ComponentPoint("grouped_gemm", 100, "prefill", 1, 0.0, "w"))
    assert pr.state == UNSUPPORTED


def test_knn_unsupported_when_below_minimum_neighbors():
    m = ProfileKNN(k=3, minimum_neighbors=2).fit(
        [ComponentPoint("selected_expert", 100, "prefill", 1, 0.2, "w1")]
    )
    assert m.predict_point(ComponentPoint("selected_expert", 120, "prefill", 1, 0.0, "w")).state == UNSUPPORTED


def test_knn_interpolated_inside_envelope():
    pr = _knn().predict_point(ComponentPoint("selected_expert", 150, "prefill", 1, 0.0, "w"))
    assert pr.state == INTERPOLATED
    assert pr.value > 0


def test_knn_extrapolated_outside_envelope():
    pr = _knn().predict_point(ComponentPoint("selected_expert", 100000, "prefill", 1, 0.0, "w"))
    assert pr.state == EXTRAPOLATED


def test_knn_exact_shape_returns_that_point():
    pr = _knn().predict_point(ComponentPoint("selected_expert", 200, "prefill", 1, 0.0, "w"))
    assert pr.nearest_distance == pytest.approx(0.0)
    assert pr.value == pytest.approx(0.30)


def test_knn_phase_is_hard_partition():
    # a decode query must not borrow prefill points -> UNSUPPORTED (no decode pts)
    pr = _knn().predict_point(ComponentPoint("selected_expert", 150, "decode", 1, 0.0, "w"))
    assert pr.state == UNSUPPORTED


def test_replay_missing_terms_are_blocked():
    missing = replay_missing_terms()
    assert "argsort_route" in missing and "argsort_inverse" in missing


def test_replay_throughput_is_derived_from_tpot():
    diag = FixedTauRouteDiagnostic(tau_route_ms=0.07)
    p = ReplayPoint("w", 1, 64, 64, 0.17, 0.098)
    tpot = diag.predict_tpot(p)
    assert diag.throughput_from_tpot(tpot) == pytest.approx(1000.0 / tpot)
