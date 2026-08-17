"""Single-profiler, resumable scheduler execution engine."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .clock import Clock, SystemClock, TimeBudget
from .faults import (
    FaultInjector,
    CollectorTimeout,
    SchedulerError,
    TerminalCollectorFailure,
    ValidationFailure,
    classify_os_error,
)
from .model import WorkUnit
from .state_machine import State
from .store import SchedulerStore, atomic_json, fsync_directory
from .validators import (
    CHECKSUM_NAME,
    MANIFEST_NAME,
    build_manifest,
    render_checksums,
    validate_collector_output,
)

Collector = Callable[[WorkUnit, Path], int] | Sequence[str]
CollectorResolver = Callable[[WorkUnit], Collector | None]


class SchedulerEngine:
    def __init__(
        self,
        store: SchedulerStore,
        collector_resolver: CollectorResolver,
        *,
        clock: Clock | None = None,
        budget: TimeBudget | None = None,
        max_attempts: int = 3,
        faults: FaultInjector | None = None,
        collector_env: Mapping[str, str] | None = None,
        execution_deadline_epoch: float | None = None,
        session_deadline_epoch: float | None = None,
        collector_timeout_seconds: float = 8 * 60,
        wall_time: Callable[[], float] = time.time,
        execution_lease_fd: int | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.collector_resolver = collector_resolver
        self.clock = clock or SystemClock()
        self.budget = budget or TimeBudget()
        self.max_attempts = max_attempts
        self.faults = faults or FaultInjector()
        self.collector_env = dict(collector_env or {})
        self.started_at = self.clock.monotonic()
        self.execution_deadline_epoch = execution_deadline_epoch
        self.session_deadline_epoch = session_deadline_epoch
        self.collector_timeout_seconds = float(collector_timeout_seconds)
        self.wall_time = wall_time
        self.execution_lease_fd = execution_lease_fd

    def register(self, units: list[WorkUnit]) -> None:
        for unit in units:
            self.store.initialize(unit)

    def run_unit(self, unit: WorkUnit) -> State:
        self.store.initialize(unit)
        record = self.store.load(unit)
        state = State(record["state"])
        if state is State.COMPLETE:
            return state
        if state is State.FAILED_RETRYABLE:
            if record["attempts"] >= self.max_attempts:
                self.store.transition(
                    unit, State.FAILED_TERMINAL, reason="maximum attempts reached"
                )
                return State.FAILED_TERMINAL
            self.store.transition(unit, State.PENDING, reason="automatic retry")
        elif state is not State.PENDING:
            if state in (State.PREFLIGHT, State.RUNNING, State.RAW_SAVED, State.VALIDATING):
                self._force_interrupted_retry(unit, state)
                record = self.store.load(unit)
                if State(record["state"]) is State.FAILED_TERMINAL:
                    return State.FAILED_TERMINAL
                self.store.transition(unit, State.PENDING, reason="resume interrupted unit")
            else:
                return state

        if not self._can_dispatch():
            return State.PENDING

        collector = self.collector_resolver(unit)
        try:
            self.store.transition(
                unit, State.PREFLIGHT, increment_attempt=True, reason=None
            )
        except ValueError:
            # Another process won the locked PENDING -> PREFLIGHT claim.
            return State(self.store.load(unit)["state"])
        if collector is None:
            self.store.transition(
                unit, State.UNAVAILABLE,
                reason=f"collector unavailable for logical pass {unit.logical_pass}",
            )
            return State.UNAVAILABLE

        attempt = self.store.load(unit)["attempts"]
        output = self.store.prepare_tmp(unit, attempt)
        try:
            self.faults.trigger("before_collector")
            self.store.transition(unit, State.RUNNING)
            return_code = self._invoke(collector, unit, output)
            self.faults.trigger("after_collector")
            if return_code != 0:
                unavailable = self._unavailable_reason(output)
                if unavailable:
                    self.store.transition(unit, State.UNAVAILABLE, reason=unavailable)
                    return State.UNAVAILABLE
                failure = self._collector_failure(output, return_code)
                raise failure
            self.store.transition(unit, State.RAW_SAVED)
            self.faults.trigger("after_raw_saved")
            self.store.transition(unit, State.VALIDATING)
            result = validate_collector_output(
                output,
                unit,
                diagnostic_mode=self.collector_env.get("C1_DIAGNOSTIC_MODE"),
            )
            manifest = build_manifest(output, unit, result)
            atomic_json(output / MANIFEST_NAME, manifest)
            self._write_fsynced(
                output / CHECKSUM_NAME, render_checksums(manifest)
            )
            self.faults.trigger("before_rename")
            self.store.publish(unit)
            self.faults.trigger("after_rename_before_state")
            self.store.transition(unit, State.COMPLETE)
            self.store.make_complete_immutable(unit)
            return State.COMPLETE
        except OSError as exc:
            return self._record_failure(unit, classify_os_error(exc))
        except SchedulerError as exc:
            return self._record_failure(unit, exc)
        except Exception as exc:
            return self._record_failure(
                unit, ValidationFailure(f"{type(exc).__name__}: {exc}")
            )

    def run_pending(self, units: list[WorkUnit]) -> dict[str, int | bool | str]:
        self.register(units)
        dispatched = 0
        budget_exhausted = False
        for unit in units:
            before = State(self.store.load(unit)["state"])
            if before is State.COMPLETE:
                continue
            if not self._can_dispatch():
                budget_exhausted = True
                break
            state = self.run_unit(unit)
            dispatched += 1
            if state is not State.COMPLETE:
                return {
                    "dispatched": dispatched,
                    "budget_exhausted": budget_exhausted,
                    "fail_fast": True,
                    "failed_work_unit_id": unit.work_unit_id,
                    "failed_state": state.value,
                }
        return {
            "dispatched": dispatched,
            "budget_exhausted": budget_exhausted,
            "fail_fast": False,
        }

    def retry_failed(self) -> int:
        count = 0
        for record in self.store.records():
            if record["state"] != State.FAILED_RETRYABLE.value:
                continue
            unit = WorkUnit.from_dict(record["work_unit"])
            if record["attempts"] >= self.max_attempts:
                self.store.transition(
                    unit, State.FAILED_TERMINAL, reason="maximum attempts reached"
                )
            else:
                self.store.transition(unit, State.PENDING, reason="manual retry")
                count += 1
        return count

    def skip_completed(self, units: list[WorkUnit]) -> list[WorkUnit]:
        self.register(units)
        return [
            unit for unit in units
            if State(self.store.load(unit)["state"]) is not State.COMPLETE
        ]

    def _force_interrupted_retry(self, unit: WorkUnit, state: State) -> None:
        record = self.store.load(unit)
        target = (
            State.FAILED_TERMINAL
            if record["attempts"] >= self.max_attempts
            else State.FAILED_RETRYABLE
        )
        self.store.transition(
            unit, target, reason=f"interrupted while {state.value}"
        )

    def _record_failure(self, unit: WorkUnit, error: SchedulerError) -> State:
        record = self.store.load(unit)
        current = State(record["state"])
        if current in (
            State.COMPLETE, State.FAILED_TERMINAL, State.SKIPPED, State.UNAVAILABLE
        ):
            return current
        target = (
            State.FAILED_RETRYABLE
            if error.retryable and record["attempts"] < self.max_attempts
            else State.FAILED_TERMINAL
        )
        self.store.transition(unit, target, reason=str(error))
        return target

    def _invoke(self, collector: Collector, unit: WorkUnit, output: Path) -> int:
        if callable(collector):
            return int(collector(unit, output))
        environment = os.environ.copy()
        environment.update({
            "PROJECTCTL_WORK_UNIT_ID": unit.work_unit_id,
            "PROJECTCTL_MODEL_ID": unit.model_id,
            "PROJECTCTL_SAMPLE_ID": unit.sample_id,
            "PROJECTCTL_REPETITION": str(unit.repetition),
            "PROJECTCTL_LOGICAL_PASS": unit.logical_pass,
            "PROJECTCTL_OUTPUT_DIR": str(output),
            **self.collector_env,
        })
        timeout = self._unit_timeout_seconds()
        if timeout <= 0:
            raise CollectorTimeout("execution deadline reached before collector start")
        process = subprocess.Popen(
            [*collector, "--output-dir", str(output)],
            env=environment,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(
                (self.execution_lease_fd,)
                if self.execution_lease_fd is not None else ()
            ),
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            self._write_process_output(output, stdout, stderr)
            raise CollectorTimeout(
                f"collector exceeded {timeout:.3f}s timeout; process group terminated"
            )
        if process.returncode != 0:
            self._write_process_output(output, stdout, stderr)
        return int(process.returncode)

    def _can_dispatch(self) -> bool:
        if self.execution_deadline_epoch is not None:
            return self.wall_time() < self.execution_deadline_epoch
        elapsed = self.clock.monotonic() - self.started_at
        return self.budget.can_dispatch(elapsed)

    def _unit_timeout_seconds(self) -> float:
        timeout = self.collector_timeout_seconds
        now = self.wall_time()
        for deadline in (
            self.execution_deadline_epoch,
            self.session_deadline_epoch,
        ):
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - now))
        return timeout

    @staticmethod
    def _write_process_output(output: Path, stdout: str, stderr: str) -> None:
        for name, content in (
            ("scheduler_stdout.log", stdout),
            ("scheduler_stderr.log", stderr),
        ):
            path = output / name
            with path.open("w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

    @staticmethod
    def _collector_failure(output: Path, return_code: int) -> SchedulerError:
        evidence = []
        for path in output.glob("*.log"):
            try:
                evidence.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        rendered = "\n".join(evidence).lower()
        terminal_markers = (
            "out of memory",
            "cuda error: an illegal memory access",
            "cuda illegal",
            "gpu has fallen off the bus",
            "gpu lost",
            "xid 79",
        )
        reason = f"collector returned nonzero exit status {return_code}"
        if any(marker in rendered for marker in terminal_markers):
            return TerminalCollectorFailure(reason + "; terminal GPU failure detected")
        return ValidationFailure(reason)

    @staticmethod
    def _unavailable_reason(output: Path) -> str | None:
        path = output / "COLLECTOR_RESULT.json"
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if result.get("status") != "unavailable":
            return None
        reason = result.get("unavailable_reason")
        return reason if isinstance(reason, str) and reason else "collector unavailable"

    @staticmethod
    def _write_fsynced(path: Path, content: str) -> None:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(path.parent)
