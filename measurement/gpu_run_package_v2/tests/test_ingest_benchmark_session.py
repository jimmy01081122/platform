from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.ingest_benchmark_session import load_measured
from scripts.trace_package_verify import COMPLETE, FAILED, verify_root
from tests.fixture_factory import build_positive, dump, refresh_checksums


def apply_standard_profile(root: Path) -> None:
    session_path = root / "SESSION_MANIFEST.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    required = ["P0", "P1", "P2", "P3", "P5"]
    optional = ["P4", "P6"]
    session["capture_profile"] = "standard"
    session["required_passes"] = required
    session["conditional_optional_passes"] = optional
    session["expected_runs"][0]["required_passes"] = required
    session["expected_runs"][0]["conditional_optional_passes"] = optional
    dump(session_path, session)


def make_p4_unsupported(root: Path) -> None:
    manifest_path = next(
        (root / "runs/fixture-group/p4_gpu_counters/runs")
        .glob("*/PASS_MANIFEST.json")
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence_path = root / "capabilities/p4.json"
    dump(evidence_path, {
        "capability": "ncu",
        "available": False,
        "reason": "fixture executable absent",
    })
    from collectors.trace_contract import sha256_file
    manifest["status"] = "unsupported"
    manifest["failure_reason"] = "ncu unavailable in fixture"
    manifest["requirement_class"] = "conditional_optional"
    manifest["capability_evidence"] = {
        "capability": "ncu",
        "available": False,
        "evidence_path": "capabilities/p4.json",
        "evidence_sha256": sha256_file(evidence_path),
    }
    dump(manifest_path, manifest)


def make_p6_optional(root: Path) -> None:
    manifest_path = next(
        (root / "runs/fixture-group/p6_detailed_optional/runs")
        .glob("*/PASS_MANIFEST.json")
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "optional_not_run"
    manifest["failure_reason"] = "optional detailed simulator not requested"
    manifest["requirement_class"] = "conditional_optional"
    dump(manifest_path, manifest)


class GenericIngestTests(unittest.TestCase):
    def standard_fixture(self, temporary: str) -> Path:
        root = build_positive(Path(temporary) / "session", 1)
        apply_standard_profile(root)
        make_p4_unsupported(root)
        make_p6_optional(root)
        refresh_checksums(root)
        return root

    def test_standard_profile_optional_passes_are_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.standard_fixture(temporary)
            code, report = verify_root(root)
        self.assertEqual(COMPLETE, code, report)
        self.assertFalse(report["accepted_incomplete"])
        self.assertTrue(all(
            finding["severity"] == "warning" for finding in report["findings"]
        ))

    def test_missing_p2_is_never_waivable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.standard_fixture(temporary)
            manifest = next(
                (root / "runs/fixture-group/p2_routing/runs")
                .glob("*/PASS_MANIFEST.json")
            )
            manifest.unlink()
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        finding = next(item for item in report["findings"]
                       if item["finding_id"] == "TRACE.P2.MANIFEST_MISSING")
        self.assertFalse(finding["waivable"])

    def test_maximal_profile_allows_evidenced_p4_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.standard_fixture(temporary)
            session_path = root / "SESSION_MANIFEST.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            required = ["P0", "P1", "P2", "P3", "P5"]
            session["capture_profile"] = "maximal_paid"
            session["required_passes"] = required
            session["conditional_optional_passes"] = ["P4", "P6"]
            session["expected_runs"][0]["required_passes"] = required
            session["expected_runs"][0]["conditional_optional_passes"] = ["P4", "P6"]
            dump(session_path, session)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(COMPLETE, code, report)

    def test_tampered_native_raw_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.standard_fixture(temporary)
            raw = next((root / "raw_traces/sha256").rglob("*.raw"))
            raw.write_bytes(raw.read_bytes() + b"tamper")
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertTrue(any(
            item["finding_id"] in {
                "TRACE.RAW.CONTENT_MISMATCH", "TRACE.CHECKSUM.MISMATCH"
            }
            for item in report["findings"]
        ))

    def test_warmup_is_explicitly_separate_from_measured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "native.jsonl"
            rows = [{"event": "pass_start"}]
            rows.extend({
                "event": "sample",
                "run_kind": "measured",
                "sample_id": f"s{index}",
            } for index in range(8))
            rows.append({
                "event": "sample", "run_kind": "warmup", "sample_id": "warmup"
            })
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            _, measured, warmups = load_measured(path)
        self.assertEqual(8, len(measured))
        self.assertEqual(1, len(warmups))


if __name__ == "__main__":
    unittest.main()
