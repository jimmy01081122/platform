from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.capture_orchestrator import build_capture_plan
from scripts.trace_package_verify import FAILED, verify_root
from tests.fixture_factory import build_positive, dump, refresh_checksums


def matrix() -> dict:
    return {
        "schema_version": "benchmark-capture-matrix-v1",
        "frozen": True,
        "session_id": "fixture-session",
        "repetitions": 2,
        "gpus": [{"gpu_id": "gpu-0"}],
        "models": [{
            "model_id": "model",
            "model_revision": "model-rev",
            "weights_revision": "weights-rev",
            "tokenizer_revision": "tokenizer-rev",
        }],
        "benchmarks": [{
            "suite_id": "suite",
            "benchmark_id": "benchmark",
            "samples": [{"sample_id": "sample"}],
        }],
        "configurations": [{
            "configuration_id": "greedy",
            "generation_config": {"temperature": 0.0},
        }],
        "passes": [f"P{index}" for index in range(7)],
    }


class CaptureOrchestratorTests(unittest.TestCase):
    def test_frozen_rtx3050_m0_matrix_is_exactly_eight_samples(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix_path = root / "configs/capture_matrices/m0_rtx3050_vertical_v1.json"
        value = json.loads(matrix_path.read_text(encoding="utf-8"))
        samples = [
            sample
            for benchmark in value["benchmarks"]
            for sample in benchmark["samples"]
        ]
        self.assertEqual("v1.4.0", value["suite"]["suite_revision"])
        self.assertEqual(
            "historical v1.2.0",
            value["evidence_status"]["external_existing_m0_evidence_revision"],
        )
        self.assertEqual(8, len(samples))
        self.assertEqual(8, len({sample["sample_id"] for sample in samples}))
        frozen_rows = {
            row["sample_id"]: row
            for row in (
                json.loads(line)
                for line in (
                    root
                    / "configs/test_suites/frozen/v1.4.0/sample_manifest.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            )
        }
        self.assertTrue(all(
            sample["sample_id"] in frozen_rows
            and frozen_rows[sample["sample_id"]]["split"] == "smoke"
            and frozen_rows[sample["sample_id"]]["raw_sample_hash"]
            == sample["raw_sample_hash"]
            for sample in samples
        ))
        self.assertEqual([f"P{index}" for index in range(7)], value["passes"])
        self.assertFalse(value["evidence_status"]["plan_is_capture_complete"])
        self.assertTrue(value["evidence_status"]["measured_artifact_is_separate"])
        plan = build_capture_plan(value, matrix_path, root)
        self.assertEqual(56, plan["state_count"])
        self.assertNotIn("complete", {state["status"] for state in plan["states"]})

    def test_expands_every_pass_and_repetition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan = build_capture_plan(
                matrix(), Path(temporary) / "matrix.json", Path(temporary)
            )
        self.assertEqual(14, plan["state_count"])
        self.assertEqual("no_go", plan["status"])
        self.assertFalse(plan["execution_allowed"])
        self.assertEqual(1, plan["profiler_concurrency"])
        self.assertTrue(plan["simultaneous_profilers_forbidden"])
        self.assertEqual({"P0", "P2", "P3"}, {
            state["pass_id"] for state in plan["states"]
            if state["mandatory_gate"]
        })
        self.assertEqual(14, len({state["state_id"] for state in plan["states"]}))
        self.assertEqual(6300, plan["session_dispatch_contract"]["stop_new_dispatch_elapsed_seconds"])
        self.assertEqual(900, plan["session_dispatch_contract"]["audit_package_reserve_seconds"])
        self.assertTrue(all(
            state["dispatch_contract"]["clock"] == "monotonic"
            and state["dispatch_contract"]["latest_dispatch_elapsed_seconds"] == 6300
            and state["dispatch_contract"]["audit_reserve_seconds"] == 900
            for state in plan["states"]
        ))

    def test_missing_collectors_are_blocked_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plan = build_capture_plan(
                matrix(), Path(temporary) / "matrix.json", Path(temporary)
            )
        self.assertEqual({"blocked"}, {state["status"] for state in plan["states"]})
        self.assertEqual(14, plan["blocked_state_count"])
        self.assertEqual("no_go", plan["status"])
        self.assertTrue(all("not implemented" in state["blocked_reason"]
                            for state in plan["states"]))

    def test_implemented_adapter_is_only_planned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = root / "adapter.py"
            adapter.write_text("# fixture\n", encoding="utf-8")
            value = matrix()
            value["collector_adapters"] = {"P0": str(adapter)}
            plan = build_capture_plan(value, root / "matrix.json", root)
        p0 = [state for state in plan["states"] if state["pass_id"] == "P0"]
        self.assertEqual({"planned"}, {state["status"] for state in p0})
        self.assertNotIn("complete", {state["status"] for state in plan["states"]})

    def test_mandatory_gates_cannot_be_omitted(self) -> None:
        value = matrix()
        value["passes"] = ["P0", "P1", "P2"]
        with self.assertRaisesRegex(ValueError, "omits mandatory gates: P3"):
            build_capture_plan(value, Path("matrix.json"))

    def test_approval_cannot_waive_blocked_p2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = build_positive(Path(temporary) / "session", 1)
            manifest_path = next(
                (root / "runs/fixture-group/p2_routing/runs")
                .glob("*/PASS_MANIFEST.json")
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "blocked"
            manifest["failure_reason"] = "collector adapter unavailable"
            dump(manifest_path, manifest)
            session_path = root / "SESSION_MANIFEST.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["accepted_incomplete"] = True
            session["approval"] = {
                "approved_by": "fixture-owner",
                "approved_utc": "2026-07-18T00:00:00Z",
                "reason": "attempted waiver",
            }
            dump(session_path, session)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code, report)
        finding = next(
            item for item in report["findings"]
            if item["finding_id"] == "TRACE.P2.STATUS_BLOCKED"
        )
        self.assertFalse(finding["waivable"])


if __name__ == "__main__":
    unittest.main()
