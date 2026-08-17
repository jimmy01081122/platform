from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scheduler.g25_deadlines import G25DeadlineTracker
from scheduler.g25_snapshot import audit_package_snapshot, freeze_package_snapshot


class G25DeadlineSnapshotTests(unittest.TestCase):
    def test_all_four_deadline_boundaries_are_exact(self):
        now = [0.0]
        tracker = G25DeadlineTracker(lambda: now[0])
        cases = [
            (5789.999, True, "DISPATCH_ALLOWED"),
            (5790.0, False, "NO_NEW_DISPATCH_TERMINATE_ACTIVE_WORKER"),
            (6300.0, False, "FINALIZATION_AND_AUDIT_ONLY"),
            (7200.0, False, "INTERNAL_HARD_DEADLINE_EXPIRED"),
            (7500.0, False, "OUTER_TIMEOUT_EXPIRED"),
        ]
        for elapsed, dispatch, phase in cases:
            now[0] = elapsed
            self.assertEqual(dispatch, tracker.may_dispatch())
            self.assertEqual(phase, tracker.phase())

    def test_snapshot_is_hash_verified_and_tamper_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            session = root / "session"
            package.mkdir()
            session.mkdir()
            (package / "scripts").mkdir()
            source = package / "scripts" / "worker.py"
            source.write_text("print('ok')\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            (package / "checksums.txt").write_text(
                f"{digest}  scripts/worker.py\n", encoding="utf-8"
            )
            inventory = freeze_package_snapshot(package, session)
            self.assertEqual(1, inventory["file_count"])
            self.assertEqual([], audit_package_snapshot(session))
            copied = session / "snapshots/package/scripts/worker.py"
            copied.write_text("tampered\n", encoding="utf-8")
            self.assertEqual(
                ["snapshot file mismatch: scripts/worker.py"],
                audit_package_snapshot(session),
            )
            copied.unlink()
            self.assertTrue(any(
                "snapshot file missing: scripts/worker.py" in item
                for item in audit_package_snapshot(session)
            ))

    def test_snapshot_refuses_source_hash_drift_and_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            session = root / "session"
            package.mkdir()
            session.mkdir()
            (package / "a").write_text("x", encoding="utf-8")
            (package / "checksums.txt").write_text(
                f"{'0' * 64}  a\n", encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                freeze_package_snapshot(package, session)

    def test_snapshot_audit_rejects_inventory_rewrite_extra_and_source_ledger_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            session = root / "session"
            package.mkdir()
            session.mkdir()
            source = package / "worker.py"
            source.write_text("original\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            (package / "checksums.txt").write_text(
                f"{digest}  worker.py\n", encoding="utf-8"
            )
            freeze_package_snapshot(package, session)
            copied = session / "snapshots/package/worker.py"
            copied.write_text("mutated\n", encoding="utf-8")
            extra = session / "snapshots/package/extra.py"
            extra.write_text("extra\n", encoding="utf-8")
            inventory_path = session / "snapshots/inventory.json"
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            inventory["files"][0]["bytes"] = copied.stat().st_size
            inventory["files"][0]["sha256"] = hashlib.sha256(
                copied.read_bytes()
            ).hexdigest()
            inventory["inventory_sha256"] = hashlib.sha256(json.dumps(
                inventory["files"], sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            inventory["source_checksums_sha256"] = "0" * 64
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            findings = audit_package_snapshot(session)
            self.assertTrue(any("source checksum" in item for item in findings))
            self.assertTrue(any("extra" in item for item in findings))

    def test_snapshot_audit_rejects_duplicate_unsafe_and_inexact_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            session = root / "session"
            package.mkdir()
            session.mkdir()
            source = package / "worker.py"
            source.write_text("original\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            (package / "checksums.txt").write_text(
                f"{digest}  worker.py\n", encoding="utf-8"
            )
            freeze_package_snapshot(package, session)
            inventory_path = session / "snapshots/inventory.json"
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            inventory["files"].append(dict(inventory["files"][0]))
            inventory["files"].append({
                "path": "../escape", "bytes": 1, "sha256": "0" * 64,
            })
            inventory["file_count"] = len(inventory["files"])
            inventory["inventory_sha256"] = hashlib.sha256(json.dumps(
                inventory["files"], sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            findings = audit_package_snapshot(session)
            self.assertTrue(any("duplicated" in item for item in findings))
            self.assertTrue(any("unsafe" in item for item in findings))
            inventory["unexpected"] = True
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            self.assertEqual(
                ["snapshot inventory fields differ from the exact contract"],
                audit_package_snapshot(session),
            )


if __name__ == "__main__":
    unittest.main()
