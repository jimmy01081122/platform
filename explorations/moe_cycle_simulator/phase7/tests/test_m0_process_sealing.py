from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[4]
APPLICATION = REPO / "explorations/moe_cycle_simulator/phase7/application"
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    exact_regular_file_set,
    file_sha256,
    load_json,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    retain_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.driver import (  # noqa: E402
    seal_driver_failure,
)
from explorations.moe_cycle_simulator.phase7.application.executor.finalize import (  # noqa: E402
    STATUS_BY_OUTCOME,
    seal_terminal_session,
    verify_terminal_session,
)
from explorations.moe_cycle_simulator.phase7.application.executor.materialization_driver import (  # noqa: E402
    MATERIALIZATION_STATUS,
    seal_materialization_failure,
    seal_materialization_terminal,
    verify_materialization_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)


def _restore_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)
    root.chmod(0o700)


def _install_authority(session: Path) -> dict:
    package = build_application_ledger(APPLICATION)
    approval = load_json(APPLICATION / "approval.template.json")
    approval["application_ledger_sha256"] = package["ledger_sha256"]
    approval_path = session.parent / f"{session.name}-approval.json"
    write_new_json(approval_path, approval)
    registry = session.parent / f"{session.name}-consumption.json"
    registry.write_text(
        __import__("json").dumps(
            {
                "schema_version": "moe-simulator-phase7-used-approval-v1",
                "approval_id": approval["approval_id"],
                "approval_token_sha256": approval["approval_token_sha256"],
                "approved_session_id": approval["approved_session_id"],
                "approval_file_sha256": file_sha256(approval_path),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return retain_authority(
        application=APPLICATION,
        approval_path=approval_path,
        registry_path=registry,
        evidence_root=session,
        expected_application_ledger_sha256=package["ledger_sha256"],
    )


class M0ProcessAndSealingTests(unittest.TestCase):
    def test_exact_set_excludes_only_root_ledger_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_new_json(root / "evidence_ledger.json", {})
            nested = root / "nested"
            nested.mkdir()
            write_new_json(nested / "evidence_ledger.json", {})
            self.assertEqual(
                exact_regular_file_set(
                    root, excluded_root_files={"evidence_ledger.json"}
                ),
                {"nested/evidence_ledger.json"},
            )
            (root / "unsafe-link").symlink_to(nested / "evidence_ledger.json")
            with self.assertRaisesRegex(M0Error, "symlink is forbidden"):
                exact_regular_file_set(
                    root, excluded_root_files={"evidence_ledger.json"}
                )

    def test_outer_signal_kills_nested_session_escaping_worker(self) -> None:
        """Reproduce an outer timeout while a grandchild has called setsid()."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "escaped-worker.pid"
            logs = root / "logs"
            logs.mkdir()
            nested = (
                "import os,signal,time,pathlib;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "pathlib.Path(os.environ['ESCAPED_PID_FILE']).write_text("
                "str(os.getpid()),encoding='utf-8');"
                "time.sleep(120)"
            )
            supervised = (
                "import os,signal,subprocess,sys,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"subprocess.Popen([sys.executable,'-c',{nested!r}],"
                "start_new_session=True,env=os.environ.copy());"
                "time.sleep(120)"
            )
            harness = textwrap.dedent(
                f"""
                import os
                import signal
                import sys
                from pathlib import Path
                from explorations.moe_cycle_simulator.phase7.application.executor import driver

                driver.DRIVER_TERM_GRACE_SECONDS = 0.25
                driver.DRIVER_KILL_GRACE_SECONDS = 0.25
                signal.signal(signal.SIGTERM, driver.signal_handler)
                signal.signal(signal.SIGINT, driver.signal_handler)
                root = Path(sys.argv[1])
                try:
                    driver.run_streamed(
                        "outer-timeout",
                        [sys.executable, "-c", {supervised!r}],
                        cwd=root,
                        logs=root / "logs",
                        environment=os.environ.copy(),
                        timeout_seconds=120,
                    )
                except driver.DriverInterrupted:
                    (root / "interrupted").write_text("yes", encoding="utf-8")
                """
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(REPO)
            environment["ESCAPED_PID_FILE"] = str(pid_file)
            process = subprocess.Popen(
                [sys.executable, "-c", harness, str(root)],
                cwd=REPO,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while not pid_file.is_file() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            if not pid_file.is_file():
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f"escaped worker did not start: {stdout!r} {stderr!r}")
            escaped_pid = int(pid_file.read_text(encoding="utf-8"))
            os.kill(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, (stdout, stderr))
            self.assertTrue((root / "interrupted").is_file())
            self.assertFalse(Path("/proc", str(escaped_pid)).exists())

    def test_pass_fail_and_incomplete_sessions_are_ledgered_and_sealed(self) -> None:
        for outcome in ("PASS", "FAIL", "INCOMPLETE"):
            with self.subTest(outcome=outcome):
                with tempfile.TemporaryDirectory() as directory:
                    session_id = f"phase7-m0-seal-{outcome.lower()}"
                    session = Path(directory) / session_id
                    session.mkdir()
                    (session / "logs").mkdir()
                    (session / "logs" / "stderr.log").write_text(
                        "prospective CPU fixture\n", encoding="utf-8"
                    )
                    if outcome == "PASS":
                        authority = _install_authority(session)
                        write_new_json(
                            session / "m0_result.json",
                            {
                                "session_id": session_id,
                                "verdict": "PASS",
                                "findings": [],
                            },
                        )
                        write_new_json(
                            session / "driver_commands.json",
                            {
                                "commands": [],
                                "authority_evidence_sha256":
                                    semantic_sha256(authority),
                            },
                        )
                    else:
                        write_new_json(
                            session / "driver_failure.json",
                            {
                                "schema_version":
                                    "moe-simulator-phase7-m0-driver-failure-v1",
                                "session_id": session_id,
                                "status": f"{outcome}_IMMUTABLE",
                                "terminal_outcome": outcome,
                                "failure": "adversarial CPU fixture",
                                "completed_commands": [],
                                "resume_allowed": False,
                                "retry_allowed": False,
                            },
                        )
                    try:
                        ledger = seal_terminal_session(
                            session, session_id, outcome  # type: ignore[arg-type]
                        )
                        self.assertEqual(
                            (session / "session_status.txt").read_text(
                                encoding="utf-8"
                            ),
                            STATUS_BY_OUTCOME[outcome],
                        )
                        on_disk = load_json(session / "evidence_ledger.json")
                        self.assertEqual(on_disk, ledger)
                        self.assertEqual(verify_terminal_session(session), ledger)
                        self.assertEqual(on_disk["terminal_outcome"], outcome)
                        digest_input = dict(on_disk)
                        digest = digest_input.pop("ledger_sha256")
                        self.assertEqual(digest, semantic_sha256(digest_input))
                        member_paths = {
                            item["path"] for item in on_disk["members"]
                        }
                        self.assertIn("session_status.txt", member_paths)
                        expected = (
                            "m0_result.json"
                            if outcome == "PASS"
                            else "driver_failure.json"
                        )
                        self.assertIn(expected, member_paths)
                        self.assertEqual(session.stat().st_mode & 0o777, 0o555)
                        for path in session.rglob("*"):
                            if path.is_file():
                                self.assertEqual(path.stat().st_mode & 0o222, 0)
                        with self.assertRaises(OSError):
                            write_new_json(session / "late-evidence.json", {})
                        target = session / expected
                        target.chmod(0o600)
                        target.write_text("{}\n", encoding="utf-8")
                        with self.assertRaises(M0Error):
                            verify_terminal_session(session)
                    finally:
                        _restore_tree(session)

    def test_materialization_complete_is_marker_last_exact_set_and_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "materialization-complete"
            root.mkdir()
            authority = _install_authority(root)
            write_new_json(
                root / "stage_result.json",
                {
                    "schema_version":
                        "moe-simulator-phase7-materialization-stage-result-v1",
                    "status": "COMPLETE_HARD_STOP",
                    "authority_evidence_sha256": semantic_sha256(authority),
                    "gpu_workload_performed": False,
                },
            )
            try:
                ledger = seal_materialization_terminal(root, "COMPLETE_HARD_STOP")
                self.assertEqual(verify_materialization_terminal(root), ledger)
                self.assertEqual(
                    (root / "materialization_status.txt").read_text(
                        encoding="utf-8"
                    ),
                    MATERIALIZATION_STATUS["COMPLETE_HARD_STOP"],
                )
                target = root / "stage_result.json"
                target.chmod(0o600)
                target.write_text("{}\n", encoding="utf-8")
                with self.assertRaises(M0Error):
                    verify_materialization_terminal(root)
            finally:
                _restore_tree(root)

    def test_cleanup_errors_are_aggregated_before_incomplete_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_id = "phase7-m0-cleanup-failure"
            session = Path(directory) / session_id
            session.mkdir()
            try:
                with patch(
                    "explorations.moe_cycle_simulator.phase7.application."
                    "executor.driver.terminate_active",
                    side_effect=(M0Error("cleanup-one"), M0Error("cleanup-two")),
                ):
                    outcome = seal_driver_failure(
                        session=session,
                        session_id=session_id,
                        original_error=M0Error("qualification failed"),
                        commands=[],
                        authority_evidence=None,
                    )
                self.assertEqual(outcome, "INCOMPLETE")
                failure = load_json(session / "driver_failure.json")
                self.assertIs(failure["cleanup"]["completed"], False)
                self.assertEqual(len(failure["cleanup"]["errors"]), 2)
                self.assertEqual(
                    load_json(session / "evidence_ledger.json")[
                        "terminal_outcome"
                    ],
                    "INCOMPLETE",
                )
                self.assertEqual(
                    (session / "session_status.txt").read_text(encoding="utf-8"),
                    STATUS_BY_OUTCOME["INCOMPLETE"],
                )
            finally:
                _restore_tree(session)

    def test_preflight_rejects_symlink_without_terminal_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_id = "phase7-m0-symlink-preflight"
            session = Path(directory) / session_id
            session.mkdir()
            authority = _install_authority(session)
            write_new_json(
                session / "m0_result.json",
                {"session_id": session_id, "verdict": "PASS", "findings": []},
            )
            write_new_json(
                session / "driver_commands.json",
                {
                    "commands": [],
                    "authority_evidence_sha256": semantic_sha256(authority),
                },
            )
            (session / "unsafe-link").symlink_to(Path(directory) / "outside")
            with self.assertRaises(M0Error):
                seal_terminal_session(session, session_id, "PASS")
            for name in (
                "session_status.txt",
                "evidence_ledger.json",
                ".session_status.txt.staged",
                ".evidence_ledger.json.staged",
            ):
                self.assertFalse((session / name).exists(), name)

    def test_terminal_seal_revalidates_retained_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_id = "phase7-m0-authority-revalidation"
            session = Path(directory) / session_id
            session.mkdir()
            authority = _install_authority(session)
            write_new_json(
                session / "m0_result.json",
                {"session_id": session_id, "verdict": "PASS", "findings": []},
            )
            write_new_json(
                session / "driver_commands.json",
                {
                    "commands": [],
                    "authority_evidence_sha256": semantic_sha256(authority),
                },
            )
            retained = session / "authority/approval.json"
            retained.chmod(0o600)
            retained.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(M0Error):
                seal_terminal_session(session, session_id, "PASS")
            self.assertFalse((session / "session_status.txt").exists())
            self.assertFalse((session / "evidence_ledger.json").exists())

    def test_materialization_terminal_revalidates_retained_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "materialization-authority"
            root.mkdir()
            authority = _install_authority(root)
            retained = root / "authority/approval.json"
            retained.chmod(0o600)
            retained.write_text("{}\n", encoding="utf-8")
            failure = {
                "schema_version":
                    "moe-simulator-phase7-materialization-stage-failure-v1",
                "status": "FAIL_OR_INCOMPLETE_IMMUTABLE",
                "failure": "adversarial retained-authority mutation",
                "completed_commands": [],
                "authority_evidence_sha256": semantic_sha256(authority),
                "authority_validation_errors": [],
                "gpu_workload_performed": False,
                "retry_allowed": False,
                "resume_allowed": False,
            }
            try:
                with self.assertRaisesRegex(
                    M0Error, "materialization failure evidence sealing failed"
                ):
                    seal_materialization_failure(
                        root=root,
                        snapshot=Path(directory) / "absent-snapshot",
                        failure=failure,
                    )
                self.assertFalse((root / "evidence_ledger.json").exists())
                self.assertTrue((root / "sealing_failure.json").is_file())
            finally:
                _restore_tree(root)

    def test_materialization_seal_error_is_explicit_and_not_ledgered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "materialization-failure"
            root.mkdir()
            failure = {
                "schema_version":
                    "moe-simulator-phase7-materialization-stage-failure-v1",
                "status": "FAIL_OR_INCOMPLETE_IMMUTABLE",
                "failure": "fixture",
                "completed_commands": [],
                "gpu_workload_performed": False,
                "retry_allowed": False,
                "resume_allowed": False,
            }
            with patch(
                "explorations.moe_cycle_simulator.phase7.application."
                "executor.materialization_driver.seal_tree",
                side_effect=M0Error("forced seal error"),
            ):
                with self.assertRaisesRegex(
                    M0Error, "materialization failure evidence sealing failed"
                ):
                    seal_materialization_failure(
                        root=root,
                        snapshot=Path(directory) / "snapshot",
                        failure=failure,
                    )
            self.assertTrue((root / "stage_failure.json").is_file())
            self.assertTrue((root / "sealing_failure.json").is_file())
            self.assertFalse((root / "evidence_ledger.json").exists())
            record = load_json(root / "sealing_failure.json")
            self.assertEqual(record["status"], "SEALING_FAILED_NOT_IMMUTABLE")
            self.assertGreaterEqual(len(record["sealing_errors"]), 1)


if __name__ == "__main__":
    unittest.main()
