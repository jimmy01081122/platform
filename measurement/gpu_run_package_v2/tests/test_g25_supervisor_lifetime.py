from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scheduler.execution_lock import ExecutionLockBusy, execution_lock
from scheduler.g25_worker_lifetime import (
    WorkerLifetimeError,
    process_group_members,
    read_process_start_ticks,
)
from scripts.g25_qualification import invoke_worker_process


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _wait_for_file(path: Path, timeout_seconds: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _process_dead(pid: int, expected_start_ticks: int) -> bool:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return True
    closing = value.rfind(")")
    fields = value[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19:
        return True
    return fields[0] == "Z" or int(fields[19]) != expected_start_ticks


class G25SupervisorLifetimeTests(unittest.TestCase):
    def test_supervisor_sigkill_kills_worker_before_lease_can_be_reacquired(self) -> None:
        supervisor_code = r'''
import os
import sys
from pathlib import Path
from scheduler.execution_lock import execution_lock
from scripts.g25_qualification import invoke_worker_process

run_root = Path(sys.argv[1])
worker_marker = Path(sys.argv[2])
supervisor_marker = Path(sys.argv[3])
child_code = r"""
import os
import sys
import time
from pathlib import Path
from scheduler.g25_worker_lifetime import install_parent_death_guard_from_environment, read_process_start_ticks
install_parent_death_guard_from_environment()
Path(sys.argv[1]).write_text(f'{os.getpid()} {read_process_start_ticks(os.getpid())}', encoding='utf-8')
time.sleep(60)
"""
with execution_lock(run_root) as lease:
    supervisor_marker.write_text(str(os.getpid()), encoding="utf-8")
    invoke_worker_process(
        [sys.executable, "-c", child_code, str(worker_marker)],
        lease=lease,
        timeout_seconds=480,
    )
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runs"
            worker_marker = root / "worker.txt"
            supervisor_marker = root / "supervisor.txt"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
                str(PACKAGE_ROOT), environment.get("PYTHONPATH", "")
            )))
            supervisor = subprocess.Popen(
                [
                    sys.executable, "-c", supervisor_code,
                    str(run_root), str(worker_marker), str(supervisor_marker),
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            worker_pid = 0
            worker_start = 0
            try:
                self.assertEqual(supervisor.pid, int(_wait_for_file(supervisor_marker)))
                worker_pid_text, worker_start_text = _wait_for_file(worker_marker).split()
                worker_pid = int(worker_pid_text)
                worker_start = int(worker_start_text)
                self.assertFalse(_process_dead(worker_pid, worker_start))
                with self.assertRaises(ExecutionLockBusy):
                    with execution_lock(run_root):
                        pass

                os.kill(supervisor.pid, signal.SIGKILL)
                supervisor.communicate(timeout=10)
                deadline = time.monotonic() + 10
                while not _process_dead(worker_pid, worker_start):
                    if time.monotonic() >= deadline:
                        self.fail("PDEATHSIG did not terminate the worker after supervisor SIGKILL")
                    time.sleep(0.01)
                with execution_lock(run_root) as recovered:
                    recovered.assert_active()
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.communicate(timeout=10)
                if worker_pid and worker_start and not _process_dead(
                    worker_pid, worker_start
                ):
                    try:
                        os.killpg(worker_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_residual_same_group_descendant_is_killed_and_hard_stops(self) -> None:
        child_code = r'''
import os
import subprocess
import sys
from pathlib import Path
from scheduler.g25_worker_lifetime import install_parent_death_guard_from_environment
install_parent_death_guard_from_environment()
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "descendant.txt"
            with execution_lock(root / "runs") as lease:
                with self.assertRaisesRegex(
                    WorkerLifetimeError, "retained descendants"
                ):
                    invoke_worker_process(
                        [sys.executable, "-c", child_code, str(marker)],
                        lease=lease,
                        timeout_seconds=480,
                    )
            leader_pid, _descendant_pid = map(int, _wait_for_file(marker).split())
            self.assertEqual([], process_group_members(leader_pid))

    def test_worker_bootstrap_rejects_missing_lifetime_guard_before_runtime(self) -> None:
        bootstrap = PACKAGE_ROOT / "scripts/g25_isolated_bootstrap.py"
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(bootstrap), "worker"],
            env={"PATH": "/usr/bin:/bin"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("worker lifetime guard environment is incomplete", result.stderr)


if __name__ == "__main__":
    unittest.main()
