from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import projectctl
from collectors.trace_contract import build_execution_alignment_key
from scheduler import (
    ExecutionLockBusy, SchedulerEngine, SchedulerStore, execution_lock,
    expand_work_units,
)


FAKE = Path(__file__).parent / "fault_injection/fake_collector.py"


def formal_generation_row(output_ids: list[int]) -> dict:
    row = {
        "suite_version": projectctl.EXPECTED_SUITE_REVISION,
        "model_revision": "model-revision",
        "tokenizer_revision": "tokenizer-revision",
        "benchmark_id": "fixture",
        "sample_id": "sample",
        "prompt_hash": "b" * 64,
        "generation_config_hash": "c" * 64,
        "seed": 20260718,
        "repetition_id": 0,
        "hardware_session_id": "fixture-hardware",
        "input_token_ids": [7, 8],
        "input_token_count": 2,
        "output_token_ids": output_ids,
        "output_token_count": len(output_ids),
        "output_hash": hashlib.sha256(json.dumps(
            output_ids, separators=(",", ":")
        ).encode()).hexdigest(),
        "stop_reason": "eos_token",
    }
    row["execution_alignment_key"] = build_execution_alignment_key(row)
    return row


def write_formal_quality(output: Path) -> None:
    (output / "quality_results.jsonl").write_text(
        json.dumps({
            "schema_version": "c1-quality-v2",
            "evaluator": "gsm8k_last_number",
            "parser_outcome": "parseable",
            "task_outcome": "correct",
            "blocking_status": "pass",
            "quality_binding_sha256": "d" * 64,
        }) + "\n",
        encoding="utf-8",
    )


class ProjectctlSchedulerTests(unittest.TestCase):
    @staticmethod
    def gpu_environment(_root: Path) -> dict:
        return {
            "gpus": [{
                "index": "0",
                "name": projectctl.EXPECTED_GPU_NAME,
                "uuid": "GPU-fixture",
                "pci_bus_id": "00000000:01:00.0",
                "total_vram_bytes": 8_000_000_000,
                "free_vram_bytes": 5_000_000_000,
            }],
            "compute_processes": [],
            "disk_free_bytes": 9 * 1024**3,
            "cuda_visible_devices": "0",
        }

    @staticmethod
    def c1_suite() -> dict:
        return {
            "stage": "C1-A",
            "suite_id": projectctl.EXPECTED_SUITE_ID,
            "suite_revision": projectctl.EXPECTED_SUITE_REVISION,
            "source_manifest_path":
                "configs/test_suites/frozen/v1.4.0/sample_manifest.jsonl",
            "source_manifest_sha256":
                projectctl.EXPECTED_SOURCE_MANIFEST_SHA256,
            "selection_manifest_path":
                "configs/test_suites/granite_c1/sample_manifest.jsonl",
            "selection_manifest_sha256":
                projectctl.EXPECTED_SELECTION_MANIFEST_SHA256,
            "snapshot_inventory_path":
                "configs/test_suites/granite_c1/snapshot_inventory.json",
            "snapshot_inventory_sha256":
                projectctl.EXPECTED_SNAPSHOT_INVENTORY_SHA256,
            "models": ["fixture-model"],
            "samples": [f"fixture-sample-{index}" for index in range(8)],
            "repetitions": 1,
            "logical_passes": ["P0", "P2"],
            "collector_commands": {
                pass_id: [sys.executable, str(FAKE), "--mode", "success"]
                for pass_id in ("P0", "P2")
            },
        }

    def start_fixture(self, root: Path) -> tuple[Path, argparse.Namespace]:
        suite_path = root / "suite.json"
        suite_path.write_text(json.dumps(self.c1_suite()), encoding="utf-8")
        model_snapshot = root / "model"
        model_snapshot.mkdir()
        (model_snapshot / "config.json").write_text("{}\n", encoding="utf-8")
        args = argparse.Namespace(
            suite=str(suite_path), session="session", output=None,
            model_snapshot=str(model_snapshot), profile=projectctl.LOCAL_PROFILE,
        )
        run_root = root / "runs"
        with (
            patch.object(projectctl, "RUN_ROOT", run_root),
            patch.object(projectctl, "GPU_PROVIDER", self.gpu_environment),
            patch.object(projectctl, "run_c1_a", return_value={
                "dispatched": 0, "budget_exhausted": False, "fail_fast": False,
            }),
        ):
            self.assertEqual(projectctl.OK, projectctl.start_run(args))
        return run_root / "session", args

    def test_start_persists_identity_and_package_requires_cleanroom_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session, _ = self.start_fixture(root)
            with patch.object(projectctl, "RUN_ROOT", root / "runs"):
                archive = root / "session.tar.gz"
                self.assertEqual(
                    projectctl.BLOCKED,
                    projectctl.package_session(session, str(archive)),
                )
                self.assertFalse(archive.exists())
                metadata = json.loads((session / "session.json").read_text())
                self.assertEqual(5_000_000_000, metadata["environment"]["gpu"]["free_vram_bytes"])
                self.assertEqual(
                    metadata["started_epoch"] + 105 * 60,
                    metadata["execution_deadline_epoch"],
                )
                self.assertEqual(
                    projectctl.sha256_file(session / "suite_snapshot.json"),
                    metadata["suite_snapshot_sha256"],
                )

    def test_named_ineligible_suite_is_blocked_before_state_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary) / "runs"
            args = argparse.Namespace(
                suite="granite_c1_regression_v1",
                session="must-not-run",
                output=None,
                model_snapshot="/not/reached",
                profile=projectctl.LOCAL_PROFILE,
            )
            with patch.object(projectctl, "RUN_ROOT", run_root):
                self.assertEqual(projectctl.BLOCKED, projectctl.start_run(args))
            self.assertFalse((run_root / "must-not-run").exists())

    def test_run_root_lock_is_nonblocking_and_records_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with execution_lock(root) as owner:
                self.assertEqual(owner["pid"], os.getpid())
                with self.assertRaises(ExecutionLockBusy):
                    with execution_lock(root):
                        pass

    def test_gpu_identity_and_decimal_free_vram_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = self.gpu_environment(root)
            bad["gpus"][0]["free_vram_bytes"] = 4_999_999_999
            with patch.object(projectctl, "GPU_PROVIDER", lambda _root: bad):
                with self.assertRaisesRegex(ValueError, "5000000000"):
                    projectctl.hardware_preflight(root)
            wrong_identity = self.gpu_environment(root)
            wrong_identity["gpus"][0]["name"] = "NVIDIA GeForce RTX 3060"
            with patch.object(
                projectctl, "GPU_PROVIDER", lambda _root: wrong_identity
            ):
                with self.assertRaisesRegex(ValueError, "RTX 3050"):
                    projectctl.hardware_preflight(root)

    def test_audit_rebuilds_expected_set_and_detects_missing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session, _ = self.start_fixture(Path(temporary))
            code, report = projectctl.audit_session(session)
            self.assertEqual(projectctl.BLOCKED, code)
            self.assertEqual(
                "missing state",
                next(
                    item["error"] for item in report["findings"]
                    if item.get("error") == "missing state"
                ),
            )

    def test_suite_tamper_is_rejected_by_resume_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session, args = self.start_fixture(root)
            snapshot = session / "suite_snapshot.json"
            snapshot.chmod(0o644)
            snapshot.write_text("{}\n", encoding="utf-8")
            args.run_action = "resume"
            args.session = "session"
            with patch.object(projectctl, "RUN_ROOT", root / "runs"):
                self.assertEqual(projectctl.BLOCKED, projectctl.run_command(args))
            with self.assertRaisesRegex(ValueError, "suite snapshot hash"):
                projectctl.audit_session(session)

    def test_arbitrary_shape_and_duplicate_ids_are_rejected(self) -> None:
        suite = self.c1_suite()
        suite["suite_id"] = "arbitrary"
        with self.assertRaisesRegex(ValueError, "suite_id"):
            projectctl.require_c1_a(
                suite, projectctl.suite_units(suite), projectctl.LOCAL_PROFILE
            )
        suite = self.c1_suite()
        suite["samples"] = ["duplicate"] * 8
        with self.assertRaisesRegex(ValueError, "unique"):
            projectctl.require_c1_a(
                suite, projectctl.suite_units(suite), projectctl.LOCAL_PROFILE
            )

    def test_granite_declared_hash_drift_is_rejected(self) -> None:
        suite = self.c1_suite()
        suite["selection_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "declaration drift"):
            projectctl.verify_granite_suite_sources(suite)

    def test_named_suite_embeds_verified_t0_prompt_overrides(self) -> None:
        suite, _path = projectctl.load_suite("granite_c1_smoke_v1")
        projectctl.verify_granite_suite_sources(suite)
        self.assertEqual("granite-c1-v1.1.0", suite["suite_revision"])
        t0 = [
            sample for sample in suite["samples"]
            if sample["task_id"] == "T0"
        ]
        self.assertEqual(4, len(t0))
        for sample in t0:
            self.assertEqual(
                sample["prompt_override"],
                sample["source_sample"]["prompt"],
            )
            self.assertEqual(
                sample["prompt_hash"],
                hashlib.sha256(
                    sample["source_sample"]["prompt"].encode()
                ).hexdigest(),
            )
            self.assertEqual(
                sample["prompt_hash"],
                sample["source_sample"]["prompt_hash"],
            )
            self.assertNotEqual(
                sample["source_sample"]["source_prompt_hash"],
                sample["source_sample"]["prompt_hash"],
            )
            self.assertEqual(
                sample["raw_sample_hash"],
                sample["source_sample"]["raw_sample_hash"],
            )

    def test_resume_revalidates_gpu_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, args = self.start_fixture(root)
            mismatch = self.gpu_environment(root)
            mismatch["gpus"][0]["uuid"] = "GPU-other"
            args.run_action = "resume"
            args.session = "session"
            with (
                patch.object(projectctl, "RUN_ROOT", root / "runs"),
                patch.object(projectctl, "GPU_PROVIDER", lambda _root: mismatch),
            ):
                self.assertEqual(projectctl.BLOCKED, projectctl.run_command(args))

    def test_model_smoke_obeys_execution_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(model_action="smoke")
            with (
                patch.object(projectctl, "RUN_ROOT", root),
                patch.object(projectctl, "_model_command_impl") as implementation,
                execution_lock(root),
            ):
                self.assertEqual(projectctl.BLOCKED, projectctl.model_command(args))
                implementation.assert_not_called()

    def test_parent_death_worker_keeps_execution_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = (
                "import os,subprocess,sys;"
                "from pathlib import Path;"
                "from scheduler import execution_lock;"
                f"r=Path({str(root)!r});"
                "c=execution_lock(r);l=c.__enter__();"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(1)'],"
                "pass_fds=(l.fileno(),),stdout=subprocess.DEVNULL,"
                "stderr=subprocess.DEVNULL);"
                "print(p.pid,flush=True);os._exit(0)"
            )
            parent = subprocess.run(
                [sys.executable, "-c", script],
                text=True, capture_output=True, check=True,
            )
            self.assertTrue(parent.stdout.strip().isdigit())
            with self.assertRaises(ExecutionLockBusy):
                with execution_lock(root):
                    pass
            time.sleep(1.2)
            with execution_lock(root):
                pass

    def test_formal_generation_evidence_rejects_missing_and_tampered_fields(self):
        output_ids = [3, 4]
        valid = formal_generation_row(output_ids)
        self.assertEqual(
            output_ids,
            projectctl._formal_generation_evidence(valid)["output_token_ids"],
        )
        invalid_rows = [
            {key: value for key, value in valid.items()
             if key != "input_token_ids"},
            {**valid, "input_token_ids": [1, True]},
            {**valid, "output_token_ids": [3, -1]},
            {**valid, "output_token_count": 1},
            {**valid, "stop_reason": ""},
            {**valid, "output_hash": "0" * 64},
            {**valid, "prompt_hash": "d" * 64},
            {
                "execution_alignment_key": "a" * 64,
                "output_token_ids": output_ids,
                "output_hash": valid["output_hash"],
            },
        ]
        for row in invalid_rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                projectctl._formal_generation_evidence(row)

    def test_formal_quality_evidence_rejects_unresolved_complete_rows(self):
        valid = {
            "schema_version": "c1-quality-v2",
            "evaluator": "gsm8k_last_number",
            "parser_outcome": "parseable",
            "task_outcome": "incorrect",
            "blocking_status": "pass",
            "quality_binding_sha256": "d" * 64,
        }
        self.assertEqual(
            "incorrect",
            projectctl._formal_quality_evidence(valid)["task_outcome"],
        )
        for invalid in (
            {**valid, "parser_outcome": "unparseable"},
            {**valid, "parser_outcome": "evaluator_error"},
            {**valid, "task_outcome": "unknown"},
            {**valid, "quality_binding_sha256": "bad"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                projectctl._formal_quality_evidence(invalid)

    def test_canary_drift_stops_after_first_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            calls: list[tuple[str, str]] = []

            def collector(unit, output):
                calls.append((unit.sample_id, unit.logical_pass))
                (output / "raw.json").write_text("{}\n", encoding="utf-8")
                output_ids = [1] if unit.logical_pass == "P0" else [2]
                (output / "generation_results.jsonl").write_text(
                    json.dumps(formal_generation_row(output_ids)) + "\n",
                    encoding="utf-8",
                )
                write_formal_quality(output)
                (output / "COLLECTOR_RESULT.json").write_text(json.dumps({
                    "status": "success", "schema_valid": True,
                    "raw_files": ["raw.json"],
                    "work_unit_id": unit.work_unit_id,
                }) + "\n", encoding="utf-8")
                return 0

            store = SchedulerStore(Path(temporary))
            engine = SchedulerEngine(store, lambda _unit: collector)
            units = expand_work_units(["m"], ["s0", "s1"], 1, ["P0", "P2"])
            result = projectctl.run_c1_a(engine, units)
            self.assertTrue(result["fail_fast"])
            self.assertTrue(result["canary"])
            self.assertEqual(2, result["dispatched"])
            self.assertEqual(calls[0][0], calls[1][0])

    def test_canary_rejects_tampered_canonical_output_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            def collector(unit, output):
                (output / "raw.json").write_text("{}\n", encoding="utf-8")
                row = formal_generation_row([1])
                if unit.logical_pass == "P2":
                    row["output_hash"] = "0" * 64
                (output / "generation_results.jsonl").write_text(
                    json.dumps(row) + "\n", encoding="utf-8"
                )
                write_formal_quality(output)
                (output / "COLLECTOR_RESULT.json").write_text(json.dumps({
                    "status": "success",
                    "schema_valid": True,
                    "raw_files": ["raw.json"],
                    "work_unit_id": unit.work_unit_id,
                }) + "\n", encoding="utf-8")
                return 0

            store = SchedulerStore(Path(temporary))
            engine = SchedulerEngine(store, lambda _unit: collector)
            units = expand_work_units(["m"], ["s0", "s1"], 1, ["P0", "P2"])
            result = projectctl.run_c1_a(engine, units)
            self.assertTrue(result["fail_fast"])
            self.assertTrue(result["canary"])
            self.assertIn("output_hash", result["reason"])
            self.assertEqual(2, result["dispatched"])

    def test_canary_only_stops_after_successful_first_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            calls: list[tuple[str, str]] = []

            def collector(unit, output):
                calls.append((unit.sample_id, unit.logical_pass))
                (output / "raw.json").write_text("{}\n", encoding="utf-8")
                output_ids = [1]
                (output / "generation_results.jsonl").write_text(
                    json.dumps(formal_generation_row(output_ids)) + "\n",
                    encoding="utf-8",
                )
                write_formal_quality(output)
                (output / "COLLECTOR_RESULT.json").write_text(json.dumps({
                    "status": "success", "schema_valid": True,
                    "raw_files": ["raw.json"],
                    "work_unit_id": unit.work_unit_id,
                }) + "\n", encoding="utf-8")
                return 0

            store = SchedulerStore(Path(temporary))
            engine = SchedulerEngine(store, lambda _unit: collector)
            units = expand_work_units(["m"], ["s0", "s1"], 1, ["P0", "P2"])
            result = projectctl.run_c1_a(engine, units, canary_only=True)
            self.assertTrue(result["canary_complete"])
            self.assertFalse(result["fail_fast"])
            self.assertEqual(2, result["dispatched"])
            self.assertEqual(2, len(calls))
            self.assertEqual(calls[0][0], calls[1][0])

    def test_verify_requires_sidecar_before_archive_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "fixture.tar.gz"
            archive.write_bytes(b"not-a-tar")
            self.assertEqual(projectctl.BLOCKED, projectctl.verify_archive(archive))

    def test_wrapper_requires_benchmark_python(self) -> None:
        wrapper = projectctl.PACKAGE_ROOT / "projectctl"
        environment = os.environ.copy()
        environment.pop("BENCHMARK_PYTHON", None)
        blocked_result = subprocess.run(
            [str(wrapper), "--help"], env=environment,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(projectctl.BLOCKED, blocked_result.returncode)
        environment["BENCHMARK_PYTHON"] = sys.executable
        ok_result = subprocess.run(
            [str(wrapper), "--help"], env=environment,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, ok_result.returncode, ok_result.stderr)


if __name__ == "__main__":
    unittest.main()
