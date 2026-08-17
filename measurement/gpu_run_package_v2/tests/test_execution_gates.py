from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import yaml

from scheduler.gpu_entrypoint_policy import (
    HARD_DISABLED_IDS,
    QUALIFICATION_ID,
    assert_qualification_route,
    hard_disabled_reason,
    load_gpu_entrypoint_policy,
)
from scripts import projectctl
from scripts.review_gate import evaluate_review


ROOT = Path(__file__).resolve().parents[1]


class ExecutionGateTests(unittest.TestCase):
    def test_s4r6_gpu_entrypoint_inventory_has_one_qualification_route(self) -> None:
        policy = load_gpu_entrypoint_policy()
        schema = json.loads(
            (ROOT / "schemas/g25_gpu_entrypoint_policy.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.Draft202012Validator(schema).validate(policy)
        rows = {
            row["entrypoint_id"]: row
            for row in policy["gpu_capable_entrypoints"]
        }
        self.assertEqual(HARD_DISABLED_IDS | {QUALIFICATION_ID}, set(rows))
        self.assertTrue(all(
            rows[entrypoint]["disposition"] == "hard_disabled"
            for entrypoint in HARD_DISABLED_IDS
        ))
        self.assertEqual(
            "qualification_only", rows[QUALIFICATION_ID]["disposition"]
        )
        assert_qualification_route()
        for entrypoint in HARD_DISABLED_IDS:
            self.assertIn(entrypoint, hard_disabled_reason(entrypoint))

    def test_projectctl_hard_disables_every_legacy_gpu_dispatch_before_io(self) -> None:
        cases = (
            (projectctl.model_command, argparse.Namespace(model_action="smoke")),
            (projectctl.trace_command, argparse.Namespace(trace_action="run")),
            (
                projectctl.diagnostic_command,
                argparse.Namespace(diagnostic_action="run"),
            ),
            (projectctl.run_command, argparse.Namespace(run_action="start")),
            (projectctl.run_command, argparse.Namespace(run_action="resume")),
            (
                projectctl.run_command,
                argparse.Namespace(run_action="retry-failed"),
            ),
        )
        for command, arguments in cases:
            with self.subTest(command=command.__name__, action=vars(arguments)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = command(arguments)
                self.assertEqual(projectctl.BLOCKED, code)
                result = json.loads(output.getvalue())
                self.assertEqual("blocked", result["status"])
                self.assertIn("S4-R6 hard-disables", result["reason"])

    def test_run_sh_gpu_modes_fail_before_any_preflight_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cases = (
                ["--smoke"],
                ["--experiment", "legacy"],
                [
                    "--benchmark-smoke", "--device", "cuda", "--output",
                    str(Path(temporary) / "cuda-output"),
                ],
                [
                    "--benchmark-smoke", "--device", "auto", "--output",
                    str(Path(temporary) / "auto-output"),
                ],
            )
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        ["bash", str(ROOT / "run.sh"), *arguments],
                        cwd=ROOT, text=True, capture_output=True, timeout=30,
                        check=False,
                    )
                    self.assertEqual(20, result.returncode)
                    self.assertIn("S4-R6", result.stderr)

    def test_d062_disables_all_paid_profiles_and_removes_h100_schedule(self) -> None:
        profiles = yaml.safe_load(
            (ROOT / "configs/gpu_profiles.yaml").read_text(encoding="utf-8")
        )["profiles"]
        storage = yaml.safe_load(
            (ROOT / "configs/storage_budget.yaml").read_text(encoding="utf-8")
        )
        paid = {
            profile_id
            for profile_id, assignment in storage["profile_assignment"].items()
            if assignment["paid_session"]
        }
        self.assertTrue(paid)
        self.assertTrue(all(profiles[item]["execution_enabled"] is False for item in paid))
        for profile_id in ("h100_pcie_80gb", "h100_sxm5_80gb"):
            self.assertFalse(profiles[profile_id]["enabled"])
            self.assertTrue(profiles[profile_id]["optional"])
            self.assertEqual("D-062", profiles[profile_id]["disabled_reason"])

        schedule = yaml.safe_load(
            (ROOT / "configs/hardware_schedule.yaml").read_text(encoding="utf-8")
        )
        self.assertFalse(schedule["scheduling_policy"]["execution_allowed"])
        self.assertNotIn("holdout", schedule["trust_roles"])
        active = (
            schedule["hardware_order"]["required"]
            + schedule["hardware_order"]["optional"]
        )
        self.assertFalse(any("h100" in item.casefold() for item in active))

    def test_local_rtx3050_pipeline_smoke_is_hard_disabled_during_s4r6(self) -> None:
        result = evaluate_review(
            ROOT,
            "rtx3050_6gb",
            None,
            None,
            local_pipeline_smoke=True,
        )
        self.assertEqual("no_go", result["status"])
        self.assertFalse(result["paid_execution"])
        self.assertIsNone(result["exception_id"])
        self.assertIn(
            "requested local pipeline-smoke exception is not allowed",
            result["failures"],
        )

    def test_valid_paid_approval_still_hard_fails_while_d062_is_active(self) -> None:
        now = datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            matrix_path = temporary_root / "matrix.json"
            matrix_path.write_text('{"frozen":true}\n', encoding="utf-8")
            digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            approval = {
                "schema_version": "gpu-execution-approval-v1",
                "approval_id": "fixture-approval",
                "superseding_decision_id": "D-063",
                "package_id": "edgehetero-benchmark-driven-m0-20260718",
                "package_checksums_sha256": digest(ROOT / "checksums.txt"),
                "matrix_sha256": digest(matrix_path),
                "gpu_profile_id": "rtx3090_24gb",
                "second_decision_layer": True,
                "reviewers": [
                    {"reviewer_id": "reviewer-a", "role": "architecture_system"},
                    {"reviewer_id": "reviewer-b", "role": "model_benchmark"},
                    {"reviewer_id": "reviewer-c", "role": "trace_statistics"},
                ],
                "blockers": [],
                "budget": {"currency": "USD", "amount": 100},
                "approved_utc": "2026-07-18T04:00:00Z",
                "expires_utc": "2026-07-18T06:00:00Z",
                "deadline_utc": "2026-07-18T07:00:00Z",
            }
            approval_path = temporary_root / "approval.json"
            approval_path.write_text(
                json.dumps(approval), encoding="utf-8"
            )
            result = evaluate_review(
                ROOT,
                "rtx3090_24gb",
                matrix_path,
                approval_path,
                now=now,
            )
        self.assertEqual("no_go", result["status"])
        self.assertIn(
            "D-062 is active and has not been superseded",
            result["failures"],
        )
        self.assertEqual(1, len(result["failures"]))

    def test_reviewer_roles_require_three_distinct_identities(self) -> None:
        policy = yaml.safe_load(
            (ROOT / "configs/gpu_execution_review.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {
                "architecture_system",
                "model_benchmark",
                "trace_statistics",
            },
            set(policy["paid_execution"]["required_distinct_reviewer_roles"]),
        )
        self.assertIn(
            "superseding_decision_id",
            policy["paid_execution"]["bind_to"],
        )

    def test_storage_unknowns_are_no_go_and_all_numeric_gates_are_nonzero(self) -> None:
        storage = yaml.safe_load(
            (ROOT / "configs/storage_budget.yaml").read_text(encoding="utf-8")
        )["preflight_estimate"]
        self.assertEqual("no_go", storage["unknown_defaults"]["policy"])
        self.assertEqual([], storage["unknown_defaults"]["nullable_fields"])
        self.assertTrue(all(storage["nonzero_gates"].values()))
        self.assertGreater(storage["write_probe"]["bytes"], 0)
        self.assertGreater(storage["write_probe"]["peak_headroom_ratio"], 1)
        self.assertTrue(
            storage["capture_matrix_coverage"]["require_every_state_and_pass"]
        )
        self.assertEqual(120, storage["paid_session_minutes"]["total_limit"])

    def test_pro6000_and_provider_metadata_preflight_are_strict(self) -> None:
        profiles = yaml.safe_load(
            (ROOT / "configs/gpu_profiles.yaml").read_text(encoding="utf-8")
        )["profiles"]
        pro6000 = profiles["rtx_pro_6000_blackwell_workstation_96gb"]
        self.assertEqual(
            "^NVIDIA RTX PRO 6000 Blackwell Workstation Edition 96GB$",
            pro6000["accepted_name_regex"],
        )
        preflight = (ROOT / "preflight.sh").read_text(encoding="utf-8")
        self.assertNotIn("--form-factor", preflight)
        self.assertIn("--provider-metadata-sha256", preflight)
        self.assertIn("normalize_sku", preflight)
        self.assertIn("estimated runtime plus package reserve exceeds 120", preflight)


if __name__ == "__main__":
    unittest.main()
