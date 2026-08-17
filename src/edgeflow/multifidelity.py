"""Validated multi-fidelity dispatch and calibrated-result gates.

The dispatcher intentionally refuses to promote a planned calibration pack into a
"calibrated" run.  Backend adapters may execute only after their required evidence
contract is present; this prevents a config label from silently upgrading fidelity.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
FIDELITIES = ("fast", "calibrated", "detailed", "rtl-transaction", "rtl-cycle")


class ConfigError(ValueError):
    """Experiment configuration violates the current fidelity contract."""


class CalibrationPackError(ConfigError):
    """Calibration pack is absent, invalid, or not ready for calibrated claims."""


def load_data_file(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    data = json.loads(text) if source.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping")
    return data


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _validate(instance: dict[str, Any], schema_name: str) -> None:
    schema = _schema(schema_name)
    resolver = jsonschema.RefResolver(
        base_uri=SCHEMAS.as_uri() + "/",
        referrer=schema,
        store={path.name: _schema(path.name) for path in SCHEMAS.glob("*.schema.json")},
    )
    errors = sorted(
        jsonschema.Draft7Validator(schema, resolver=resolver).iter_errors(instance),
        key=lambda error: list(error.path),
    )
    if errors:
        rendered = "; ".join(
            f"{'.'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in errors[:5]
        )
        raise ConfigError(rendered)


def validate_experiment_config(config: dict[str, Any]) -> dict[str, Any]:
    simulation = config.get("simulation")
    if not isinstance(simulation, dict):
        raise ConfigError("simulation mapping is required")
    wrapper = {
        "schema_version": config.get("schema_version", "simulation-config-v2"),
        "simulation": simulation,
    }
    _validate(wrapper, "simulation_config.schema.json")
    fidelity = simulation["fidelity"]
    if fidelity not in FIDELITIES:
        raise ConfigError(f"unsupported fidelity: {fidelity}")
    experiment_id = config.get("experiment_id", "standalone-simulation")
    return {
        "experiment_id": experiment_id,
        "fidelity": fidelity,
        "backend": simulation["backend"],
        "simulation": simulation,
    }


def load_calibration_pack(path: str | Path, *, require_ready: bool = True) -> dict[str, Any]:
    pack_path = Path(path)
    if not pack_path.is_absolute():
        pack_path = ROOT / pack_path
    pack = load_data_file(pack_path)
    try:
        _validate(pack, "calibration_pack.schema.json")
    except ConfigError as error:
        raise CalibrationPackError(f"{pack_path}: {error}") from error
    if require_ready and pack["status"] not in {"frozen", "validated"}:
        raise CalibrationPackError(
            f"{pack_path}: status={pack['status']!r}; calibrated execution requires frozen or validated"
        )
    return pack


@dataclass(frozen=True)
class Backend:
    name: str
    fidelities: frozenset[str]
    execute: Callable[[dict[str, Any]], dict[str, Any]]


def _boundary_result(config: dict[str, Any]) -> dict[str, Any]:
    simulation = config["simulation"]
    return {
        "status": "BOUNDARY_ONLY",
        "experiment_id": config["experiment_id"],
        "simulation_fidelity": config["fidelity"],
        "backend": config["backend"],
        "evidence": "unknown",
        "claim_limit": (
            "dispatcher and contract validated; no backend measurement or simulation was executed"
        ),
        "workload_window": simulation.get("workload_window"),
    }


class MultiFidelityDispatcher:
    """Dispatch only to explicitly registered adapters with matching fidelity."""

    def __init__(self) -> None:
        self._backends: dict[str, Backend] = {
            "edgeflow": Backend(
                name="edgeflow",
                fidelities=frozenset({"fast"}),
                execute=_boundary_result,
            )
        }

    def register(self, backend: Backend) -> None:
        if backend.name in self._backends:
            raise ConfigError(f"backend already registered: {backend.name}")
        self._backends[backend.name] = backend

    def dispatch(self, raw_config: dict[str, Any]) -> dict[str, Any]:
        config = validate_experiment_config(raw_config)
        simulation = config["simulation"]
        if config["fidelity"] == "calibrated":
            load_calibration_pack(simulation["calibration_pack"], require_ready=True)
        backend = self._backends.get(config["backend"])
        if backend is None:
            raise ConfigError(
                f"backend {config['backend']!r} is not registered; "
                "an unavailable detailed/RTL backend cannot be silently substituted"
            )
        if config["fidelity"] not in backend.fidelities:
            raise ConfigError(
                f"backend {backend.name!r} does not support fidelity {config['fidelity']!r}"
            )
        return backend.execute(config)


def absolute_percentage_error(predicted: float, measured: float) -> float:
    if measured == 0:
        raise ValueError("MAPE is undefined when a measured value is zero")
    return abs(predicted - measured) / abs(measured) * 100.0


def evaluate_mape(
    rows: list[dict[str, float]],
    *,
    aggregate_limit_percent: float = 15.0,
    point_limit_percent: float = 20.0,
) -> dict[str, Any]:
    """Evaluate without dropping failed points; callers must retain the returned rows."""
    if not rows:
        raise ValueError("at least one validation row is required")
    evaluated = []
    for row in rows:
        ape = absolute_percentage_error(row["predicted"], row["measured"])
        evaluated.append({**row, "absolute_percentage_error": ape, "point_pass": ape <= point_limit_percent})
    mape = sum(row["absolute_percentage_error"] for row in evaluated) / len(evaluated)
    return {
        "mape_percent": mape,
        "aggregate_limit_percent": aggregate_limit_percent,
        "point_limit_percent": point_limit_percent,
        "aggregate_pass": mape <= aggregate_limit_percent,
        "all_points_within_limit": all(row["point_pass"] for row in evaluated),
        "status": (
            "passed"
            if mape <= aggregate_limit_percent and all(row["point_pass"] for row in evaluated)
            else "failed"
        ),
        "rows": evaluated,
        "on_failure": ["retain", "analyze", "restrict-domain", "do-not-hide"],
    }
