from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from c1_canonicalize import canonicalize
from c1_system_ir import build_system_ir


class C1CanonicalTests(unittest.TestCase):
    def test_conversion_is_deterministic_and_raw_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "routing.jsonl"
            rows = []
            for token in (1, 0):
                rows.append({
                    "schema_version": "c1-routing-event-v1",
                    "event_key": str(token + 1) * 64,
                    "execution_alignment_key": "a" * 64,
                    "request_id": "r", "phase": "decode",
                    "generation_step": token, "token_index": token,
                    "layer_id": 0, "router_module": "router",
                    "dispatch_index": 0,
                    "selected_experts": list(range(8)),
                    "top_k": 8, "actual_dispatch": True,
                    "router_logits": None, "routing_weights": None,
                    "gate_dtype": "unknown",
                    "unavailable_reasons": {
                        "router_logits": "not exposed",
                        "routing_weights": "not exposed",
                    },
                })
            raw.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            before = raw.read_bytes()
            first = root / "first.json"
            second = root / "second.json"
            canonicalize(raw, first)
            canonicalize(raw, second)
            self.assertEqual(before, raw.read_bytes())
            self.assertEqual(first.read_bytes(), second.read_bytes())
            system = build_system_ir(first, root / "system.json")
            self.assertEqual("logical_only", system["timing_policy"])
            self.assertTrue(all(
                item["timestamp_class"] == "logical" for item in system["events"]
            ))
            self.assertTrue(all("duration_ns" not in item for item in system["events"]))

    def test_rejects_adversarial_routing_semantics(self):
        base = {
            "schema_version": "c1-routing-event-v1",
            "event_key": "b" * 64,
            "execution_alignment_key": "a" * 64,
            "request_id": "r", "phase": "decode", "generation_step": 0,
            "token_index": 0, "layer_id": 0, "router_module": "router",
            "dispatch_index": 0, "selected_experts": list(range(8)),
            "top_k": 8, "actual_dispatch": True,
            "router_logits": None, "routing_weights": [0.125] * 8,
            "gate_dtype": "fp32",
            "unavailable_reasons": {"router_logits": "not exposed"},
        }
        mutations = {
            "nan": {"routing_weights": [math.nan] + [0.125] * 7},
            "infinity": {"routing_weights": [math.inf] + [0.125] * 7},
            "expert99": {"selected_experts": list(range(7)) + [99]},
            "duplicate": {"selected_experts": list(range(7)) + [6]},
            "topk": {"top_k": 7},
            "weight_sum": {"routing_weights": [0.2] * 8},
            "weight_range": {
                "routing_weights": [-0.1, 0.2, 0.2, 0.2, 0.2, 0.1, 0.1, 0.1]
            },
            "gate_dtype": {"gate_dtype": "float64"},
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw = root / "routing.jsonl"
                raw.write_text(
                    json.dumps({**base, **mutation}) + "\n", encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    canonicalize(raw, root / "canonical.json")

    def test_dtype_specific_quantized_weight_sum_gate(self):
        base = {
            "schema_version": "c1-routing-event-v1",
            "event_key": "b" * 64,
            "execution_alignment_key": "a" * 64,
            "request_id": "r", "phase": "decode", "generation_step": 0,
            "token_index": 0, "layer_id": 0, "router_module": "router",
            "dispatch_index": 0, "selected_experts": list(range(8)),
            "top_k": 8, "actual_dispatch": True,
            "router_logits": None,
            "unavailable_reasons": {"router_logits": "not exposed"},
        }
        valid = (
            (
                "bf16",
                [
                    0.37890625, 0.224609375, 0.02978515625,
                    0.01129150390625, 0.05126953125, 0.027099609375,
                    0.263671875, 0.01470947265625,
                ],
            ),
            (
                "fp16",
                [
                    0.378173828125, 0.22509765625, 0.02972412109375,
                    0.01126861572265625, 0.051239013671875,
                    0.027069091796875, 0.2626953125, 0.01470947265625,
                ],
            ),
            ("bf16", [0.125] * 7 + [0.1299]),
            ("fp16", [0.125] * 7 + [0.1259]),
            ("fp32", [0.125] * 8),
            ("unknown", [0.125] * 8),
        )
        invalid = (
            ("bf16", [0.125] * 7 + [0.131]),
            ("fp16", [0.125] * 7 + [0.1261]),
            ("fp32", [0.125] * 7 + [0.12502]),
            ("unknown", [0.125] * 7 + [0.12502]),
        )
        for should_pass, cases in ((True, valid), (False, invalid)):
            for gate_dtype, weights in cases:
                with self.subTest(
                    should_pass=should_pass, gate_dtype=gate_dtype
                ), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    raw = root / "routing.jsonl"
                    raw.write_text(json.dumps({
                        **base,
                        "gate_dtype": gate_dtype,
                        "routing_weights": weights,
                    }) + "\n", encoding="utf-8")
                    if should_pass:
                        canonicalize(raw, root / "canonical.json")
                    else:
                        with self.assertRaises(ValueError):
                            canonicalize(raw, root / "canonical.json")


if __name__ == "__main__":
    unittest.main()
