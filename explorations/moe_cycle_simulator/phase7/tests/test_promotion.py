from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from explorations.moe_cycle_simulator.phase7.promotion import (
    ContractError,
    assess_variant_change,
    build_artifact_ledger,
    invalidate_state_for_variant_change,
    new_state,
    preflight,
    promote_cpu_mock_stage,
    semantic_sha256,
    validate_plan,
    verify_artifact_ledger,
)


def sha(character: str) -> str:
    return character * 64


def make_plan() -> dict:
    identity = {
        "collector_hash": sha("1"),
        "adapter_hash": sha("2"),
        "schema_hash": sha("3"),
        "correlation_hash": sha("4"),
        "alignment_hash": sha("5"),
        "routing_hook_hash": sha("6"),
        "generation_identity_hash": sha("7"),
    }
    return {
        "schema_version": "moe-simulator-phase7-plan-v1",
        "session_id": "phase7-cpu-mock-fresh-0001",
        "fresh_session": True,
        "resume_session_id": None,
        "retry_failed": False,
        "copy_prior_evidence": False,
        "execution_profile": "CPU_MOCK_DRY_RUN",
        "variant_identity": identity,
        "variant_hash": semantic_sha256(identity),
        "v0": {
            "execution_role": "OFFLINE_VALIDATION",
            "gpu_execution": False,
            "runtime_pass": False,
        },
        "promotions": {
            "M0": {"unit_ids": ["runtime-capacity-contract"]},
            "M1": {"sample_ids": [f"canary-{index:02d}" for index in range(8)]},
            "M2": {
                "sample_ids": [f"core-{index:02d}" for index in range(48)],
                "repetitions": 3,
                "passes": ["P0", "P2"],
                "cell_count": 288,
            },
            "M3": {
                "sample_ids": [f"deep-{index:02d}" for index in range(12)],
                "repetitions": 3,
                "passes": ["P1", "P3", "P4"],
                "cell_count": 108,
            },
            "M4": {
                "scenario_ids": [f"serving-{index:02d}" for index in range(6)],
                "repetitions": 3,
                "modes": ["CLEAN", "INSTRUMENTED"],
                "session_count": 36,
            },
        },
    }


def make_approval(plan: dict, stage: str, token_character: str = "a") -> dict:
    return {
        "schema_version": "moe-simulator-phase7-owner-approval-v1",
        "approval_id": f"owner-{stage.lower()}-approval-0001",
        "stage": stage,
        "session_id": plan["session_id"],
        "variant_hash": plan["variant_hash"],
        "plan_hash": semantic_sha256(plan),
        "scope": f"PHASE7_{stage}_CPU_MOCK_DRY_RUN",
        "decision": "APPROVE",
        "token_sha256": sha(token_character),
    }


class PlanContractTests(unittest.TestCase):
    def test_exact_matrix_and_v0_pass(self) -> None:
        result = validate_plan(make_plan())
        self.assertEqual(result["variant_hash"], make_plan()["variant_hash"])

    def test_wrong_matrix_counts_fail_closed(self) -> None:
        for stage, field, bad_value in (
            ("M2", "cell_count", 287),
            ("M3", "cell_count", 109),
            ("M4", "session_count", 35),
        ):
            with self.subTest(stage=stage):
                plan = make_plan()
                plan["promotions"][stage][field] = bad_value
                with self.assertRaises(ContractError):
                    validate_plan(plan)

    def test_v0_cannot_be_runtime_or_gpu_pass(self) -> None:
        for field in ("gpu_execution", "runtime_pass"):
            plan = make_plan()
            plan["v0"][field] = True
            with self.assertRaises(ContractError):
                validate_plan(plan)

    def test_resume_retry_and_copy_prior_evidence_are_rejected(self) -> None:
        mutations = (
            ("fresh_session", False),
            ("resume_session_id", "old-session"),
            ("retry_failed", True),
            ("copy_prior_evidence", True),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                plan = make_plan()
                plan[field] = value
                with self.assertRaises(ContractError):
                    validate_plan(plan)

    def test_only_cpu_mock_dry_run_profile_is_accepted(self) -> None:
        plan = make_plan()
        plan["execution_profile"] = "GPU_FORMAL"
        with self.assertRaises(ContractError):
            validate_plan(plan)


class StateAndApprovalTests(unittest.TestCase):
    def test_fresh_session_registry_rejects_existing_id(self) -> None:
        plan = make_plan()
        with self.assertRaises(ContractError):
            new_state(plan, [plan["session_id"]])

    def test_stage_order_and_exact_owner_binding(self) -> None:
        plan = make_plan()
        state = new_state(plan, [])
        result = preflight(plan, make_approval(plan, "M0"), state, stage="M0")
        self.assertFalse(result.execution_performed)
        with self.assertRaises(ContractError):
            preflight(plan, make_approval(plan, "M1"), state, stage="M1")

    def test_approval_plan_hash_and_stage_are_blocking(self) -> None:
        plan = make_plan()
        state = new_state(plan, [])
        for field, value in (
            ("plan_hash", sha("f")),
            ("decision", "DENY"),
            ("scope", "PHASE7_M0_GPU"),
        ):
            with self.subTest(field=field):
                approval = make_approval(plan, "M0")
                approval[field] = value
                with self.assertRaises(ContractError):
                    preflight(plan, approval, state, stage="M0")

    def test_independent_stage_token_is_required(self) -> None:
        plan = make_plan()
        state = new_state(plan, [])
        state["status"] = "ACTIVE_CPU_MOCK"
        state["completed_stages"] = ["M0"]
        state["approvals"] = {
            "M0": {
                "approval_id": "owner-m0",
                "token_sha256": sha("a"),
                "approval_hash": sha("e"),
            }
        }
        state["evidence_ledgers"] = {"M0": sha("b")}
        approval = make_approval(plan, "M1", token_character="a")
        with self.assertRaises(ContractError):
            preflight(plan, approval, state, stage="M1")

    def test_all_seven_identity_changes_invalidate_m1_and_require_fresh_session(self) -> None:
        plan = make_plan()
        state = new_state(plan, [])
        state["status"] = "ACTIVE_CPU_MOCK"
        state["completed_stages"] = ["M0", "M1"]
        state["approvals"] = {
            "M0": {
                "approval_id": "owner-m0",
                "token_sha256": sha("a"),
                "approval_hash": sha("e"),
            },
            "M1": {
                "approval_id": "owner-m1",
                "token_sha256": sha("b"),
                "approval_hash": sha("f"),
            },
        }
        state["evidence_ledgers"] = {"M0": sha("c"), "M1": sha("d")}
        for index, field in enumerate(plan["variant_identity"]):
            with self.subTest(field=field):
                proposed = dict(plan["variant_identity"])
                proposed[field] = hashlib.sha256(field.encode()).hexdigest()
                report = assess_variant_change(state, plan, proposed)
                self.assertEqual(report["changed_fields"], [field])
                self.assertEqual(report["invalidated_stages"], ["M1"])
                self.assertFalse(report["m1_evidence_valid"])
                self.assertTrue(report["fresh_session_required"])
                self.assertFalse(report["promotion_allowed"])

    def test_identity_invalidation_is_terminal_and_preserves_history(self) -> None:
        plan = make_plan()
        state = new_state(plan, [])
        state["status"] = "ACTIVE_CPU_MOCK"
        state["completed_stages"] = ["M0", "M1"]
        state["approvals"] = {
            "M0": {
                "approval_id": "owner-m0",
                "token_sha256": sha("a"),
                "approval_hash": sha("e"),
            },
            "M1": {
                "approval_id": "owner-m1",
                "token_sha256": sha("b"),
                "approval_hash": sha("f"),
            },
        }
        state["evidence_ledgers"] = {"M0": sha("c"), "M1": sha("d")}
        proposed = dict(plan["variant_identity"])
        proposed["collector_hash"] = sha("8")
        invalidated = invalidate_state_for_variant_change(
            state, plan, proposed
        )
        self.assertEqual(invalidated["status"], "INVALIDATED_VARIANT_CHANGE")
        self.assertEqual(invalidated["completed_stages"], ["M0", "M1"])
        self.assertEqual(
            invalidated["invalidations"][0]["invalidated_stages"], ["M1"]
        )
        with self.assertRaises(ContractError):
            preflight(
                plan,
                make_approval(plan, "M2", token_character="9"),
                invalidated,
                stage="M2",
            )

    def test_cpu_mock_transition_requires_ledger_and_advances_one_stage(self) -> None:
        plan = make_plan()
        state = new_state(plan, [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "m0.json").write_text('{"status":"PASS"}\n', encoding="utf-8")
            ledger = build_artifact_ledger(
                root,
                ["m0.json"],
                stage="M0",
                session_id=plan["session_id"],
            )
            promoted = promote_cpu_mock_stage(
                plan,
                make_approval(plan, "M0"),
                state,
                stage="M0",
                evidence_root=root,
                artifact_ledger=ledger,
            )
            self.assertEqual(promoted["completed_stages"], ["M0"])
            self.assertEqual(promoted["status"], "ACTIVE_CPU_MOCK")
            self.assertEqual(promoted["claim_boundary"], "FRAMEWORK_VALIDATION_ONLY")
            self.assertEqual(promoted["gpu_authority"], "NONE")

    def test_full_m0_through_m4_state_machine_uses_independent_approvals(self) -> None:
        plan = make_plan()
        state = new_state(plan, [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stage, token_character in zip(
                ("M0", "M1", "M2", "M3", "M4"), ("a", "b", "c", "d", "e")
            ):
                artifact_name = f"{stage.lower()}.json"
                (root / artifact_name).write_text(
                    json.dumps({"stage": stage, "evidence_class": "CPU_MOCK"}),
                    encoding="utf-8",
                )
                ledger = build_artifact_ledger(
                    root,
                    [artifact_name],
                    stage=stage,
                    session_id=plan["session_id"],
                )
                state = promote_cpu_mock_stage(
                    plan,
                    make_approval(plan, stage, token_character=token_character),
                    state,
                    stage=stage,
                    evidence_root=root,
                    artifact_ledger=ledger,
                )
        self.assertEqual(state["completed_stages"], ["M0", "M1", "M2", "M3", "M4"])
        self.assertEqual(state["status"], "COMPLETE_CPU_MOCK_FRAMEWORK_ONLY")
        self.assertEqual(
            len({entry["token_sha256"] for entry in state["approvals"].values()}),
            5,
        )


class ArtifactLedgerTests(unittest.TestCase):
    def test_ledger_round_trip_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.json").write_text('{"a":1}\n', encoding="utf-8")
            (root / "b.bin").write_bytes(b"evidence")
            ledger = build_artifact_ledger(
                root,
                ["b.bin", "a.json"],
                stage="V0",
                session_id="phase7-cpu-mock-fresh-0001",
            )
            verify_artifact_ledger(root, ledger)
            self.assertEqual(
                [member["path"] for member in ledger["members"]],
                ["a.json", "b.bin"],
            )
            (root / "b.bin").write_bytes(b"tampered")
            with self.assertRaises(ContractError):
                verify_artifact_ledger(root, ledger)

    def test_ledger_rejects_escape_duplicate_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").write_text("a", encoding="utf-8")
            outside = root.parent / f"{root.name}-outside"
            outside.write_text("outside", encoding="utf-8")
            self.addCleanup(lambda: outside.unlink(missing_ok=True))
            with self.assertRaises(ContractError):
                build_artifact_ledger(
                    root, ["a", "a"], stage="M0", session_id="phase7-session-0001"
                )
            with self.assertRaises(ContractError):
                build_artifact_ledger(
                    root,
                    [f"../{outside.name}"],
                    stage="M0",
                    session_id="phase7-session-0001",
                )
            (root / "link").symlink_to(root / "a")
            with self.assertRaises(ContractError):
                build_artifact_ledger(
                    root, ["link"], stage="M0", session_id="phase7-session-0001"
                )


class CliTests(unittest.TestCase):
    def test_cli_preflight_is_dry_run_only(self) -> None:
        plan = make_plan()
        state = new_state(plan, [])
        approval = make_approval(plan, "M0")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            state_path = root / "state.json"
            approval_path = root / "approval.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            state_path.write_text(json.dumps(state), encoding="utf-8")
            approval_path.write_text(json.dumps(approval), encoding="utf-8")
            runner = Path(__file__).resolve().parents[1] / "runner.py"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "preflight",
                    "--plan",
                    str(plan_path),
                    "--state",
                    str(state_path),
                    "--approval",
                    str(approval_path),
                    "--stage",
                    "M0",
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["action"], "PREFLIGHT_ONLY")
            self.assertFalse(result["execution_performed"])
            self.assertEqual(result["gpu_authority"], "NONE")

    def test_cli_requires_dry_run_latch(self) -> None:
        runner = Path(__file__).resolve().parents[1] / "runner.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(runner),
                "preflight",
                "--plan",
                "missing",
                "--state",
                "missing",
                "--approval",
                "missing",
                "--stage",
                "M0",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_cli_builds_and_verifies_artifact_ledger_without_execution(self) -> None:
        runner = Path(__file__).resolve().parents[1] / "runner.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_text('{"status":"PASS"}\n', encoding="utf-8")
            built = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "build-ledger",
                    "--root",
                    str(root),
                    "--stage",
                    "V0",
                    "--session-id",
                    "phase7-cpu-mock-fresh-0001",
                    "--artifact",
                    "result.json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            ledger_path = root / "ledger.json"
            ledger_path.write_text(built.stdout, encoding="utf-8")
            verified = subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "verify-ledger",
                    "--root",
                    str(root),
                    "--ledger",
                    str(ledger_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            result = json.loads(verified.stdout)
            self.assertFalse(result["execution_performed"])
            self.assertEqual(result["gpu_authority"], "NONE")


if __name__ == "__main__":
    unittest.main()
