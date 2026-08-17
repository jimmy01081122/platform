#!/usr/bin/env python3
"""Execute the Phase 7 CPU-only unittest suite."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TEST_ROOT.parents[3]


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    suite = unittest.defaultTestLoader.discover(
        str(TEST_ROOT), pattern="test_*.py", top_level_dir=str(REPO_ROOT)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
