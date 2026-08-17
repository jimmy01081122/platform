from __future__ import annotations

import json
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import c1_cleanroom_verify
from c1_canonicalize import canonicalize
from c1_cleanroom_verify import verify
from c1_system_ir import build_system_ir


def routing_row() -> dict:
    return {
        "schema_version": "c1-routing-event-v1",
        "event_key": "b" * 64, "execution_alignment_key": "a" * 64,
        "request_id": "r", "phase": "decode", "generation_step": 0,
        "token_index": 0, "layer_id": 0, "router_module": "router",
        "dispatch_index": 0, "selected_experts": list(range(8)), "top_k": 8,
        "actual_dispatch": True, "router_logits": None, "routing_weights": None,
        "gate_dtype": "unknown",
        "unavailable_reasons": {
            "router_logits": "not exposed", "routing_weights": "not exposed"
        },
    }


def write_inventory(package: Path) -> None:
    files = []
    for path in sorted(package.rglob("*")):
        if not path.is_file() or path.name == "PACKAGE_INVENTORY.json":
            continue
        files.append({
            "path": path.relative_to(package).as_posix(),
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        })
    (package / "PACKAGE_INVENTORY.json").write_text(
        json.dumps({
            "schema_version": "c1-package-inventory-v1",
            "files": files,
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_package(package: Path) -> None:
    raw = package / "raw/P2/routing.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(routing_row()) + "\n", encoding="utf-8")
    canonical = canonicalize(raw, package / "canonical/routing.json")
    system = build_system_ir(
        package / "canonical/routing.json", package / "canonical/system.json"
    )
    (package / "summary.json").write_text(
        json.dumps({
            "schema_version": "c1-rebuilt-summary-v1",
            "routing_event_count": len(canonical["events"]),
            "system_event_count": len(system["events"]),
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_inventory(package)


class C1CleanroomTests(unittest.TestCase):
    def test_rebuilds_summary_and_rejects_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "source"
            build_package(package)
            code, report = verify(package, base / "clean")
            self.assertEqual(0, code)
            self.assertEqual(1, report["summary"]["routing_event_count"])
            self.assertTrue(all(
                item["byte_equal"] and item["deterministic"]
                for item in report["derivative_comparisons"]
            ))

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "source"
            build_package(package)
            (package / "leak.txt").write_text(
                "api_key=abcdefghijklmnop\n", encoding="utf-8"
            )
            write_inventory(package)
            code, report = verify(package, base / "clean")
            self.assertEqual(1, code)
            self.assertTrue(any(item["kind"] == "secret" for item in report["scan_findings"]))

    def test_rejects_untracked_file_and_absolute_snapshot_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "source"
            build_package(package)
            (package / "untracked.txt").write_text("injected\n", encoding="utf-8")
            code, report = verify(package, base / "clean-untracked")
            self.assertEqual(1, code)
            self.assertTrue(any(
                "untracked files" in error for error in report["inventory_errors"]
            ))

            (package / "untracked.txt").unlink()
            (package / "session_manifest.json").write_text(
                json.dumps({"model_snapshot_path": "/home/user/private/snapshot"}),
                encoding="utf-8",
            )
            write_inventory(package)
            code, report = verify(package, base / "clean-absolute")
            self.assertEqual(1, code)
            self.assertTrue(any(
                item["kind"] == "absolute_path"
                for item in report["scan_findings"]
            ))

    def test_package_inventory_cannot_hide_work_unit_extra_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "source"
            build_package(package)
            unit = package / "complete/unit"
            unit.mkdir(parents=True)
            tracked = unit / "tracked.json"
            tracked.write_text("{}\n", encoding="utf-8")
            entry = {
                "path": "tracked.json",
                "sha256": sha256(tracked.read_bytes()).hexdigest(),
                "bytes": tracked.stat().st_size,
            }
            (unit / "WORK_UNIT_MANIFEST.json").write_text(
                json.dumps({"files": [entry]}), encoding="utf-8"
            )
            (unit / "checksums.sha256").write_text(
                f"{entry['sha256']}  tracked.json\n", encoding="utf-8"
            )
            (unit / "injected.txt").write_text("globally listed\n", encoding="utf-8")
            write_inventory(package)
            code, report = verify(package, base / "clean")
            self.assertEqual(1, code)
            self.assertTrue(any(
                "WORK_UNIT_MANIFEST.json contains untracked files: injected.txt"
                in error
                for error in report["inventory_errors"]
            ))

    def test_rejects_derivative_mismatch_and_nondeterminism(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "source"
            build_package(package)
            summary = json.loads((package / "summary.json").read_text())
            summary["routing_event_count"] = 99
            (package / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            write_inventory(package)
            code, report = verify(package, base / "clean-mismatch")
            self.assertEqual(1, code)
            self.assertIn(
                "rebuilt derivative mismatch: summary.json",
                report["rebuild_errors"],
            )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            package = base / "source"
            build_package(package)
            original = c1_cleanroom_verify._rebuild
            calls = 0

            def unstable(raw_bytes, destination):
                nonlocal calls
                result = original(raw_bytes, destination)
                calls += 1
                with (destination / "summary.json").open("a", encoding="utf-8") as stream:
                    stream.write(f"{calls}\n")
                return result

            with patch.object(c1_cleanroom_verify, "_rebuild", side_effect=unstable):
                code, report = verify(package, base / "clean-nondeterministic")
            self.assertEqual(1, code)
            self.assertIn(
                "nondeterministic rebuild output: summary.json",
                report["rebuild_errors"],
            )


if __name__ == "__main__":
    unittest.main()
