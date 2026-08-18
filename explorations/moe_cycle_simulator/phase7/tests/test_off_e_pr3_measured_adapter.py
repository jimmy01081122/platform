"""Stage A2 regression tests for the OFF-E-PR3 measured -> Canonical IR adapter.

These exercise the measured adapter on the read-only evidence. They build a
small (event-capped) bundle for speed and assert that it passes the phase2 IR1
contract, that the mandated byte conservation holds on the raw, that routing is
traceable, and that the retained mock adapter is byte-for-byte intact.
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
ADAPTERS = REPO / "explorations/moe_cycle_simulator/phase7/adapters"
sys.path.insert(0, str(ADAPTERS))
sys.path.insert(0, str(REPO / "explorations/moe_cycle_simulator/phase2"))
sys.path.insert(0, str(REPO / "explorations/moe_cycle_simulator/tools"))

import off_e_pr3_measured_adapter as adapter  # noqa: E402
from canonical_ir import IR_KINDS, validate_records  # noqa: E402
from contract_runtime import canonical_bytes  # noqa: E402

# Red line 5: the mock adapter is retained as a fixture and must not change.
MOCK_ADAPTER = ADAPTERS / "vllm_mock_adapter.py"
MOCK_ADAPTER_SHA256 = (
    "32f1c0a7268dd021e3d01f3a49666950240b797a4b650229892521052efd5a6c"
)

EVIDENCE_ROOT = REPO / "evidence"


def _load_points():
    point_dirs = adapter.discover_points(EVIDENCE_ROOT)
    return [adapter.load_point(d) for d in point_dirs]


class MockAdapterIntactTests(unittest.TestCase):
    def test_mock_adapter_unchanged(self) -> None:
        self.assertTrue(MOCK_ADAPTER.exists())
        digest = hashlib.sha256(MOCK_ADAPTER.read_bytes()).hexdigest()
        self.assertEqual(digest, MOCK_ADAPTER_SHA256)


class OffEPr3MeasuredAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.points = _load_points()
        except FileNotFoundError:
            raise unittest.SkipTest("OFF-E-PR3 evidence not present")
        claim = adapter.build_claim_boundary()
        cls.claim_hash = hashlib.sha256(canonical_bytes(claim)).hexdigest()
        cls.records, cls.variant = adapter.build_records(
            cls.points, cls.claim_hash, max_events_per_point=2)

    def test_all_fifteen_points_discovered(self) -> None:
        self.assertEqual(len(self.points), 15)

    def test_bundle_passes_ir1(self) -> None:
        # raises CanonicalIRError on any schema / cross-IR / conservation break
        validate_records(self.records, bundle_evidence_class="MEASURED")

    def test_all_nine_kinds_present(self) -> None:
        kinds = {r["ir_kind"] for r in self.records}
        self.assertEqual(kinds, IR_KINDS)

    def test_expected_per_kind_counts(self) -> None:
        counts: dict[str, int] = {}
        for r in self.records:
            counts[r["ir_kind"]] = counts.get(r["ir_kind"], 0) + 1
        self.assertEqual(counts["ModelIR"], 1)
        self.assertEqual(counts["PlatformIR"], 15)
        self.assertEqual(counts["WorkloadIR"], 15)
        self.assertEqual(counts["PlacementIR"], 15)
        self.assertEqual(counts["ResultIR"], 15)
        self.assertEqual(counts["CalibrationIR"], 15)
        self.assertEqual(counts["RoutingIR"], 32)
        self.assertEqual(counts["ClockAlignmentIR"], 1)

    def test_byte_conservation_and_traceability(self) -> None:
        report = adapter.conservation_report(self.points)
        self.assertTrue(report["all_ok"], report)
        for row in report["points"]:
            self.assertTrue(
                row["checks"][
                    "h2d_bytes == demand_load_count * expert_object_bytes"],
                row["label"])
            self.assertTrue(
                row["checks"]["routing_sha256 traces to routing .npy bytes"],
                row["label"])

    def test_routing_is_aggregate_without_scores(self) -> None:
        routing = [r for r in self.records if r["ir_kind"] == "RoutingIR"]
        for r in routing:
            self.assertEqual(r["payload"]["routing_scope"], "AGGREGATE")
            self.assertIsNone(r["payload"]["canonical_scores"])
            self.assertEqual(
                sum(int(x) for x in r["payload"]["aggregate_expert_demand"]),
                159 * adapter.TOP_K)

    def test_claim_boundary_bound_to_every_record(self) -> None:
        for r in self.records:
            self.assertIn(
                self.claim_hash, r["provenance"]["source_content_ids"],
                r["record_id"])


if __name__ == "__main__":
    unittest.main()
