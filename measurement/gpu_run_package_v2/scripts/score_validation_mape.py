#!/usr/bin/env python3
"""Score frozen-calibration predictions on an independent validation split."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

GATE_PERCENT = 15.0


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top level must be an object")
    return value


def score(
    calibration: dict[str, Any], validation: dict[str, Any], *,
    zero_policy: str,
) -> dict[str, Any]:
    if calibration.get("schema_version") != "mape-calibration-v1":
        raise ValueError("calibration schema_version must be mape-calibration-v1")
    if calibration.get("fit_split") != "calibration":
        raise ValueError("parameters must be fitted only on the calibration split")
    if calibration.get("frozen_before_validation") is not True:
        raise ValueError("calibration must declare frozen_before_validation=true")
    parameters = calibration.get("frozen_parameters")
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("calibration frozen_parameters must be a non-empty object")
    parameter_hash = canonical_hash(parameters)
    if calibration.get("frozen_parameters_sha256") != parameter_hash:
        raise ValueError("frozen_parameters_sha256 does not match parameters")
    if validation.get("schema_version") != "mape-validation-v1":
        raise ValueError("validation schema_version must be mape-validation-v1")
    if validation.get("split") != "validation":
        raise ValueError("validation split must be exactly validation")
    if validation.get("calibration_parameters_sha256") != parameter_hash:
        raise ValueError("validation does not reference the frozen calibration parameters")
    if any(
        key in validation
        for key in ("fitted_parameters", "optimized_parameters", "tuned_parameters")
    ):
        raise ValueError("validation data may not contain refitted/tuned parameters")
    calibration_sources = set(calibration.get("source_ids", []))
    points = validation.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("validation points must be a non-empty array")

    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    skipped: list[str] = []
    seen: set[str] = set()
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"validation point {index} must be an object")
        point_id = point.get("point_id")
        metric, domain = point.get("metric"), point.get("domain")
        source_id = point.get("source_id")
        if not all(isinstance(item, str) and item for item in (
            point_id, metric, domain, source_id
        )):
            raise ValueError(f"validation point {index} lacks string IDs/metric/domain")
        if point_id in seen:
            raise ValueError(f"duplicate validation point_id: {point_id}")
        seen.add(point_id)
        if source_id in calibration_sources:
            raise ValueError(
                f"validation source {source_id} was used for calibration"
            )
        measured, predicted = point.get("measured"), point.get("predicted")
        if (
            not isinstance(measured, (int, float))
            or isinstance(measured, bool)
            or not isinstance(predicted, (int, float))
            or isinstance(predicted, bool)
            or not math.isfinite(measured)
            or not math.isfinite(predicted)
        ):
            raise ValueError(f"{point_id}: measured/predicted must be finite numbers")
        if measured == 0:
            if zero_policy == "reject":
                raise ValueError(f"{point_id}: measured zero is invalid for MAPE")
            skipped.append(point_id)
            continue
        groups[(metric, domain)].append(abs((measured - predicted) / measured) * 100)
    if not groups:
        raise ValueError("zero policy removed every validation point")

    results = []
    for (metric, domain), errors in sorted(groups.items()):
        value = sum(errors) / len(errors)
        results.append({
            "metric": metric,
            "domain": domain,
            "n": len(errors),
            "mape_percent": value,
            "gate_percent": GATE_PERCENT,
            "pass": value <= GATE_PERCENT,
        })
    all_errors = [value for values in groups.values() for value in values]
    overall = sum(all_errors) / len(all_errors)
    gate_pass = all(item["pass"] for item in results)
    return {
        "schema_version": "validation-mape-report-v1",
        "calibration_parameters_sha256": parameter_hash,
        "fit_split": "calibration",
        "score_split": "validation",
        "validation_refit_performed": False,
        "zero_policy": zero_policy,
        "skipped_zero_point_ids": skipped,
        "per_metric_domain": results,
        "overall_mape_percent": overall,
        "gate_percent": GATE_PERCENT,
        "gate_pass": gate_pass,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--zero-policy", choices=("reject", "skip"), default="reject")
    args = parser.parse_args()
    try:
        result = score(
            load_object(args.calibration),
            load_object(args.validation),
            zero_policy=args.zero_policy,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["gate_pass"] else 20


if __name__ == "__main__":
    raise SystemExit(main())
