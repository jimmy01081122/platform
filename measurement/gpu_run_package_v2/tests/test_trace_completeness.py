from __future__ import annotations

import io
import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))

from collectors.pass_manifest import main as finalize_pass  # noqa: E402
from trace_package_verify import (  # noqa: E402
    APPROVED_INCOMPLETE, COMPLETE, FAILED, package_root,
    validate_release_reports, verify_root,
)
from tests.fixture_factory import build_positive, dump, refresh_checksums  # noqa: E402


def ids(report: dict) -> set[str]:
    return {item["finding_id"] for item in report["findings"]}


class TraceCompletenessTests(unittest.TestCase):
    def fixture(self, temporary: str) -> Path:
        return build_positive(
            Path(temporary) / "hardware_session_fixture",
            required_repetitions=3,
        )

    @staticmethod
    def pass_manifest(root: Path, pass_directory: str, repetition: int) -> Path:
        pass_id = pass_directory.split("_", 1)[0]
        run_id = f"fixture-run-{pass_id}-r{repetition}"
        return (
            root / "runs" / "fixture-group" / pass_directory / "runs"
            / run_id / "PASS_MANIFEST.json"
        )

    def test_minimal_positive_fixture_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            code, report = verify_root(root)
            for pass_directory in (
                "p0_baseline",
                "p1_timeline",
                "p2_routing",
                "p3_memory_transfer",
                "p4_gpu_counters",
                "p5_telemetry",
                "p6_detailed_optional",
            ):
                manifests = list(
                    (root / "runs" / "fixture-group" / pass_directory / "runs")
                    .glob("*/PASS_MANIFEST.json")
                )
                self.assertEqual(3, len(manifests), pass_directory)
        self.assertEqual(COMPLETE, code, report)
        self.assertEqual("complete", report["status"])

    def test_checksum_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            raw = next((root / "raw_traces/sha256").rglob("*.raw"))
            raw.write_bytes(raw.read_bytes() + b"tampered")
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.CHECKSUM.MISMATCH", ids(report))

    def test_missing_p0_manifest_fails_even_if_checksums_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            self.pass_manifest(root, "p0_baseline", 1).unlink()
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.P0.REPETITION_INCOMPLETE", ids(report))

    def test_cross_pass_hash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            path = self.pass_manifest(root, "p1_timeline", 0)
            manifest = json.loads(path.read_text())
            manifest["identity"]["workload_hash"] = "b" * 64
            dump(path, manifest)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.IDENTITY.WORKLOAD_HASH_MISMATCH", ids(report))

    def test_accepted_incomplete_without_approval_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            pass_path = self.pass_manifest(root, "p6_detailed_optional", 0)
            manifest = json.loads(pass_path.read_text())
            manifest["status"] = "unsupported"
            manifest["failure_reason"] = "fixture profiler does not expose P6"
            dump(pass_path, manifest)
            session_path = root / "SESSION_MANIFEST.json"
            session = json.loads(session_path.read_text())
            session["accepted_incomplete"] = True
            dump(session_path, session)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.APPROVAL.MISSING", ids(report))

    def test_approved_incomplete_returns_ten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            pass_path = self.pass_manifest(root, "p6_detailed_optional", 0)
            manifest = json.loads(pass_path.read_text())
            manifest["status"] = "unsupported"
            manifest["failure_reason"] = "fixture profiler does not expose P6"
            dump(pass_path, manifest)
            session_path = root / "SESSION_MANIFEST.json"
            session = json.loads(session_path.read_text())
            session["accepted_incomplete"] = True
            session["approval"] = {
                "approved_by": "fixture-owner",
                "approved_utc": "2026-07-18T00:00:00Z",
                "reason": "P6 is unavailable on the fixture device",
            }
            dump(session_path, session)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(APPROVED_INCOMPLETE, code, report)

    def test_empty_raw_is_non_waivable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            raw = next((root / "raw_traces/sha256").rglob("*.raw"))
            raw.write_bytes(b"")
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.RAW.EMPTY", ids(report))

    def test_truncated_raw_is_non_waivable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            inventory_path = root / "raw_traces/RAW_INVENTORY.json"
            inventory = json.loads(inventory_path.read_text())
            inventory["entries"][0]["truncated"] = True
            dump(inventory_path, inventory)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.RAW.TRUNCATED", ids(report))

    def test_repetition_shortfall_fails_for_p0(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            self.pass_manifest(root, "p0_baseline", 2).unlink()
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.P0.REPETITION_INCOMPLETE", ids(report))

    def test_formal_candidate_enforces_three_reps_despite_smoke_self_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = build_positive(Path(temporary) / "session", 1)
            code, report = verify_root(
                root, requested_release_class="formal_candidate"
            )
        self.assertEqual(FAILED, code)
        self.assertEqual("formal_candidate", report["enforced_release_class"])
        self.assertIn("TRACE.REPETITION.CONTRACT_INVALID", ids(report))

    def test_formal_release_recommends_five_reps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            _, report = verify_root(
                root, requested_release_class="formal_release"
            )
        self.assertIn("TRACE.REPETITION.RELEASE_RECOMMENDATION", ids(report))

    def test_formal_p5_insufficient_sampling_is_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            path = self.pass_manifest(root, "p5_telemetry", 0)
            manifest = json.loads(path.read_text())
            manifest["sampling_sufficiency"] = {
                "status": "insufficient",
                "duration_seconds": 1.2,
                "sample_count": 10,
                "minimum_duration_seconds": 5,
                "minimum_sample_count": 20,
            }
            dump(path, manifest)
            refresh_checksums(root)
            code, report = verify_root(
                root, requested_release_class="formal_candidate"
            )
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.P5.SAMPLING_INSUFFICIENT", ids(report))

    def test_missing_frozen_matrix_binding_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            session_path = root / "SESSION_MANIFEST.json"
            session = json.loads(session_path.read_text())
            session.pop("frozen_matrix")
            dump(session_path, session)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.MATRIX.BINDING_INCOMPLETE", ids(report))

    def test_canonical_source_ids_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            canonical = next((root / "canonical_traces").glob("*.json"))
            value = json.loads(canonical.read_text())
            value["source_content_ids"] = ["f" * 64]
            dump(canonical, value)
            inventory_path = root / "raw_traces/RAW_INVENTORY.json"
            inventory = json.loads(inventory_path.read_text())
            conversion = next(
                item for item in inventory["conversions"]
                if item["canonical_path"] == canonical.relative_to(root).as_posix()
            )
            conversion["canonical_sha256"] = hashlib.sha256(
                canonical.read_bytes()
            ).hexdigest()
            dump(inventory_path, inventory)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertEqual(FAILED, code)
        self.assertIn("TRACE.CANONICAL.SOURCE_IDS_MISMATCH", ids(report))

    def test_pass_manifest_refuses_to_overwrite_same_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            manifest_path = self.pass_manifest(root, "p0_baseline", 1)
            original = manifest_path.read_bytes()
            manifest = json.loads(original)
            identity_path = root / "identity-r1.json"
            dump(identity_path, {
                "identity": manifest["identity"],
                "clock": manifest["clock"],
            })
            raw_id = manifest["raw_artifacts"][0]["content_id"]
            argv = [
                "pass_manifest",
                "--session-root", str(root),
                "--identity", str(identity_path),
                "--pass-id", "P0",
                "--status", "complete",
                "--rerun-command", "fixture-rerun",
                "--raw-content-id", raw_id,
            ]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaisesRegex(
                    SystemExit, "refusing to overwrite existing run manifest"
                ):
                    finalize_pass()
            self.assertEqual(original, manifest_path.read_bytes())

    def test_safe_complete_archive_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            archive_path = Path(temporary) / "complete.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(root, arcname=root.name)
            archive_path.with_suffix(archive_path.suffix + ".sha256").write_text(
                f"{hashlib.sha256(archive_path.read_bytes()).hexdigest()}  "
                f"{archive_path.name}\n",
                encoding="utf-8",
            )
            with package_root(archive_path) as extracted:
                code, report = verify_root(extracted)
        self.assertEqual(COMPLETE, code, report)

    def test_archive_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "unsafe.tar"
            with tarfile.open(archive_path, "w") as archive:
                member = tarfile.TarInfo("../escape")
                payload = b"escape"
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                with package_root(archive_path):
                    pass

    def test_archive_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "unsafe-link.tar"
            with tarfile.open(archive_path, "w") as archive:
                member = tarfile.TarInfo("session/link")
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                archive.addfile(member)
            with self.assertRaisesRegex(ValueError, "unsafe archive member type"):
                with package_root(archive_path):
                    pass

    def test_archive_without_sidecar_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            archive_path = Path(temporary) / "missing-sidecar.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(root, arcname=root.name)
            with self.assertRaisesRegex(ValueError, "sidecar is missing"):
                with package_root(archive_path):
                    pass

    def test_archive_with_wrong_sidecar_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            archive_path = Path(temporary) / "wrong-sidecar.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(root, arcname=root.name)
            archive_path.with_suffix(archive_path.suffix + ".sha256").write_text(
                f"{'0' * 64}  {archive_path.name}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "sidecar does not match"):
                with package_root(archive_path):
                    pass

    def test_formal_release_reports_pass_all_four_domain_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump(root / "VALIDATION_MAPE_REPORT.json", {
                "schema_version": "validation-mape-report-v1",
                "calibration_parameters_sha256": "a" * 64,
                "fit_split": "calibration",
                "score_split": "validation",
                "validation_refit_performed": False,
                "zero_policy": "reject",
                "skipped_zero_point_ids": [],
                "per_metric_domain": [
                    {
                        "metric": metric,
                        "domain": f"{metric}-domain",
                        "n": 3,
                        "mape_percent": 10.0,
                        "gate_percent": 15.0,
                        "pass": True,
                    }
                    for metric in (
                        "component_latency",
                        "pcie_transfer_latency",
                        "moe_replay_tpot",
                        "moe_replay_throughput",
                    )
                ],
                "overall_mape_percent": 10.0,
                "gate_percent": 15.0,
                "gate_pass": True,
            })
            dump(root / "QUALITY_RELEASE_REPORT.json", {
                "schema_version": "quality-release-report-v1",
                "gate_pass": True,
            })
            findings = []
            validate_release_reports(
                root, findings, release_class="formal_release"
            )
        self.assertEqual([], findings)

    def test_formal_release_rejects_missing_metric_and_failed_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump(root / "VALIDATION_MAPE_REPORT.json", {
                "schema_version": "validation-mape-report-v1",
                "calibration_parameters_sha256": "a" * 64,
                "fit_split": "calibration",
                "score_split": "validation",
                "validation_refit_performed": False,
                "zero_policy": "reject",
                "skipped_zero_point_ids": [],
                "per_metric_domain": [{
                    "metric": "component_latency",
                    "domain": "component",
                    "n": 1,
                    "mape_percent": 16.0,
                    "gate_percent": 15.0,
                    "pass": False,
                }],
                "overall_mape_percent": 16.0,
                "gate_percent": 15.0,
                "gate_pass": False,
            })
            dump(root / "QUALITY_RELEASE_REPORT.json", {
                "schema_version": "quality-release-report-v1",
                "gate_pass": False,
            })
            findings = []
            validate_release_reports(
                root, findings, release_class="formal_release"
            )
        finding_ids = {item.finding_id for item in findings}
        self.assertIn("TRACE.RELEASE.MAPE_GATE_FAILED", finding_ids)
        self.assertIn("TRACE.RELEASE.MAPE_DOMAIN_GATE_FAILED", finding_ids)
        self.assertIn("TRACE.RELEASE.MAPE_METRIC_MISSING", finding_ids)
        self.assertIn("TRACE.RELEASE.QUALITY_GATE_FAILED", finding_ids)

    def test_formal_optional_p4_p6_do_not_require_raw_when_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            evidence = root / "provenance/capabilities/ncu-unavailable.txt"
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text("ncu not installed on fixture host\n", encoding="utf-8")
            evidence_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()
            for pass_directory, status in (
                ("p4_gpu_counters", "unsupported"),
                ("p6_detailed_optional", "optional_not_run"),
            ):
                for repetition in range(3):
                    path = self.pass_manifest(root, pass_directory, repetition)
                    manifest = json.loads(path.read_text())
                    manifest["status"] = status
                    manifest["failure_reason"] = f"{pass_directory} is optional"
                    manifest["requirement_class"] = "conditional_optional"
                    manifest["raw_artifacts"] = []
                    manifest["converter_provenance"]["input_content_ids"] = []
                    for field in manifest["raw_observation"].values():
                        field.clear()
                        field.update({
                            "status": "not_applicable",
                            "source_content_ids": [],
                        })
                    if pass_directory.startswith("p4"):
                        manifest["capability_evidence"] = {
                            "capability": "ncu",
                            "available": False,
                            "evidence_path": evidence.relative_to(root).as_posix(),
                            "evidence_sha256": evidence_hash,
                        }
                    dump(path, manifest)
            session_path = root / "SESSION_MANIFEST.json"
            session = json.loads(session_path.read_text())
            session["release_class"] = "formal_candidate"
            session["capture_profile"] = "standard"
            session["required_passes"] = ["P0", "P1", "P2", "P3", "P5"]
            session["conditional_optional_passes"] = ["P4", "P6"]
            session["expected_runs"][0]["required_passes"] = session["required_passes"]
            session["expected_runs"][0]["conditional_optional_passes"] = ["P4", "P6"]
            capture_plan_path = root / session["capture_plan"]["path"]
            capture_plan = json.loads(capture_plan_path.read_text())
            capture_plan["states"][0]["status"] = "unsupported"
            capture_plan["states"].append({
                "state_id": "fixture-p6-binding",
                "pass_id": "P6",
                "status": "optional_not_run",
                "blocked_reason": "fixture optional pass was not run",
                "command": "python3 collectors/p6.py",
                "estimate_minutes": 1,
            })
            dump(capture_plan_path, capture_plan)
            session["capture_plan"]["sha256"] = hashlib.sha256(
                capture_plan_path.read_bytes()
            ).hexdigest()
            dump(session_path, session)
            result_path = root / "RESULT_PACKAGE_MANIFEST.json"
            result = json.loads(result_path.read_text())
            result["release_class"] = "formal_candidate"
            dump(result_path, result)
            refresh_checksums(root)
            _, report = verify_root(root)
        finding_ids = ids(report)
        self.assertNotIn("TRACE.P4.RAW_ENVIRONMENT_MISSING", finding_ids)
        self.assertNotIn("TRACE.P6.RAW_ENVIRONMENT_MISSING", finding_ids)
        self.assertNotIn("TRACE.P4.STATUS_UNSUPPORTED", finding_ids)
        self.assertNotIn("TRACE.P6.STATUS_OPTIONAL_NOT_RUN", finding_ids)
        self.assertNotIn("TRACE.MATRIX.STATUS_UNSUPPORTED", finding_ids)
        self.assertNotIn("TRACE.MATRIX.STATUS_OPTIONAL_NOT_RUN", finding_ids)


if __name__ == "__main__":
    unittest.main()
