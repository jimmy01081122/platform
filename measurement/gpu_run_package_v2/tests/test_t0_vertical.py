from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))

from canonicalize_trace import canonicalize  # noqa: E402
from system_simulate import simulate  # noqa: E402
from t0_fixture_runner import run  # noqa: E402
from trace_package_verify import COMPLETE, verify_root  # noqa: E402


class T0VerticalSliceTests(unittest.TestCase):
    fixture = PACKAGE_ROOT / "fixtures/t0/t0_fixture.json"

    def build(self, temporary: str, name: str = "t0") -> tuple[Path, dict]:
        root = Path(temporary) / name
        return root, run(self.fixture, root)

    def test_runner_to_simulation_is_deterministic_and_provenance_complete(self) -> None:
        expected = json.loads(self.fixture.read_text())["expected"]
        with tempfile.TemporaryDirectory() as temporary:
            first, first_result = self.build(temporary, "first")
            second, second_result = self.build(temporary, "second")
            code, report = verify_root(first)
            self.assertEqual(COMPLETE, code, report)
            self.assertEqual(first_result["summary"], second_result["summary"])
            self.assertEqual(first_result, second_result)
            self.assertEqual(expected["latency_ns"], first_result["summary"]["latency_ns"])
            self.assertEqual(expected["dma_wait_ns"], first_result["summary"]["dma_wait_ns"])
            self.assertEqual(
                expected["backpressure_events"],
                first_result["summary"]["dma_backpressure_events"],
            )
            self.assertEqual(
                expected["residency_high_water_experts"],
                first_result["summary"]["residency_high_water_experts"],
            )
            self.assertEqual(
                (first / "canonical_traces/moe_routing.json").read_bytes(),
                (second / "canonical_traces/moe_routing.json").read_bytes(),
            )
            manifests = list(first.glob("runs/*/*/runs/*/PASS_MANIFEST.json"))
            self.assertEqual(7, len(manifests))
            self.assertEqual(7, len(list((first / "raw_traces/native").glob("*.jsonl"))))

    def test_p4_is_only_a_synthetic_unavailable_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.build(temporary)
            events = [
                json.loads(line)
                for line in (root / "raw_traces/native/p4.jsonl").read_text().splitlines()
            ]
            self.assertEqual(1, len(events))
            self.assertFalse(events[0]["counters_available"])
            self.assertEqual("synthetic_marker", events[0]["evidence"]["class"])
            self.assertFalse(events[0]["evidence"]["measurement_claim"])

    def test_tampered_native_trace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.build(temporary)
            raw = root / "raw_traces/native/p2.jsonl"
            raw.write_text(raw.read_text() + "{}\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                canonicalize(
                    root / "T0_CAPTURE_MANIFEST.json",
                    root / "tampered-routing.json",
                    root / "tampered-system.json",
                )

    def test_future_dependency_ordering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _ = self.build(temporary)
            ir_path = root / "canonical_traces/expanded_system_events.json"
            ir = json.loads(ir_path.read_text())
            ir["events"][0]["depends_on"] = [ir["events"][1]["event_id"]]
            bad = root / "bad-order.json"
            bad.write_text(json.dumps(ir))
            with self.assertRaisesRegex(ValueError, "ordering violation"):
                simulate(bad, root / "must-not-exist.json")


if __name__ == "__main__":
    unittest.main()
