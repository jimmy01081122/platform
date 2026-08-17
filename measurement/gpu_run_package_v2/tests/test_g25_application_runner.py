from __future__ import annotations

import json
import argparse
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scheduler.execution_lock import execution_lock
from scheduler.g25_session import G25SessionStore
from adapters.models.granite_moe.snapshot import validate_exact_snapshot
from scripts import g25_application_runner as runner
from scripts import g25_qualification as g25
from scripts import projectctl
from tests.test_g25_application import approval_fixture, facts_fixture


class G25ApplicationRunnerTests(unittest.TestCase):
    class FakeContainmentController:
        evidence = {
            "schema_version": "g25-test-application-cgroup-v1",
            "unit": "fixture.service",
            "delegated": True,
        }

        def prepare_cell(self, cell_id):
            return SimpleNamespace(cell_id=cell_id)

        def emergency_drain_all(self):
            return []

        def assert_all_cells_empty(self):
            return None

        def close(self):
            return None

    @staticmethod
    def lifetime_guard_fixture(cell_id: str, *, timed_out: bool = False) -> dict:
        return {
            "schema_version": "g25-worker-lifetime-guard-v2",
            "mechanism": "systemd-delegated-cgroup-v2+pdeathsig-v2",
            "expected_parent_pid": 123,
            "expected_parent_start_ticks": 456,
            "lease_fd": 7,
            "lease_device": 8,
            "lease_inode": 9,
            "pdeathsig": "SIGKILL",
            "pdeathsig_number": 9,
            "ready_observed": True,
            "move_observed": True,
            "go_sent": True,
            "membership_ack_observed": True,
            "cell_cgroup_path": f"/fixture.service/g25-cell-{cell_id}",
            "cell_cgroup_device": 10,
            "cell_cgroup_inode": 11,
            "cgroup_kill_supported": True,
            "populated_zero_observed": True,
            "drain": {
                "initial_populated": int(timed_out),
                "term_sent": timed_out,
                "term_sent_monotonic_ns": 1 if timed_out else None,
                "term_grace_seconds": 30,
                "cgroup_kill_written": not timed_out,
                "cgroup_kill_monotonic_ns": None if timed_out else 2,
                "populated_zero_monotonic_ns": 3,
                "final_populated": 0,
            },
        }

    @staticmethod
    def final_audit(session: Path, execution_report: dict) -> dict:
        receipt = execution_report["seal_anchor"]
        return runner.audit_finalized_application(
            session,
            seal_anchor=Path(receipt["path"]),
            expected_anchor_sha256=receipt["sha256"],
        )

    def test_application_snapshot_gate_requires_exact_root_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "snapshot"
            root.mkdir()
            metadata = Path(temporary) / "metadata.json"
            metadata.write_text("{}\n", encoding="utf-8")
            payload = b"model fixture"
            (root / "payload.bin").write_bytes(payload)
            expected = {
                "payload.bin": (len(payload), hashlib.sha256(payload).hexdigest())
            }

            def tiny_validator(path, *, required_root):
                return validate_exact_snapshot(
                    path, expected_files=expected, required_root=required_root
                )

            with (
                patch.object(runner, "MODEL_SNAPSHOT_ROOT", root),
                patch.object(runner, "MODEL_INVENTORY_PATH", metadata),
                patch.object(runner, "validate_exact_snapshot", side_effect=tiny_validator),
            ):
                inventory = runner.validate_model_snapshot(root)
                self.assertEqual(1, inventory["observed_file_count"])
                runner.assert_model_snapshot_unchanged(inventory)
                (root / "rogue.py").write_text("pass\n", encoding="utf-8")
                with self.assertRaisesRegex(runner.ApplicationBlocked, "file set mismatch"):
                    runner.assert_model_snapshot_unchanged(inventory)

    def test_source_binding_uses_present_authoritative_quality_engine(self):
        self.assertEqual(
            runner.PACKAGE_ROOT / "scripts/benchmark_quality.py",
            runner.BENCHMARK_QUALITY_PATH,
        )
        self.assertTrue(runner.BENCHMARK_QUALITY_PATH.is_file())

    def test_exact_application_argv_has_frozen_outer_timeout_and_environment(self):
        argv = runner.exact_application_argv(
            approval_record=Path("approval.json"),
            review_record=Path("review.md"),
            evaluation_record=Path("evaluation.json"),
            review_tag="review-tag",
            model_snapshot=Path("model"),
        )
        self.assertEqual("/usr/bin/systemd-run", argv[0])
        self.assertIn("--property=Delegate=yes", argv)
        self.assertIn("--property=KillMode=control-group", argv)
        timeout_argv = argv[argv.index("--") + 1 :]
        self.assertEqual(
            ["/usr/bin/timeout", "--signal=TERM", "--kill-after=30s", "7500s", "/usr/bin/env", "-i"],
            timeout_argv[:6],
        )
        for value in (
            f"G25_RUNTIME_ROOT={runner.PACKAGE_ROOT / '.benchmark-runtime'}",
            "PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1",
            "CUDA_VISIBLE_DEVICES=GPU-4d160805-02d8-24aa-ef6a-2685832658a3",
            "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1",
            "CUBLAS_WORKSPACE_CONFIG=:4096:8", "CUDA_LAUNCH_BLOCKING=1",
            "PYTHONHASHSEED=0", "LC_ALL=C", "LANG=C",
            "--inhibit-cache", "qualification", "start",
        ):
            self.assertIn(value, argv)
        self.assertFalse(any(value.startswith("PYTHONPATH=") for value in argv))
        python_index = argv.index("/usr/bin/python3")
        self.assertEqual(
            ["-I", "-S", "-B", "-X", "utf8"],
            argv[python_index + 1:python_index + 6],
        )
        self.assertEqual(
            str(runner.PACKAGE_ROOT / "scripts/g25_isolated_bootstrap.py"),
            argv[python_index + 6],
        )
        self.assertEqual("projectctl", argv[python_index + 7])

    def test_outer_timeout_parent_verifier_accepts_only_frozen_wrapper(self):
        observed_timeout = [
            "/usr/bin/timeout", "--signal=TERM", "--kill-after=30s", "7500s",
            "/usr/bin/env", "python3",
        ]
        from scheduler.g25_cgroup_v2 import build_systemd_run_argv

        expected = build_systemd_run_argv(observed_timeout)
        valid = b"\0".join(item.encode() for item in observed_timeout) + b"\0"
        stat_fields = ["0"] * 20
        stat_fields[19] = "90000"
        process_stat = "123 (timeout) " + " ".join(stat_fields)
        with (
            patch.object(Path, "read_bytes", return_value=valid),
            patch.object(Path, "read_text", return_value=process_stat),
            patch.object(Path, "resolve", return_value=Path("/usr/bin/timeout")),
            patch.object(runner.os, "getppid", return_value=123),
            patch.object(runner.os, "sysconf", return_value=100),
            patch.object(runner.time, "clock_gettime", return_value=1000.0),
        ):
            runner.verify_outer_timeout_parent(expected)
        with (
            patch.object(
                Path, "read_bytes",
                return_value=valid.replace(b"python3", b"/bin/false"),
            ),
            patch.object(runner.os, "getppid", return_value=123),
        ):
            with self.assertRaises(runner.ApplicationBlocked):
                runner.verify_outer_timeout_parent(expected)

    def fake_snapshot(self, _package: Path, session: Path) -> dict:
        snapshot_root = session / "snapshots/package"
        fixtures = {
            "scripts/g25_isolated_bootstrap.py": "# frozen bootstrap\n",
            "scripts/g25_worker.py": "# frozen worker\n",
        }
        rows = []
        for relative, content in sorted(fixtures.items()):
            path = snapshot_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            rows.append({
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        source = session / "snapshots/source_checksums.txt"
        source.write_text(
            "".join(f"{row['sha256']}  {row['path']}\n" for row in rows),
            encoding="utf-8",
        )
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        inventory_value = {
            "schema_version": "g25-session-package-snapshot-v1",
            "source_checksums_sha256": source_hash,
            "file_count": len(rows),
            "files": rows,
            "inventory_sha256": runner.canonical_hash(rows),
        }
        inventory = session / "snapshots/inventory.json"
        inventory.write_text(json.dumps(inventory_value), encoding="utf-8")
        return {
            "inventory_sha256": inventory_value["inventory_sha256"],
            "source_checksums_sha256": source_hash,
            "file_count": len(rows),
        }

    def worker(self, argv, evidence_path, *, lease, containment, timeout_seconds):
        self.assertEqual(480, timeout_seconds)
        self.assertTrue(lease.active)
        self.assertRegex(containment.cell_id, r"^[0-9a-f]{64}$")
        descriptor_path = Path(argv[argv.index("--cell-descriptor") + 1])
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        selection = descriptor["selection"]
        evidence = g25.synthetic_worker_evidence(
            selection, descriptor["ceiling"], "QUALIFIED"
        )
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        stdout = "ignored worker log"
        stderr = ""
        # A synthetic identity is intentionally invalid in a real session. It
        # still exercises all 12 durable cells and the no-common-ceiling path.
        return {
            "supervisor_result": True,
            "worker_pid": 4242,
            "argv": list(argv),
            "stdout": stdout,
            "stderr": stderr,
            "return_code": 0,
            "timed_out": False,
            "wall_time_seconds": 1.0,
            "parent_started_unix_ns": 1,
            "parent_finished_unix_ns": 2,
            "termination_signal": None,
            "timeout_stage": "completed",
            "term_grace_seconds": 30,
            "lifetime_guard": self.lifetime_guard_fixture(containment.cell_id),
            "evidence_payload": evidence,
            "evidence_file_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
        }

    def execute(self, root: Path, *, binding_effect=None, build_ledger_effect=None,
                prevalidate_effect=None,
                **overrides):
        approval = approval_fixture()
        bindings = {
            "binding": "same",
            "same_source_review_sha256": "a" * 64,
            "evaluation_record_sha256": "b" * 64,
        }
        model = root / "model"
        model.mkdir()
        approval_path = root / "approval.json"; approval_path.write_text("{}")
        review_path = root / "review.json"; review_path.write_text("{}")
        evaluation_path = root / "evaluation.json"; evaluation_path.write_text("{}")
        args = dict(
            output_root=root / "qualification",
            run_root=root / "runs",
            approval_record=approval_path,
            review_record=review_path,
            evaluation_record=evaluation_path,
            review_tag="review-tag",
            model_snapshot=model,
            provider=lambda _root: facts_fixture(),
            worker_invoker=self.worker,
            containment_factory=self.FakeContainmentController,
        )
        args.update(overrides)
        expected_argv = runner.exact_application_argv(
            approval_record=approval_path,
            review_record=review_path,
            evaluation_record=evaluation_path,
            review_tag="review-tag",
            model_snapshot=model,
        )
        monotonic = args.get("monotonic", runner.boottime)
        args.setdefault("outer_timeout_verifier", lambda observed: {
            "schema_version": "g25-outer-timeout-observation-v2",
            "pid": 1,
            "executable": "/usr/bin/timeout",
            "argv": list(observed),
            "argv_sha256": runner.canonical_hash(list(observed)),
            "observed_timeout_argv": runner._approved_timeout_argv(observed),
            "observed_timeout_argv_sha256": runner.canonical_hash(
                runner._approved_timeout_argv(observed)
            ),
            "clock": "CLOCK_BOOTTIME",
            "clock_ticks_per_second": 100,
            "start_ticks": int(float(monotonic()) * 100),
            "started_boottime_seconds": float(monotonic()),
            "observed_boottime_seconds": float(monotonic()),
        })
        target = {"annotated_tag": "review-tag"}
        approval_sha256 = hashlib.sha256(approval_path.read_bytes()).hexdigest()
        prevalidated = (
            approval, target, bindings, bindings,
            expected_argv, {"review_id": "review-fixture"},
            {"evaluation_id": "evaluation-fixture", "evaluator": {
                "gate_alias": "5.6sol", "model": "gpt-5.6-sol"
            }}, {
                "inventory_sha256": "c" * 64,
                "absolute_path": str(model.resolve()),
            }, approval_sha256,
        )

        def prevalidate(*_args, **_kwargs):
            if prevalidate_effect is not None:
                prevalidate_effect()
            return prevalidated

        with (
            patch.object(runner, "prevalidate_application", side_effect=prevalidate),
            patch.object(runner, "resolve_review_target", return_value=target),
            patch.object(
                runner, "application_bindings",
                side_effect=binding_effect,
                return_value=bindings if binding_effect is None else None,
            ),
            patch.object(runner, "freeze_package_snapshot", side_effect=self.fake_snapshot),
            patch.object(runner, "audit_package_snapshot", return_value=[]),
            patch.object(runner, "configure_parent_determinism"),
            patch.object(
                runner,
                "verify_live_loaded_closure",
                side_effect=lambda role: {
                    "schema_version": "g25-live-loaded-closure-v1",
                    "role": role,
                    "system_closure_sha256": "1" * 64,
                    "loaded_object_count": 0,
                    "loaded_objects": [],
                    "loaded_set_sha256": runner.canonical_hash([]),
                    "dependency_edges": [],
                    "dependency_edges_sha256": runner.canonical_hash([]),
                },
            ),
            patch.object(runner, "assert_model_snapshot_unchanged", return_value={}),
            patch.object(g25, "_decode_frozen_output_ids", return_value="#### 42"),
            patch.object(runner, "verify_package_ledger", return_value=[]),
            patch.object(runner, "verify_runtime_inventory", return_value={}),
            patch.object(
                runner, "build_ledger",
                side_effect=build_ledger_effect,
                wraps=runner.build_ledger if build_ledger_effect is None else None,
            ),
            execution_lock(root / "runs") as lease,
        ):
            return runner.execute_application(lease=lease, **args)

    def test_complete_twelve_cell_control_chain_is_audited_but_not_qualified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, terminal = self.execute(root)
            session = root / "qualification" / runner.SESSION_ID
            self.assertEqual(20, code)
            self.assertEqual("EXECUTION_COMPLETE", terminal["disposition"])
            self.assertEqual(12, terminal["gpu_cells"])
            self.assertFalse(terminal["qualification_pass"])
            self.assertTrue((session / "terminal.json").is_file())
            self.assertTrue((session / "application_audit.json").is_file())
            self.assertTrue((session / "audit.json").is_file())
            states = [
                json.loads(path.read_text(encoding="utf-8"))["state"]
                for path in (session / "cell_state").glob("*.json")
            ]
            self.assertEqual(["RECORDED"] * 12, sorted(states))

    def test_only_clean_post_terminal_composite_audit_can_return_pass(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            g25, "classify_worker_evidence",
            return_value=("QUALIFIED", "fixture-qualified"),
        ), patch.object(
            g25, "_decode_frozen_output_ids", return_value="#### 42",
        ):
            root = Path(temporary)
            code, report = self.execute(root)
            session = root / "qualification" / runner.SESSION_ID
            self.assertEqual(0, code)
            self.assertTrue(report["qualification_pass"])
            self.assertTrue(report["final_audit"]["qualification_pass"])
            stored_terminal = json.loads((session / "terminal.json").read_text())
            self.assertTrue(stored_terminal["qualification_pass"])
            self.assertTrue((session / "final_seal.json").is_file())
            self.assertTrue(self.final_audit(session, report)["qualification_pass"])

    def test_dynamic_identity_failure_preserves_partial_session_and_never_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = [0]

            def provider(_root):
                calls[0] += 1
                if calls[0] == 6:
                    facts = facts_fixture()
                    facts["gpus"][0]["uuid"] = "GPU-drift"
                    return facts
                return facts_fixture()

            code, terminal = self.execute(root, provider=provider)
            self.assertEqual(20, code)
            self.assertEqual("INCOMPLETE_HARD_STOP", terminal["disposition"])
            self.assertEqual(4, terminal["gpu_cells"])
            self.assertFalse(terminal["resume"])
            self.assertFalse(terminal["retry_failed"])
            session = root / "qualification" / runner.SESSION_ID
            audit = json.loads((session / "application_audit.json").read_text())
            self.assertEqual(4, audit["recorded_cell_count"])
            self.assertEqual(8, len(audit["missing_or_incomplete_cell_ids"]))

    def test_qualification_audit_failure_cannot_leave_terminal_complete_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runner, "audit_session", return_value={
                "status": "incomplete", "findings": ["forced audit drift"]
            }):
                code, terminal = self.execute(root)
            self.assertEqual(20, code)
            self.assertEqual("INCOMPLETE_HARD_STOP", terminal["disposition"])
            session = root / "qualification" / runner.SESSION_ID
            state = json.loads((session / "session_state.json").read_text())
            self.assertEqual("TERMINAL_INCOMPLETE", state["state"])
            self.assertFalse(terminal["qualification_pass"])

    def test_terminal_builder_cannot_pass_an_incomplete_disposition_or_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = G25SessionStore.create(
                Path(temporary), runner.SESSION_ID,
                [f"cell-{index:02d}" for index in range(12)],
            )
            store.transition_session("FINALIZING")
            store.transition_session("AUDITING")
            store.transition_session("SEALING")
            store.transition_session("TERMINAL_INCOMPLETE")
            terminal = runner._terminal(
                store,
                disposition="INCOMPLETE_HARD_STOP",
                reason="forced deadline",
                gpu_cells=12,
                application_audit={
                    "status": "COMPLETE_SHAPE_AUDITED",
                    "ledger_eligible": True,
                    "findings": [],
                },
                qualification_audit={"status": "complete", "findings": []},
                selection_pass=True,
                deadline_ok=False,
                qualification_audit_artifact="audit.json",
            )
            self.assertFalse(terminal["qualification_pass"])
            self.assertFalse(terminal["deadline_ok"])

    def test_deadline_crossing_during_qualification_audit_hard_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [0.0]

            def delayed_audit(_root):
                now[0] = 7200.0
                return {"status": "complete", "findings": []}

            with patch.object(runner, "audit_session", side_effect=delayed_audit), patch.object(
                runner, "select_common_ceiling",
                return_value={"status": "QUALIFIED", "selected_common_ceiling": 256},
            ):
                code, terminal = self.execute(
                    root,
                    clock=lambda: 1000.0 + now[0],
                    monotonic=lambda: now[0],
                )
            self.assertEqual(20, code)
            self.assertEqual("INCOMPLETE_HARD_STOP", terminal["disposition"])
            self.assertFalse(terminal["qualification_pass"])
            self.assertIn("7200-second", terminal["reason"])

    def test_outer_start_time_includes_prevalidation_and_creates_zero_session_when_expired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [0.0]
            provider = Mock()

            def consume_deadline():
                now[0] = 7200.0

            with self.assertRaisesRegex(runner.ApplicationBlocked, "during validation"):
                self.execute(
                    root,
                    monotonic=lambda: now[0],
                    prevalidate_effect=consume_deadline,
                    provider=provider,
                )
            provider.assert_not_called()
            self.assertFalse((root / "qualification" / runner.SESSION_ID).exists())

    def test_qualification_audit_exception_is_bound_into_terminal_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                runner, "audit_session", side_effect=RuntimeError("forced crash")
            ):
                code, terminal = self.execute(root)
            self.assertEqual(20, code)
            self.assertEqual("INCOMPLETE_HARD_STOP", terminal["disposition"])
            session = root / "qualification" / runner.SESSION_ID
            failure = json.loads(
                (session / "qualification_audit_failure.json").read_text()
            )
            self.assertEqual("incomplete", failure["status"])
            self.assertIn("RuntimeError: forced crash", failure["findings"])
            self.assertIsNotNone(terminal["qualification_audit_sha256"])

    def test_5790_boundary_stops_new_dispatch_and_seals_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [0.0]
            calls = [0]

            def worker(*args, **kwargs):
                calls[0] += 1
                result = self.worker(*args, **kwargs)
                if calls[0] == 3:
                    now[0] = 5790.0
                return result

            code, terminal = self.execute(
                root, clock=lambda: 1000.0 + now[0],
                monotonic=lambda: now[0], worker_invoker=worker,
            )
            self.assertEqual(20, code)
            self.assertEqual(3, terminal["gpu_cells"])
            self.assertIn("5790-second", terminal["reason"])

    def test_dynamic_preflight_cannot_cross_5790_then_dispatch_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [0.0]
            calls = [0]

            def provider(_root):
                calls[0] += 1
                if calls[0] == 2:
                    now[0] = 5790.0
                return facts_fixture()

            worker = Mock()
            code, terminal = self.execute(
                root,
                clock=lambda: 1000.0 + now[0],
                monotonic=lambda: now[0],
                provider=provider,
                worker_invoker=worker,
            )
            self.assertEqual(20, code)
            self.assertEqual(0, terminal["gpu_cells"])
            self.assertIn("immediately before worker", terminal["reason"])
            worker.assert_not_called()

    def test_worker_crossing_6300_is_recorded_then_hard_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [0.0]

            def worker(*args, **kwargs):
                result = self.worker(*args, **kwargs)
                now[0] = 6300.0
                return result

            code, terminal = self.execute(
                root,
                clock=lambda: 1000.0 + now[0],
                monotonic=lambda: now[0],
                worker_invoker=worker,
            )
            self.assertEqual(20, code)
            self.assertEqual(1, terminal["gpu_cells"])
            self.assertIn("6300-second execution cutoff", terminal["reason"])
            session = root / "qualification" / runner.SESSION_ID
            audit = json.loads((session / "application_audit.json").read_text())
            self.assertEqual(1, audit["recorded_cell_count"])

    def test_existing_session_is_never_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.execute(root)
            with self.assertRaises(FileExistsError):
                self.execute(root)

    def test_timeout_is_recorded_and_does_not_shrink_twelve_cell_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = [0]

            def worker(*args, **kwargs):
                calls[0] += 1
                if calls[0] == 5:
                    return {
                        "supervisor_result": True, "stdout": "", "stderr": "",
                        "worker_pid": 4242, "argv": list(args[0]),
                        "return_code": -15, "timed_out": True,
                        "wall_time_seconds": 480.0,
                        "parent_started_unix_ns": 1, "parent_finished_unix_ns": 2,
                        "termination_signal": "SIGTERM",
                        "timeout_stage": "cell_timeout_sigterm", "term_grace_seconds": 30,
                        "lifetime_guard": self.lifetime_guard_fixture(
                            kwargs["containment"].cell_id, timed_out=True
                        ),
                        "evidence_payload": None, "evidence_file_sha256": None,
                        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                        "exception": "TimeoutExpired",
                    }
                return self.worker(*args, **kwargs)

            code, terminal = self.execute(root, worker_invoker=worker)
            self.assertEqual(20, code)
            self.assertEqual(12, calls[0])
            self.assertEqual(12, terminal["gpu_cells"])
            session = root / "qualification" / runner.SESSION_ID
            classes = [
                json.loads(path.read_text())["classification"]
                for path in (session / "cell_state").glob("*.json")
            ]
            self.assertEqual(1, classes.count("TIMEOUT"))

    def test_package_hash_drift_before_fifth_cell_hard_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = [0]

            def bindings(_review, _evaluation):
                calls[0] += 1
                return {
                    "binding": "drift" if calls[0] == 6 else "same",
                    "same_source_review_sha256": "a" * 64,
                    "evaluation_record_sha256": "b" * 64,
                }

            code, terminal = self.execute(root, binding_effect=bindings)
            self.assertEqual(20, code)
            self.assertEqual(4, terminal["gpu_cells"])
            self.assertIn("hash drift", terminal["reason"])

    def test_failure_after_twelfth_cell_preserves_all_raw_as_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, terminal = self.execute(
                root, build_ledger_effect=RuntimeError("injected ledger failure")
            )
            self.assertEqual(20, code)
            self.assertEqual(12, terminal["gpu_cells"])
            self.assertEqual("INCOMPLETE_HARD_STOP", terminal["disposition"])
            session = root / "qualification" / runner.SESSION_ID
            audit = json.loads((session / "application_audit.json").read_text())
            self.assertEqual(12, audit["recorded_cell_count"])
            self.assertFalse((session / "verdict.json").exists())

    def test_authorization_and_worker_io_manifests_are_journal_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.execute(root)
            session = root / "qualification" / runner.SESSION_ID
            authorization = session / "authorization_manifest.json"
            authorization.write_text("{}\n", encoding="utf-8")
            report = runner.audit_partial_session(session)
            self.assertTrue(any(
                "AUTHORIZATION_BOUND" in finding for finding in report["findings"]
            ))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.execute(root)
            session = root / "qualification" / runner.SESSION_ID
            manifest = next((session / "worker_io").glob("*/io_manifest.json"))
            manifest.write_text("{}\n", encoding="utf-8")
            report = runner.audit_partial_session(session)
            self.assertTrue(any(
                "worker I/O manifest" in finding for finding in report["findings"]
            ))

    def test_final_composite_audit_detects_terminal_and_audit_tamper(self):
        for relative in ("terminal.json", "application_audit.json", "audit.json", "final_seal.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _code, execution_report = self.execute(root)
                session = root / "qualification" / runner.SESSION_ID
                path = session / relative
                value = json.loads(path.read_text(encoding="utf-8"))
                value["tampered"] = True
                path.write_text(json.dumps(value), encoding="utf-8")
                report = self.final_audit(session, execution_report)
                self.assertFalse(report["qualification_pass"])
                self.assertTrue(report["findings"])

    def test_final_audit_replays_all_live_qualification_evidence(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            g25, "classify_worker_evidence",
            return_value=("QUALIFIED", "fixture-qualified"),
        ), patch.object(
            g25, "_decode_frozen_output_ids", return_value="#### 42",
        ):
            root = Path(temporary)
            code, execution_report = self.execute(root)
            session = root / "qualification" / runner.SESSION_ID
            self.assertEqual(0, code)
            self.assertTrue(self.final_audit(session, execution_report)[
                "qualification_pass"
            ])
            targets = [
                (session / "session.json", "mutate"),
                (session / "ledger.json", "mutate"),
                (session / "verdict.json", "mutate"),
                (next((session / "cells").glob("*.json")), "mutate"),
                (next((session / "raw").glob("*.json")), "delete"),
            ]
            for path, action in targets:
                with self.subTest(path=path.relative_to(session), action=action):
                    original = path.read_bytes()
                    if action == "delete":
                        path.unlink()
                    else:
                        value = json.loads(original)
                        value["tampered"] = True
                        path.write_text(json.dumps(value), encoding="utf-8")
                    report = self.final_audit(session, execution_report)
                    self.assertFalse(report["qualification_pass"])
                    self.assertTrue(any(
                        "qualification evidence inventory" in finding
                        or "qualification audit" in finding
                        for finding in report["findings"]
                    ))
                    path.write_bytes(original)

    def test_recursive_session_inventory_rejects_every_unsealed_entry_class(self):
        cases = (
            "root_file", "descriptor_file", "worker_file", "empty_directory",
            "symlink", "fifo",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary, (
                patch.object(
                    g25, "classify_worker_evidence",
                    return_value=("QUALIFIED", "fixture-qualified"),
                )
            ), patch.object(g25, "_decode_frozen_output_ids", return_value="#### 42"):
                root = Path(temporary)
                code, execution_report = self.execute(root)
                session = root / "qualification" / runner.SESSION_ID
                self.assertEqual(0, code)
                cell_id = next((session / "worker_io").iterdir()).name
                if case == "root_file":
                    (session / "rogue.json").write_text("{}\n", encoding="utf-8")
                elif case == "descriptor_file":
                    (session / "descriptors" / "rogue.json").write_text(
                        "{}\n", encoding="utf-8"
                    )
                elif case == "worker_file":
                    (session / "worker_io" / cell_id / "rogue.bin").write_bytes(b"x")
                elif case == "empty_directory":
                    (session / "rogue-empty").mkdir()
                elif case == "symlink":
                    (session / "descriptors" / "rogue-link").symlink_to(
                        session / "terminal.json"
                    )
                else:
                    os.mkfifo(session / "worker_io" / cell_id / "rogue-fifo")
                audit = self.final_audit(session, execution_report)
                self.assertFalse(audit["qualification_pass"])
                self.assertTrue(any(
                    "recursive session file inventory" in finding
                    for finding in audit["findings"]
                ))

    def test_external_anchor_is_exclusive_and_original_hash_defeats_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            g25, "classify_worker_evidence",
            return_value=("QUALIFIED", "fixture-qualified"),
        ), patch.object(g25, "_decode_frozen_output_ids", return_value="#### 42"):
            root = Path(temporary)
            code, execution_report = self.execute(root)
            session = root / "qualification" / runner.SESSION_ID
            self.assertEqual(0, code)
            receipt = execution_report["seal_anchor"]
            seal_path = session / "final_seal.json"
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                runner.write_external_seal_anchor(session, seal)

            missing_trust = runner.audit_finalized_application(
                session,
                seal_anchor=Path(receipt["path"]),
                expected_anchor_sha256=None,
            )
            self.assertFalse(missing_trust["qualification_pass"])
            self.assertTrue(any(
                "trusted external seal anchor SHA-256 is required" in finding
                for finding in missing_trust["findings"]
            ))

            # Re-encode the semantically identical seal and coherently update the
            # rewritable receipt.  Only the independently retained original hash
            # distinguishes this from the sealed execution.
            seal_path.write_text(
                json.dumps(seal, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            anchor_path = Path(receipt["path"])
            anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
            anchor["final_seal"] = {
                "path": "final_seal.json",
                "bytes": seal_path.stat().st_size,
                "sha256": hashlib.sha256(seal_path.read_bytes()).hexdigest(),
            }
            anchor_payload = {
                key: value for key, value in anchor.items()
                if key != "anchor_payload_sha256"
            }
            anchor["anchor_payload_sha256"] = runner.canonical_hash(anchor_payload)
            anchor_path.chmod(0o600)
            anchor_path.write_text(
                json.dumps(anchor, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            audit = self.final_audit(session, execution_report)
            self.assertFalse(audit["qualification_pass"])
            self.assertTrue(any(
                "external seal anchor differs from the trusted SHA-256" in finding
                for finding in audit["findings"]
            ))

    def test_cross_artifact_replay_rejects_rehashed_wrong_worker_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.execute(root)
            session = root / "qualification" / runner.SESSION_ID
            io_root = next((session / "worker_io").iterdir())
            cell_id = io_root.name
            supervisor_path = io_root / "supervisor.json"
            supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
            supervisor["argv"][-1] = "/different/model/snapshot"
            supervisor_path.write_text(json.dumps(supervisor), encoding="utf-8")
            manifest_path = io_root / "io_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["supervisor"] = {
                "bytes": supervisor_path.stat().st_size,
                "sha256": hashlib.sha256(supervisor_path.read_bytes()).hexdigest(),
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            raw_path = session / "raw" / f"{cell_id}.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["parent_process"]["io_manifest_sha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
            raw["parent_process"]["worker_argv_sha256"] = runner.canonical_hash(
                supervisor["argv"]
            )
            raw_path.write_text(json.dumps(raw), encoding="utf-8")
            report = runner.audit_partial_session(session)
            self.assertTrue(any(
                "supervisor worker argv differs" in finding
                for finding in report["findings"]
            ))

    def test_lost_package_wide_lease_hard_stops_before_next_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = [0]

            def assert_active(lease):
                calls[0] += 1
                if calls[0] >= 6:
                    raise RuntimeError("injected lease loss")
                self.assertTrue(lease.active)

            with patch.object(runner.ExecutionLease, "assert_active", new=assert_active):
                code, terminal = self.execute(root)
            self.assertEqual(20, code)
            self.assertEqual(4, terminal["gpu_cells"])
            self.assertIn("lease loss", terminal["reason"])

    def test_invalid_approval_causes_zero_provider_zero_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approval = root / "approval.json"
            review = root / "review.md"
            evaluation = root / "evaluation.json"
            model = root / "model"
            approval.write_text("{}", encoding="utf-8")
            review.write_text("review", encoding="utf-8")
            evaluation.write_text("{}", encoding="utf-8")
            model.mkdir()
            provider = Mock()
            args = argparse.Namespace(
                qualification_action="start",
                approval_record=str(approval), review_record=str(review),
                evaluation_record=str(evaluation),
                review_tag="review-tag", model_snapshot=str(model),
            )
            with (
                patch.object(projectctl, "QUALIFICATION_ROOT", root / "qualification"),
                patch.object(projectctl, "RUN_ROOT", root / "runs"),
                patch.object(projectctl, "G25_GPU_PROVIDER", provider),
                patch.object(
                    runner, "prevalidate_application",
                    side_effect=runner.ApplicationBlocked("invalid approval"),
                ),
            ):
                code = projectctl.qualification_command(args)
            self.assertEqual(projectctl.BLOCKED, code)
            provider.assert_not_called()
            self.assertFalse((root / "qualification").exists())

    def test_qualification_uses_same_package_wide_lease_as_formal_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approval = root / "approval.json"; approval.write_text("{}")
            review = root / "review.md"; review.write_text("review")
            evaluation = root / "evaluation.json"; evaluation.write_text("{}")
            model = root / "model"; model.mkdir()
            args = argparse.Namespace(
                qualification_action="start", approval_record=str(approval),
                review_record=str(review), review_tag="review-tag",
                evaluation_record=str(evaluation),
                model_snapshot=str(model),
            )
            execute = Mock()
            with (
                patch.object(projectctl, "QUALIFICATION_ROOT", root / "qualification"),
                patch.object(projectctl, "RUN_ROOT", root / "runs"),
                patch.object(runner, "prevalidate_application", return_value=None),
                patch.object(runner, "execute_application", execute),
                execution_lock(root / "runs"),
            ):
                code = projectctl.qualification_command(args)
            self.assertEqual(projectctl.BLOCKED, code)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
