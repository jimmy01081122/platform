from __future__ import annotations

import unittest

from collectors.c1_contract import CollectorRequest
from collectors.c1_p0 import collect as collect_p0
from collectors.c1_p1 import collect as collect_p1
from collectors.c1_p2 import _normalize_dispatch, collect as collect_p2
from collectors.c1_p3 import collect as collect_p3
from collectors.c1_p5_basic import collect as collect_p5
from collectors.trace_contract import canonical_hash


def execution() -> dict:
    return {
        "suite_version": "v1", "model_revision": "m1",
        "tokenizer_revision": "t1", "benchmark_id": "gsm8k",
        "sample_id": "s1", "prompt_hash": "a" * 64,
        "generation_config_hash": canonical_hash({"max_new_tokens": 2}),
        "seed": 1, "repetition_id": 0, "hardware_session_id": "h1",
    }


class Runner:
    def load_model(self): return None
    def tokenize(self, prompt): return [1, 2]
    def generate(self, tokens, **config):
        return {"input_token_count": 2, "output_token_ids": [3, 4], "ttft_ns": 1}
    def collect_quality_result(self, result): return {"status": "pass"}
    def collect_runtime_metadata(self):
        return {"peak_vram_bytes": 10, "oom": False, "allocator_retries": 0}
    def cleanup(self): return None


class UnavailableProfiler:
    def available(self): return False, "CUDA profiler permission denied"


class Telemetry:
    def sample(self):
        return {
            "sample_index": 0, "monotonic_ns": 1,
            "gpu_utilization_percent": 10.0,
            "unavailable_reasons": {"power_watts": "NVML unavailable"},
        }


class C1CollectorTests(unittest.TestCase):
    def setUp(self):
        self.runner = Runner()
        self.request = CollectorRequest(
            execution(), "prompt", {"max_new_tokens": 2}, "request-1"
        )

    def test_p0_collects_required_baseline(self):
        row = collect_p0(self.runner, self.request).records[0]
        self.assertEqual(2, row["output_token_count"])
        self.assertEqual("pass", row["quality"]["status"])
        self.assertIn("throughput_tokens_per_second", row)
        self.assertEqual(1, row["ttft_ns"])
        self.assertIsNotNone(row["tpot_ns"])
        self.assertEqual({}, row["unavailable_reasons"])

    def test_p0_null_streaming_metrics_have_unavailable_reasons(self):
        class NoStreamingRunner(Runner):
            def generate(self, tokens, **config):
                return {"input_token_count": 2, "output_token_ids": [3]}

        row = collect_p0(NoStreamingRunner(), self.request).records[0]
        self.assertIsNone(row["ttft_ns"])
        self.assertIsNone(row["tpot_ns"])
        self.assertIn("ttft_ns", row["unavailable_reasons"])
        self.assertIn("tpot_ns", row["unavailable_reasons"])

    def test_p1_records_environment_unavailability_without_fake_artifact(self):
        result = collect_p1(self.runner, self.request, UnavailableProfiler())
        self.assertEqual("unavailable_due_to_environment", result.status)
        self.assertEqual([], result.artifacts)

    def test_p2_requires_complete_proven_actual_dispatch(self):
        dispatches = []
        for layer in range(24):
            for call, sequence_length in ((0, 2), (1, 1)):
                for token in range(sequence_length):
                    dispatches.append({
                        "token_index": token,
                        "global_token_position": (
                            token if call == 0 else 2 + call - 1 + token
                        ),
                        "layer_id": layer,
                        "selected_experts": list(range(8)),
                        "router_logits": [0.0] * 32,
                        "routing_weights": [0.125] * 8,
                        "gate_dtype": "fp32",
                        "unavailable_reasons": {},
                        "phase": "prefill" if call == 0 else "decode",
                        "call_index": call,
                        "generation_step": 0 if call == 0 else call - 1,
                        "input_sequence_length": sequence_length,
                        "generation_input_token_count": 2,
                        "generation_output_token_count": 2,
                        "actual_dispatch_verified": True,
                    })
        result = collect_p2(self.runner, self.request, dispatches)
        row = result.records[0]
        self.assertTrue(row["actual_dispatch"])
        self.assertEqual(8, row["top_k"])
        self.assertIn("event_key", row)
        self.assertEqual(72, len(result.records))

    def test_p2_gate_sum_uses_preserved_dtype_tolerance(self):
        base = {
            "token_index": 0,
            "global_token_position": 0,
            "layer_id": 0,
            "selected_experts": list(range(8)),
            "router_logits": [0.0] * 32,
            "routing_weights": [
                0.37890625, 0.224609375, 0.02978515625, 0.01129150390625,
                0.05126953125, 0.027099609375, 0.263671875, 0.01470947265625,
            ],
            "gate_dtype": "bf16",
            "phase": "prefill",
            "call_index": 0,
            "generation_step": 0,
            "input_sequence_length": 1,
            "actual_dispatch_verified": True,
        }
        row = _normalize_dispatch(base, self.request, "a" * 64)
        self.assertEqual("bf16", row["gate_dtype"])
        with self.assertRaises(ValueError):
            _normalize_dispatch(
                {
                    **base,
                    "routing_weights": [0.125] * 7 + [0.131],
                },
                self.request,
                "a" * 64,
            )
        with self.assertRaises(ValueError):
            _normalize_dispatch(
                {**base, "gate_dtype": "float64"},
                self.request,
                "a" * 64,
            )

    def test_p3_and_p5_mark_unavailable_fields(self):
        p3 = collect_p3(self.runner, self.request).records[0]
        self.assertIn("fragmentation", p3["unavailable_reasons"])
        p5 = collect_p5(Telemetry(), self.request, sampling_interval_ms=100).records[0]
        self.assertEqual("observed", p5["fields"]["gpu_utilization_percent"]["status"])
        self.assertEqual(
            "unavailable_due_to_environment", p5["fields"]["power_watts"]["status"]
        )


if __name__ == "__main__":
    unittest.main()
