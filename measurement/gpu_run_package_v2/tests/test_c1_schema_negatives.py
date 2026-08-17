from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def validator(name: str):
    schema = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
    validator_type = getattr(
        jsonschema, "Draft202012Validator", jsonschema.Draft7Validator
    )
    return validator_type(schema)


class C1SchemaNegativeTests(unittest.TestCase):
    @staticmethod
    def valid_routing() -> dict:
        return {
            "schema_version": "c1-routing-event-v1",
            "event_key": "a" * 64,
            "execution_alignment_key": "b" * 64,
            "request_id": "r",
            "phase": "prefill",
            "call_index": 0,
            "generation_step": 0,
            "input_sequence_length": 1,
            "token_index": 0,
            "global_token_position": 0,
            "layer_id": 0,
            "router_module": "router",
            "selected_experts": list(range(8)),
            "top_k": 8,
            "actual_dispatch": True,
            "actual_dispatch_verified": True,
            "router_logits": [0.0] * 32,
            "routing_weights": [0.125] * 8,
            "gate_dtype": "fp32",
            "generation_input_token_count": 1,
            "generation_output_token_count": 1,
            "unavailable_reasons": {},
        }

    def test_routing_null_without_reason_is_rejected(self):
        value = {
            "schema_version": "c1-routing-event-v1",
            "event_key": "a" * 64, "execution_alignment_key": "b" * 64,
            "request_id": "r", "phase": "decode", "generation_step": 0,
            "token_index": 0, "layer_id": 0, "router_module": "router",
            "selected_experts": [1], "top_k": 1, "actual_dispatch": True,
            "router_logits": None, "routing_weights": None,
            "unavailable_reasons": {},
        }
        self.assertTrue(list(validator("c1_routing.schema.json").iter_errors(value)))

    def test_routing_rejects_expert_range_duplicates_and_wrong_topk(self):
        for field, value in (
            ("selected_experts", [0, 1, 2, 3, 4, 5, 6, 32]),
            ("selected_experts", [0, 1, 2, 3, 4, 5, 6, 6]),
            ("top_k", 7),
        ):
            row = self.valid_routing()
            row[field] = value
            with self.subTest(field=field, value=value):
                self.assertTrue(list(
                    validator("c1_routing.schema.json").iter_errors(row)
                ))

    def test_routing_rejects_infinity_and_nan_is_not_serializable_json(self):
        row = self.valid_routing()
        row["router_logits"][0] = math.inf
        self.assertTrue(list(
            validator("c1_routing.schema.json").iter_errors(row)
        ))
        row["router_logits"][0] = math.nan
        with self.assertRaises(ValueError):
            json.dumps(row, allow_nan=False)

    def test_routing_rejects_unrestricted_gate_dtype(self):
        row = self.valid_routing()
        row["gate_dtype"] = "float64"
        self.assertTrue(list(
            validator("c1_routing.schema.json").iter_errors(row)
        ))

    def test_system_ir_rejects_fabricated_physical_timing(self):
        value = {
            "schema_version": "c1-system-event-v1",
            "event_type": "EXPERT_ROUTE", "event_key": "a" * 64,
            "request_id": "r", "token_id": 0, "layer_id": 0, "expert_id": 1,
            "timestamp_class": "logical", "duration_ns": 100,
            "provenance": {"routing": "measured", "timing": "unassigned"},
        }
        self.assertTrue(list(validator("c1_system.schema.json").iter_errors(value)))

    def test_unavailable_pass_requires_reason(self):
        value = {
            "schema_version": "c1-pass-v1", "work_unit_id": "u",
            "pass_id": "P1", "status": "UNAVAILABLE",
            "execution_alignment_key": "a" * 64, "raw_artifacts": [],
        }
        self.assertTrue(list(validator("c1_pass.schema.json").iter_errors(value)))


if __name__ == "__main__":
    unittest.main()
