from __future__ import annotations

import ast
import base64
import hashlib
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_bootstrap import (  # noqa: E402
    GateMBootstrapError,
    _enable_subreaper,
    _run_remote_controller,
    _validated_remote_executable,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    write_bundle,
)


class GateMBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="phase7-gate-m-bootstrap-"))
        self.addCleanup(self._cleanup)
        self.application = self.root / "application"
        (self.application / "executor").mkdir(parents=True)
        self.evidence = self.root / "evidence"

    def _cleanup(self) -> None:
        if not self.root.exists():
            return
        for directory, directories, files in os.walk(
            self.root, topdown=True, followlinks=False
        ):
            root = Path(directory)
            if not root.is_symlink():
                root.chmod(0o700)
            for name in files:
                path = root / name
                if not path.is_symlink():
                    path.chmod(0o600)
            for name in directories:
                path = root / name
                if not path.is_symlink():
                    path.chmod(0o700)
        shutil.rmtree(self.root)

    def _write_remote(self, body: str) -> None:
        path = self.application / "executor/gate_m_remote.py"
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def _run(self) -> bytes:
        writer_socket, reader_socket = socket.socketpair()
        reader_socket.settimeout(2)
        output = writer_socket.makefile("wb", buffering=0)
        try:
            _run_remote_controller(
                application=self.application,
                evidence_root=self.evidence,
                materialization_deadline_ns=time.monotonic_ns() + 2_000_000_000,
                provenance_deadline_ns=time.monotonic_ns() + 3_000_000_000,
                export_deadline_ns=time.monotonic_ns() + 4_000_000_000,
                output_stream=output,
                remote_timeout_executable=Path(sys.executable).resolve(strict=True),
                remote_timeout_executable_sha256=hashlib.sha256(
                    Path(sys.executable).resolve(strict=True).read_bytes()
                ).hexdigest(),
                remote_python_executable=Path(sys.executable).resolve(strict=True),
                remote_python_executable_sha256=hashlib.sha256(
                    Path(sys.executable).resolve(strict=True).read_bytes()
                ).hexdigest(),
            )
            output.close()
            writer_socket.close()
            chunks = []
            while True:
                chunk = reader_socket.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            if not output.closed:
                output.close()
            writer_socket.close()
            reader_socket.close()

    def test_relay_preserves_one_exact_length_hash_frame(self) -> None:
        payload = b'{"test":true}'
        digest = hashlib.sha256(payload).hexdigest()
        self._write_remote(
            "import os,sys\n"
            f"payload={payload!r}\n"
            f"header=b'MOE_GATE_M_EXPORT_V1 {len(payload)} {digest}\\n'\n"
            "os.write(sys.stdout.fileno(),header+payload)\n"
        )
        self.assertEqual(
            self._run(),
            f"MOE_GATE_M_EXPORT_V1 {len(payload)} {digest}\n".encode() + payload,
        )

    def test_standalone_full_receive_install_launch_and_relay_chain(self) -> None:
        allowed = self.root / "vault"
        allowed.mkdir()
        source_application = self.root / "source/application"
        (source_application / "executor").mkdir(parents=True)
        payload = b'{"full_chain":true}'
        digest = hashlib.sha256(payload).hexdigest()
        remote = source_application / "executor/gate_m_remote.py"
        remote.write_text(
            "#!/usr/bin/env python3\n"
            "import os,sys\n"
            f"payload={payload!r}\n"
            f"header=b'MOE_GATE_M_EXPORT_V1 {len(payload)} {digest}\\n'\n"
            "os.write(sys.stdout.fileno(),header+payload)\n",
            encoding="utf-8",
        )
        remote.chmod(0o755)
        for name in (
            "approval.template.json",
            "environment_disclosure_approval.template.json",
            "materialization_approval.template.json",
        ):
            (source_application / name).write_text("{}\n", encoding="utf-8")
        bundle_path = self.root / "full-chain.bundle.json"
        write_bundle(source_application, bundle_path)
        bundle = bundle_path.read_bytes()
        deployment_source = (
            REPO
            / "explorations/moe_cycle_simulator/phase7/application/executor/deployment_bootstrap.py"
        ).read_bytes() + (
            b"\n# CPU fixture: keep the production bootstrap body and replace only "
            b"the mount probe.\n"
            b"def _mount_identity(_root):\n"
            b"    return {'mount_identity_sha256': '1' * 64}\n"
        )
        project = allowed / "phase7-gate-m-full-chain"
        target = (
            project
            / "packages/materialization/repo/explorations/moe_cycle_simulator/phase7/application"
        )
        receipt = project / "packages/materialization/deployment_receipt.json"
        command = [
            sys.executable,
            "-I",
            "-B",
            str(
                REPO
                / "explorations/moe_cycle_simulator/phase7/application/executor/gate_m_bootstrap.py"
            ),
            "--allowed-root",
            str(allowed),
            "--project-root",
            str(project),
            "--expected-mount-identity-sha256",
            "1" * 64,
            "--incoming",
            str(project / "incoming/application.bundle.json"),
            "--target",
            str(target),
            "--receipt",
            str(receipt),
            "--expected-size",
            str(len(bundle)),
            "--expected-sha256",
            hashlib.sha256(bundle).hexdigest(),
            "--deployment-bootstrap-source-base64",
            base64.b64encode(deployment_source).decode("ascii"),
            "--deployment-bootstrap-source-sha256",
            hashlib.sha256(deployment_source).hexdigest(),
            "--remote-timeout-executable",
            str(Path(sys.executable).resolve(strict=True)),
            "--remote-timeout-executable-sha256",
            hashlib.sha256(
                Path(sys.executable).resolve(strict=True).read_bytes()
            ).hexdigest(),
            "--remote-python-executable",
            str(Path(sys.executable).resolve(strict=True)),
            "--remote-python-executable-sha256",
            hashlib.sha256(
                Path(sys.executable).resolve(strict=True).read_bytes()
            ).hexdigest(),
            "--materialization-evidence-root",
            str(project / "evidence/materialization"),
        ]
        for relative in (
            "evidence",
            "incoming",
            "packages/materialization/repo/explorations/moe_cycle_simulator/phase7",
        ):
            command.extend(("--prepare-relative-dir", relative))
        completed = subprocess.run(
            command,
            cwd=self.root,
            env={"PATH": os.environ.get("PATH", "")},
            input=bundle,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(
            completed.stdout,
            f"MOE_GATE_M_EXPORT_V1 {len(payload)} {digest}\n".encode() + payload,
        )
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o444)

    def test_relay_rejects_short_extra_and_hash_drift(self) -> None:
        payload = b"abcdef"
        digest = hashlib.sha256(payload).hexdigest()
        bodies = (
            f"import os,sys;os.write(sys.stdout.fileno(),b'MOE_GATE_M_EXPORT_V1 7 {digest}\\n'+{payload!r})\n",
            f"import os,sys;os.write(sys.stdout.fileno(),b'MOE_GATE_M_EXPORT_V1 5 {digest}\\n'+{payload!r})\n",
            f"import os,sys;os.write(sys.stdout.fileno(),b'MOE_GATE_M_EXPORT_V1 6 {'0' * 64}\\n'+{payload!r})\n",
        )
        for body in bodies:
            with self.subTest():
                self._write_remote(body)
                with self.assertRaises(GateMBootstrapError):
                    self._run()

    def test_bootstrap_source_imports_only_standard_library(self) -> None:
        source = (
            REPO
            / "explorations/moe_cycle_simulator/phase7/application/executor/gate_m_bootstrap.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            node.names[0].name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        }
        imports.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertLessEqual(
            imports,
            {
                "__future__",
                "argparse",
                "base64",
                "binascii",
                "ctypes",
                "hashlib",
                "os",
                "selectors",
                "signal",
                "subprocess",
                "sys",
                "time",
                "pathlib",
                "typing",
            },
        )
        self.assertNotIn("torch", source)
        self.assertNotIn("import vllm", source)

    def test_remote_executable_identity_ignores_path_shadow_and_rejects_drift(self) -> None:
        executable = self.root / "approved-python"
        shutil.copy2(Path(sys.executable).resolve(strict=True), executable)
        executable.chmod(0o755)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        shadow = self.root / "shadow"
        shadow.mkdir()
        (shadow / "approved-python").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        (shadow / "approved-python").chmod(0o755)
        previous = os.environ.get("PATH")
        os.environ["PATH"] = str(shadow)
        try:
            self.assertEqual(
                _validated_remote_executable(
                    executable, digest, "remote Python executable"
                ),
                executable,
            )
        finally:
            if previous is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = previous
        executable.write_bytes(executable.read_bytes() + b"drift")
        executable.chmod(0o755)
        with self.assertRaisesRegex(GateMBootstrapError, "identity differs"):
            _validated_remote_executable(
                executable, digest, "remote Python executable"
            )

    def test_setsid_descendant_is_killed_and_blocks_completion(self) -> None:
        _enable_subreaper()
        pidfile = self.root / "escaped.pid"
        payload = b'{"test":true}'
        digest = hashlib.sha256(payload).hexdigest()
        self._write_remote(
            "import os,sys,time\n"
            "child=os.fork()\n"
            "if child==0:\n"
            " os.setsid()\n"
            f" open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
            " time.sleep(30)\n"
            " os._exit(0)\n"
            f"payload={payload!r}\n"
            f"header=b'MOE_GATE_M_EXPORT_V1 {len(payload)} {digest}\\n'\n"
            "os.write(sys.stdout.fileno(),header+payload)\n"
        )
        with self.assertRaisesRegex(GateMBootstrapError, "deadline|descendant"):
            self._run()
        escaped = int(pidfile.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while Path(f"/proc/{escaped}").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(Path(f"/proc/{escaped}").exists())


if __name__ == "__main__":
    unittest.main()
