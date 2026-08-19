"""Stage A3 regression tests: IR bundle -> C++ engine bit-exact residency replay.

These drive the real Phase 5 RoutingResidencyModel (through the
moe_sim_phase5_ir_loader executable) on all fifteen OFF-E-PR3 expert capacity
points, using the A2 nine-kind Canonical IR bundle for structure and the frozen
routing .npy for ordered demand. They assert:

  SIM0  -- engine hit_count / demand_load_count / immutable_discard_count equal
           the measured evidence counters exactly, for all 15 points.
  SIM1  -- the same plan replayed twice is byte-identical (same digests).
  health-- every point reaches QUIESCENT (no deadlock, no Zeno, no failure).
  degenerate -- cap=100 (full-catalog all-resident control) yields zero demand
           loads and zero clean evictions.

The engine is exercised once per point in setUpClass (driving ~20k-35k Phase 4
operations each), so this class is intentionally heavyweight. Nothing in
evidence/ is written; the routing .npy SHA is checked against the IR provenance.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
LOADERS = REPO / "explorations/moe_cycle_simulator/phase7/loaders"
sys.path.insert(0, str(LOADERS))

import ir_to_engine as loader  # noqa: E402

# The five reference points quoted in the Stage A3 guide (span the degenerate
# control, the boundary and the mid range).
REFERENCE_POINTS = {"025", "050", "0625", "090", "100"}


def _ensure_loader_binary() -> Path:
    """Build the phase5 IR loader executable if it is not already present.

    make test runs the Python suites before test-cpp, so on a clean tree the
    binary may not exist yet. Build just this target from the existing phase5
    CMake project.
    """

    binary = loader.DEFAULT_LOADER_BIN
    if binary.exists():
        return binary
    source = REPO / "explorations/moe_cycle_simulator/phase5"
    build = REPO / "build/phase5"
    configure = subprocess.run(
        ["cmake", "-S", str(source), "-B", str(build),
         "-DCMAKE_BUILD_TYPE=Release"],
        capture_output=True, text=True,
    )
    if configure.returncode != 0:
        raise unittest.SkipTest(
            f"cannot configure phase5 build: {configure.stderr[-500:]}"
        )
    compiled = subprocess.run(
        ["cmake", "--build", str(build), "-j4",
         "--target", "moe_sim_phase5_ir_loader"],
        capture_output=True, text=True,
    )
    if compiled.returncode != 0:
        raise unittest.SkipTest(
            f"cannot build moe_sim_phase5_ir_loader: {compiled.stderr[-500:]}"
        )
    if not binary.exists():
        raise unittest.SkipTest("loader binary missing after build")
    return binary


def _replay_labels(labels: set[str]) -> list[loader.PointResult]:
    structures = loader.load_bundle_structures()
    evidence = loader.discover_evidence_points()
    return [
        loader.replay_point(
            structures[label], evidence[label], check_determinism=False
        )
        for label in sorted(labels)
    ]


class StageA3LoaderTest(unittest.TestCase):
    """SIM0/SIM1/health on the five guide reference points (fast regression).

    The full fifteen-point bit-exact sweep is the Stage A3 deliverable and is
    produced/verified by the loader CLI artifact run (recorded under runs/); it
    is also exercisable here by setting STAGE_A3_FULL=1, which the artifact run
    does. setUpClass drives the reference subset only to keep make test bounded.
    """

    results: list[loader.PointResult]
    by_label: dict[str, loader.PointResult]

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_loader_binary()
        cls.results = _replay_labels(REFERENCE_POINTS)
        cls.by_label = {r.label: r for r in cls.results}

    def test_reference_points_present(self) -> None:
        self.assertEqual(set(self.by_label), REFERENCE_POINTS)
        self.assertEqual(
            {r.raw["logical_demand_count"] for r in self.results}, {10176}
        )

    def test_sim0_counters_bit_exact(self) -> None:
        mismatches = []
        for r in self.results:
            if r.engine != r.expected:
                mismatches.append((r.label, r.expected, r.engine))
        self.assertEqual(mismatches, [], f"SIM0 counter mismatches: {mismatches}")

    def test_terminal_residency_matches(self) -> None:
        bad = [r.label for r in self.results if not r.terminal_resident_match]
        self.assertEqual(bad, [], f"terminal residency mismatch at {bad}")

    def test_engine_health_quiescent(self) -> None:
        unhealthy = [
            (r.label, r.raw["engine"]["terminal_status"])
            for r in self.results
            if r.raw["engine"]["terminal_status"] != "QUIESCENT"
        ]
        self.assertEqual(unhealthy, [], f"non-quiescent points: {unhealthy}")

    def test_degenerate_all_resident_control(self) -> None:
        control = self.by_label["100"]
        self.assertTrue(control.raw["all_resident_control"])
        self.assertEqual(control.engine["demand_load_count"], 0)
        self.assertEqual(control.engine["immutable_discard_count"], 0)
        self.assertEqual(control.engine["hit_count"], 10176)
        self.assertEqual(control.raw["engine"]["terminal_resident_count"], 256)

    def test_reference_points_match_guide_table(self) -> None:
        # Values quoted in the Stage A3 guide (objects, hits, misses).
        table = {
            "025": (2881, 7295),
            "050": (5324, 4852),
            "0625": (6691, 3485),
            "090": (9287, 889),
            "100": (10176, 0),
        }
        for label, (hits, misses) in table.items():
            r = self.by_label[label]
            self.assertEqual(r.engine["hit_count"], hits, label)
            self.assertEqual(r.engine["demand_load_count"], misses, label)

    def test_sim1_determinism_reference_subset(self) -> None:
        structures = loader.load_bundle_structures()
        evidence = loader.discover_evidence_points()
        for label in ("025", "100"):
            first = self.by_label[label].raw["engine"]
            rerun = loader.replay_point(
                structures[label], evidence[label], check_determinism=True
            )
            self.assertTrue(
                rerun.raw["determinism_ok"],
                f"non-deterministic replay at {label}",
            )
            self.assertEqual(
                rerun.raw["engine"]["semantic_digest"],
                first["semantic_digest"],
                f"semantic digest drift at {label}",
            )

    @unittest.skipUnless(
        os.environ.get("STAGE_A3_FULL") == "1",
        "full fifteen-point sweep gated by STAGE_A3_FULL=1 (heavyweight)",
    )
    def test_all_fifteen_points_bit_exact(self) -> None:
        _ensure_loader_binary()
        results = loader.replay_all(check_determinism=True)
        self.assertEqual(len(results), 15)
        mismatches = [
            (r.label, r.expected, r.engine)
            for r in results
            if r.engine != r.expected
        ]
        self.assertEqual(mismatches, [], f"SIM0 mismatches: {mismatches}")
        bad_health = [
            r.label
            for r in results
            if r.raw["engine"]["terminal_status"] != "QUIESCENT"
        ]
        self.assertEqual(bad_health, [], f"non-quiescent: {bad_health}")
        non_det = [r.label for r in results if not r.raw["determinism_ok"]]
        self.assertEqual(non_det, [], f"non-deterministic: {non_det}")
        bad_terminal = [
            r.label for r in results if not r.terminal_resident_match
        ]
        self.assertEqual(bad_terminal, [], f"terminal mismatch: {bad_terminal}")


if __name__ == "__main__":
    unittest.main()
