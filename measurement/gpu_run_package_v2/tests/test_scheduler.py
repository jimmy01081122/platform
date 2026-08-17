from __future__ import annotations

import errno
import json
import sys
import tempfile
import unittest
from pathlib import Path

from scheduler import (
    FakeClock,
    FaultInjector,
    InjectedCrash,
    SchedulerEngine,
    SchedulerStore,
    State,
    TimeBudget,
    WorkUnit,
    expand_work_units,
)
from scheduler.validators import verify_complete


FAKE = Path(__file__).parent / "fault_injection/fake_collector.py"


def successful_collector(unit: WorkUnit, output: Path) -> int:
    (output / "raw.json").write_text(
        json.dumps(unit.identity) + "\n", encoding="utf-8"
    )
    (output / "COLLECTOR_RESULT.json").write_text(json.dumps({
        "status": "success",
        "schema_valid": True,
        "raw_files": ["raw.json"],
        "work_unit_id": unit.work_unit_id,
    }) + "\n", encoding="utf-8")
    return 0


class SchedulerTests(unittest.TestCase):
    def make(self, root: Path, collector=successful_collector, **kwargs):
        store = SchedulerStore(root)
        engine = SchedulerEngine(store, lambda _unit: collector, **kwargs)
        return store, engine

    def test_deterministic_identity_and_p5_basic_logical_id(self) -> None:
        first = WorkUnit("model", "sample", 2, "P5_BASIC")
        second = WorkUnit("model", "sample", 2, "P5_BASIC", {"ignored": True})
        self.assertEqual(first.work_unit_id, second.work_unit_id)
        self.assertEqual("P5_BASIC", first.as_dict()["logical_pass"])
        self.assertEqual(64, len(first.work_unit_id))

    def test_expansion_is_model_sample_repetition_pass(self) -> None:
        units = expand_work_units(
            ["m1", "m2"], ["s1", "s2"], 3, ["P0", "P5_BASIC"]
        )
        self.assertEqual(24, len(units))
        self.assertEqual(24, len({unit.work_unit_id for unit in units}))

    def test_success_is_atomic_immutable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []

            def collector(unit, output):
                calls.append(unit.work_unit_id)
                return successful_collector(unit, output)

            store, engine = self.make(root, collector)
            unit = WorkUnit("m", "s", 0, "P0")
            self.assertEqual(State.COMPLETE, engine.run_unit(unit))
            self.assertFalse((store.tmp_dir / unit.work_unit_id).exists())
            complete = store.complete_dir / unit.work_unit_id
            self.assertTrue(complete.is_dir())
            self.assertEqual([], verify_complete(complete, unit))
            self.assertEqual(State.COMPLETE, engine.run_unit(unit))
            self.assertEqual(1, len(calls))
            self.assertEqual(0o444, (complete / "raw.json").stat().st_mode & 0o777)
            events = [
                json.loads(line)
                for line in store.journal_path.read_text().splitlines()
            ]
            self.assertIn("TRANSITION", {event["event"] for event in events})

    def test_exclusive_tmp_preserves_abandoned_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SchedulerStore(Path(temporary))
            unit = WorkUnit("m", "s", 0, "P0")
            first = store.prepare_tmp(unit, 1)
            (first / "partial").write_text("evidence")
            second = store.prepare_tmp(unit, 2)
            self.assertTrue(second.is_dir())
            abandoned = list(store.abandoned_dir.iterdir())
            self.assertEqual(1, len(abandoned))
            self.assertEqual("evidence", (abandoned[0] / "partial").read_text())

    def test_rename_before_state_crash_reconciles_without_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, engine = self.make(
                Path(temporary),
                faults=FaultInjector({
                    "after_rename_before_state": InjectedCrash("power loss")
                }),
            )
            unit = WorkUnit("m", "s", 0, "P2")
            with self.assertRaises(InjectedCrash):
                engine.run_unit(unit)
            self.assertEqual(State.VALIDATING.value, store.load(unit)["state"])
            self.assertEqual(
                {"recovered": 1, "corrupt": 0}, store.reconcile()
            )
            self.assertEqual(State.COMPLETE.value, store.load(unit)["state"])

    def test_second_repetition_interruption_resumes_only_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, engine = self.make(
                Path(temporary),
                faults=FaultInjector({
                    "before_collector": InjectedCrash("simulated SIGTERM")
                }),
            )
            complete = WorkUnit("m", "s", 0, "P0")
            interrupted = WorkUnit("m", "s", 1, "P0")
            engine.faults = FaultInjector()
            self.assertEqual(State.COMPLETE, engine.run_unit(complete))
            engine.faults = FaultInjector({
                "before_collector": InjectedCrash("simulated SIGTERM")
            })
            with self.assertRaises(InjectedCrash):
                engine.run_unit(interrupted)
            engine.faults = FaultInjector()
            engine.run_pending([complete, interrupted])
            self.assertEqual(1, store.load(complete)["attempts"])
            self.assertEqual(2, store.load(interrupted)["attempts"])

    def test_fake_collector_nonzero_sigterm_and_partial_raw_retry(self) -> None:
        for mode in ("nonzero", "sigterm", "partial"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                command = [sys.executable, str(FAKE), "--mode", mode]
                store, engine = self.make(Path(temporary), command)
                unit = WorkUnit("m", "s", 0, "P2")
                self.assertEqual(State.FAILED_RETRYABLE, engine.run_unit(unit))
                self.assertFalse(
                    (store.complete_dir / unit.work_unit_id).exists()
                )

    def test_unavailable_is_explicit_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SchedulerStore(Path(temporary))
            engine = SchedulerEngine(store, lambda _unit: None)
            unit = WorkUnit("m", "s", 0, "P3")
            self.assertEqual(State.UNAVAILABLE, engine.run_unit(unit))
            self.assertIn("unavailable", store.load(unit)["reason"])

    def test_enospc_is_retryable_and_never_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, engine = self.make(
                Path(temporary),
                faults=FaultInjector({
                    "before_rename": OSError(errno.ENOSPC, "injected")
                }),
            )
            unit = WorkUnit("m", "s", 0, "P3")
            self.assertEqual(State.FAILED_RETRYABLE, engine.run_unit(unit))
            self.assertFalse(
                (store.complete_dir / unit.work_unit_id).exists()
            )
            temporary_manifest = (
                store.tmp_dir / unit.work_unit_id / "WORK_UNIT_MANIFEST.json"
            )
            self.assertNotEqual(
                "COMPLETE",
                json.loads(temporary_manifest.read_text()).get("status"),
            )

    def test_checksum_mismatch_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, engine = self.make(Path(temporary))
            unit = WorkUnit("m", "s", 0, "P0")
            engine.run_unit(unit)
            raw = store.complete_dir / unit.work_unit_id / "raw.json"
            raw.chmod(0o644)
            raw.write_text("tampered\n")
            self.assertTrue(any(
                "checksum mismatch" in error
                for error in verify_complete(raw.parent, unit)
            ))

    def test_max_attempts_becomes_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = [sys.executable, str(FAKE), "--mode", "nonzero"]
            store, engine = self.make(
                Path(temporary), command, max_attempts=2
            )
            unit = WorkUnit("m", "s", 0, "P0")
            self.assertEqual(State.FAILED_RETRYABLE, engine.run_unit(unit))
            self.assertEqual(State.FAILED_TERMINAL, engine.run_unit(unit))
            self.assertEqual(2, store.load(unit)["attempts"])

    def test_fake_clock_enforces_105_15_budget_without_sleep(self) -> None:
        clock = FakeClock()
        budget = TimeBudget(
            session_minutes=120,
            stop_dispatch_before_end_minutes=15,
            packaging_reserve_minutes=15,
        )
        self.assertEqual(6300, budget.dispatch_deadline_seconds)
        self.assertTrue(budget.can_dispatch(6299.999))
        self.assertFalse(budget.can_dispatch(6300))
        with tempfile.TemporaryDirectory() as temporary:
            store, engine = self.make(
                Path(temporary), clock=clock, budget=budget
            )
            unit = WorkUnit("m", "s", 0, "P0")
            clock.advance(105 * 60)
            result = engine.run_pending([unit])
            self.assertTrue(result["budget_exhausted"])
            self.assertEqual(State.PENDING.value, store.load(unit)["state"])

    def test_persistent_execution_deadline_stops_dispatch_after_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, engine = self.make(
                Path(temporary),
                execution_deadline_epoch=100.0,
                session_deadline_epoch=1000.0,
                wall_time=lambda: 100.0,
            )
            unit = WorkUnit("m", "s", 0, "P0")
            result = engine.run_pending([unit])
            self.assertTrue(result["budget_exhausted"])
            self.assertEqual(State.PENDING.value, store.load(unit)["state"])

    def test_subprocess_timeout_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = [sys.executable, "-c", "import time; time.sleep(60)"]
            store, engine = self.make(
                Path(temporary), command, collector_timeout_seconds=0.05
            )
            unit = WorkUnit("m", "s", 0, "P0")
            self.assertEqual(State.FAILED_RETRYABLE, engine.run_unit(unit))
            self.assertIn("timeout", store.load(unit)["reason"])

    def test_oom_is_terminal_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = [
                sys.executable, "-c",
                "import sys; print('CUDA out of memory', file=sys.stderr); sys.exit(1)",
            ]
            store, engine = self.make(Path(temporary), command)
            unit = WorkUnit("m", "s", 0, "P2")
            self.assertEqual(State.FAILED_TERMINAL, engine.run_unit(unit))
            self.assertEqual(1, store.load(unit)["attempts"])

    def test_run_pending_fail_fast_stops_after_first_noncomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = [sys.executable, str(FAKE), "--mode", "nonzero"]
            store, engine = self.make(Path(temporary), command)
            units = [
                WorkUnit("m", "s0", 0, "P0"),
                WorkUnit("m", "s1", 0, "P0"),
            ]
            result = engine.run_pending(units)
            self.assertTrue(result["fail_fast"])
            self.assertEqual(1, result["dispatched"])
            self.assertEqual(State.PENDING.value, store.load(units[1])["state"])


if __name__ == "__main__":
    unittest.main()
