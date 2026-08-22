"""TRACK_GPU_PREP: probe smoke tests + parser/fixture tests.

Verifies (all pure CPU, no GPU/torch/serving):
  * both new probes run their full path against the mock backend;
  * every parser accepts its normal fixture and RAISES on its failure fixture
    (loud failure, never a silent skip -- the hf_sample_download.py anti-pattern);
  * the sealed manifest is deterministic and its audit catches tampering;
  * PENDING_A2 markers are present wherever output fields depend on A2's schema.
"""

from __future__ import annotations

import copy
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


def test_longctx_probe_refuses_noncanonical_measured_repeat_count(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        long_context_kv_probe,
        "_build_backend",
        lambda *_a, **_k: pytest.fail("runtime must not be constructed"),
    )
    args = long_context_kv_probe.parse_args([
        "--backend", "vllm_longctx_offload_on",
        "--repeats", "2",
        "--out", str(tmp_path / "never.json"),
    ])
    with pytest.raises(BackendError, match="repeat count is frozen at 3"):
        long_context_kv_probe.run(args)


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


def test_longctx_probe_stops_on_the_exact_oom_repeat(tmp_path, monkeypatch):
    """A returned OOM on repeat 2 must not allow repeat 3 to execute."""
    from measurement.probes import mock_backend

    class MidRepeatOom(mock_backend.MockLongContextBackend):
        def __init__(self):
            super().__init__(kv_budget_bytes=10**9)
            self.calls: dict[int, int] = {}

        def measure(self, seq_len):
            self.calls[seq_len] = self.calls.get(seq_len, 0) + 1
            if seq_len == 65536 and self.calls[seq_len] == 2:
                return {
                    "seq_len": seq_len,
                    "oom": True,
                    "error": "CUDA out of memory in repeat 1",
                    "failure_classification": "CUDA_OR_ENGINE_OOM",
                }
            return super().measure(seq_len)

    backend = MidRepeatOom()
    monkeypatch.setattr(
        long_context_kv_probe, "_build_backend", lambda *_a, **_k: backend
    )
    args = long_context_kv_probe.parse_args([
        "--backend", "mock_longctx", "--out", str(tmp_path / "oom-repeat.json"),
        "--seq-lens", "4096,65536,1048576", "--repeats", "3",
    ])
    result = long_context_kv_probe.run(args)
    terminal = result["records"][-1]
    assert backend.calls == {4096: 3, 65536: 2}
    assert terminal["oom"] is True
    assert terminal["repeat_index"] == 1
    assert terminal["valid_repeats_completed"] == 1
    assert len(terminal["completed_repeat_measurements"]) == 1
    longctx_kv_parser.validate(result)


def test_longctx_probe_means_are_primary_and_feed_ir(tmp_path, monkeypatch):
    from measurement.probes import mock_backend

    class VaryingBackend(mock_backend.MockLongContextBackend):
        def __init__(self):
            super().__init__(kv_budget_bytes=8 * 10**9)
            self.call_index = 0

        def measure(self, seq_len):
            row = super().measure(seq_len)
            index = self.call_index
            self.call_index += 1
            row["ttft_ns"] = [10, 11, 11][index]
            row["decode_per_token_ns"] = [20, 21, 21][index]
            row["kv_move_ns"] = [30, 31, 31][index]
            return row

    monkeypatch.setattr(
        long_context_kv_probe, "_build_backend", lambda *_a, **_k: VaryingBackend()
    )
    result = long_context_kv_probe.run(long_context_kv_probe.parse_args([
        "--backend", "mock_longctx", "--out", str(tmp_path / "means.json"),
        "--seq-lens", "1048576", "--repeats", "3",
    ]))
    record = result["records"][0]
    assert record["primary_statistic"] == "arithmetic_mean"
    assert record["ttft_ns"] == pytest.approx(32 / 3)
    assert record["ttft_ns_repeats"] == [10, 11, 11]
    ttft_point = next(
        point for point in result["ir_evaluation_points"]
        if point["metric"] == "longctx_ttft"
    )
    assert float(ttft_point["measured_value"]) == pytest.approx(32 / 3)
    measured_shape = copy.deepcopy(result)
    measured_shape["evidence"] = "measured"
    assert all(
        point["evidence_class"] == "MEASURED"
        and point["fidelity"] == "MEASURED"
        for point in longctx_result_to_points(measured_shape)
    )
    longctx_kv_parser.validate(result)


def _formal_measured_longctx_shape(tmp_path):
    args = long_context_kv_probe.parse_args([
        "--backend", "mock_longctx",
        "--seq-lens", ",".join(map(str, longctx_kv_parser.FORMAL_SEQ_LENS)),
        "--kv-budget-bytes", str(8 * 10**9),
        "--repeats", "3",
        "--out", str(tmp_path / "formal-longctx-shape.json"),
    ])
    result = long_context_kv_probe.run(args)
    result["backend"] = "vllm_longctx_offload_on"
    result["evidence"] = "measured"
    result["runtime_variant"] = "offload-on"
    result["runtime_identity"] = {
        "resolved_config": {
            "kv_offloading_backend": "native",
            "kv_offloading_size_gb": 140.0,
        },
        "environment": {"VLLM_USE_SIMPLE_KV_OFFLOAD": None},
    }
    for record in result["records"]:
        for repeat in record["repeat_measurements"]:
            repeat["measurement_source"] = {"worker_hook_observed": True}
        record["measurement_source"] = {
            "worker_hook_observed": True,
            "worker_hook_observed_repeats": [True, True, True],
            "primary_statistic": "arithmetic_mean",
        }
    return result


def test_longctx_parser_enforces_formal_measured_grid_and_worker_source(tmp_path):
    result = _formal_measured_longctx_shape(tmp_path)
    longctx_kv_parser.validate(result)

    missing = copy.deepcopy(result)
    missing["records"].pop()
    with pytest.raises(ValidationError, match="omitted requested sequence lengths"):
        longctx_kv_parser.validate(missing)

    unobserved = copy.deepcopy(result)
    unobserved["records"][0]["measurement_source"]["worker_hook_observed"] = False
    with pytest.raises(ValidationError, match="worker_hook_observed"):
        longctx_kv_parser.validate(unobserved)


def test_longctx_parser_enforces_terminal_oom_semantics(tmp_path):
    result = _formal_measured_longctx_shape(tmp_path)
    result["records"][-1] = {
        "seq_len": longctx_kv_parser.FORMAL_SEQ_LENS[-1],
        "oom": True,
        "measurement_failed": False,
        "stopped_sweep": True,
        "error": "CUDA out of memory",
        "failure_classification": "CUDA_OR_ENGINE_OOM",
        "repeat_index": 0,
        "repeats_expected": 3,
        "valid_repeats_completed": 0,
        "completed_repeat_measurements": [],
    }
    longctx_kv_parser.validate(result)

    not_stopped = copy.deepcopy(result)
    not_stopped["records"][-1]["stopped_sweep"] = False
    with pytest.raises(ValidationError, match="stopped_sweep"):
        longctx_kv_parser.validate(not_stopped)

    not_last = copy.deepcopy(result)
    not_last["records"].append(copy.deepcopy(not_last["records"][0]))
    with pytest.raises(ValidationError, match="terminal OOM is not last"):
        longctx_kv_parser.validate(not_last)


def test_longctx_parser_recomputes_means_and_conservation(tmp_path):
    result = _formal_measured_longctx_shape(tmp_path)

    bad_mean = copy.deepcopy(result)
    bad_mean["records"][0]["ttft_ns"] += 1
    with pytest.raises(ValidationError, match="arithmetic mean"):
        longctx_kv_parser.validate(bad_mean)

    bad_array = copy.deepcopy(result)
    bad_array["records"][0]["ttft_ns_repeats"][0] += 1
    with pytest.raises(ValidationError, match="disagrees with raw repeat"):
        longctx_kv_parser.validate(bad_array)

    too_many_blocks = copy.deepcopy(result)
    row = too_many_blocks["records"][-1]["repeat_measurements"][0]
    row["kv_offloaded_blocks"] = row["kv_blocks_total"] + 1
    with pytest.raises(ValidationError, match="exceeds kv_blocks_total"):
        longctx_kv_parser.validate(too_many_blocks)

    bad_block_accounting = copy.deepcopy(result)
    row = bad_block_accounting["records"][-1]["repeat_measurements"][0]
    row["kv_offloaded_blocks"] -= 1
    with pytest.raises(ValidationError, match="offloaded-byte accounting"):
        longctx_kv_parser.validate(bad_block_accounting)

    move_without_offload = copy.deepcopy(result)
    row = move_without_offload["records"][0]["repeat_measurements"][0]
    row["kv_move_ns"] = 1
    row["kv_move_bytes"] = 1
    with pytest.raises(ValidationError, match="nonzero KV move"):
        longctx_kv_parser.validate(move_without_offload)


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


def _formal_measured_dispatch_shape(tmp_path):
    args = inserving_dispatch_probe.parse_args([
        "--backend", "mock_dispatch",
        "--concurrency", "1,2,4,8",
        "--steps", "128",
        "--repeats", "3",
        "--out", str(tmp_path / "formal-shape.json"),
    ])
    result = inserving_dispatch_probe.run(args)
    result["backend"] = "vllm_dispatch"
    result["evidence"] = "measured"
    result["runtime_identity"] = {
        "worker_extension_cls": "test",
        "instrumentation_perturbation": {
            "method": "paired_uninstrumented_vs_instrumented_serving_latency",
            "sample_count": 3,
            "uninstrumented_control_latency_ns": 1000,
            "instrumented_latency_ns": 1040,
            "relative_overhead": 0.04,
            "threshold": 0.05,
            "status": "PASS",
        },
    }
    for group in result["groups"]:
        for step in group["per_step"]:
            step["measurement_source"] = {"worker_hook_observed": True}
    return result


def test_dispatch_parser_enforces_formal_measured_grid_and_worker_source(tmp_path):
    result = _formal_measured_dispatch_shape(tmp_path)
    dispatch_parser.validate(result)

    missing = copy.deepcopy(result)
    missing["groups"][0]["per_step"].pop()
    with pytest.raises(ValidationError, match="steps_instrumented|formal"):
        dispatch_parser.validate(missing)

    unobserved = copy.deepcopy(result)
    unobserved["groups"][0]["per_step"][0]["measurement_source"][
        "worker_hook_observed"
    ] = False
    with pytest.raises(ValidationError, match="worker_hook_observed"):
        dispatch_parser.validate(unobserved)


def test_dispatch_parser_rejects_backend_evidence_and_physical_shape_drift(tmp_path):
    result = _formal_measured_dispatch_shape(tmp_path)

    arbitrary_backend = copy.deepcopy(result)
    arbitrary_backend["backend"] = "hand_written_fixture"
    with pytest.raises(ValidationError, match="unregistered backend/evidence"):
        dispatch_parser.validate(arbitrary_backend)

    missing_period = copy.deepcopy(result)
    del missing_period["groups"][0]["per_step"][0]["dispatch_period_ns"]
    with pytest.raises(ValidationError, match="dispatch_period_ns"):
        dispatch_parser.validate(missing_period)

    wrong_routing_width = copy.deepcopy(result)
    step = wrong_routing_width["groups"][0]["per_step"][0]
    step["expert_tokens"] = 999
    with pytest.raises(ValidationError, match="routing_width"):
        dispatch_parser.validate(wrong_routing_width)


def test_dispatch_parser_enforces_instrumentation_perturbation_limit(tmp_path):
    result = _formal_measured_dispatch_shape(tmp_path)
    guard = result["runtime_identity"]["instrumentation_perturbation"]
    guard["instrumented_latency_ns"] = 1060
    guard["relative_overhead"] = 0.06
    with pytest.raises(ValidationError, match="exceeds 5%"):
        dispatch_parser.validate(result)

    missing_guard = _formal_measured_dispatch_shape(tmp_path)
    del missing_guard["runtime_identity"]["instrumentation_perturbation"]
    with pytest.raises(ValidationError, match="instrumentation_perturbation"):
        dispatch_parser.validate(missing_guard)


def test_dispatch_probe_persists_structured_runtime_refusal(tmp_path, monkeypatch):
    class RefusingRuntimeBackend:
        name = "vllm_dispatch"
        runtime_config = {"model": "/model"}
        runtime_identity = {
            "adapter": "test-refusal",
            "measurement_capability": {
                "status": "TARGET_1_MEASUREMENT_REFUSED",
                "measurement_supported": False,
                "refused_fields": ["T_prepare_ns"],
                "reasons": ["worker timing hook unavailable"],
            },
        }

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    backend = RefusingRuntimeBackend()
    monkeypatch.setattr(
        inserving_dispatch_probe, "_build_backend", lambda *_a, **_k: backend
    )
    out = tmp_path / "refused.json"
    rc = inserving_dispatch_probe.main([
        "--backend", "vllm_dispatch",
        "--model-path", "/model",
        "--runtime-adapter-module", "measurement.probes.vllm_runtime_adapter",
        "--concurrency", "1,2,4,8",
        "--steps", "128",
        "--repeats", "3",
        "--out", str(out),
    ])
    result = json.loads(out.read_text())
    assert rc == 1
    assert backend.closed is True
    assert result["runtime_identity"] == backend.runtime_identity
    assert result["evidence"] == "measurement_refused_not_measurement"
    assert result["terminal_failure"]["classification"] == (
        "TARGET_1_MEASUREMENT_REFUSED"
    )
    assert result["groups"] == []
    assert result["ir_evaluation_points"] == []
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
        assert pt["evidence_class"] == "SYNTHETIC"
        assert pt["fidelity"] == "FUNCTIONAL_ONLY"
        assert pt["repetitions"] == 3
        assert pt["sample_count"] == 3


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
            evidence_class="SYNTHETIC", fidelity="FUNCTIONAL_ONLY",
            measurement_noise_floor="0",
            bootstrap_ci_95={"lower": "1", "upper": "1"},
        )


def test_ir_point_builder_rejects_coordinate_outside_envelope():
    with pytest.raises(IREvaluationPointError, match="outside envelope"):
        build_calibration_point(
            metric="x", unit="ns", measured_value="1",
            coordinate=[{"name": "seq_len", "value": "999999"}],
            envelope_dimensions=[{"name": "seq_len", "lower": "1", "upper": "10"}],
            runtime_variant_hash="a" * 64, repetitions=3, sample_count=3,
            resampling_strata=["x"],
            evidence_class="SYNTHETIC", fidelity="FUNCTIONAL_ONLY",
            measurement_noise_floor="0",
            bootstrap_ci_95={"lower": "1", "upper": "1"},
        )


def test_ir_point_builder_never_inflates_observed_repetitions():
    with pytest.raises(IREvaluationPointError, match="never inflate"):
        build_calibration_point(
            metric="x", unit="ns", measured_value="1",
            coordinate=[{"name": "seq_len", "value": "1"}],
            envelope_dimensions=[{"name": "seq_len", "lower": "1", "upper": "1"}],
            runtime_variant_hash="a" * 64, repetitions=1, sample_count=1,
            resampling_strata=["x"],
            evidence_class="SYNTHETIC", fidelity="FUNCTIONAL_ONLY",
            measurement_noise_floor="0",
            bootstrap_ci_95={"lower": "1", "upper": "1"},
        )


def test_ir_point_validator_fixtures():
    ok = json.loads((FIXTURES / "ir_points_pass.json").read_text())
    ir_point_validator.validate_probe_result(ok)
    bad = json.loads((FIXTURES / "ir_points_fail_coordinate.json").read_text())
    with pytest.raises(ValidationError, match="coordinate names|envelope"):
        ir_point_validator.validate_probe_result(bad)
