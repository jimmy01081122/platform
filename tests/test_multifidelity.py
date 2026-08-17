from pathlib import Path

import pytest
import yaml

from edgeflow.multifidelity import (
    CalibrationPackError,
    ConfigError,
    MultiFidelityDispatcher,
    evaluate_mape,
    validate_experiment_config,
)


ROOT = Path(__file__).resolve().parents[1]


def test_fast_config_validates_and_dispatches_boundary() -> None:
    config = {
        "experiment_id": "smoke",
        "schema_version": "simulation-config-v2",
        "simulation": {
            "fidelity": "fast",
            "backend": "edgeflow",
            "calibration_pack": None,
            "workload_window": None,
            "checkpoint": False,
        },
    }
    assert validate_experiment_config(config)["fidelity"] == "fast"
    result = MultiFidelityDispatcher().dispatch(config)
    assert result["status"] == "BOUNDARY_ONLY"
    assert result["evidence"] == "unknown"


def test_calibrated_config_refuses_planned_pack() -> None:
    config = yaml.safe_load((ROOT / "configs/fidelity/default_simulation.yaml").read_text())
    with pytest.raises(CalibrationPackError, match="requires frozen or validated"):
        MultiFidelityDispatcher().dispatch(config)


def test_unregistered_backend_is_not_silently_substituted() -> None:
    config = {
        "schema_version": "simulation-config-v2",
        "simulation": {
            "fidelity": "detailed",
            "backend": "gpu-cycle-missing",
            "calibration_pack": None,
            "workload_window": "representative",
            "checkpoint": True,
        },
    }
    with pytest.raises(ConfigError, match="not registered"):
        MultiFidelityDispatcher().dispatch(config)


def test_mape_retains_failed_points() -> None:
    result = evaluate_mape(
        [
            {"predicted": 100.0, "measured": 100.0},
            {"predicted": 150.0, "measured": 100.0},
        ]
    )
    assert result["status"] == "failed"
    assert len(result["rows"]) == 2
    assert result["rows"][1]["point_pass"] is False
    assert "do-not-hide" in result["on_failure"]
