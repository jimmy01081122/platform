from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scheduler.g25_cgroup_v2 import (
    APPLICATION_UNIT,
    CgroupDrainError,
    CgroupUnavailable,
    CgroupV2Controller,
    attest_systemd_properties,
    build_systemd_run_argv,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.value)


class G25CgroupV2Tests(unittest.TestCase):
    def properties(self, control_group: str = "/fixture.service") -> dict[str, str]:
        return {
            "ActiveState": "active",
            "SubState": "running",
            "Delegate": "yes",
            "KillMode": "control-group",
            "KillSignal": "15",
            "FinalKillSignal": "9",
            "SendSIGKILL": "yes",
            "TimeoutStopUSec": "30s",
            "RuntimeMaxUSec": "2h 5min",
            "OOMPolicy": "kill",
            "Restart": "no",
            "TasksMax": "512",
            "ControlGroup": control_group,
        }

    def fixture_controller(self, root: Path, clock: FakeClock, *, signals=None):
        root.mkdir()
        (root / "cgroup.type").write_text("domain\n", encoding="utf-8")
        (root / "cgroup.kill").write_text("", encoding="utf-8")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

        def initialize(path: Path) -> None:
            (path / "cgroup.procs").write_text("", encoding="utf-8")
            (path / "cgroup.events").write_text(
                "populated 0\nfrozen 0\n", encoding="utf-8"
            )
            (path / "cgroup.kill").write_text("", encoding="utf-8")

        def finalize(path: Path) -> None:
            for name in ("cgroup.procs", "cgroup.events", "cgroup.kill"):
                (path / name).unlink()

        state = {"path": "/fixture.service"}
        controller = CgroupV2Controller(
            mountpoint=root.parent,
            relative_path="/fixture.service",
            unit=APPLICATION_UNIT,
            root_fd=root_fd,
            systemd_properties=self.properties(),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            signal_sender=lambda pid, sig: (signals if signals is not None else []).append(
                (pid, sig)
            ),
            process_start_ticks=lambda _pid: 777,
            process_cgroup_reader=lambda _pid: f"0::{state['path']}\n",
            cell_initializer=initialize,
            cell_finalizer=finalize,
        )
        return controller, state

    def test_exact_systemd_run_argv_is_fixed_and_has_no_scope_fallback(self) -> None:
        inner = ["/usr/bin/timeout", "7500s", "/bin/true"]
        argv = build_systemd_run_argv(inner)
        self.assertEqual("/usr/bin/systemd-run", argv[0])
        self.assertIn(f"--unit={APPLICATION_UNIT}", argv)
        self.assertIn("--property=Delegate=yes", argv)
        self.assertIn("--property=KillMode=control-group", argv)
        self.assertIn("--property=RuntimeMaxSec=7500s", argv)
        self.assertIn("--property=TimeoutStopSec=30s", argv)
        self.assertNotIn("--scope", argv)
        self.assertEqual(inner, argv[argv.index("--") + 1 :])

    def test_systemd_property_attestation_is_exact(self) -> None:
        value = self.properties()
        self.assertEqual(value, attest_systemd_properties(
            value, expected_cgroup="/fixture.service"
        ))
        for key in value:
            with self.subTest(key=key):
                mutated = dict(value)
                mutated[key] = "drift"
                with self.assertRaises(CgroupUnavailable):
                    attest_systemd_properties(
                        mutated, expected_cgroup="/fixture.service"
                    )

    def test_missing_user_bus_fails_before_systemctl_or_worker(self) -> None:
        with patch.object(Path, "stat", side_effect=FileNotFoundError), patch(
            "scheduler.g25_cgroup_v2.subprocess.run"
        ) as run:
            with self.assertRaisesRegex(CgroupUnavailable, "user bus"):
                CgroupV2Controller.discover_and_preflight()
        run.assert_not_called()

    def test_move_requires_exact_pid_and_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = FakeClock()
            root = Path(temporary) / "fixture.service"
            controller, state = self.fixture_controller(root, clock)
            cell = controller.prepare_cell("a" * 64)
            state["path"] = cell.relative_path
            evidence = controller.move_and_verify(
                cell, pid=1234, expected_start_ticks=777
            )
            self.assertTrue(evidence["move_observed"])
            self.assertEqual([1234], controller.pids(cell))
            with self.assertRaisesRegex(Exception, "identity changed"):
                controller._process_start_ticks = lambda _pid: 778
                controller.move_and_verify(cell, pid=1234, expected_start_ticks=777)
            (cell.path / "cgroup.events").write_text(
                "populated 0\nfrozen 0\n", encoding="utf-8"
            )
            cell.populated_zero_observed = True
            controller.close_cell(cell)
            controller.close()

    def test_term_waits_full_thirty_seconds_then_uses_recursive_kill(self) -> None:
        signals = []
        with tempfile.TemporaryDirectory() as temporary:
            clock = FakeClock()
            root = Path(temporary) / "fixture.service"
            controller, state = self.fixture_controller(root, clock, signals=signals)
            cell = controller.prepare_cell("b" * 64)
            state["path"] = cell.relative_path
            controller.move_and_verify(cell, pid=2222, expected_start_ticks=777)
            (cell.path / "cgroup.events").write_text(
                "populated 1\nfrozen 0\n", encoding="utf-8"
            )

            original_write_kill = controller._write_kill

            def write_kill(target):
                self.assertGreaterEqual(clock.value, 30.0)
                result = original_write_kill(target)
                (target.path / "cgroup.events").write_text(
                    "populated 0\nfrozen 0\n", encoding="utf-8"
                )
                return result

            controller._write_kill = write_kill
            evidence = controller.terminate_and_drain(cell, graceful=True)
            self.assertEqual([(2222, 15)], signals)
            self.assertTrue(evidence.cgroup_kill_written)
            self.assertEqual(30.0, clock.value)
            controller.close_cell(cell)
            controller.close()

    def test_graceful_zero_avoids_kill_but_normal_exit_always_kills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = FakeClock()
            root = Path(temporary) / "fixture.service"
            controller, _state = self.fixture_controller(root, clock)
            first = controller.prepare_cell("c" * 64)
            graceful = controller.terminate_and_drain(first, graceful=True)
            self.assertFalse(graceful.cgroup_kill_written)
            controller.close_cell(first)

            second = controller.prepare_cell("d" * 64)
            normal = controller.finalize_normal_exit(second)
            self.assertTrue(normal.cgroup_kill_written)
            controller.close_cell(second)
            controller.close()

    def test_kill_without_populated_zero_never_releases_cell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = FakeClock()
            root = Path(temporary) / "fixture.service"
            controller, _state = self.fixture_controller(root, clock)
            cell = controller.prepare_cell("e" * 64)
            (cell.path / "cgroup.events").write_text(
                "populated 1\nfrozen 0\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(CgroupDrainError, "remained populated"):
                controller.emergency_kill(cell)
            self.assertFalse(cell.populated_zero_observed)
            with self.assertRaises(CgroupDrainError):
                controller.close_cell(cell)
            for descriptor in (cell.kill_fd, cell.events_fd, cell.procs_fd, cell.dir_fd):
                os.close(descriptor)
            os.remove(cell.path / "cgroup.procs")
            os.remove(cell.path / "cgroup.events")
            os.remove(cell.path / "cgroup.kill")
            os.rmdir(cell.path)
            controller._cells.clear()
            controller.close()


if __name__ == "__main__":
    unittest.main()
