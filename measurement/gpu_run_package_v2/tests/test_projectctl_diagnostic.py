from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scheduler import SchedulerEngine, SchedulerStore, State
from scheduler.store import atomic_json
from scripts import projectctl


class ProjectctlDiagnosticTests(unittest.TestCase):
    @staticmethod
    def gpu_environment(_root: Path) -> dict:
        return {
            "gpus": [{
                "index": "0",
                "name": projectctl.EXPECTED_GPU_NAME,
                "uuid": "GPU-fixture",
                "pci_bus_id": "0000:01:00.0",
                "total_vram_bytes": 8_000_000_000,
                "free_vram_bytes": 5_000_000_000,
            }],
            "compute_processes": [],
            "disk_free_bytes": 9 * 1024**3,
            "cuda_visible_devices": "0",
        }

    @staticmethod
    def write_parent_evidence(
        path: Path, run_root: Path
    ) -> tuple[str, str, str]:
        session_id = "granite-c1a-canary-v3-20260718"
        parent_root = run_root / session_id
        parent_root.mkdir(parents=True)
        suite_path = parent_root / "suite_snapshot.json"
        suite_path.write_text('{"fixture":"parent-suite"}\n', encoding="utf-8")
        suite_hash = projectctl.sha256_file(suite_path)
        session_path = parent_root / "session.json"
        session_path.write_text(json.dumps({
            "session_id": session_id,
            "suite_snapshot_sha256": suite_hash,
        }, sort_keys=True) + "\n", encoding="utf-8")
        session_hash = projectctl.sha256_file(session_path)
        observation_specs = [
            (
                "P0", "COMPLETE",
                "e1a161aa5465674e1b165333c89e70ff51fe8e3eaa6bbde94b4e59df903912dc",
                [41, 0],
                "4d7b656061287b5fe72bd824eae96717ffda0397a1dbd79f8118f0316a78c522",
                "complete", "generation_results.jsonl",
            ),
            (
                "P2", "FAILED_RETRYABLE",
                "d868116d5a2a999ed89d64c42b2806675125544f5c6ff000036af1fd1e6da7e5",
                [20, 41, 20, 0],
                "d1b182b9d9e7bdd8f0dcbc7b536966d9d5d3640cc8431d09e6f21469772c1ddc",
                ".tmp", "failure_generation_results.jsonl",
            ),
        ]
        observations = []
        for pass_id, state, unit_id, ids, output_hash, area, filename in (
            observation_specs
        ):
            generation_dir = parent_root / area / unit_id
            generation_dir.mkdir(parents=True)
            generation_path = generation_dir / filename
            generation_path.write_text(json.dumps({
                "execution_alignment_key":
                    "cfe453fa353f372ca2dd87e48c82135a668005c5ef966ce95efe4ef7c9af6b77",
                "output_token_ids": ids,
                "output_hash": output_hash,
            }, sort_keys=True) + "\n", encoding="utf-8")
            state_dir = parent_root / "state"
            state_dir.mkdir(exist_ok=True)
            (state_dir / f"{unit_id}.json").write_text(json.dumps({
                "state": state,
                "work_unit": {"work_unit_id": unit_id},
            }, sort_keys=True) + "\n", encoding="utf-8")
            observations.append({
                "pass": pass_id,
                "sample_id": "c1a-t0-01",
                "state": state,
                "work_unit_id": unit_id,
                "generation_artifact_sha256":
                    projectctl.sha256_file(generation_path),
                "output_token_ids": ids,
                "output_hash": output_hash,
                "execution_alignment_key":
                    "cfe453fa353f372ca2dd87e48c82135a668005c5ef966ce95efe4ef7c9af6b77",
            })
        document = {
            "schema_version": "c1-token-drift-parent-evidence-v1",
            "evidence_class": "diagnostic_parent_only",
            "formal_gate_pass": False,
            "parent_session": session_id,
            "suite_snapshot_sha256": suite_hash,
            "parent_session_record_sha256": session_hash,
            "observations": observations,
        }
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return projectctl.sha256_file(path), session_hash, suite_hash

    def make_session(self, base: Path) -> tuple[Path, dict, list]:
        root = base / "diagnostic_runs" / "diag"
        root.mkdir(parents=True)
        suite = projectctl.load_structured(projectctl.DIAGNOSTIC_SUITE_PATH)
        units = projectctl.validate_diagnostic_suite(suite)
        atomic_json(root / "suite_snapshot.json", suite)
        model = base / "model"
        model.mkdir()
        (model / "config.json").write_text("{}\n", encoding="utf-8")
        parent = base / "v3-evidence.json"
        run_root = base / "scheduler_runs"
        parent_hash, session_hash, suite_hash = self.write_parent_evidence(
            parent, run_root
        )
        old_values = (
            projectctl.RUN_ROOT,
            projectctl.PARENT_SESSION_RECORD_SHA256,
            projectctl.PARENT_SUITE_SNAPSHOT_SHA256,
        )
        self.addCleanup(
            lambda: (
                setattr(projectctl, "RUN_ROOT", old_values[0]),
                setattr(
                    projectctl, "PARENT_SESSION_RECORD_SHA256", old_values[1]
                ),
                setattr(
                    projectctl, "PARENT_SUITE_SNAPSHOT_SHA256", old_values[2]
                ),
            )
        )
        projectctl.RUN_ROOT = run_root
        projectctl.PARENT_SESSION_RECORD_SHA256 = session_hash
        projectctl.PARENT_SUITE_SNAPSHOT_SHA256 = suite_hash
        session = {
            "schema_version": "projectctl-session-v2",
            "session_class": "diagnostic_non_c1",
            "diagnostic_mode": projectctl.DIAGNOSTIC_MODE,
            "session_id": root.name,
            "profile": projectctl.LOCAL_PROFILE,
            "suite_snapshot_sha256": projectctl.sha256_file(
                root / "suite_snapshot.json"
            ),
            "started_epoch": 1.0,
            "execution_deadline_epoch": 10_000_000_000.0,
            "session_deadline_epoch": 10_000_000_100.0,
            "model_snapshot": projectctl.snapshot_inventory(model),
            "environment": {
                "gpu": {
                    "name": projectctl.EXPECTED_GPU_NAME,
                    "uuid": "GPU-fixture",
                    "pci_bus_id": "0000:01:00.0",
                }
            },
            "parent_provenance": {
                "session_id": "granite-c1a-canary-v3-20260718",
                "path": str(parent.resolve()),
                "sha256": parent_hash,
                "usage": "provenance_only",
            },
        }
        atomic_json(root / "session.json", session)
        return root, suite, units

    @staticmethod
    def collector_for(classification: str, calls: list[tuple[str, int]]):
        def collector(unit, output):
            calls.append((unit.logical_pass, unit.repetition))
            if classification == "baseline_nondeterminism":
                ids = [1 + unit.repetition] if unit.logical_pass == "P0" else [1]
            elif classification == "hook_effect":
                ids = [1] if unit.logical_pass == "P0" else [2]
            elif classification == "hook_instability":
                ids = (
                    [1] if unit.logical_pass == "P0"
                    else [2 + unit.repetition]
                )
            else:
                ids = [1]
            output_hash = hashlib.sha256(
                json.dumps(ids, separators=(",", ":")).encode()
            ).hexdigest()
            (output / "raw.json").write_text("{}\n", encoding="utf-8")
            (output / "generation_results.jsonl").write_text(json.dumps({
                "prompt_hash":
                    "3fd469a534c95ffb5a243ef6627b05cb1515012b4f482940218b8367c6b511c3",
                "input_token_count": 2,
                "tokenization_metadata": {"mode": "pinned_chat_template"},
                "output_token_ids": ids,
                "output_hash": output_hash,
                "output_token_count": len(ids),
                "stop_reason": "max_new_tokens",
                "execution_alignment_key": (
                    "a" if unit.repetition == 0 else "c"
                ) * 64,
            }) + "\n", encoding="utf-8")
            (output / "diagnostic_scores.json").write_text(json.dumps({
                "schema_version": "c1-token-drift-diagnostic-v1",
                "mode": "token_drift_v1",
                "evidence_class": "diagnostic_non_c1",
                "observation": "t0_semantic_match",
                "semantic_equality_used_for_alignment": False,
                "execution_alignment_key": (
                    "a" if unit.repetition == 0 else "c"
                ) * 64,
                "score_diagnostics": {
                    "schema_version": "token-drift-score-diagnostics-v1",
                    "capture_phase": "post_generate",
                    "step_count": len(ids),
                    "steps": [
                        {
                            "generation_step": index,
                            "generated_token_id": token,
                            "top2_token_ids": [token, token + 1],
                            "top2_logits": [2.0, 1.0],
                            "margin": 1.0,
                            "score_dtype": "float32",
                            "score_shape": [1, 32],
                            "score_tensor_bytes": 128,
                            "full_score_tensor_sha256": (
                                "b" * 64
                                if (
                                    classification == "latent_score_effect"
                                    and unit.logical_pass == "P2"
                                )
                                else f"{index + 1:x}" * 64
                            ),
                        }
                        for index, token in enumerate(ids)
                    ],
                },
                "tokenization_diagnostics": {
                    "mode": "pinned_chat_template"
                },
                "runtime_diagnostics": {
                    "schema_version": "token-drift-runtime-diagnostics-v2",
                    "deterministic_flags": {
                        "torch_deterministic_algorithms_enabled": (
                            unit.repetition == 0
                            if classification == "baseline_runtime_drift"
                            else True
                        ),
                        "cuda_matmul_allow_tf32": False,
                        "cudnn_enabled": True,
                        "cudnn_deterministic": True,
                        "cudnn_benchmark": False,
                        "cudnn_allow_tf32": False,
                        "cuda_matmul_allow_bf16_reduced_precision_reduction":
                            False,
                        "cuda_matmul_allow_fp16_reduced_precision_reduction":
                            False,
                        "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
                        "CUDA_LAUNCH_BLOCKING": "1",
                    }
                },
                "quality": {"status": "unknown"},
            }) + "\n", encoding="utf-8")
            if unit.logical_pass == "P2":
                (output / "routing_dispatch.jsonl").write_text(json.dumps({
                    "event_key": (
                        f"routing-{unit.repetition}"
                        if classification == "hook_instability_routing"
                        else "stable-routing"
                    ),
                    "selected_experts": (
                        [unit.repetition, 2, 3, 4, 5, 6, 7, 8]
                        if classification == "hook_instability_routing"
                        else [0, 1, 2, 3, 4, 5, 6, 7]
                    ),
                }) + "\n", encoding="utf-8")
            (output / "COLLECTOR_RESULT.json").write_text(json.dumps({
                "status": "success",
                "schema_valid": True,
                "raw_files": ["raw.json", "diagnostic_scores.json"],
                "work_unit_id": unit.work_unit_id,
            }) + "\n", encoding="utf-8")
            return 0

        return collector

    def run_classification(self, classification: str):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root, _suite, units = self.make_session(Path(temporary.name))
        calls: list[tuple[str, int]] = []
        collector = self.collector_for(classification, calls)
        engine = SchedulerEngine(
            SchedulerStore(root), lambda _unit: collector, max_attempts=1
        )
        result = projectctl.run_diagnostic_staged(engine, units)
        report = json.loads(
            (root / "diagnostic_compare.json").read_text(encoding="utf-8")
        )
        return root, calls, result, report

    def test_baseline_nondeterminism_stops_before_p2(self) -> None:
        root, calls, result, report = self.run_classification(
            "baseline_nondeterminism"
        )
        self.assertEqual(
            "baseline_condition_instability", report["classification"]
        )
        self.assertTrue(result["fail_fast"])
        self.assertEqual([("P0", 0), ("P0", 1)], calls)
        states = SchedulerStore(root).records()
        self.assertEqual(
            2, sum(row["state"] == State.PENDING.value for row in states)
        )

    def test_runtime_invariant_drift_stops_before_p2(self) -> None:
        root, calls, result, report = self.run_classification(
            "baseline_runtime_drift"
        )
        self.assertIsNone(report["classification"])
        self.assertTrue(result["fail_fast"])
        self.assertIn("runtime flags are incomplete", result["reason"])
        self.assertEqual([("P0", 0), ("P0", 1)], calls)
        self.assertEqual(
            2,
            sum(
                row["state"] == State.PENDING.value
                for row in SchedulerStore(root).records()
            ),
        )

    def test_hook_effect_classification_uses_four_units(self) -> None:
        _root, calls, result, report = self.run_classification("hook_effect")
        self.assertEqual(
            "instrumentation_association", report["classification"]
        )
        self.assertEqual("observational_only", report["classification_scope"])
        self.assertFalse(report["causal_claim_authorized"])
        self.assertTrue(report["score_evidence_validated"])
        self.assertEqual(2, len(report["P2_score_evidence"]))
        self.assertFalse(result["fail_fast"])
        self.assertEqual(4, len(calls))

    def test_hook_instability_classification_uses_four_units(self) -> None:
        _root, calls, result, report = self.run_classification(
            "hook_instability"
        )
        self.assertEqual(
            "instrumented_condition_instability", report["classification"]
        )
        self.assertFalse(result["fail_fast"])
        self.assertEqual(4, len(calls))

    def test_routing_fingerprint_instability_is_classified(self) -> None:
        _root, calls, result, report = self.run_classification(
            "hook_instability_routing"
        )
        self.assertEqual(
            "instrumented_condition_instability", report["classification"]
        )
        self.assertFalse(result["fail_fast"])
        self.assertEqual(4, len(calls))

    def test_score_only_drift_is_not_no_observed_drift(self) -> None:
        _root, calls, result, report = self.run_classification(
            "latent_score_effect"
        )
        self.assertEqual(
            "instrumentation_association", report["classification"]
        )
        self.assertFalse(result["fail_fast"])
        self.assertEqual(4, len(calls))

    def test_no_observed_drift_is_never_a_formal_pass(self) -> None:
        _root, calls, result, report = self.run_classification(
            "no_observed_effect"
        )
        self.assertEqual(
            "no_observed_drift_under_tested_configuration",
            report["classification"],
        )
        self.assertFalse(report["formal_gate_pass"])
        self.assertFalse(report["semantic_equivalence_accepted_as_pass"])
        self.assertFalse(result["fail_fast"])
        self.assertEqual(4, len(calls))

    def test_compare_rejects_score_evidence_inconsistent_with_generation(self):
        root, _calls, _result, _report = self.run_classification("hook_effect")
        _session, _suite, units = projectctl.load_diagnostic_session(root)
        unit = next(
            row for row in units
            if row.logical_pass == "P2" and row.repetition == 0
        )
        path = (
            SchedulerStore(root).complete_dir
            / unit.work_unit_id
            / "diagnostic_scores.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["score_diagnostics"]["steps"][0]["generated_token_id"] = 31
        path.chmod(path.stat().st_mode | 0o200)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        code, report = projectctl.compare_diagnostic(root)
        self.assertEqual(projectctl.BLOCKED, code)
        self.assertEqual("diagnostic_incomplete", report["status"])
        self.assertIn("score step 0 is invalid", report["reason"])

    def test_compare_rejects_score_alignment_key_mismatch(self):
        root, _calls, _result, _report = self.run_classification("hook_effect")
        _session, _suite, units = projectctl.load_diagnostic_session(root)
        unit = next(
            row for row in units
            if row.logical_pass == "P2" and row.repetition == 0
        )
        path = (
            SchedulerStore(root).complete_dir
            / unit.work_unit_id
            / "diagnostic_scores.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["execution_alignment_key"] = "b" * 64
        path.chmod(path.stat().st_mode | 0o200)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        code, report = projectctl.compare_diagnostic(root)
        self.assertEqual(projectctl.BLOCKED, code)
        self.assertIn("alignment differs", report["reason"])

    def test_suite_is_fixed_four_units_and_worker_env_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, suite, units = self.make_session(Path(temporary))
            self.assertEqual(4, len(units))
            self.assertEqual(1, suite["max_attempts"])
            self.assertTrue(
                suite["generation_config"]["return_dict_in_generate"]
            )
            self.assertTrue(suite["generation_config"]["output_scores"])
            session, _, _ = projectctl.load_diagnostic_session(root)
            engine = projectctl.make_engine(
                root, suite, session,
                extra_collector_env={
                    "C1_DIAGNOSTIC_MODE": projectctl.DIAGNOSTIC_MODE
                },
            )
            self.assertEqual(
                projectctl.DIAGNOSTIC_MODE,
                engine.collector_env["C1_DIAGNOSTIC_MODE"],
            )
            self.assertEqual(
                ":4096:8",
                engine.collector_env["CUBLAS_WORKSPACE_CONFIG"],
            )
            self.assertEqual(
                "1", engine.collector_env["CUDA_LAUNCH_BLOCKING"]
            )
            self.assertEqual("0", engine.collector_env["PYTHONHASHSEED"])

    def test_formal_session_loader_rejects_diagnostic_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _suite, _units = self.make_session(Path(temporary))
            args = argparse.Namespace(session=str(root))
            with self.assertRaises(ValueError):
                projectctl.load_session(args)
            with self.assertRaises(ValueError):
                projectctl.audit_session(root)

    def test_parent_evidence_hash_cannot_authorize_wrong_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wrong-parent.json"
            path.write_text(
                json.dumps({
                    "schema_version": "c1-token-drift-parent-evidence-v1",
                    "parent_session": "some-other-session",
                }) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "session identity drift"):
                projectctl._verify_parent_evidence(
                    str(path), projectctl.sha256_file(path)
                )

    def test_diagnostic_session_identifier_cannot_escape_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            projectctl, "DIAGNOSTIC_ROOT", Path(temporary) / "diagnostic_runs"
        ):
            for identifier in (
                "../scheduler_runs/diag",
                str(Path(temporary) / "absolute-diag"),
                "nested/diag",
                "..",
            ):
                with self.subTest(identifier=identifier), self.assertRaises(
                    ValueError
                ):
                    projectctl.diagnostic_session_root(identifier)

    def test_parent_wrapper_rejected_when_physical_source_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _suite, _units = self.make_session(Path(temporary))
            source = (
                projectctl.RUN_ROOT
                / projectctl.PARENT_DIAGNOSTIC_SESSION_ID
                / "session.json"
            )
            source.write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "source identity mismatch"
            ):
                projectctl.load_diagnostic_session(root)

    def test_parent_source_rejects_intermediate_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, _suite, _units = self.make_session(base)
            complete = (
                projectctl.RUN_ROOT
                / projectctl.PARENT_DIAGNOSTIC_SESSION_ID
                / "complete"
            )
            external = base / "external-complete"
            complete.rename(external)
            complete.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(
                ValueError, "source artifact is missing or untrusted"
            ):
                projectctl.load_diagnostic_session(root)

    def test_parent_source_rejects_session_root_symlink_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _suite, _units = self.make_session(Path(temporary))
            parent_root = (
                projectctl.RUN_ROOT / projectctl.PARENT_DIAGNOSTIC_SESSION_ID
            )
            alias = projectctl.RUN_ROOT / "same-files-different-physical-root"
            parent_root.rename(alias)
            parent_root.symlink_to(alias, target_is_directory=True)
            with self.assertRaisesRegex(
                ValueError, "source path is not trusted"
            ):
                projectctl.load_diagnostic_session(root)

    def test_compare_rejects_invalid_score_dtype(self) -> None:
        root, _calls, _result, _report = self.run_classification(
            "no_observed_effect"
        )
        _session, _suite, units = projectctl.load_diagnostic_session(root)
        unit = next(
            row for row in units
            if row.logical_pass == "P2" and row.repetition == 0
        )
        path = (
            SchedulerStore(root).complete_dir
            / unit.work_unit_id
            / "diagnostic_scores.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["score_diagnostics"]["steps"][0]["score_dtype"] = "int64"
        path.chmod(path.stat().st_mode | 0o200)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        code, report = projectctl.compare_diagnostic(root)
        self.assertEqual(projectctl.BLOCKED, code)
        self.assertIn("score step 0 is invalid", report["reason"])

    def test_compare_rejects_incoherent_score_tensor_bytes(self) -> None:
        root, _calls, _result, _report = self.run_classification(
            "no_observed_effect"
        )
        _session, _suite, units = projectctl.load_diagnostic_session(root)
        unit = next(
            row for row in units
            if row.logical_pass == "P2" and row.repetition == 0
        )
        path = (
            SchedulerStore(root).complete_dir
            / unit.work_unit_id
            / "diagnostic_scores.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["score_diagnostics"]["steps"][0]["score_tensor_bytes"] = 64
        path.chmod(path.stat().st_mode | 0o200)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        code, report = projectctl.compare_diagnostic(root)
        self.assertEqual(projectctl.BLOCKED, code)
        self.assertIn("score step 0 is invalid", report["reason"])

    def test_compare_rejects_boolean_score_shape(self) -> None:
        root, _calls, _result, _report = self.run_classification(
            "no_observed_effect"
        )
        _session, _suite, units = projectctl.load_diagnostic_session(root)
        unit = next(
            row for row in units
            if row.logical_pass == "P2" and row.repetition == 0
        )
        path = (
            SchedulerStore(root).complete_dir
            / unit.work_unit_id
            / "diagnostic_scores.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["score_diagnostics"]["steps"][0]["score_shape"] = [True, 32]
        path.chmod(path.stat().st_mode | 0o200)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        code, report = projectctl.compare_diagnostic(root)
        self.assertEqual(projectctl.BLOCKED, code)
        self.assertIn("score step 0 is invalid", report["reason"])

    def test_compare_normalizes_equivalent_score_dtype_aliases(self) -> None:
        root, _calls, _result, _report = self.run_classification(
            "no_observed_effect"
        )
        _session, _suite, units = projectctl.load_diagnostic_session(root)
        unit = next(
            row for row in units
            if row.logical_pass == "P2" and row.repetition == 0
        )
        path = (
            SchedulerStore(root).complete_dir
            / unit.work_unit_id
            / "diagnostic_scores.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["score_diagnostics"]["steps"][0]["score_dtype"] = (
            "torch.float32"
        )
        path.chmod(path.stat().st_mode | 0o200)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        code, report = projectctl.compare_diagnostic(root)
        self.assertEqual(projectctl.OK, code)
        self.assertEqual(
            "no_observed_drift_under_tested_configuration",
            report["classification"],
        )

    def test_compare_rejects_empty_runtime_flags(self) -> None:
        root, _calls, _result, _report = self.run_classification(
            "no_observed_effect"
        )
        _session, _suite, units = projectctl.load_diagnostic_session(root)
        for unit in units:
            path = (
                SchedulerStore(root).complete_dir
                / unit.work_unit_id
                / "diagnostic_scores.json"
            )
            document = json.loads(path.read_text(encoding="utf-8"))
            document["runtime_diagnostics"]["deterministic_flags"] = {}
            path.chmod(path.stat().st_mode | 0o200)
            path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        code, report = projectctl.compare_diagnostic(root)
        self.assertEqual(projectctl.BLOCKED, code)
        self.assertIn("runtime flags are incomplete", report["reason"])

    def test_diagnostic_run_isolated_end_to_end_without_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            model = base / "model"
            model.mkdir()
            (model / "config.json").write_text("{}\n", encoding="utf-8")
            parent = base / "v3.json"
            formal_root = base / "scheduler_runs"
            parent_hash, session_hash, suite_hash = self.write_parent_evidence(
                parent, formal_root
            )
            args = argparse.Namespace(
                session="diag-e2e",
                model_snapshot=str(model),
                profile=projectctl.LOCAL_PROFILE,
                parent_evidence=str(parent),
                parent_evidence_sha256=parent_hash,
            )
            diagnostic_root = base / "diagnostic_runs"
            with (
                patch.object(projectctl, "DIAGNOSTIC_ROOT", diagnostic_root),
                patch.object(projectctl, "RUN_ROOT", formal_root),
                patch.object(
                    projectctl, "PARENT_SESSION_RECORD_SHA256", session_hash
                ),
                patch.object(
                    projectctl, "PARENT_SUITE_SNAPSHOT_SHA256", suite_hash
                ),
                patch.object(
                    projectctl, "GPU_PROVIDER", self.gpu_environment
                ),
                patch.dict(os.environ, {
                    "C1_ADAPTER_FACTORY":
                        "tests.fake_c1_adapter:FakeTokenDriftDiagnosticAdapter",
                }, clear=False),
            ):
                code = projectctl.diagnostic_run(args)
            root = diagnostic_root / "diag-e2e"
            self.assertEqual(
                projectctl.OK, code, SchedulerStore(root).records()
            )
            self.assertEqual(4, len(SchedulerStore(root).records()))
            self.assertEqual(
                {"COMPLETE"},
                {row["state"] for row in SchedulerStore(root).records()},
            )
            report = json.loads(
                (root / "diagnostic_compare.json").read_text()
            )
            self.assertEqual(
                "no_observed_drift_under_tested_configuration",
                report["classification"],
            )
            self.assertFalse(report["formal_gate_pass"])
            session = json.loads((root / "session.json").read_text())
            self.assertEqual("diagnostic_non_c1", session["session_class"])
            self.assertFalse((formal_root / "current_session.json").exists())


if __name__ == "__main__":
    unittest.main()
