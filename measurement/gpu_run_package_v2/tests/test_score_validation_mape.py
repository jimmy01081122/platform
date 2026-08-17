from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.score_validation_mape import canonical_hash, score

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def inputs() -> tuple[dict, dict]:
    parameters = {"latency_scale": 1.25, "queue_penalty": 0.2}
    calibration = {
        "schema_version": "mape-calibration-v1",
        "fit_split": "calibration",
        "frozen_before_validation": True,
        "frozen_parameters": parameters,
        "frozen_parameters_sha256": canonical_hash(parameters),
        "source_ids": ["cal-1", "cal-2"],
    }
    validation = {
        "schema_version": "mape-validation-v1",
        "split": "validation",
        "calibration_parameters_sha256": canonical_hash(parameters),
        "points": [
            {
                "point_id": "v1",
                "source_id": "val-1",
                "metric": "component_latency",
                "domain": "component",
                "measured": 100.0,
                "predicted": 110.0,
            },
            {
                "point_id": "v2",
                "source_id": "val-2",
                "metric": "pcie_transfer_latency",
                "domain": "pcie",
                "measured": 200.0,
                "predicted": 220.0,
            },
            {
                "point_id": "v3",
                "source_id": "val-3",
                "metric": "moe_replay_tpot",
                "domain": "moe_replay",
                "measured": 50.0,
                "predicted": 55.0,
            },
            {
                "point_id": "v4",
                "source_id": "val-4",
                "metric": "moe_replay_throughput",
                "domain": "moe_replay",
                "measured": 80.0,
                "predicted": 88.0,
            },
        ],
    }
    return calibration, validation


class ValidationMapeTests(unittest.TestCase):
    def test_scores_per_metric_domain_and_passes_15_percent_gate(self) -> None:
        calibration, validation = inputs()
        result = score(calibration, validation, zero_policy="reject")
        self.assertTrue(result["gate_pass"])
        self.assertEqual("validation-mape-report-v1", result["schema_version"])
        self.assertEqual(4, len(result["per_metric_domain"]))
        self.assertAlmostEqual(10.0, result["overall_mape_percent"])
        self.assertFalse(result["validation_refit_performed"])

    def test_gate_fails_if_any_metric_domain_exceeds_15_percent(self) -> None:
        calibration, validation = inputs()
        validation["points"][2]["predicted"] = 60.0
        result = score(calibration, validation, zero_policy="reject")
        self.assertFalse(result["gate_pass"])

    def test_zero_measured_rejected_by_default(self) -> None:
        calibration, validation = inputs()
        validation["points"][0]["measured"] = 0
        with self.assertRaisesRegex(ValueError, "measured zero"):
            score(calibration, validation, zero_policy="reject")

    def test_zero_skip_policy_is_explicit(self) -> None:
        calibration, validation = inputs()
        validation["points"][0]["measured"] = 0
        result = score(calibration, validation, zero_policy="skip")
        self.assertEqual(["v1"], result["skipped_zero_point_ids"])

    def test_validation_source_cannot_overlap_calibration(self) -> None:
        calibration, validation = inputs()
        validation["points"][0]["source_id"] = "cal-1"
        with self.assertRaisesRegex(ValueError, "used for calibration"):
            score(calibration, validation, zero_policy="reject")

    def test_validation_cannot_supply_tuned_parameters(self) -> None:
        calibration, validation = inputs()
        validation["tuned_parameters"] = {"latency_scale": 1.1}
        with self.assertRaisesRegex(ValueError, "refitted/tuned"):
            score(calibration, validation, zero_policy="reject")

    def test_run_sh_scores_schema_report(self) -> None:
        calibration, validation = inputs()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calibration_path = root / "calibration.json"
            validation_path = root / "validation.json"
            output_path = root / "VALIDATION_MAPE_REPORT.json"
            calibration_path.write_text(json.dumps(calibration), encoding="utf-8")
            validation_path.write_text(json.dumps(validation), encoding="utf-8")
            result = subprocess.run(
                [
                    str(PACKAGE_ROOT / "run.sh"),
                    "--score-validation-mape",
                    "--calibration",
                    str(calibration_path),
                    "--validation",
                    str(validation_path),
                    "--output",
                    str(output_path),
                ],
                cwd=PACKAGE_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            report = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("validation-mape-report-v1", report["schema_version"])
        self.assertTrue(report["gate_pass"])


if __name__ == "__main__":
    unittest.main()
