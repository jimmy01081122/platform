from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scheduler.g25_session import (
    G25SessionStore,
    audit_partial_session,
    canonical_hash,
)


CELL_IDS = [f"cell-{index:02d}" for index in range(12)]


class FakeClocks:
    def __init__(self) -> None:
        self.epoch = 1000.0
        self.elapsed = 50.0

    def clock(self) -> float:
        return self.epoch

    def monotonic(self) -> float:
        return self.elapsed


class G25SessionStorageTests(unittest.TestCase):
    def create(self, root: Path, *, fault=None) -> tuple[G25SessionStore, FakeClocks]:
        clocks = FakeClocks()
        store = G25SessionStore.create(
            root,
            "fresh-session",
            CELL_IDS,
            clock=clocks.clock,
            monotonic=clocks.monotonic,
            fault=fault,
        )
        return store, clocks

    def test_session_reservation_is_exclusive_and_exactly_twelve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.create(root)
            with self.assertRaises(FileExistsError):
                G25SessionStore.create(root, "fresh-session", CELL_IDS)
            with self.assertRaises(ValueError):
                G25SessionStore.create(root, "short", CELL_IDS[:-1])

    def test_journal_is_hash_chained_and_uses_injected_clocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, clocks = self.create(Path(temporary))
            clocks.epoch = 1001.5
            clocks.elapsed = 51.25
            store.transition_session("PREFLIGHTING")
            rows = [
                json.loads(line)
                for line in store.journal_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([0, 1], [row["sequence"] for row in rows])
            self.assertEqual(rows[0]["event_sha256"], rows[1]["previous_event_sha256"])
            body = {key: value for key, value in rows[1].items() if key != "event_sha256"}
            self.assertEqual(canonical_hash(body), rows[1]["event_sha256"])
            self.assertEqual(1.25, rows[1]["elapsed_seconds"])

    def test_partial_audit_lists_missing_cells_and_never_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _clocks = self.create(Path(temporary))
            store.transition_session("PREFLIGHTING")
            report = audit_partial_session(store.root)
            self.assertEqual("PARTIAL_AUDIT", report["status"])
            self.assertEqual(0, report["recorded_cell_count"])
            self.assertEqual(CELL_IDS, report["missing_or_incomplete_cell_ids"])
            self.assertFalse(report["ledger_eligible"])
            self.assertFalse(report["qualification_pass"])

    def test_recorded_raw_is_bound_and_tampering_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _clocks = self.create(Path(temporary))
            cell = CELL_IDS[0]
            for state in ("DISPATCHED", "RUNNING", "PROCESS_EXITED"):
                store.transition_cell(cell, state)
            descriptor = store.write_raw(cell, {"worker": "evidence"})
            store.transition_cell(cell, "CLASSIFIED", classification="QUALIFIED")
            store.transition_cell(cell, "RECORDED")
            report = audit_partial_session(store.root)
            self.assertEqual(1, report["recorded_cell_count"])
            self.assertEqual(0, report["finding_count"])
            (store.root / descriptor["path"]).write_text("{}\n", encoding="utf-8")
            report = audit_partial_session(store.root)
            self.assertTrue(any("raw" in item for item in report["findings"]))
            self.assertFalse(report["ledger_eligible"])

    def test_raw_and_materialized_descriptor_cannot_be_rewritten_around_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _clocks = self.create(Path(temporary))
            cell = CELL_IDS[0]
            for state in ("DISPATCHED", "RUNNING", "PROCESS_EXITED"):
                store.transition_cell(cell, state)
            descriptor = store.write_raw(cell, {"worker": "original"})
            store.transition_cell(cell, "CLASSIFIED", classification="QUALIFIED")
            store.transition_cell(cell, "RECORDED")
            raw = store.root / descriptor["path"]
            raw.write_text('{"worker":"mutated"}\n', encoding="utf-8")
            materialized = store.cell_state(cell)
            materialized["raw_descriptor"] = {
                "path": descriptor["path"],
                "bytes": raw.stat().st_size,
                "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            }
            store._write_state(store._cell_path(cell), materialized)
            report = audit_partial_session(store.root)
            self.assertTrue(any(
                "materialized raw descriptor differs from journal" in item
                for item in report["findings"]
            ))
            self.assertTrue(any("raw" in item for item in report["findings"]))

    def test_complete_twelve_is_only_ledger_eligible_and_still_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _clocks = self.create(Path(temporary))
            for index, cell in enumerate(CELL_IDS):
                for state in ("DISPATCHED", "RUNNING", "PROCESS_EXITED"):
                    store.transition_cell(cell, state)
                store.write_raw(cell, {"cell": index})
                classification = "QUALIFIED" if index else "TRUNCATED"
                store.transition_cell(cell, "CLASSIFIED", classification=classification)
                store.transition_cell(cell, "RECORDED")
            report = audit_partial_session(store.root)
            self.assertEqual("COMPLETE_SHAPE_AUDITED", report["status"])
            self.assertTrue(report["ledger_eligible"])
            self.assertFalse(report["qualification_pass"])

    def test_fault_after_journal_fsync_leaves_detectable_partial_state(self) -> None:
        armed = {"value": False}

        def fault(point: str) -> None:
            if armed["value"] and point == "after_journal_fsync":
                raise RuntimeError("injected crash")

        with tempfile.TemporaryDirectory() as temporary:
            store, _clocks = self.create(Path(temporary), fault=fault)
            armed["value"] = True
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                store.transition_cell(CELL_IDS[0], "DISPATCHED")
            self.assertEqual("PENDING", store.cell_state(CELL_IDS[0])["state"])
            report = audit_partial_session(store.root)
            self.assertEqual("PARTIAL_AUDIT", report["status"])
            self.assertFalse(report["qualification_pass"])
            self.assertTrue(any("materialized cell" in item for item in report["findings"]))

    def test_journal_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _clocks = self.create(Path(temporary))
            store.transition_session("PREFLIGHTING")
            rows = store.journal_path.read_text(encoding="utf-8").splitlines()
            event = json.loads(rows[0])
            event["event_type"] = "TAMPERED"
            rows[0] = json.dumps(event, sort_keys=True, separators=(",", ":"))
            store.journal_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            report = audit_partial_session(store.root)
            self.assertGreater(report["finding_count"], 0)
            self.assertFalse(report["ledger_eligible"])

    def test_invalid_transitions_and_raw_overwrite_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _clocks = self.create(Path(temporary))
            with self.assertRaises(ValueError):
                store.transition_cell(CELL_IDS[0], "RUNNING")
            with self.assertRaises(ValueError):
                store.write_raw(CELL_IDS[0], {})
            with self.assertRaises(ValueError):
                store.transition_session("READY")

    def test_mark_unfinished_preserves_recorded_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _clocks = self.create(Path(temporary))
            cell = CELL_IDS[0]
            for state in ("DISPATCHED", "RUNNING", "PROCESS_EXITED"):
                store.transition_cell(cell, state)
            store.write_raw(cell, {"cell": 0})
            store.transition_cell(cell, "CLASSIFIED", classification="TIMEOUT")
            store.transition_cell(cell, "RECORDED")
            changed = store.mark_unfinished_cells("deadline")
            self.assertNotIn(cell, changed)
            self.assertEqual("RECORDED", store.cell_state(cell)["state"])
            self.assertEqual(11, len(changed))


if __name__ == "__main__":
    unittest.main()
