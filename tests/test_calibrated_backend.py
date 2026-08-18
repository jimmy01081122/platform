import json
from pathlib import Path

import pytest

from edgeflow.calibrated_backend import CalibrationError, calibrate


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _calibration_rows() -> list[dict]:
    def row(record_id: str, role: str, value: float, **fields: object) -> dict:
        return {
            "record_id": record_id,
            "calibration_role": role,
            "repeats_ms": [value, value],
            **fields,
        }

    return [
        row("cal-cpu", "cpu_runtime", 0.1),
        # Two distinct shape points for the (non-excluded) selected_expert op:
        # the production fit now requires >= 2 distinct shape-axis values and no
        # longer silently falls back to a flat constant for single-shape input
        # (see calibrated_backend.SHAPE_INSENSITIVE_OPERATIONS / P-012 follow-up).
        # expert_tokens=1 is the component evaluation point's effective
        # tokens_per_launch, so predicted selected_expert cost there stays exactly
        # 1.0 ms and the downstream MAPE assertions are unchanged; the second
        # point only supplies a small physical positive slope.
        row("cal-gpu", "gpu_service", 1.0, operation="selected_expert", case="fixture,expert_tokens=1"),
        row("cal-gpu-2", "gpu_service", 1.001, operation="selected_expert", case="fixture,expert_tokens=2"),
        row("cal-h2d-100", "pcie_transfer", 0.3, direction="h2d", bytes=100),
        row("cal-h2d-200", "pcie_transfer", 0.4, direction="h2d", bytes=200),
        row("cal-d2h-100", "pcie_transfer", 0.375, direction="d2h", bytes=100),
        row("cal-d2h-200", "pcie_transfer", 0.5, direction="d2h", bytes=200),
        row(
            "cal-copy",
            "copy_engine",
            0.24,
            direction="h2d",
            bytes=100,
            copy_streams=2,
        ),
        row("cal-memory-100", "memory", 0.1, bytes=100),
        row("cal-memory-300", "memory", 0.2, bytes=300),
        row("cal-queue-1", "queueing", 0.05, queue_depth=1),
        row("cal-queue-3", "queueing", 0.11, queue_depth=3),
        row(
            "cal-contention",
            "contention",
            1.1,
            concurrency=2,
            base_service_ms=1.0,
        ),
    ]


def _points(prefix: str, component_multiplier: float = 1.0) -> list[dict]:
    component_features = {
        "cpu_calls": 1,
        "gpu_operations": {"selected_expert": 1},
        "memory_bytes": 100,
        "queue_depth": 1,
        "concurrency": 1,
    }
    replay_features = {
        **component_features,
        "tokens": 10,
        "transfers": [{"direction": "h2d", "bytes": 100, "copy_streams": 1}],
    }
    domain = {"phase": "decode", "concurrency": 1, "platform": "synthetic"}
    return [
        {
            "point_id": f"{prefix}-component",
            "metric": "component_latency",
            "measured": 1.25 * component_multiplier,
            "features": component_features,
            "domain": domain,
        },
        {
            "point_id": f"{prefix}-pcie",
            "metric": "pcie_transfer_latency",
            "measured": 0.3,
            "features": {"direction": "h2d", "bytes": 100, "copy_streams": 1},
            "domain": domain,
        },
        {
            "point_id": f"{prefix}-tpot",
            "metric": "moe_replay_tpot",
            "measured": 0.155,
            "features": replay_features,
            "domain": domain,
        },
        {
            "point_id": f"{prefix}-throughput",
            "metric": "moe_replay_throughput",
            "measured": 1000.0 / 0.155,
            "features": replay_features,
            "domain": domain,
        },
    ]


@pytest.fixture
def synthetic_calibration_pack(tmp_path: Path) -> Path:
    """Build an explicitly non-hardware fixture with all four isolated splits."""
    _write_json(
        tmp_path / "environment.json",
        {
            "hardware": "synthetic",
            "driver": "synthetic",
            "cuda": "synthetic",
            "runtime": "synthetic",
        },
    )
    profiler = tmp_path / "profiler.json"
    _write_json(profiler, {"scope": "synthetic-unit-test"})
    split_rows = {
        "calibration": _calibration_rows(),
        "validation": [
            {"record_id": "validation-raw", "repeats_ms": [1.0, 1.0]}
        ],
        "holdout_workload": [
            {"record_id": "workload-raw", "repeats_ms": [1.0, 1.0]}
        ],
        "holdout_platform": [
            {"record_id": "platform-raw", "repeats_ms": [1.0, 1.0]}
        ],
    }
    data_split = {}
    for split, rows in split_rows.items():
        result = {
            "schema_version": "gpu-benchmark-result-v1",
            "status": "synthetic-test-fixture",
            "evidence": "synthetic-unit-test",
            "hardware_validation": False,
            "split": split,
            "raw_profiler_output": "profiler.json",
            "raw_benchmarks": rows,
        }
        if split != "calibration":
            result["evaluation_points"] = _points(
                split, component_multiplier=1.1 if split == "validation" else 1.0
            )
        artifact = tmp_path / f"{split}.json"
        _write_json(artifact, result)
        data_split[split] = {
            "selector": split,
            "artifact": artifact.name,
            "sha256": None,
            "evidence": {"class": "synthetic"},
        }
    pack = {
        "schema_version": "calibration-pack-v1",
        "pack_id": "synthetic-calibrated-backend-unit-test",
        "status": "frozen",
        "calibrated_domain": None,
        "data_split": data_split,
        "measurement_contract": {
            "environment_manifest": "environment.json",
        },
    }
    pack_path = tmp_path / "pack.json"
    _write_json(pack_path, pack)
    return pack_path


def test_synthetic_fit_is_serializable_and_mape_is_correct(
    synthetic_calibration_pack: Path,
) -> None:
    report = calibrate(
        synthetic_calibration_pack, allow_synthetic_test_fixture=True
    )
    json.dumps(report)
    assert report["evidence"] == "synthetic-unit-test"
    assert report["hardware_validation"] is False
    assert report["parameters"]["fit_split"] == "calibration"
    assert report["parameters"]["pcie"]["h2d"]["bandwidth_bytes_per_ms"] == pytest.approx(
        1000.0
    )
    component = report["metrics"]["component_latency"]
    expected_validation_ape = abs(1.25 - 1.375) / 1.375 * 100.0
    assert component["rows"][0]["ape_pct"] == pytest.approx(expected_validation_ape)
    assert component["mape_pct"] == pytest.approx(expected_validation_ape / 3.0)
    assert report["split_metrics"]["validation"]["component_latency"][
        "mape_pct"
    ] == pytest.approx(expected_validation_ape)
    assert report["status"] == "pass"


def test_split_leakage_is_rejected(synthetic_calibration_pack: Path) -> None:
    pack = json.loads(synthetic_calibration_pack.read_text())
    validation_path = synthetic_calibration_pack.parent / "validation.json"
    validation = json.loads(validation_path.read_text())
    validation["raw_benchmarks"][0]["record_id"] = "cal-cpu"
    _write_json(validation_path, validation)
    with pytest.raises(CalibrationError, match="split leakage"):
        calibrate(synthetic_calibration_pack, allow_synthetic_test_fixture=True)


def test_validation_probe_cannot_influence_fit(
    synthetic_calibration_pack: Path,
) -> None:
    validation_path = synthetic_calibration_pack.parent / "validation.json"
    validation = json.loads(validation_path.read_text())
    validation["raw_benchmarks"][0].update(
        {"calibration_role": "cpu_runtime", "repeats_ms": [999.0, 999.0]}
    )
    _write_json(validation_path, validation)
    report = calibrate(
        synthetic_calibration_pack, allow_synthetic_test_fixture=True
    )
    assert report["parameters"]["cpu_runtime"]["per_call_ms"] == pytest.approx(0.1)


def test_split_mape_failure_is_retained_and_status_is_fail(
    synthetic_calibration_pack: Path,
) -> None:
    validation_path = synthetic_calibration_pack.parent / "validation.json"
    validation = json.loads(validation_path.read_text())
    validation["evaluation_points"][0]["measured"] = 2.0
    _write_json(validation_path, validation)
    report = calibrate(
        synthetic_calibration_pack, allow_synthetic_test_fixture=True
    )
    component = report["metrics"]["component_latency"]
    assert report["status"] == "fail"
    assert component["mape_pass"] is True
    assert component["over_threshold_points"][0]["point_id"] == "validation-component"
    assert "validation-component" in report["calibrated_domain"]["excluded_point_ids"]


@pytest.mark.parametrize("missing", ["raw", "environment"])
def test_missing_raw_or_environment_fails_fast(
    synthetic_calibration_pack: Path, missing: str
) -> None:
    if missing == "raw":
        validation_path = synthetic_calibration_pack.parent / "validation.json"
        validation = json.loads(validation_path.read_text())
        validation.pop("raw_benchmarks")
        _write_json(validation_path, validation)
        expected = "lacks raw_benchmarks"
    else:
        (synthetic_calibration_pack.parent / "environment.json").unlink()
        expected = "missing required artifact"
    with pytest.raises(CalibrationError, match=expected):
        calibrate(synthetic_calibration_pack, allow_synthetic_test_fixture=True)


def test_synthetic_fixture_requires_explicit_opt_in(
    synthetic_calibration_pack: Path,
) -> None:
    with pytest.raises(CalibrationError, match="not measured hardware evidence"):
        calibrate(synthetic_calibration_pack)
