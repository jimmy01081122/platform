"""TRACK_GPU_PREP: probe smoke tests + parser/fixture tests.

Verifies (all pure CPU, no GPU/torch/serving):
  * both new probes run their full path against the mock backend;
  * every parser accepts its normal fixture and RAISES on its failure fixture
    (loud failure, never a silent skip -- the hf_sample_download.py anti-pattern);
  * the sealed manifest is deterministic and its audit catches tampering;
  * PENDING_A2 markers are present wherever output fields depend on A2's schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from measurement.probes import long_context_kv_probe, inserving_dispatch_probe
from measurement.probes.mock_backend import BackendError
from measurement.parsers import ValidationError
from measurement.parsers import (
    longctx_kv_parser, dispatch_parser, sealed_manifest_validator,
    component_eval_parser, serving_tail_parser, ir_point_validator,
)
from measurement.probes.ir_evaluation_point import (
    longctx_result_to_points, dispatch_result_to_points, IREvaluationPointError,
    build_calibration_point,
)
from calibration.sealed import build_holdout_split_v1

FIXTURES = Path(__file__).parent / "fixtures" / "gpu_prep"
REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Probe CPU smoke tests
# --------------------------------------------------------------------------- #

def test_longctx_probe_cpu_smoke(tmp_path):
    out = tmp_path / "lc.json"
    rc = long_context_kv_probe.main([
        "--backend", "mock_longctx", "--out", str(out),
        "--seq-lens", "4096,65536,1048576", "--kv-budget-bytes", str(8 * 10**9),
    ])
    assert rc == 0
    result = json.loads(out.read_text())
    assert result["evidence"] == "cpu_smoke_test_not_measurement"
    assert result["sweep_crossed_offload_boundary"] is True
    # PREP-2: IR eval-point fields are filled against A2's schema (was PENDING_A2)
    assert result["ir_evaluation_point_fields"] == "FILLED_PREP2"
    longctx_kv_parser.validate(result)  # self-consistent


def test_longctx_probe_records_oom_and_stops(tmp_path, monkeypatch):
    # a backend that OOMs on the 2nd length must stop the sweep, not shorten
    from measurement.probes import mock_backend

    class OomBackend(mock_backend.MockLongContextBackend):
        def measure(self, seq_len):
            if seq_len >= 65536:
                raise BackendError("simulated OOM")
            return super().measure(seq_len)

    monkeypatch.setattr(long_context_kv_probe, "_build_backend",
                        lambda *_a, **_k: OomBackend(kv_budget_bytes=10**9))
    out = tmp_path / "oom.json"
    long_context_kv_probe.main(["--backend", "mock_longctx", "--out", str(out),
                                "--seq-lens", "4096,65536,1048576"])
    result = json.loads(out.read_text())
    oom = [r for r in result["records"] if r.get("oom")]
    assert oom and oom[-1]["stopped_sweep"] is True
    # sweep stopped at the OOM point; the 1048576 point was never attempted
    assert all(r["seq_len"] != 1048576 for r in result["records"])


def test_dispatch_probe_cpu_smoke(tmp_path):
    out = tmp_path / "d.json"
    rc = inserving_dispatch_probe.main([
        "--backend", "mock_dispatch", "--out", str(out),
        "--concurrency", "1,4", "--steps", "8",
    ])
    assert rc == 0
    result = json.loads(out.read_text())
    assert result["evidence"] == "cpu_smoke_test_not_measurement"
    # PREP-2: break-even decomposition + IR eval-point fields are now filled
    assert result["break_even_decomposition_fields"] == [
        "T_prepare_ns", "T_queue_ns", "T_sync_ns", "T_move_ns"]
    assert result["ir_evaluation_point_fields"] == "FILLED_PREP2"
    dispatch_parser.validate(result)


def test_probes_refuse_gpu_backend(tmp_path):
    # pure-CPU track: the real GPU backend must refuse, never silently degrade
    with pytest.raises(BackendError):
        long_context_kv_probe.run(long_context_kv_probe.parse_args(
            ["--backend", "gpu", "--out", str(tmp_path / "x.json")]))
    with pytest.raises(BackendError):
        inserving_dispatch_probe.run(inserving_dispatch_probe.parse_args(
            ["--backend", "gpu", "--out", str(tmp_path / "y.json")]))


# --------------------------------------------------------------------------- #
# Parser fixtures: normal accepted, failure RAISES
# --------------------------------------------------------------------------- #

def test_longctx_parser_fixtures():
    ok = json.loads((FIXTURES / "longctx_kv_pass.json").read_text())
    assert longctx_kv_parser.validate(ok)["sweep_crossed_offload_boundary"] is True
    bad = json.loads((FIXTURES / "longctx_kv_fail_conservation.json").read_text())
    with pytest.raises(ValidationError, match="resident|total"):
        longctx_kv_parser.validate(bad)


def test_dispatch_parser_fixtures():
    ok = json.loads((FIXTURES / "dispatch_pass.json").read_text())
    dispatch_parser.validate(ok)
    bad = json.loads((FIXTURES / "dispatch_fail_bytes.json").read_text())
    with pytest.raises(ValidationError, match="dispatch_bytes"):
        dispatch_parser.validate(bad)


def test_component_eval_parser_fixtures():
    ok = json.loads((FIXTURES / "component_eval_pass.json").read_text())
    component_eval_parser.validate(ok, enforce_gap4=True)  # carries expert_tokens
    bad = json.loads((FIXTURES / "component_eval_fail_measured_type.json").read_text())
    with pytest.raises(ValidationError, match="measured"):
        component_eval_parser.validate(bad)


def test_component_eval_gap4_legacy_flagged_then_enforced():
    legacy = json.loads((FIXTURES / "component_eval_gap4_legacy.json").read_text())
    # legacy mode: flagged (needs join) but not fatal
    root = component_eval_parser.validate(legacy, enforce_gap4=False)
    assert root["_gap4_affected_points"], "legacy point should be flagged as GAP-4-affected"
    # post-fix enforcement: the same missing feature is fatal
    with pytest.raises(ValidationError, match="GAP-4"):
        component_eval_parser.validate(legacy, enforce_gap4=True)


def test_serving_tail_parser_fixtures():
    ok = json.loads((FIXTURES / "serving_tail_pass.json").read_text())
    serving_tail_parser.validate(ok)
    bad = json.loads((FIXTURES / "serving_tail_fail_resumed.json").read_text())
    with pytest.raises(ValidationError, match="resumed|partial"):
        serving_tail_parser.validate(bad)


def test_serving_tail_rejects_non_monotone_percentiles():
    ok = json.loads((FIXTURES / "serving_tail_pass.json").read_text())
    ok["completion_latency"]["p95_ms"] = 100.0  # now p50 > p95
    with pytest.raises(ValidationError, match="monotone"):
        serving_tail_parser.validate(ok)


# --------------------------------------------------------------------------- #
# Sealed manifest: determinism + tamper audit
# --------------------------------------------------------------------------- #

def test_sealed_split_is_deterministic():
    a = build_holdout_split_v1.build_manifest(sealed_at="FIXED")
    b = build_holdout_split_v1.build_manifest(sealed_at="FIXED")
    assert a["assignment_sha256"] == b["assignment_sha256"]
    assert json.dumps(a["cells"], sort_keys=True) == json.dumps(b["cells"], sort_keys=True)
    # every split targeted has at least one holdout cell for each metric family
    holdout = {c["metric"] for c in a["cells"] if c["split"] == "holdout"}
    assert {"pcie_transfer_latency", "component_latency"} <= holdout


def test_committed_sealed_manifest_passes_audit():
    manifest = json.loads(
        (REPO / "calibration" / "sealed" / "holdout_split_v1_manifest.json").read_text())
    root = sealed_manifest_validator.validate(manifest)
    assert root["cell_total"] == sum(root["cell_counts"].values())


def test_sealed_manifest_fixture_tamper_is_caught():
    ok = json.loads((FIXTURES / "sealed_manifest_pass.json").read_text())
    sealed_manifest_validator.validate(ok)
    tampered = json.loads((FIXTURES / "sealed_manifest_fail_tamper.json").read_text())
    with pytest.raises(ValidationError, match="altered after sealing|assignment"):
        sealed_manifest_validator.validate(tampered)


# --------------------------------------------------------------------------- #
# PREP-2: probe output -> CalibrationIR evaluation points (no join)
# --------------------------------------------------------------------------- #

CANON_SCHEMA = (REPO / "explorations/moe_cycle_simulator/phase2/schemas"
                / "canonical_ir.schema.json")


def _calibration_subschema():
    schema = json.loads(CANON_SCHEMA.read_text())
    cal = dict(schema["$defs"]["calibration"])
    cal["$defs"] = schema["$defs"]
    return cal


def test_longctx_probe_emits_filled_ir_points(tmp_path):
    out = tmp_path / "lc.json"
    long_context_kv_probe.main([
        "--backend", "mock_longctx", "--out", str(out),
        "--seq-lens", "4096,65536,1048576",
    ])
    result = json.loads(out.read_text())
    # PENDING_A2 is gone; fields are filled against A2's schema
    assert result["ir_evaluation_point_fields"] == "FILLED_PREP2"
    assert result["ir_evaluation_point_schema"] == "CalibrationIR"
    ir_point_validator.validate_probe_result(result)
    # every point carries seq_len directly in the coordinate (no join to raw)
    for pt in result["ir_evaluation_points"]:
        names = {c["name"] for c in pt["evaluation_coordinate"]}
        assert "seq_len" in names


def test_dispatch_probe_emits_filled_ir_points_with_breakeven(tmp_path):
    out = tmp_path / "d.json"
    inserving_dispatch_probe.main([
        "--backend", "mock_dispatch", "--out", str(out),
        "--concurrency", "1,4", "--steps", "4",
    ])
    result = json.loads(out.read_text())
    assert result["ir_evaluation_point_fields"] == "FILLED_PREP2"
    # break-even decomposition is now concrete field names, not PENDING_A2
    assert result["break_even_decomposition_fields"] == [
        "T_prepare_ns", "T_queue_ns", "T_sync_ns", "T_move_ns"]
    ir_point_validator.validate_probe_result(result)
    # operand shape (expert_tokens) carried directly -- the GAP-4 fix
    metrics = {pt["metric"] for pt in result["ir_evaluation_points"]}
    assert {"dispatch_bytes", "dispatch_T_move", "dispatch_T_prepare"} <= metrics
    for pt in result["ir_evaluation_points"]:
        names = {c["name"] for c in pt["evaluation_coordinate"]}
        assert "expert_tokens" in names and "concurrency" in names


def test_ir_points_validate_against_real_a2_schema(tmp_path):
    """The strongest PREP-2 check: points validate against STAGE_A2's actual
    CalibrationIR schema, built from the probe result alone (no join)."""
    jsonschema = pytest.importorskip("jsonschema")
    cal = _calibration_subschema()
    out = tmp_path / "d.json"
    inserving_dispatch_probe.main([
        "--backend", "mock_dispatch", "--out", str(out),
        "--concurrency", "1,2,4", "--steps", "3",
    ])
    result = json.loads(out.read_text())
    points = dispatch_result_to_points(result)  # built from result only
    assert points
    for pt in points:
        jsonschema.validate(pt, cal)


def test_ir_point_builder_rejects_shape_envelope_mismatch():
    with pytest.raises(IREvaluationPointError, match="coordinate names"):
        build_calibration_point(
            metric="x", unit="ns", measured_value="1",
            coordinate=[{"name": "expert_tokens", "value": "8"}],
            envelope_dimensions=[{"name": "WRONG", "lower": "1", "upper": "10"}],
            runtime_variant_hash="a" * 64, repetitions=3, sample_count=3,
            resampling_strata=["x"],
        )


def test_ir_point_builder_rejects_coordinate_outside_envelope():
    with pytest.raises(IREvaluationPointError, match="outside envelope"):
        build_calibration_point(
            metric="x", unit="ns", measured_value="1",
            coordinate=[{"name": "seq_len", "value": "999999"}],
            envelope_dimensions=[{"name": "seq_len", "lower": "1", "upper": "10"}],
            runtime_variant_hash="a" * 64, repetitions=3, sample_count=3,
            resampling_strata=["x"],
        )


def test_ir_point_validator_fixtures():
    ok = json.loads((FIXTURES / "ir_points_pass.json").read_text())
    ir_point_validator.validate_probe_result(ok)
    bad = json.loads((FIXTURES / "ir_points_fail_coordinate.json").read_text())
    with pytest.raises(ValidationError, match="coordinate names|envelope"):
        ir_point_validator.validate_probe_result(bad)
