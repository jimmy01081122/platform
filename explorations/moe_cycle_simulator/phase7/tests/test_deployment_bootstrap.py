from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[4]
APPLICATION = REPO / "explorations/moe_cycle_simulator/phase7/application"
BOOTSTRAP = APPLICATION / "executor/deployment_bootstrap.py"
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    canonical_bytes,
    load_json_bytes,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bootstrap import (  # noqa: E402
    BootstrapError,
    MAX_BUNDLE_BYTES,
    _mount_identity,
    initialize_project_root,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    build_bundle,
    verify_install,
)


class StandaloneDeploymentBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="phase7-bootstrap-test-"))
        self.addCleanup(self._cleanup)
        self.source = self.base / "application-source"
        (self.source / "executor").mkdir(parents=True)
        (self.source / "schemas").mkdir()
        for relative, payload in {
            "README.md": b"standalone deployment fixture\n",
            "approval.template.json": b'{"approval":"mutable"}\n',
            "environment_disclosure_approval.template.json": b'{"approval":"d0"}\n',
            "materialization_approval.template.json": b'{"approval":"materialization"}\n',
            "executor/driver.py": b"VALUE = 1\n",
            "schemas/result.json": b'{"type":"object"}\n',
        }.items():
            (self.source / relative).write_bytes(payload)
        self.bundle = build_bundle(self.source)
        self.allowed = self.base / "allowed"
        self.allowed.mkdir()

    def _cleanup(self) -> None:
        if not self.base.exists():
            return
        for directory, directories, files in os.walk(self.base, topdown=True, followlinks=False):
            directory_path = Path(directory)
            if not directory_path.is_symlink():
                os.chmod(directory_path, 0o700)
            for name in files:
                path = directory_path / name
                if not path.is_symlink():
                    os.chmod(path, 0o600)
            for name in directories:
                path = directory_path / name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
        shutil.rmtree(self.base)

    def _paths(self, suffix: str) -> tuple[Path, Path, Path]:
        return (
            self.allowed / f"incoming-{suffix}.json",
            self.allowed / f"target-{suffix}",
            self.allowed / f"receipt-{suffix}.json",
        )

    def _run(
        self,
        payload: bytes,
        *,
        suffix: str,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        allowed_root: Path | None = None,
        incoming: Path | None = None,
        target: Path | None = None,
        receipt: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[bytes], Path, Path, Path]:
        default_incoming, default_target, default_receipt = self._paths(suffix)
        incoming = default_incoming if incoming is None else incoming
        target = default_target if target is None else target
        receipt = default_receipt if receipt is None else receipt
        root = self.allowed if allowed_root is None else allowed_root
        command = [
            sys.executable,
            "-I",
            "-B",
            str(BOOTSTRAP),
            "--allowed-root",
            str(root),
            "--incoming",
            str(incoming),
            "--target",
            str(target),
            "--receipt",
            str(receipt),
            "--expected-size",
            str(len(payload) if expected_size is None else expected_size),
            "--expected-sha256",
            expected_sha256 or hashlib.sha256(payload).hexdigest(),
        ]
        completed = subprocess.run(
            command,
            cwd=self.base,
            env={"PATH": os.environ.get("PATH", "")},
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return completed, incoming, target, receipt

    def test_isolated_cli_receives_installs_seals_and_emits_compatible_receipt(self) -> None:
        completed, incoming, target, receipt = self._run(self.bundle, suffix="success")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        expected_hash = hashlib.sha256(self.bundle).hexdigest()
        self.assertEqual(completed.stdout.decode().strip(), expected_hash)
        self.assertEqual(incoming.read_bytes(), self.bundle)
        self.assertEqual(stat.S_IMODE(incoming.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o444)
        for member in target.rglob("*"):
            self.assertEqual(
                stat.S_IMODE(member.stat().st_mode),
                0o555 if member.is_dir() else 0o444,
            )
        verified = verify_install(
            allowed_root=self.allowed,
            target=target,
            receipt=receipt,
        )
        self.assertEqual(verified["bundle_sha256"], expected_hash)

    def test_bootstrap_has_only_stdlib_imports_and_no_project_import(self) -> None:
        tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        self.assertEqual(
            imported_roots,
            {
                "__future__",
                "argparse",
                "base64",
                "binascii",
                "ctypes",
                "errno",
                "hashlib",
                "json",
                "os",
                "re",
                "stat",
                "sys",
                "pathlib",
                "typing",
            },
        )

    def test_hash_mismatch_preserves_quarantined_incoming_and_publishes_nothing(self) -> None:
        completed, incoming, target, receipt = self._run(
            self.bundle,
            suffix="hash-mismatch",
            expected_sha256="0" * 64,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"SHA-256", completed.stderr)
        self.assertEqual(incoming.read_bytes(), self.bundle)
        self.assertEqual(stat.S_IMODE(incoming.stat().st_mode), 0o600)
        self.assertFalse(target.exists())
        self.assertFalse(receipt.exists())

    def test_short_and_extra_stdin_are_rejected_with_no_publication(self) -> None:
        completed, incoming, target, receipt = self._run(
            self.bundle[:-1],
            suffix="short",
            expected_size=len(self.bundle),
            expected_sha256=hashlib.sha256(self.bundle).hexdigest(),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(incoming.exists())
        self.assertFalse(target.exists())
        self.assertFalse(receipt.exists())

        completed, incoming, target, receipt = self._run(
            self.bundle + b"x",
            suffix="extra",
            expected_size=len(self.bundle),
            expected_sha256=hashlib.sha256(self.bundle).hexdigest(),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(incoming.stat().st_size, len(self.bundle))
        self.assertFalse(target.exists())
        self.assertFalse(receipt.exists())

    def test_duplicate_float_and_noncanonical_json_are_rejected_after_exact_receive(self) -> None:
        duplicate = b'{"schema_version":"duplicate",' + self.bundle[1:]
        floating = self.bundle.replace(
            b'"member_count":', b'"member_count":1.0,"discarded":', 1
        )
        pretty = (
            json.dumps(json.loads(self.bundle), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode()
        for index, payload in enumerate((duplicate, floating, pretty)):
            completed, incoming, target, receipt = self._run(
                payload, suffix=f"strict-{index}"
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(incoming.exists())
            self.assertEqual(stat.S_IMODE(incoming.stat().st_mode), 0o600)
            self.assertFalse(target.exists())
            self.assertFalse(receipt.exists())

    def test_traversal_duplicate_member_hash_and_ledger_drift_are_rejected(self) -> None:
        original = load_json_bytes(self.bundle, "test bundle")
        cases = []
        for invalid_path in ("../escape", "/absolute", "dir\\escape"):
            changed = copy.deepcopy(original)
            changed["members"][0]["path"] = invalid_path
            cases.append(changed)
        duplicate = copy.deepcopy(original)
        duplicate["members"].append(copy.deepcopy(duplicate["members"][0]))
        duplicate["member_count"] += 1
        duplicate["total_payload_bytes"] += duplicate["members"][0]["size_bytes"]
        cases.append(duplicate)
        hash_drift = copy.deepcopy(original)
        hash_drift["members"][0]["sha256"] = "0" * 64
        cases.append(hash_drift)
        ledger_drift = copy.deepcopy(original)
        ledger_drift["package_ledger"]["ledger_sha256"] = "0" * 64
        cases.append(ledger_drift)
        for index, value in enumerate(cases):
            payload = canonical_bytes(value)
            completed, _, target, receipt = self._run(
                payload, suffix=f"adversarial-{index}"
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(target.exists())
            self.assertFalse(receipt.exists())

    def test_freshness_symlink_and_nonregular_paths_fail_before_receive(self) -> None:
        incoming, target, receipt = self._paths("symlink-incoming")
        outside = self.base / "outside"
        outside.write_bytes(b"outside")
        incoming.symlink_to(outside)
        completed, _, _, _ = self._run(
            self.bundle,
            suffix="unused",
            incoming=incoming,
            target=target,
            receipt=receipt,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(outside.read_bytes(), b"outside")

        fifo, target, receipt = self._paths("fifo-incoming")
        os.mkfifo(fifo)
        completed, _, _, _ = self._run(
            self.bundle,
            suffix="unused-fifo",
            incoming=fifo,
            target=target,
            receipt=receipt,
        )
        self.assertNotEqual(completed.returncode, 0)

        incoming, target, receipt = self._paths("symlink-target")
        target.symlink_to(self.base, target_is_directory=True)
        completed, _, _, _ = self._run(
            self.bundle,
            suffix="unused-target",
            incoming=incoming,
            target=target,
            receipt=receipt,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(incoming.exists())

    def test_allowed_root_alias_escape_receipt_under_target_and_oversize_are_rejected(self) -> None:
        alias = self.base / "allowed-alias"
        alias.symlink_to(self.allowed, target_is_directory=True)
        completed, incoming, target, receipt = self._run(
            self.bundle,
            suffix="root-alias",
            allowed_root=alias,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(incoming.exists())
        self.assertFalse(target.exists())
        self.assertFalse(receipt.exists())

        incoming, target, _ = self._paths("receipt-under-target")
        completed, _, _, receipt = self._run(
            self.bundle,
            suffix="unused-receipt",
            incoming=incoming,
            target=target,
            receipt=target / "receipt.json",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(incoming.exists())
        self.assertFalse(target.exists())
        self.assertFalse(receipt.exists())

        incoming, target, receipt = self._paths("oversize")
        completed, _, _, _ = self._run(
            b"x",
            suffix="unused-oversize",
            incoming=incoming,
            target=target,
            receipt=receipt,
            expected_size=MAX_BUNDLE_BYTES + 1,
            expected_sha256=hashlib.sha256(b"x").hexdigest(),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(incoming.exists())

    def test_reuse_of_any_published_path_is_rejected_without_overwrite(self) -> None:
        completed, incoming, target, receipt = self._run(self.bundle, suffix="one-shot")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        incoming_hash = hashlib.sha256(incoming.read_bytes()).hexdigest()
        completed, _, _, _ = self._run(self.bundle, suffix="one-shot")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(hashlib.sha256(incoming.read_bytes()).hexdigest(), incoming_hash)
        self.assertTrue(target.is_dir())
        self.assertTrue(receipt.is_file())

    def test_mount_identity_matches_d0_reference_algorithm(self) -> None:
        mountinfo = self.base / "mountinfo"
        boot_id = self.base / "boot_id"
        boot_id.write_text("00000000-0000-0000-0000-000000000001\n", encoding="utf-8")
        device = self.allowed.stat().st_dev
        mountinfo.write_text(
            f"40 30 0:99 / {self.allowed} rw,nosuid - ext4 /dev/test rw,relatime\n",
            encoding="utf-8",
        )
        observed = _mount_identity(
            self.allowed,
            mountinfo_path=mountinfo,
            boot_id_path=boot_id,
        )
        self.assertEqual(observed["device_id"], device)
        self.assertRegex(observed["mount_identity_sha256"], r"^[0-9a-f]{64}$")

    def test_project_initializer_is_fresh_mount_bound_and_exact(self) -> None:
        project = self.allowed / "flow-r12-deploy-0001"
        identity = "1" * 64
        with mock.patch(
            "explorations.moe_cycle_simulator.phase7.application.executor."
            "deployment_bootstrap._mount_identity",
            return_value={"mount_identity_sha256": identity},
        ):
            value = initialize_project_root(
                allowed_root=self.allowed,
                project_root=project,
                relative_directories=[
                    "incoming",
                    "packages/materialization",
                    "packages/m0",
                    "model/ledger",
                    "fixtures",
                    "authority/registries",
                    "evidence",
                    "export",
                ],
                expected_mount_identity_sha256=identity,
            )
            self.assertEqual(value, project)
            self.assertEqual(stat.S_IMODE(project.stat().st_mode), 0o700)
            self.assertTrue((project / "packages/materialization").is_dir())
            with self.assertRaises(BootstrapError):
                initialize_project_root(
                    allowed_root=self.allowed,
                    project_root=project,
                    relative_directories=["incoming"],
                    expected_mount_identity_sha256=identity,
                )

        second = self.allowed / "flow-r12-deploy-0002"
        with mock.patch(
            "explorations.moe_cycle_simulator.phase7.application.executor."
            "deployment_bootstrap._mount_identity",
            return_value={"mount_identity_sha256": "2" * 64},
        ):
            with self.assertRaises(BootstrapError):
                initialize_project_root(
                    allowed_root=self.allowed,
                    project_root=second,
                    relative_directories=["incoming"],
                    expected_mount_identity_sha256=identity,
                )
        self.assertFalse(second.exists())

    def test_project_initializer_rejects_escape_duplicate_and_unicode(self) -> None:
        identity = "3" * 64
        with mock.patch(
            "explorations.moe_cycle_simulator.phase7.application.executor."
            "deployment_bootstrap._mount_identity",
            return_value={"mount_identity_sha256": identity},
        ):
            for index, directories in enumerate(
                (["../escape"], ["same", "same"], ["unicodé"])
            ):
                with self.subTest(directories=directories):
                    with self.assertRaises(BootstrapError):
                        initialize_project_root(
                            allowed_root=self.allowed,
                            project_root=self.allowed / f"project-{index}",
                            relative_directories=directories,
                            expected_mount_identity_sha256=identity,
                        )


if __name__ == "__main__":
    unittest.main()
