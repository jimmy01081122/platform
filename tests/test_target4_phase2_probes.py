"""CPU-only contract tests for the three target_4 Phase 2 probes.

Normal mock outputs traverse the same serialization and parsers as GPU outputs.
Every malformed/partial case raises; no parser silently drops a cell.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from measurement.parsers.common import ValidationError
from measurement.parsers.component_eval_parser import validate as component_eval_validate
from measurement.parsers import (
    component_shape_parser,
    dequant_weight_bytes_parser,
    sort_permute_parser,
)
from measurement.probes import (
    component_shape_probe,
    dequant_weight_bytes_probe,
    sort_permute_probe,
)
from measurement.probes.mock_backend import BackendError
from measurement.probes.target4_phase2_backend import (
    MockTarget4Backend,
    TorchTarget4Backend,
    resolve_backend,
)
from measurement.probes.target4_phase2_common import (
    DIRECT_DERIVATION,
    FROZEN_EXPERT_TOKENS,
    SORT_PERMUTE_OPERATIONS,
)


def _load(path):
    return json.loads(path.read_text())


def _component_result(tmp_path):
    path = tmp_path / "component.json"
    assert component_shape_probe.main([
        "--backend", "mock",
        "--sealed-manifest", str(
            Path("calibration/sealed/holdout_split_v1_manifest.json").resolve()
        ),
        "--split", "fit",
        "--out", str(path),
    ]) == 0
    return _load(path)


def _sort_result(tmp_path):
    path = tmp_path / "sort.json"
    assert sort_permute_probe.main([
        "--backend", "mock", "--out", str(path),
    ]) == 0
    return _load(path)


def _dequant_result(tmp_path):
    path = tmp_path / "dequant.json"
    assert dequant_weight_bytes_probe.main([
        "--backend", "mock",
        "--weight-bytes", "1048576,22020096,88080384",
        "--fixed-expert-tokens", "32",
        "--out", str(path),
    ]) == 0
    return _load(path)


def test_component_shape_sealed_fit_subset_and_gap4(tmp_path):
    result = _component_result(tmp_path)
    component_shape_parser.validate(result)
    component_eval_validate(result, enforce_gap4=True)
    assert len(result["raw_benchmarks"]) == 41
    assert result["axes"]["expert_tokens"] == list(FROZEN_EXPERT_TOKENS)
    assert all("case" not in row for row in result["raw_benchmarks"])
    assert all(
        point["derivation"] == DIRECT_DERIVATION
        and "expert_tokens" in point["features"]
        and point["domain"]["split"] == "fit"
        for point in result["evaluation_points"]
    )


def test_component_shape_parser_rejects_partial_grid(tmp_path):
    result = _component_result(tmp_path)
    result["raw_benchmarks"].pop()
    result["evaluation_points"].pop()
    with pytest.raises(ValidationError, match="incomplete sealed fit subset"):
        component_shape_parser.validate(result)


def test_component_shape_parser_rejects_case_string(tmp_path):
    result = _component_result(tmp_path)
    result["raw_benchmarks"][0]["case"] = "expert_tokens=8"
    with pytest.raises(ValidationError, match="case is forbidden"):
        component_shape_parser.validate(result)


def test_component_shape_parser_rejects_point_shape_drift(tmp_path):
    result = _component_result(tmp_path)
    result["evaluation_points"][0]["features"]["expert_tokens"] = 999
    with pytest.raises(ValidationError, match="expert_tokens"):
        component_shape_parser.validate(result)


def test_parser_rejects_mock_masquerading_as_measured(tmp_path):
    result = _component_result(tmp_path)
    result["evidence"] = "measured"
    with pytest.raises(ValidationError, match="mock may never masquerade"):
        component_shape_parser.validate(result)


def test_component_probe_rejects_nonfrozen_expert_axis(tmp_path):
    args = component_shape_probe.parse_args([
        "--backend", "mock", "--expert-tokens", "8,16",
        "--sealed-manifest", str(
            Path("calibration/sealed/holdout_split_v1_manifest.json").resolve()
        ),
        "--split", "fit",
        "--out", str(tmp_path / "bad.json"),
    ])
    with pytest.raises(BackendError, match="frozen"):
        component_shape_probe.run(args)


def test_sort_permute_full_explicit_graph_and_gap4(tmp_path):
    result = _sort_result(tmp_path)
    sort_permute_parser.validate(result)
    component_eval_validate(result, enforce_gap4=True)
    assert len(result["raw_benchmarks"]) == 32
    for tokens in FROZEN_EXPERT_TOKENS:
        operations = {
            row["operation"] for row in result["raw_benchmarks"]
            if row["expert_tokens"] == tokens
        }
        assert operations == set(SORT_PERMUTE_OPERATIONS)


def test_sort_permute_parser_rejects_missing_cell(tmp_path):
    result = _sort_result(tmp_path)
    result["raw_benchmarks"].pop()
    result["evaluation_points"].pop()
    with pytest.raises(ValidationError, match="incomplete formal grid"):
        sort_permute_parser.validate(result)


def test_sort_probe_rejects_nonfrozen_n_axis(tmp_path):
    args = sort_permute_probe.parse_args([
        "--backend", "mock", "--expert-tokens", "8,16,32",
        "--out", str(tmp_path / "bad.json"),
    ])
    with pytest.raises(BackendError, match="frozen"):
        sort_permute_probe.run(args)


def test_dequant_weight_bytes_grid_fixed_control_and_proxy_label(tmp_path):
    result = _dequant_result(tmp_path)
    dequant_weight_bytes_parser.validate(result)
    component_eval_validate(result, enforce_gap4=True)
    assert result["evidence_limit"] == "DEQUANT_PROXY_ONLY"
    assert {row["expert_tokens"] for row in result["raw_benchmarks"]} == {32}
    assert {row["weight_bytes"] for row in result["raw_benchmarks"]} == {
        1048576, 22020096, 88080384,
    }


def test_dequant_parser_rejects_token_control_drift(tmp_path):
    result = _dequant_result(tmp_path)
    result["raw_benchmarks"][1]["expert_tokens"] = 64
    with pytest.raises(ValidationError, match="expert_tokens"):
        dequant_weight_bytes_parser.validate(result)


def test_dequant_probe_requires_owner_axes(tmp_path):
    with pytest.raises(SystemExit):
        dequant_weight_bytes_probe.parse_args([
            "--backend", "mock", "--out", str(tmp_path / "missing.json"),
        ])


@pytest.mark.parametrize(
    "weight_axis,match",
    [("1048576", "at least two"), ("65,129", "group128")],
)
def test_dequant_probe_rejects_unfit_or_unaligned_axis(tmp_path, weight_axis, match):
    args = dequant_weight_bytes_probe.parse_args([
        "--backend", "mock", "--weight-bytes", weight_axis,
        "--fixed-expert-tokens", "32", "--out", str(tmp_path / "bad.json"),
    ])
    with pytest.raises(BackendError, match=match):
        dequant_weight_bytes_probe.run(args)


def test_mock_backend_is_deterministic_and_stamped_synthetic():
    backend = MockTarget4Backend()
    first = backend.measure_sort_permute("argsort_route", 128, 5)
    second = backend.measure_sort_permute("argsort_route", 128, 5)
    assert first == second
    assert backend.evidence == "cpu_smoke_test_not_measurement"


def test_gpu_backend_refuses_cuda_unavailable_without_fallback(monkeypatch):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    with pytest.raises(BackendError, match="CUDA unavailable"):
        TorchTarget4Backend()


def test_unknown_backend_fails_closed():
    with pytest.raises(BackendError, match="unregistered"):
        resolve_backend("fallback")


def test_repeat_count_is_frozen_for_all_three_probes(tmp_path):
    component = component_shape_probe.parse_args([
        "--backend", "mock", "--repeats", "4",
        "--sealed-manifest", str(
            Path("calibration/sealed/holdout_split_v1_manifest.json").resolve()
        ),
        "--split", "fit", "--out", str(tmp_path / "c.json"),
    ])
    sort = sort_permute_probe.parse_args([
        "--backend", "mock", "--repeats", "4", "--out", str(tmp_path / "s.json"),
    ])
    dequant = dequant_weight_bytes_probe.parse_args([
        "--backend", "mock", "--weight-bytes", "1048576,22020096",
        "--fixed-expert-tokens", "32", "--repeats", "4",
        "--out", str(tmp_path / "d.json"),
    ])
    for probe, args in (
        (component_shape_probe, component),
        (sort_permute_probe, sort),
        (dequant_weight_bytes_probe, dequant),
    ):
        with pytest.raises(BackendError, match="n=5"):
            probe.run(args)


def test_target4_parser_rejects_control_and_statistics_drift(tmp_path):
    result = _sort_result(tmp_path)

    root_warmup = copy.deepcopy(result)
    root_warmup["warmup"] = 9
    with pytest.raises(ValidationError, match="warmup"):
        sort_permute_parser.validate(root_warmup)

    invalid_iterations = copy.deepcopy(result)
    invalid_iterations["raw_benchmarks"][0]["inner_iterations"] = 0
    with pytest.raises(ValidationError, match="inner_iterations"):
        sort_permute_parser.validate(invalid_iterations)

    tampered_variance = copy.deepcopy(result)
    tampered_variance["raw_benchmarks"][0]["statistics"]["variance"] += 1
    with pytest.raises(ValidationError, match="statistics.variance"):
        sort_permute_parser.validate(tampered_variance)

    tampered_ci = copy.deepcopy(result)
    tampered_ci["raw_benchmarks"][0]["statistics"]["ci95"][1] += 1
    with pytest.raises(ValidationError, match="statistics.ci95"):
        sort_permute_parser.validate(tampered_ci)


def test_target4_parser_requires_complete_measured_runtime_identity(tmp_path):
    result = _sort_result(tmp_path)
    result["backend"] = "gpu"
    result["evidence"] = "measured"
    result["runtime_identity"] = {"backend": "gpu"}
    with pytest.raises(ValidationError, match="torch_version"):
        sort_permute_parser.validate(result)


def test_component_probe_refuses_accidental_holdout_measurement(tmp_path):
    args = component_shape_probe.parse_args([
        "--backend", "mock",
        "--sealed-manifest", str(
            Path("calibration/sealed/holdout_split_v1_manifest.json").resolve()
        ),
        "--split", "holdout",
        "--out", str(tmp_path / "holdout.json"),
    ])
    with pytest.raises(BackendError, match="sealed for STAGE_A4"):
        component_shape_probe.run(args)


def test_component_probe_isolates_authorized_holdout_subset(tmp_path):
    path = tmp_path / "holdout.json"
    assert component_shape_probe.main([
        "--backend", "mock",
        "--sealed-manifest", str(
            Path("calibration/sealed/holdout_split_v1_manifest.json").resolve()
        ),
        "--split", "holdout",
        "--authorize-holdout-measurement",
        "--out", str(path),
    ]) == 0
    result = _load(path)
    component_shape_parser.validate(result)
    assert len(result["raw_benchmarks"]) == 12
    assert {row["sealed_split"] for row in result["raw_benchmarks"]} == {"holdout"}


def test_component_parser_rejects_split_relabel(tmp_path):
    result = _component_result(tmp_path)
    result["raw_benchmarks"][0]["sealed_split"] = "holdout"
    with pytest.raises(ValidationError, match="sealed_split"):
        component_shape_parser.validate(result)
