from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import projectctl
from scripts.c1_quality import canonical_hash, quality_binding_document
from scripts.c1_worker import main as c1_worker_main, quality_document
from scripts.c1_cleanroom_verify import verify as cleanroom_verify
from scheduler.state_machine import State


def suite_document(passes: list[str] | None = None) -> dict:
    prompt = "fixture prompt"
    source = {
        "sample_id": "source-1",
        "task_id": "T1",
        "prompt": prompt,
        "reference": 42,
    }
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
        "models": ["ibm-granite/granite-3.1-1b-a400m-instruct"],
        "model": {
            "id": "ibm-granite/granite-3.1-1b-a400m-instruct",
            "revision": "fixture-rev",
            "tokenizer_revision": "fixture-rev",
        },
        "samples": [
            {
                "sample_id": f"instance-{index}",
                "source_sample_id": f"source-{index}",
                "benchmark_id": "fixture",
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                "source_sample": {**source, "sample_id": f"source-{index}"},
            }
            for index in range(8)
        ],
        "repetitions": 1,
        "logical_passes": passes or ["P0", "P2"],
        "generation_config": {
            "max_new_tokens": 2, "do_sample": False, "num_beams": 1,
            "use_cache": True, "seed": 7,
        },
    }


class C1WorkerIntegrationTests(unittest.TestCase):
    @staticmethod
    def refresh_unit_manifests(unit: Path, changed: Path) -> None:
        manifest_path = unit / "WORK_UNIT_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        for entry in manifest["files"]:
            if entry["path"] == changed.name:
                entry["sha256"] = projectctl.sha256_file(changed)
                entry["bytes"] = changed.stat().st_size
        manifest_path.chmod(manifest_path.stat().st_mode | 0o200)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        checksums_path = unit / "checksums.sha256"
        checksums_path.chmod(checksums_path.stat().st_mode | 0o200)
        checksums_path.write_text(
            "".join(
                f"{entry['sha256']}  {entry['path']}\n"
                for entry in manifest["files"]
            ),
            encoding="utf-8",
        )

    def run_fixture(
        self, root: Path, *, drift: bool = False,
        passes: list[str] | None = None, extra_env: dict[str, str] | None = None,
        expected_code: int = projectctl.OK,
    ) -> Path:
        suite = root / "suite.json"
        suite.write_text(json.dumps(suite_document(passes)), encoding="utf-8")
        model_snapshot = root / "model-snapshot"
        model_snapshot.mkdir()
        (model_snapshot / "config.json").write_text("{}\n", encoding="utf-8")
        run_root = root / "runs"
        environment = {
            "C1_ADAPTER_FACTORY": "tests.fake_c1_adapter:FakeC1Adapter",
            "C1_FAKE_P2_DRIFT": "1" if drift else "0",
        }
        environment.update(extra_env or {})
        args = type("Args", (), {
            "suite": str(suite), "session": "fixture-session", "output": None,
            "profile": projectctl.LOCAL_PROFILE,
            "model_snapshot": str(model_snapshot),
        })()
        gpu = {
            "gpus": [{
                "name": projectctl.EXPECTED_GPU_NAME,
                "uuid": "GPU-fixture",
                "pci_bus_id": "0000:01:00.0",
                "total_vram_bytes": 8_000_000_000,
                "free_vram_bytes": 5_000_000_000,
            }],
            "compute_processes": [],
            "disk_free_bytes": 9 * 1024**3,
        }
        with (
            patch.object(projectctl, "RUN_ROOT", run_root),
            patch.object(projectctl, "GPU_PROVIDER", lambda _root: gpu),
            # Collector integration tests intentionally bypass only the
            # production C1-A matrix gate.
            patch.object(projectctl, "require_c1_a"),
            patch.object(
                projectctl,
                "run_c1_a",
                side_effect=(
                    None if passes in (None, ["P0", "P2"])
                    else lambda engine, units, **_kwargs: engine.run_pending(units)
                ),
            ) if passes not in (None, ["P0", "P2"]) else patch.object(
                projectctl, "verify_granite_suite_sources",
                wraps=projectctl.verify_granite_suite_sources,
            ),
            patch.dict(os.environ, environment, clear=False),
        ):
            self.assertEqual(expected_code, projectctl.start_run(args))
        return run_root / "fixture-session"

    def test_worker_atomic_audit_package_and_verify_without_gpu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = self.run_fixture(root)
            records = projectctl.SchedulerStore(session).records()
            self.assertEqual({State.COMPLETE.value}, {row["state"] for row in records})
            self.assertFalse(any((session / ".tmp").iterdir()))
            audit_code, audit_report = projectctl.audit_session(session)
            self.assertEqual(projectctl.OK, audit_code)
            audit_path = session / "TRACE_AUDIT.json"
            sidecar_path = session / "TRACE_AUDIT.json.sha256"
            self.assertTrue(audit_path.is_file())
            self.assertTrue(sidecar_path.is_file())
            self.assertEqual(
                hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                sidecar_path.read_text().split()[0],
            )
            provenance = json.loads(audit_path.read_text())
            self.assertEqual(16, provenance["expected_unit_count"])
            self.assertEqual(16, provenance["complete_unit_count"])
            self.assertEqual(0, provenance["finding_count"])
            self.assertEqual(
                audit_report["provenance_artifact"]["sha256"],
                hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            )
            audit_path.write_text('{"tampered":true}\n', encoding="utf-8")
            rebuilt_code, _ = projectctl.audit_session(session)
            self.assertEqual(projectctl.OK, rebuilt_code)
            self.assertEqual(
                hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                sidecar_path.read_text().split()[0],
            )
            self.assertEqual(
                "c1-trace-audit-provenance-v1",
                json.loads(audit_path.read_text())["schema_version"],
            )
            archive = root / "fixture.tar.gz"
            unrelated_run_root = root / "unrelated-global-run-root"
            with (
                patch.object(projectctl, "RUN_ROOT", unrelated_run_root),
                projectctl.execution_lock(unrelated_run_root),
            ):
                self.assertEqual(
                    projectctl.OK,
                    projectctl.package_session(session, str(archive)),
                )
            self.assertEqual(projectctl.OK, projectctl.verify_archive(archive))
            cleanroom_code, report = cleanroom_verify(archive, root / "cleanroom")
            self.assertEqual(0, cleanroom_code, report)
            self.assertGreater(report["summary"]["routing_event_count"], 0)
            self.assertGreater(report["summary"]["system_event_count"], 0)

    def test_p0_p2_output_drift_blocks_audit_and_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = self.run_fixture(
                root, drift=True, expected_code=projectctl.BLOCKED
            )
            code, report = projectctl.audit_session(session)
            self.assertEqual(projectctl.BLOCKED, code)
            self.assertTrue(any(
                item.get("error") == "P0/P2 output drift"
                for item in report["findings"]
            ))
            self.assertEqual(
                projectctl.BLOCKED,
                projectctl.package_session(session, str(root / "blocked.tar.gz")),
            )

    def test_audit_rejects_manifested_malformed_generation_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = self.run_fixture(Path(temporary))
            store = projectctl.SchedulerStore(session)
            record = next(
                row for row in store.records()
                if row["work_unit"]["logical_pass"] == "P0"
            )
            unit = store.complete_dir / record["work_unit"]["work_unit_id"]
            generation_path = unit / "generation_results.jsonl"
            row = json.loads(generation_path.read_text())
            row["stop_reason"] = ""
            generation_path.chmod(generation_path.stat().st_mode | 0o200)
            generation_path.write_text(
                json.dumps(row, sort_keys=True) + "\n", encoding="utf-8"
            )
            manifest_path = unit / "WORK_UNIT_MANIFEST.json"
            manifest = json.loads(manifest_path.read_text())
            for entry in manifest["files"]:
                if entry["path"] == "generation_results.jsonl":
                    entry["sha256"] = projectctl.sha256_file(generation_path)
                    entry["bytes"] = generation_path.stat().st_size
            manifest_path.chmod(manifest_path.stat().st_mode | 0o200)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            checksums_path = unit / "checksums.sha256"
            checksums_path.chmod(checksums_path.stat().st_mode | 0o200)
            checksums_path.write_text(
                "".join(
                    f"{entry['sha256']}  {entry['path']}\n"
                    for entry in manifest["files"]
                ),
                encoding="utf-8",
            )
            code, report = projectctl.audit_session(session)
            self.assertEqual(projectctl.BLOCKED, code)
            self.assertTrue(any(
                "c1_benchmark.schema.json validation failed" in error
                for finding in report["findings"]
                for error in finding.get("errors", [])
            ))

    def test_audit_revalidates_v2_quality_semantics_and_work_unit_binding(self):
        mutations = (
            ("pass_identity", lambda row: row.update({"pass_id": "P1"})),
            ("unknown_schema_field", lambda row: row.update({
                "undeclared_quality_field": True
            })),
            ("coherent_semantic_tamper", lambda row: row.update({
                "task_outcome": "unknown",
                "quality_binding_sha256": canonical_hash(
                    quality_binding_document({**row, "task_outcome": "unknown"})
                ),
            })),
        )
        for label, mutate in mutations:
            with self.subTest(mutation=label), tempfile.TemporaryDirectory() as temporary:
                session = self.run_fixture(Path(temporary))
                store = projectctl.SchedulerStore(session)
                record = next(
                    row for row in store.records()
                    if row["work_unit"]["logical_pass"] == "P0"
                )
                unit = store.complete_dir / record["work_unit"]["work_unit_id"]
                quality_path = unit / "quality_results.jsonl"
                quality = json.loads(quality_path.read_text())
                mutate(quality)
                quality_path.chmod(quality_path.stat().st_mode | 0o200)
                quality_path.write_text(
                    json.dumps(quality, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                self.refresh_unit_manifests(unit, quality_path)
                code, report = projectctl.audit_session(session)
                self.assertEqual(projectctl.BLOCKED, code)
                self.assertTrue(any(
                    "invalid formal evidence" in error
                    for finding in report["findings"]
                    for error in finding.get("errors", [])
                ))

    def test_cleanroom_adversary_blocks_and_removes_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = self.run_fixture(root)
            archive = root / "adversary.tar.gz"
            with patch.object(
                projectctl, "cleanroom_verify",
                return_value=(1, {"status": "failed", "rebuild_errors": ["drift"]}),
            ) as verifier:
                self.assertEqual(
                    projectctl.BLOCKED,
                    projectctl.package_session(session, str(archive)),
                )
            verifier.assert_called_once()
            self.assertFalse(archive.exists())
            self.assertFalse(
                archive.with_suffix(archive.suffix + ".sha256").exists()
            )

    def test_p1_native_profiler_bytes_are_manifested(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = self.run_fixture(
                Path(temporary),
                passes=["P1"],
                extra_env={
                    "C1_P1_BACKEND_FACTORY":
                        "tests.fake_c1_adapter:FakeProfilerBackend",
                },
            )
            complete = next((session / "complete").iterdir())
            native = complete / "raw/profiler/torch_trace.json"
            self.assertGreater(native.stat().st_size, 0)
            timeline = json.loads((complete / "runtime_timeline.json").read_text())
            descriptor = timeline["artifacts"][0]
            self.assertEqual("raw/profiler/torch_trace.json", descriptor["path"])
            self.assertEqual(
                hashlib.sha256(native.read_bytes()).hexdigest(),
                descriptor["sha256"],
            )
            result = json.loads((complete / "COLLECTOR_RESULT.json").read_text())
            self.assertIn(descriptor["path"], result["raw_files"])
            manifest = json.loads((complete / "WORK_UNIT_MANIFEST.json").read_text())
            self.assertIn(descriptor["path"], {row["path"] for row in manifest["files"]})

    def test_p5_records_observed_and_partial_unavailable_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = self.run_fixture(
                Path(temporary),
                passes=["P5_BASIC"],
                extra_env={
                    "C1_TELEMETRY_FACTORY":
                        "tests.fake_c1_adapter:FakeTelemetrySampler",
                },
            )
            complete = next((session / "complete").iterdir())
            availability = json.loads(
                (complete / "telemetry_availability.json").read_text()
            )
            self.assertEqual(
                "observed",
                availability["fields"]["gpu_utilization_percent"]["status"],
            )
            self.assertEqual(
                "unavailable_due_to_environment",
                availability["fields"]["power_watts"]["status"],
            )
            raw = complete / availability["raw_artifact"]["path"]
            self.assertGreater(raw.stat().st_size, 0)

    def test_p3_records_host_before_after_and_cuda_availability(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = self.run_fixture(Path(temporary), passes=["P3"])
            complete = next((session / "complete").iterdir())
            memory = json.loads((complete / "memory_observations.json").read_text())
            self.assertGreater(memory["host_rss_before_bytes"], 0)
            self.assertGreater(memory["host_rss_after_bytes"], 0)
            self.assertTrue(memory["oom_available"])
            self.assertIn("allocator_retries_available", memory)
            if not memory["allocator_retries_available"]:
                self.assertIn(
                    "allocator_retries", memory["unavailable_reasons"]
                )

    def test_quality_uses_records_and_leaves_interference_unknown(self):
        quality = quality_document(
            {"evaluator": "fixture", "validity": True, "correctness": True},
            {"output_token_ids": [1], "output_token_count": 1},
            [{
                "selected_experts": [0, 1, 2, 3, 4, 5, 6, 32],
                "router_logits": [float("nan")],
            }],
            "a" * 64,
            "P2",
        )
        self.assertFalse(quality["finite_values"])
        self.assertFalse(quality["expert_ids_legal"])
        self.assertIsNone(quality["instrumentation_semantic_interference"])
        self.assertEqual("fail", quality["status"])

    def test_t0_integer_semantic_mismatch_fails_quality(self):
        quality = quality_document(
            {
                "evaluator": "t0_integer_semantics_v1",
                "validity": True,
                "correctness": False,
            },
            {"output_token_ids": [2], "output_token_count": 1},
            [{"latency_ns": 1}],
            "a" * 64,
            "P0",
        )
        self.assertEqual("fail", quality["status"])
        self.assertTrue(any(
            "parsed integer sequence" in reason for reason in quality["reasons"]
        ))
        self.assertFalse(any(
            "token IDs" in reason for reason in quality["reasons"]
        ))

    def test_t1_parseable_incorrect_answer_fails_closed(self):
        quality = quality_document(
            {
                "evaluator": "gsm8k_last_number",
                "validity": True,
                "correctness": False,
                "details": {"parsed_answer": 41, "expected_answer": 42},
            },
            {"output_token_ids": [41], "output_token_count": 1},
            [{"latency_ns": 1}],
            "a" * 64,
            "P0",
        )
        self.assertEqual("fail", quality["status"])
        self.assertIn(
            "T1 gsm8k_last_number evaluator reported correctness=false",
            quality["reasons"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite = root / "suite.json"
            document = suite_document(["P0"])
            document["samples"][0]["source_sample"]["task_id"] = "T1"
            suite.write_text(json.dumps(document), encoding="utf-8")
            output = root / "output"
            environment = {
                "C1_ADAPTER_FACTORY":
                    "tests.fake_c1_adapter:FakeT1QualityFailureAdapter",
                "PROJECTCTL_SUITE_SNAPSHOT": str(suite),
                "PROJECTCTL_SAMPLE_ID": "instance-0",
                "PROJECTCTL_REPETITION": "0",
                "PROJECTCTL_SESSION_ID": "fixture-session",
                "PROJECTCTL_HARDWARE_SESSION_ID": "fixture-hardware",
                "PROJECTCTL_LOGICAL_PASS": "P0",
                "PROJECTCTL_WORK_UNIT_ID": "fixture-unit",
                "PROJECTCTL_MODEL_ID": "fixture-granite",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(
                    1, c1_worker_main(["--output-dir", str(output)])
                )
            failure = json.loads(
                (output / "failure_quality_results.jsonl").read_text()
            )
            self.assertIn(
                "T1 gsm8k_last_number evaluator reported correctness=false",
                failure["reasons"],
            )

    def test_quality_failure_retains_generation_runtime_and_partial_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite = root / "suite.json"
            suite.write_text(json.dumps(suite_document(["P0"])), encoding="utf-8")
            output = root / "output"
            environment = {
                "C1_ADAPTER_FACTORY":
                    "tests.fake_c1_adapter:FakeQualityFailureAdapter",
                "PROJECTCTL_SUITE_SNAPSHOT": str(suite),
                "PROJECTCTL_SAMPLE_ID": "instance-1",
                "PROJECTCTL_REPETITION": "0",
                "PROJECTCTL_SESSION_ID": "fixture-session",
                "PROJECTCTL_HARDWARE_SESSION_ID": "fixture-hardware",
                "PROJECTCTL_LOGICAL_PASS": "P0",
                "PROJECTCTL_WORK_UNIT_ID": "fixture-unit",
                "PROJECTCTL_MODEL_ID": "fixture-granite",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(1, c1_worker_main([
                    "--output-dir", str(output),
                ]))
            expected = {
                "failure_generation_results.jsonl",
                "failure_runtime_metadata.json",
                "failure_quality_results.jsonl",
                "failure_partial_baseline_metrics.json",
            }
            self.assertTrue(all((output / name).is_file() for name in expected))
            generation = json.loads(
                (output / "failure_generation_results.jsonl").read_text().strip()
            )
            self.assertEqual([9, 10], generation["output_token_ids"])
            quality = json.loads(
                (output / "failure_quality_results.jsonl").read_text().strip()
            )
            self.assertEqual("fail", quality["status"])
            self.assertTrue(any(
                "parsed integer sequence" in reason
                for reason in quality["reasons"]
            ))
            result = json.loads((output / "COLLECTOR_RESULT.json").read_text())
            self.assertEqual("failed", result["status"])
            self.assertFalse(result["schema_valid"])
            self.assertTrue(expected.issubset(result["raw_files"]))
            self.assertFalse((output / "pass_manifest.json").exists())
            self.assertFalse((output / "generation_results.jsonl").exists())

    def test_authorized_token_drift_diagnostic_saves_scores_and_completes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite_value = suite_document(["P0"])
            suite_value["evidence_class"] = "diagnostic_non_c1"
            suite_value["generation_config"].update({
                "return_dict_in_generate": True,
                "output_scores": True,
            })
            suite_value["samples"][0]["source_sample"]["task_id"] = "T0"
            suite = root / "suite.json"
            suite.write_text(json.dumps(suite_value), encoding="utf-8")
            output = root / "output"
            environment = {
                "C1_ADAPTER_FACTORY":
                    "tests.fake_c1_adapter:FakeInvalidTokenDriftDiagnosticAdapter",
                "C1_DIAGNOSTIC_MODE": "token_drift_v1",
                "PROJECTCTL_SUITE_SNAPSHOT": str(suite),
                "PROJECTCTL_SAMPLE_ID": "instance-0",
                "PROJECTCTL_REPETITION": "0",
                "PROJECTCTL_SESSION_ID": "fixture-session",
                "PROJECTCTL_HARDWARE_SESSION_ID": "fixture-hardware",
                "PROJECTCTL_LOGICAL_PASS": "P0",
                "PROJECTCTL_WORK_UNIT_ID": "fixture-unit",
                "PROJECTCTL_MODEL_ID": "fixture-granite",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(0, c1_worker_main([
                    "--output-dir", str(output),
                ]))
            diagnostic_path = output / "diagnostic_scores.json"
            diagnostic = json.loads(diagnostic_path.read_text())
            self.assertEqual("t0_semantic_mismatch", diagnostic["observation"])
            self.assertFalse(diagnostic["semantic_equality_used_for_alignment"])
            self.assertEqual(
                "post_generate",
                diagnostic["score_diagnostics"]["capture_phase"],
            )
            self.assertEqual(
                [9, 10],
                [
                    row["generated_token_id"]
                    for row in diagnostic["score_diagnostics"]["steps"]
                ],
            )
            self.assertIn(
                "CUBLAS_WORKSPACE_CONFIG",
                diagnostic["runtime_diagnostics"]["deterministic_flags"],
            )
            quality = json.loads(
                (output / "quality_results.jsonl").read_text().strip()
            )
            self.assertEqual("unknown", quality["status"])
            self.assertFalse(quality["benchmark_parseable"])
            self.assertEqual(0.0, quality["score"])
            manifest = json.loads((output / "pass_manifest.json").read_text())
            descriptor = next(
                row for row in manifest["raw_artifacts"]
                if row["path"] == "diagnostic_scores.json"
            )
            self.assertEqual(diagnostic_path.stat().st_size, descriptor["bytes"])
            self.assertEqual(
                hashlib.sha256(diagnostic_path.read_bytes()).hexdigest(),
                descriptor["sha256"],
            )
            result = json.loads((output / "COLLECTOR_RESULT.json").read_text())
            self.assertEqual("success", result["status"])
            self.assertIn("diagnostic_scores.json", result["raw_files"])
            runtime = json.loads((output / "runtime_metadata.json").read_text())
            self.assertNotIn("diagnostic_metadata", runtime)

    def test_diagnostic_mode_without_diagnostic_evidence_class_still_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite_value = suite_document(["P0"])
            suite_value["samples"][0]["source_sample"]["task_id"] = "T0"
            suite = root / "suite.json"
            suite.write_text(json.dumps(suite_value), encoding="utf-8")
            output = root / "output"
            environment = {
                "C1_ADAPTER_FACTORY":
                    "tests.fake_c1_adapter:FakeTokenDriftDiagnosticAdapter",
                "C1_DIAGNOSTIC_MODE": "token_drift_v1",
                "PROJECTCTL_SUITE_SNAPSHOT": str(suite),
                "PROJECTCTL_SAMPLE_ID": "instance-0",
                "PROJECTCTL_REPETITION": "0",
                "PROJECTCTL_SESSION_ID": "fixture-session",
                "PROJECTCTL_HARDWARE_SESSION_ID": "fixture-hardware",
                "PROJECTCTL_LOGICAL_PASS": "P0",
                "PROJECTCTL_WORK_UNIT_ID": "fixture-unit",
                "PROJECTCTL_MODEL_ID": "fixture-granite",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(1, c1_worker_main([
                    "--output-dir", str(output),
                ]))
            self.assertFalse((output / "diagnostic_scores.json").exists())
            result = json.loads((output / "COLLECTOR_RESULT.json").read_text())
            self.assertEqual("failed", result["status"])

    def test_worker_p2_emits_strict_routing_and_record_derived_quality(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite = root / "suite.json"
            suite.write_text(json.dumps(suite_document(["P2"])), encoding="utf-8")
            output = root / "output"
            environment = {
                "C1_ADAPTER_FACTORY": "tests.fake_c1_adapter:FakeC1Adapter",
                "PROJECTCTL_SUITE_SNAPSHOT": str(suite),
                "PROJECTCTL_SAMPLE_ID": "instance-1",
                "PROJECTCTL_REPETITION": "0",
                "PROJECTCTL_SESSION_ID": "fixture-session",
                "PROJECTCTL_HARDWARE_SESSION_ID": "fixture-hardware",
                "PROJECTCTL_LOGICAL_PASS": "P2",
                "PROJECTCTL_WORK_UNIT_ID": "fixture-unit",
                "PROJECTCTL_MODEL_ID": "fixture-granite",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(0, c1_worker_main([
                    "--output-dir", str(output),
                ]))
            routes = [
                json.loads(line)
                for line in (output / "routing_dispatch.jsonl").read_text().splitlines()
            ]
            self.assertEqual(72, len(routes))
            self.assertEqual({0, 1}, {row["call_index"] for row in routes})
            self.assertEqual({"prefill", "decode"}, {row["phase"] for row in routes})
            quality = json.loads(
                (output / "quality_results.jsonl").read_text().strip()
            )
            self.assertTrue(quality["finite_values"])
            self.assertTrue(quality["expert_ids_legal"])
            self.assertIsNone(quality["instrumentation_semantic_interference"])
            generation = json.loads(
                (output / "generation_results.jsonl").read_text().strip()
            )
            self.assertEqual(
                "pinned_chat_template",
                generation["tokenization_metadata"]["mode"],
            )
            self.assertTrue(
                generation["tokenization_metadata"]["add_generation_prompt"]
            )


if __name__ == "__main__":
    unittest.main()
