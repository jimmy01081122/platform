from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scheduler import g25_runtime_closure as closure


class G25RuntimeClosureTests(unittest.TestCase):
    @staticmethod
    def clean_environment() -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin:/usr/lib/wsl/lib",
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_isolated(self, body: str) -> subprocess.CompletedProcess[str]:
        source = (
            "import sys\n"
            f"sys.path.insert(0, {str(closure.PACKAGE_ROOT)!r})\n"
            "from scheduler import g25_runtime_closure as closure\n"
            + body
        )
        return subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", "-X", "utf8", "-c", source],
            env=self.clean_environment(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_static_elf_loader_and_system_input_closure_passes(self) -> None:
        with patch.dict(os.environ, self.clean_environment(), clear=True):
            attestation = closure.verify_static_system_closure()
        self.assertEqual(30, attestation["system_file_count"])
        self.assertGreater(len(attestation["dependency_edges"]), 0)
        self.assertEqual(
            [
                "/usr/bin/env", "/usr/bin/git", "/usr/bin/python3.10",
                "/usr/bin/timeout", "/usr/lib/wsl/lib/nvidia-smi",
            ],
            attestation["static_executables"],
        )

    def test_attested_python_argv_factory_has_one_loader_isolation_prefix(self) -> None:
        argv = closure.build_attested_python_argv(
            "bf16-probe", package_root=closure.PACKAGE_ROOT,
            python_executable=Path("/usr/bin/python3"),
        )
        self.assertEqual(
            [
                "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
                "--inhibit-cache", "/usr/bin/python3",
                "-I", "-S", "-B", "-X", "utf8",
                str(closure.PACKAGE_ROOT / "scripts/g25_isolated_bootstrap.py"),
                "bf16-probe",
            ],
            argv,
        )

    def test_direct_interpreter_cannot_claim_attested_loader_entry(self) -> None:
        source = (
            "import sys\n"
            f"sys.path.insert(0, {str(closure.PACKAGE_ROOT)!r})\n"
            "from scheduler.g25_runtime_closure import "
            "verify_current_attested_python_argv\n"
            "verify_current_attested_python_argv('bf16-probe')\n"
        )
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", "-X", "utf8", "-c", source],
            env=self.clean_environment(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("bypassed the attested loader", result.stderr)

    def test_forbidden_loader_and_locale_environment_is_fail_closed(self) -> None:
        _inventory, contract = closure.load_system_closure()
        for name in contract["forbidden_environment"]:
            with self.subTest(name=name), patch.dict(
                os.environ, {name: "/tmp/injected"}, clear=True
            ):
                with self.assertRaisesRegex(
                    closure.RuntimeClosureError, "forbidden.*environment"
                ):
                    closure.verify_forbidden_environment(contract)

    def test_static_input_hash_drift_and_preload_presence_are_rejected(self) -> None:
        real_system_files = closure._system_files

        def drifted(inventory):
            values = real_system_files(inventory)
            first = next(iter(values))
            values[first] = "0" * 64
            return values

        with patch.dict(
            os.environ, self.clean_environment(), clear=True
        ), patch.object(closure, "_system_files", side_effect=drifted):
            with self.assertRaisesRegex(closure.RuntimeClosureError, "input differs"):
                closure.verify_static_system_closure()

        original_exists = Path.exists

        def injected_preload(path):
            if str(path) == "/etc/ld.so.preload":
                return True
            return original_exists(path)

        with patch.dict(
            os.environ, self.clean_environment(), clear=True
        ), patch.object(Path, "exists", new=injected_preload):
            with self.assertRaisesRegex(closure.RuntimeClosureError, "must remain absent"):
                closure.verify_static_system_closure()

    def test_live_loaded_set_rejects_unbound_and_deleted_mappings(self) -> None:
        unbound = self.run_isolated(
            "import mmap, os, tempfile\n"
            "from pathlib import Path\n"
            "with tempfile.TemporaryDirectory() as temporary:\n"
            " path = Path(temporary) / 'rogue-mapping.bin'\n"
            " path.write_bytes(b'\\0' * mmap.PAGESIZE)\n"
            " descriptor = os.open(path, os.O_RDONLY)\n"
            " mapped = mmap.mmap(descriptor, mmap.PAGESIZE, flags=mmap.MAP_PRIVATE, "
            "prot=mmap.PROT_READ | mmap.PROT_EXEC)\n"
            " os.close(descriptor)\n"
            " try:\n"
            "  closure.verify_live_loaded_closure('bootstrap')\n"
            " except closure.RuntimeClosureError as error:\n"
            "  print(error)\n"
            " finally:\n"
            "  mapped.close()\n"
        )
        self.assertEqual(0, unbound.returncode, unbound.stderr)
        self.assertIn("unbound file-backed mapping", unbound.stdout)

        deleted = self.run_isolated(
            "import mmap, os, tempfile\n"
            "from pathlib import Path\n"
            "with tempfile.TemporaryDirectory() as temporary:\n"
            " path = Path(temporary) / 'rogue-mapping.bin'\n"
            " path.write_bytes(b'\\0' * mmap.PAGESIZE)\n"
            " descriptor = os.open(path, os.O_RDONLY)\n"
            " mapped = mmap.mmap(descriptor, mmap.PAGESIZE, access=mmap.ACCESS_READ)\n"
            " os.close(descriptor)\n"
            " path.unlink()\n"
            " try:\n"
            "  closure.verify_live_loaded_closure('bootstrap')\n"
            " except closure.RuntimeClosureError as error:\n"
            "  print(error)\n"
            " finally:\n"
            "  mapped.close()\n"
        )
        self.assertEqual(0, deleted.returncode, deleted.stderr)
        self.assertIn("deleted file remains mapped", deleted.stdout)

    def test_live_dependency_graph_rejects_unresolved_needed_object(self) -> None:
        result = self.run_isolated(
            "from pathlib import Path\n"
            "original = closure.parse_elf_identity\n"
            "def injected(path):\n"
            " value = original(path)\n"
            " if path == Path('/usr/bin/python3.10') and value is not None:\n"
            "  value = dict(value)\n"
            "  value['needed'] = [*value['needed'], 'libnot-approved.so']\n"
            " return value\n"
            "closure.parse_elf_identity = injected\n"
            "try:\n"
            " closure.verify_live_loaded_closure('bootstrap')\n"
            "except closure.RuntimeClosureError as error:\n"
            " print(error)\n"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("does not resolve uniquely", result.stdout)


if __name__ == "__main__":
    unittest.main()
