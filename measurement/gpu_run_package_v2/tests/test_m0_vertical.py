from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))
M0_EVIDENCE_SKIP = "external measured evidence not bundled"

from canonicalize_trace import canonicalize_m0  # noqa: E402
from system_simulate import simulate  # noqa: E402
from workload_expand import expand_m0  # noqa: E402


def m0_evidence_root() -> Path:
    value = os.environ.get("M0_EVIDENCE_ROOT")
    if not value:
        raise unittest.SkipTest(M0_EVIDENCE_SKIP)
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise unittest.SkipTest(f"{M0_EVIDENCE_SKIP}: {root}")
    return root


class M0VerticalSliceTests(unittest.TestCase):
    def pipeline(self, root: Path, output: Path) -> tuple[dict, list[dict], dict]:
        routing, records = canonicalize_m0(
            root, output / "m0_moe_routing.json", output / "benchmark_records.jsonl"
        )
        expand_m0(
            output / "m0_moe_routing.json", output / "m0_system_events.json"
        )
        result = simulate(
            output / "m0_system_events.json", output / "m0_route_analysis.json"
        )
        return routing, records, result

    def test_measured_4_plus_4_pipeline_preserves_routes_and_claim_boundaries(self) -> None:
        m0_root = m0_evidence_root()
        raw_before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                m0_root / "p0/native.jsonl",
                m0_root / "p2/native.jsonl",
                m0_root / "p3/native.jsonl",
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            routing, records, result = self.pipeline(m0_root, Path(temporary))
        self.assertEqual(8, len(records))
        self.assertEqual({"T1", "T2"}, {event["suite_class"] for event in routing["events"]})
        self.assertEqual(
            {"gsm8k": 4, "mmlu": 4},
            {
                benchmark: sum(record["benchmark_id"] == benchmark for record in records)
                for benchmark in ("gsm8k", "mmlu")
            },
        )
        self.assertTrue(all(
            event["evidence"]["class"]
            == "measured_gpu_gate_logits_reconstructed_topk"
            and event["evidence"]["synthetic"] is False
            and event["routing_semantics"] == "reconstructed_topk_from_gate_logits"
            and event["actual_dispatch_verified"] is False
            and event["drop_overflow_unavailable"] is True
            for event in routing["events"]
        ))
        self.assertGreater(result["route_statistics"]["assignment_count"], 0)
        self.assertEqual(8, result["residency_estimate"]["misses"])
        self.assertIsNone(result["latency"]["predicted"])
        self.assertFalse(result["provenance"]["hardware_latency_claim"])
        self.assertTrue(all(
            hashlib.sha256(path.read_bytes()).hexdigest() == digest
            for path, digest in raw_before.items()
        ))

    def test_native_checksum_tamper_is_rejected(self) -> None:
        m0_root = m0_evidence_root()
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "m0"
            shutil.copytree(m0_root, copied)
            with (copied / "p2/native.jsonl").open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                canonicalize_m0(
                    copied, copied / "route.json", copied / "records.jsonl"
                )

    def test_cross_pass_output_drift_is_rejected_after_checksum_refresh(self) -> None:
        m0_root = m0_evidence_root()
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "m0"
            shutil.copytree(m0_root, copied)
            path = copied / "p3/native.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                event = json.loads(line)
                if event.get("event") == "sample" and event.get("run_kind") == "measured":
                    event["output_hash"] = "0" * 64
                    lines[index] = json.dumps(event, separators=(",", ":"))
                    break
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            checksums_path = copied / "checksums.json"
            checksums = json.loads(checksums_path.read_text())
            for item in checksums["files"]:
                if item["path"] == "p3/native.jsonl":
                    item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                    item["bytes"] = path.stat().st_size
            checksums_path.write_text(json.dumps(checksums))
            with self.assertRaisesRegex(ValueError, "cross-pass output_hash mismatch"):
                canonicalize_m0(
                    copied, copied / "route.json", copied / "records.jsonl"
                )


if __name__ == "__main__":
    unittest.main()
