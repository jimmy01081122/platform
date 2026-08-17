#!/usr/bin/env python3
"""Validate or execute an EdgeFlow multi-fidelity experiment config."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow.multifidelity import (  # noqa: E402
    CalibrationPackError,
    ConfigError,
    MultiFidelityDispatcher,
    load_data_file,
    validate_experiment_config,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "run"))
    parser.add_argument("--config", required=True, help="JSON or YAML experiment config")
    parser.add_argument("--output", help="write result JSON here; stdout when omitted")
    args = parser.parse_args(argv)

    try:
        config = load_data_file(args.config)
        if args.command == "validate":
            validated = validate_experiment_config(config)
            result = {
                "status": "VALID",
                "experiment_id": validated["experiment_id"],
                "fidelity": validated["fidelity"],
            }
        else:
            result = MultiFidelityDispatcher().dispatch(config)
    except (OSError, json.JSONDecodeError, ConfigError, CalibrationPackError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
