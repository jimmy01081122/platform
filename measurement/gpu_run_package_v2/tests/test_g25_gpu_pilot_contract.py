from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from scheduler import g25_historical_evidence
from scripts import g25_qualification as g25
from scripts import projectctl


class G25GpuPilotContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.session, cls.matrix, cls.artifacts = g25.load_pilot_contracts()

    def test_configs_match_schemas_and_have_zero_authority(self):
        pairs = (
            (g25.PILOT_SESSION_PATH, "g25_gpu_pilot_session_contract.schema.json"),
            (g25.PILOT_MATRIX_PATH, "g25_gpu_pilot_matrix.schema.json"),
            (g25.PILOT_ARTIFACTS_PATH, "g25_expected_artifacts.schema.json"),
        )
        for path, schema_name in pairs:
            with self.subTest(path=path.name):
                value = json.loads(path.read_text())
                schema = json.loads((g25.PACKAGE_ROOT / "schemas" / schema_name).read_text())
                jsonschema.Draft202012Validator(schema).validate(value)
        self.assertEqual("not_authorized", self.session["authority"]["gpu_execution"])
        self.assertEqual("not_authorized", self.session["authority"]["formal_g3_r5"])

    def test_matrix_is_exact_full_cartesian_without_selection_escape(self):
        self.assertEqual(12, self.matrix["expected_cells"])
        self.assertEqual([256, 384, 512], self.matrix["ceilings"])
        self.assertEqual(4, len(self.matrix["instances"]))
        self.assertFalse(self.matrix["early_success_stop"])
        self.assertFalse(self.matrix["per_sample_ceiling"])
        self.assertFalse(self.matrix["r4_cell_or_generation_reuse"])
        self.assertFalse(self.matrix["answer_correctness_affects_dispatch_or_selection"])
        self.assertEqual(set(self.matrix["instances"]), set(self.matrix["frozen_rendered_inputs"]))
        for identity in self.matrix["frozen_rendered_inputs"].values():
            self.assertRegex(identity["rendered_chat_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(identity["input_token_ids_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(identity["input_token_count"], 0)

    def test_process_deadline_and_failure_contract_is_bounded(self):
        process = self.session["process_strategy"]
        deadlines = self.session["deadlines"]
        failure = self.session["failure_and_resume"]
        self.assertEqual("fresh_model_process_per_cell", process["model_lifetime"])
        self.assertFalse(process["cache_reuse_between_cells"])
        self.assertEqual(480, deadlines["cell_timeout_seconds"])
        self.assertEqual(7200, deadlines["session_hard_deadline_seconds"])
        self.assertEqual(1410, deadlines["stop_new_dispatch_remaining_seconds"])
        self.assertEqual(5790, deadlines["latest_new_dispatch_elapsed_seconds"])
        self.assertEqual(6300, deadlines["execution_cutoff_elapsed_seconds"])
        self.assertEqual(30, deadlines["term_grace_seconds"])
        self.assertFalse(failure["resume_same_session"])
        self.assertFalse(failure["retry_failed"])

    def test_plan_is_cpu_only_and_start_is_strictly_application_gated(self):
        plan = g25.pilot_plan()
        self.assertFalse(plan["gpu_used"])
        self.assertFalse(plan["gpu_authorized"])
        with tempfile.TemporaryDirectory() as directory:
            status = g25.pilot_status(Path(directory))
        self.assertEqual("NOT_RUN", status["status"])
        parser = projectctl.build_parser()
        self.assertEqual("plan", parser.parse_args(["qualification", "plan"]).qualification_action)
        with self.assertRaises(SystemExit):
            parser.parse_args(["qualification", "start"])
        self.assertEqual(
            "application_implemented_pending_fresh_review_not_authorized",
            plan["status"],
        )
        self.assertEqual(
            [
                "new_s4_r2_application_source_not_yet_same_hash_reviewed",
                "fresh_three_role_GO_not_yet_recorded",
                "5_6sol_GO_not_yet_recorded",
                "owner_exact_command_approval_not_yet_recorded",
            ],
            self.session["gpu_application_blockers"],
        )

    def test_static_preflight_never_claims_execution_ready(self):
        def fake_run(argv, **_kwargs):
            if argv[1:3] == ["status", "--porcelain"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[1:3] == ["tag", "--points-at"]:
                return subprocess.CompletedProcess(argv, 0, "review-tag\n", "")
            if argv[1:3] == ["cat-file", "-t"]:
                return subprocess.CompletedProcess(argv, 0, "tag\n", "")
            raise AssertionError(argv)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            g25.subprocess, "run", side_effect=fake_run
        ), patch.object(g25, "_checksums_valid", return_value=True):
            report = g25.pilot_static_preflight(Path(directory))
        self.assertEqual("static_pass_dynamic_gpu_pending", report["status"])
        self.assertFalse(report["execution_ready"])
        self.assertFalse(report["gpu_authorized"])
        self.assertIn("owner_approval_record_and_exact_command_hash", report["pending_dynamic_gpu_checks"])
        for check in (
            "r4_session_immutable", "r4_suite_snapshot_immutable",
            "r4_journal_immutable", "r4_failed_state_immutable",
            "r4_failure_quality_immutable",
        ):
            self.assertTrue(report["checks"][check])

        drifted_historical = (
            g25_historical_evidence.verify_historical_evidence_archive()
        )
        drifted_historical["r4_journal_sha256"] = "0" * 64

        with tempfile.TemporaryDirectory() as directory, patch.object(
            g25.subprocess, "run", side_effect=fake_run
        ), patch.object(g25, "_checksums_valid", return_value=True), patch.object(
            g25_historical_evidence,
            "verify_historical_evidence_archive",
            return_value=drifted_historical,
        ):
            drifted = g25.pilot_static_preflight(Path(directory))
        self.assertEqual("blocked", drifted["status"])
        self.assertIn("r4_journal_immutable", drifted["blockers"])

    def test_streaming_writer_preserves_atomic_cells_before_worker_crash(self):
        contract = g25.load_contract()
        selections = g25._manifest_selections(contract)
        calls = 0

        def provider(selection, ceiling):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic worker crash")
            return g25.synthetic_worker_evidence(selection, ceiling, "QUALIFIED")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                g25.write_qualification_session(
                    Path(directory), "stream-crash", provider,
                    synthetic=True, gpu_used=False,
                )
            root = Path(directory) / "stream-crash"
            self.assertEqual(1, len(list((root / "raw").glob("*.json"))))
            self.assertEqual(1, len(list((root / "cells").glob("*.json"))))
            self.assertFalse((root / "ledger.json").exists())
            self.assertEqual(selections["c1a-t1-00"]["instance_id"], "c1a-t1-00")


if __name__ == "__main__":
    unittest.main()
