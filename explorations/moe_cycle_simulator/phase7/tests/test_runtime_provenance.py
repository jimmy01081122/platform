from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_provenance import (  # noqa: E402
    ProvenanceUnavailable,
    build_source_tree_ledger,
    capture_static_provenance,
    seal_runtime_provenance,
    verify_runtime_provenance,
)


def _restore(root: Path) -> None:
    if not root.exists():
        return
    for directory, directories, files in os.walk(root, topdown=True):
        path = Path(directory)
        if not path.is_symlink():
            path.chmod(0o700)
        for name in files:
            member = path / name
            if not member.is_symlink():
                member.chmod(0o600)
        for name in directories:
            member = path / name
            if not member.is_symlink():
                member.chmod(0o700)


class RuntimeProvenanceTests(unittest.TestCase):
    def _fixture(self, base: Path) -> dict[str, object]:
        source = base / "vllm-source"
        source.mkdir()
        (source / "vllm").mkdir()
        (source / "vllm/__init__.py").write_text(
            "__version__ = '0.9.2'\n", encoding="utf-8"
        )
        source_ledger = build_source_tree_ledger(source)
        wheel = base / "vllm.whl"
        wheel.write_bytes(b"test-wheel")
        build = base / "build.json"
        write_new_json(build, {"builder": "test", "commands": ["build"]})
        sbom = base / "sbom.json"
        write_new_json(
            sbom,
            {
                "bomFormat": "CycloneDX",
                "components": [{"name": "vllm", "version": "0.9.2"}],
            },
        )
        installed = {
            "schema_version": "moe-simulator-phase7-installed-distribution-ledger-v1",
            "distribution_name": "vllm",
            "distribution_version": "0.9.2",
            "member_count": 1,
            "total_size_bytes": 1,
            "members": [
                {
                    "declared_path": "vllm-0.9.2.dist-info/RECORD",
                    "size_bytes": 1,
                    "sha256": "1" * 64,
                }
            ],
        }
        installed["ledger_sha256"] = semantic_sha256(installed)
        return {
            "vllm_version": "0.9.2",
            "source_commit": "a" * 40,
            "source_tree": source,
            "expected_source_tree_ledger_sha256": source_ledger["ledger_sha256"],
            "wheel": wheel,
            "expected_wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "build_environment_ledger": build,
            "expected_build_environment_ledger_sha256": hashlib.sha256(
                build.read_bytes()
            ).hexdigest(),
            "container_sbom": sbom,
            "expected_container_sbom_sha256": hashlib.sha256(
                sbom.read_bytes()
            ).hexdigest(),
            "container_image": "provider/vllm:test",
            "container_digest": "sha256:" + "2" * 64,
            "installed": installed,
        }

    def test_complete_static_capture_is_gpu_free_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture = self._fixture(base)
            installed = fixture.pop("installed")
            with patch(
                "explorations.moe_cycle_simulator.phase7.application.executor."
                "runtime_provenance.build_installed_distribution_manifest",
                return_value=installed,
            ):
                result, manifests = capture_static_provenance(**fixture)
            self.assertTrue(result["m0_provenance_eligible"])
            self.assertFalse(result["capability_boundary"]["vllm_imported"])
            self.assertEqual(
                result["capability_boundary"]["runtime_selected_backend_observation"],
                "PENDING_M0_R1_STARTUP",
            )
            root = base / "evidence"
            root.mkdir()
            write_new_json(root / "runtime_provenance.json", result)
            write_new_json(root / "source_tree_ledger.json", manifests["source_tree"])
            write_new_json(
                root / "installed_distribution.json",
                manifests["installed_distribution"],
            )
            try:
                ledger = seal_runtime_provenance(root, "COMPLETE")
                self.assertEqual(verify_runtime_provenance(root), ledger)
                target = root / "runtime_provenance.json"
                target.chmod(0o600)
                target.write_text("{}\n", encoding="utf-8")
                with self.assertRaises(M0Error):
                    verify_runtime_provenance(root)
            finally:
                _restore(root)

    def test_missing_source_is_blocked_but_hash_drift_is_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture = self._fixture(base)
            fixture.pop("installed")
            missing = base / "missing-source"
            fixture["source_tree"] = missing
            with self.assertRaises(ProvenanceUnavailable):
                capture_static_provenance(**fixture)
            fixture["source_tree"] = base / "vllm-source"
            fixture["expected_source_tree_ledger_sha256"] = "0" * 64
            with self.assertRaisesRegex(M0Error, "source tree ledger differs"):
                capture_static_provenance(**fixture)

    def test_collector_source_forbids_runtime_or_cuda_imports(self) -> None:
        source = (
            REPO
            / "explorations/moe_cycle_simulator/phase7/application/executor/runtime_provenance.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("import vllm", source)
        self.assertNotIn("import torch", source)
        self.assertNotIn("torch.cuda", source)


if __name__ == "__main__":
    unittest.main()
