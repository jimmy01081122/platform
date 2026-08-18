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
from measurement.probes import PENDING_A2_SENTINEL
from measurement.probes.mock_backend import BackendError
from measurement.parsers import ValidationError
from measurement.parsers import (
    longctx_kv_parser, dispatch_parser, sealed_manifest_validator,
    component_eval_parser, serving_tail_parser,
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
    # output field that depends on A2 schema must be a PENDING_A2 marker
    assert result["ir_evaluation_point_fields"] == PENDING_A2_SENTINEL
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
    assert result["break_even_decomposition_fields"] == PENDING_A2_SENTINEL
    assert result["ir_evaluation_point_fields"] == PENDING_A2_SENTINEL
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
