from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from c1_trace_audit import audit, work_unit_id


class C1AuditTests(unittest.TestCase):
    def test_exact_work_units_and_projectctl_remediation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {
                "suite_id": "suite-v1",
                "samples": [{"benchmark_id": "gsm8k", "sample_id": "s0"}],
                "repetitions": 2,
                "mandatory_passes": ["P0", "P2"],
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            unit = work_unit_id(plan["samples"][0], 0, "P0")
            complete = root / "complete" / unit
            complete.mkdir(parents=True)
            (complete / "PASS_MANIFEST.json").write_text(
                json.dumps({
                    "status": "COMPLETE",
                    "execution_alignment_key": "a" * 64,
                }),
                encoding="utf-8",
            )
            code, report = audit(plan_path, root)
            self.assertEqual(1, code)
            self.assertEqual(4, report["expected_work_unit_count"])
            self.assertEqual(3, len(report["missing_work_units"]))
            self.assertTrue(all(
                command.startswith("projectctl trace run ")
                for command in report["supplement_commands"]
            ))
            self.assertTrue(all("./run.sh" not in command for command in report["supplement_commands"]))

    def test_cross_pass_alignment_drift_fails_even_without_output_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = {"benchmark_id": "gsm8k", "sample_id": "s0"}
            plan = {
                "suite_id": "suite-v1", "samples": [sample],
                "repetitions": 1, "mandatory_passes": ["P0", "P2"],
            }
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            for pass_id, alignment in (("P0", "a" * 64), ("P2", "b" * 64)):
                unit = work_unit_id(sample, 0, pass_id)
                complete = root / "complete" / unit
                complete.mkdir(parents=True)
                (complete / "PASS_MANIFEST.json").write_text(
                    json.dumps({
                        "status": "COMPLETE",
                        "execution_alignment_key": alignment,
                    }),
                    encoding="utf-8",
                )
                (complete / "generation_results.jsonl").write_text(
                    json.dumps({"output_hash": "c" * 64}) + "\n",
                    encoding="utf-8",
                )
            code, report = audit(plan_path, root)
            self.assertEqual(1, code)
            self.assertEqual([], report["missing_work_units"])
            self.assertEqual(
                ["execution_alignment_drift"],
                [item["kind"] for item in report["cross_pass_findings"]],
            )


if __name__ == "__main__":
    unittest.main()
