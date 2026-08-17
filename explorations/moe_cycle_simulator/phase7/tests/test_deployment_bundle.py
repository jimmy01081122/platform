from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
APPLICATION = REPO / "explorations/moe_cycle_simulator/phase7/application"
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    canonical_bytes,
    load_json_bytes,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    MUTABLE_APPROVAL_FILES,
    _validated_bundle,
    build_bundle,
    install_bundle,
    receive_bundle,
    verify_install,
    write_bundle,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)


class DeploymentBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="phase7-deployment-test-"))
        self.addCleanup(self._cleanup)
        self.source = self.base / "application"
        (self.source / "executor").mkdir(parents=True)
        (self.source / "schemas").mkdir()
        (self.source / "__pycache__").mkdir()
        files = {
            "README.md": b"phase7 application\n",
            "approval.template.json": b'{"approval":"mutable"}\n',
            "environment_disclosure_approval.template.json": b'{"approval":"d0"}\n',
            "materialization_approval.template.json": b'{"approval":"materialize"}\n',
            "executor/tool.py": b"VALUE = 1\n",
            "schemas/example.json": b'{"type":"object"}\n',
            "ignored.pyc": b"ignored",
            "__pycache__/tool.cpython-310.pyc": b"ignored cache",
        }
        for relative, payload in files.items():
            (self.source / relative).write_bytes(payload)
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

    def _payload(self) -> bytes:
        return build_bundle(self.source)

    def _bundle_file(self, payload: bytes | None = None) -> Path:
        path = self.base / f"bundle-{len(list(self.base.glob('bundle-*')))}.json"
        path.write_bytes(self._payload() if payload is None else payload)
        return path

    def _installed(self) -> tuple[Path, Path, dict]:
        target = self.allowed / "installed-application"
        receipt = self.allowed / "installation-receipt.json"
        value = install_bundle(
            self._bundle_file(),
            allowed_root=self.allowed,
            target=target,
            receipt=receipt,
        )
        return target, receipt, value

    def test_build_is_canonical_includes_approvals_and_recomputes_ledger(self) -> None:
        payload = self._payload()
        value = load_json_bytes(payload, "test bundle")
        self.assertEqual(payload, canonical_bytes(value))
        paths = {member["path"] for member in value["members"]}
        self.assertTrue(set(MUTABLE_APPROVAL_FILES).issubset(paths))
        self.assertNotIn("ignored.pyc", paths)
        self.assertFalse(any("__pycache__" in path for path in paths))
        self.assertEqual(value["package_ledger"], build_application_ledger(self.source))
        immutable_paths = {
            member["path"] for member in value["package_ledger"]["members"]
        }
        self.assertTrue(set(MUTABLE_APPROVAL_FILES).isdisjoint(immutable_paths))

    def test_build_output_is_fresh_read_only_and_outside_source(self) -> None:
        output = self.base / "application.bundle.json"
        digest = write_bundle(self.source, output)
        self.assertEqual(len(digest), 64)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)
        with self.assertRaises(M0Error):
            write_bundle(self.source, output)
        with self.assertRaises(M0Error):
            write_bundle(self.source, self.source / "forbidden.bundle.json")

    def test_build_rejects_symlink_fifo_and_empty_unrepresented_directory(self) -> None:
        link = self.source / "link"
        link.symlink_to("README.md")
        with self.assertRaises(M0Error):
            self._payload()
        link.unlink()
        fifo = self.source / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(M0Error):
            self._payload()
        fifo.unlink()
        (self.source / "empty").mkdir()
        with self.assertRaises(M0Error):
            self._payload()

    def test_build_rejects_source_root_symlink(self) -> None:
        alias = self.base / "application-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(M0Error):
            build_bundle(alias)
        with self.assertRaises(M0Error):
            write_bundle(alias, self.base / "alias.bundle.json")

    def test_strict_json_rejects_duplicate_float_and_noncanonical_form(self) -> None:
        payload = self._payload()
        duplicate = b'{"schema_version":"duplicate",' + payload[1:]
        floating = payload.replace(
            b'"member_count":', b'"member_count":1.0,"discarded_integer":', 1
        )
        pretty = (json.dumps(json.loads(payload), sort_keys=True, indent=2) + "\n").encode()
        for invalid in (duplicate, floating, pretty):
            with self.subTest(invalid=invalid[:40]):
                with self.assertRaises(M0Error):
                    _validated_bundle(invalid)

    def test_member_path_guards_reject_traversal_backslash_absolute_and_duplicate(self) -> None:
        original = load_json_bytes(self._payload(), "test bundle")
        for invalid_path in (
            "../escape",
            "/absolute",
            "dir\\escape",
            "a//b",
            "line\nbreak",
            "unicodé/path",
        ):
            changed = copy.deepcopy(original)
            changed["members"][0]["path"] = invalid_path
            with self.subTest(path=invalid_path):
                with self.assertRaises(M0Error):
                    _validated_bundle(canonical_bytes(changed))
        duplicate = copy.deepcopy(original)
        duplicate["members"].append(copy.deepcopy(duplicate["members"][0]))
        duplicate["member_count"] += 1
        duplicate["total_payload_bytes"] += duplicate["members"][0]["size_bytes"]
        with self.assertRaises(M0Error):
            _validated_bundle(canonical_bytes(duplicate))

    def test_bundle_rejects_size_hash_and_immutable_ledger_drift(self) -> None:
        original = load_json_bytes(self._payload(), "test bundle")
        mutations = []
        size_drift = copy.deepcopy(original)
        size_drift["members"][0]["size_bytes"] += 1
        size_drift["total_payload_bytes"] += 1
        mutations.append(size_drift)
        hash_drift = copy.deepcopy(original)
        hash_drift["members"][0]["sha256"] = "0" * 64
        mutations.append(hash_drift)
        ledger_drift = copy.deepcopy(original)
        ledger_drift["package_ledger"]["ledger_sha256"] = "0" * 64
        mutations.append(ledger_drift)
        for changed in mutations:
            with self.subTest():
                with self.assertRaises(M0Error):
                    _validated_bundle(canonical_bytes(changed))

    def test_receive_is_bounded_canonical_read_only_and_fresh(self) -> None:
        payload = self._payload()
        output = self.base / "received.bundle.json"
        digest = receive_bundle(io.BytesIO(payload), output, max_bundle_bytes=len(payload))
        self.assertEqual(len(digest), 64)
        self.assertEqual(output.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)
        with self.assertRaises(M0Error):
            receive_bundle(io.BytesIO(payload), output, max_bundle_bytes=len(payload))
        bounded_output = self.base / "must-not-exist.bundle.json"
        with self.assertRaises(M0Error):
            receive_bundle(
                io.BytesIO(payload), bounded_output, max_bundle_bytes=len(payload) - 1
            )
        self.assertFalse(bounded_output.exists())

    def test_receive_cli_reads_only_bounded_stdin_into_fresh_file(self) -> None:
        payload = self._payload()
        output = self.base / "stdin-received.bundle.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(APPLICATION / "executor/deployment_bundle.py"),
                "receive",
                "--output",
                str(output),
                "--max-bundle-bytes",
                str(len(payload)),
            ],
            cwd=REPO,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(output.read_bytes(), payload)
        rejected = subprocess.run(
            [
                sys.executable,
                "-B",
                str(APPLICATION / "executor/deployment_bundle.py"),
                "receive",
                "--output",
                str(self.base / "oversize-must-not-exist.json"),
                "--max-bundle-bytes",
                str(len(payload) - 1),
            ],
            cwd=REPO,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse((self.base / "oversize-must-not-exist.json").exists())

    def test_install_and_verify_exact_set_modes_ledger_and_bundle_hash(self) -> None:
        target, receipt, installed = self._installed()
        self.assertEqual(installed, verify_install(
            allowed_root=self.allowed, target=target, receipt=receipt
        ))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o444)
        for path in target.rglob("*"):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o555 if path.is_dir() else 0o444)
        source_bundle = load_json_bytes(self._payload(), "source bundle")
        self.assertEqual(installed["package_ledger"], source_bundle["package_ledger"])

    def test_install_rejects_existing_target_receipt_stage_and_symlink_input(self) -> None:
        payload = self._payload()
        bundle = self._bundle_file(payload)
        target = self.allowed / "target"
        receipt = self.allowed / "receipt.json"
        target.mkdir()
        with self.assertRaises(M0Error):
            install_bundle(
                bundle, allowed_root=self.allowed, target=target, receipt=receipt
            )
        target.rmdir()
        receipt.write_text("occupied", encoding="utf-8")
        with self.assertRaises(M0Error):
            install_bundle(
                bundle, allowed_root=self.allowed, target=target, receipt=receipt
            )
        receipt.unlink()
        alias = self.base / "bundle-alias.json"
        alias.symlink_to(bundle)
        with self.assertRaises(M0Error):
            install_bundle(
                alias, allowed_root=self.allowed, target=target, receipt=receipt
            )

        digest = hashlib.sha256(payload).hexdigest()
        stage = target.with_name(f".{target.name}.deploy-{digest[:16]}.staged")
        stage.mkdir()
        with self.assertRaises(M0Error):
            install_bundle(
                bundle, allowed_root=self.allowed, target=target, receipt=receipt
            )

    def test_install_rejects_symlink_target_absolute_escape_and_receipt_under_target(self) -> None:
        bundle = self._bundle_file()
        outside = self.base / "outside"
        outside.mkdir()
        target = self.allowed / "target"
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(M0Error):
            install_bundle(
                bundle,
                allowed_root=self.allowed,
                target=target,
                receipt=self.allowed / "receipt.json",
            )
        target.unlink()
        with self.assertRaises(M0Error):
            install_bundle(
                bundle,
                allowed_root=self.allowed,
                target=self.base / "escaped-target",
                receipt=self.allowed / "receipt.json",
            )
        with self.assertRaises(M0Error):
            install_bundle(
                bundle,
                allowed_root=self.allowed,
                target=target,
                receipt=target / "receipt.json",
            )

    def test_verify_detects_content_mode_and_extra_member_drift(self) -> None:
        target, receipt, _ = self._installed()
        readme = target / "README.md"
        os.chmod(readme, 0o644)
        with self.assertRaises(M0Error):
            verify_install(allowed_root=self.allowed, target=target, receipt=receipt)
        os.chmod(readme, 0o444)
        os.chmod(target, 0o755)
        extra = target / "extra.txt"
        extra.write_bytes(b"extra")
        os.chmod(extra, 0o444)
        os.chmod(target, 0o555)
        with self.assertRaises(M0Error):
            verify_install(allowed_root=self.allowed, target=target, receipt=receipt)

    def test_verify_rejects_receipt_mode_and_target_symlink(self) -> None:
        target, receipt, _ = self._installed()
        os.chmod(receipt, 0o644)
        with self.assertRaises(M0Error):
            verify_install(allowed_root=self.allowed, target=target, receipt=receipt)
        os.chmod(receipt, 0o444)
        # Moving the real target and substituting a symlink must not be followed.
        saved = self.allowed / "saved-target"
        target.rename(saved)
        target.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(M0Error):
            verify_install(allowed_root=self.allowed, target=target, receipt=receipt)


if __name__ == "__main__":
    unittest.main()
