from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))

import package_results  # noqa: E402
from trace_audit import normalize_remediation  # noqa: E402
from trace_package_verify import COMPLETE, package_root, verify_root  # noqa: E402
from tests.fixture_factory import build_positive, dump, refresh_checksums  # noqa: E402


class PackageResultsTests(unittest.TestCase):
    def test_fixture_packages_with_matching_sidecar_and_result_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = build_positive(base / "session")
            archive = base / "fixture.tar.gz"
            result = subprocess.run(
                [
                    sys.executable,
                    str(PACKAGE_ROOT / "scripts/package_results.py"),
                    "--session-root",
                    str(root),
                    "--output",
                    str(archive),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            sidecar = archive.with_suffix(archive.suffix + ".sha256")
            expected = hashlib.sha256()
            with archive.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    expected.update(chunk)
            self.assertEqual(
                f"{expected.hexdigest()}  {archive.name}\n",
                sidecar.read_text(encoding="utf-8"),
            )
            self.assertFalse(list(base.glob(f".{archive.name}.*.tmp")))
            with package_root(archive) as extracted:
                code, report = verify_root(extracted)
                manifest = json.loads(
                    (extracted / "RESULT_PACKAGE_MANIFEST.json").read_text(
                        encoding="utf-8"
                    )
                )
            self.assertEqual(COMPLETE, code, report)
            self.assertEqual("gpu-result-archive-v2", manifest["schema_version"])
            self.assertTrue(root.is_dir())

    def test_post_publish_failure_removes_outputs_but_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = build_positive(base / "session")
            archive = base / "fixture.tar.gz"

            @contextmanager
            def rejected_archive(_path: Path):
                raise ValueError("fixture verifier rejection")
                yield  # pragma: no cover

            report = {"release_eligible": False}
            argv = [
                "package_results.py",
                "--session-root",
                str(root),
                "--output",
                str(archive),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    package_results.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                mock.patch.object(
                    package_results,
                    "verify_root",
                    side_effect=[(COMPLETE, report), (COMPLETE, report)],
                ),
                mock.patch.object(package_results, "package_root", rejected_archive),
            ):
                with self.assertRaisesRegex(ValueError, "fixture verifier rejection"):
                    package_results.main()
            self.assertTrue(root.is_dir())
            self.assertFalse(archive.exists())
            self.assertFalse(
                archive.with_suffix(archive.suffix + ".sha256").exists()
            )
            self.assertFalse(list(base.glob(f".{archive.name}.*.tmp")))

    def test_result_manifest_schema_violation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = build_positive(Path(temporary) / "session")
            path = root / "RESULT_PACKAGE_MANIFEST.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["schema_version"] = "unknown-result-schema"
            dump(path, manifest)
            refresh_checksums(root)
            code, report = verify_root(root)
        self.assertNotEqual(COMPLETE, code)
        self.assertIn(
            "TRACE.RESULT_MANIFEST.SCHEMA_INVALID",
            {item["finding_id"] for item in report["findings"]},
        )

    def test_formal_candidate_result_cannot_claim_release_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = build_positive(Path(temporary) / "session")
            session_path = root / "SESSION_MANIFEST.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["release_class"] = "formal_candidate"
            dump(session_path, session)
            manifest_path = root / "RESULT_PACKAGE_MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["release_class"] = "formal_candidate"
            manifest["release_eligible"] = True
            dump(manifest_path, manifest)
            refresh_checksums(root)
            _, report = verify_root(root)
        self.assertIn(
            "TRACE.RESULT_MANIFEST.NON_RELEASE_ELIGIBLE",
            {item["finding_id"] for item in report["findings"]},
        )

    def test_missing_collector_reports_blocked_state_not_fake_run_sh_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = (
                root / "runs/group/p4_gpu_counters/runs/run/PASS_MANIFEST.json"
            )
            dump(manifest_path, {
                "collector_adapter": "collectors/not-installed.py",
                "blocked_command": "ncu --set full python benchmark.py",
            })
            finding = {
                "finding_id": "TRACE.P4.STATUS_UNSUPPORTED",
                "severity": "incomplete",
                "path": manifest_path.relative_to(root).as_posix(),
                "rerun_command": "./run.sh --run-group group --profiler-pass p4 --resume",
            }
            normalize_remediation(root, finding)
        self.assertNotIn("rerun_command", finding)
        self.assertEqual(
            "blocked_no_executable_collector",
            finding["details"]["remediation_state"],
        )
        self.assertEqual(
            "ncu --set full python benchmark.py",
            finding["details"]["blocked_state_command"],
        )


if __name__ == "__main__":
    unittest.main()
