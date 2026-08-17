from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path

from explorations.moe_cycle_simulator.phase7.run_cpu_validation import (
    resolve_source_identity,
)


class RunCpuValidationSourceIdentityTests(unittest.TestCase):
    def test_archive_requires_and_retains_exact_commit_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            commit = "1" * 40
            tree = "2" * 40
            self.assertEqual(
                resolve_source_identity(
                    Path(temp),
                    explicit_commit_sha1=commit,
                    explicit_tree_sha1=tree,
                ),
                (commit, tree, "EXPLICIT_ARCHIVE_BINDING"),
            )

    def test_partial_or_malformed_explicit_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "supplied together"):
                resolve_source_identity(
                    root,
                    explicit_commit_sha1="1" * 40,
                    explicit_tree_sha1=None,
                )
            with self.assertRaisesRegex(ValueError, "40 lowercase hex"):
                resolve_source_identity(
                    root,
                    explicit_commit_sha1="A" * 40,
                    explicit_tree_sha1="2" * 40,
                )

    def test_git_checkout_rejects_a_false_explicit_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("source\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Phase7 Test",
                    "-c",
                    "user.email=phase7-test@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                cwd=repo,
                check=True,
            )
            with self.assertRaisesRegex(ValueError, "does not match Git checkout"):
                resolve_source_identity(
                    repo,
                    explicit_commit_sha1="1" * 40,
                    explicit_tree_sha1="2" * 40,
                )


if __name__ == "__main__":
    unittest.main()
