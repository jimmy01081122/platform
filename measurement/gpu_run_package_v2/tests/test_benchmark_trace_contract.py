from __future__ import annotations

import unittest

from collectors.trace_contract import (
    build_alignment_key, canonical_hash, validate_benchmark_trace_record,
)


def record() -> dict:
    generation = {"temperature": 0.0, "max_new_tokens": 8}
    alignment = {
        "suite_id": "suite",
        "sample_id": "sample",
        "model_revision": "model-rev",
        "generation_config_hash": canonical_hash(generation),
        "seed": 7,
        "request_id": "request-0",
        "token_index": 0,
        "layer_index": 0,
        "repetition_index": 0,
        "session_id": "session",
    }
    alignment["alignment_key"] = build_alignment_key(alignment)
    return {
        "schema_version": "benchmark-trace-record-v1",
        "record_id": "record",
        "model_id": "model",
        "model_revision": "model-rev",
        "weights_revision": "weights-rev",
        "tokenizer_revision": "tokenizer-rev",
        "suite_id": "suite",
        "benchmark_id": "benchmark",
        "sample_id": "sample",
        "template_id": "template",
        "template_hash": "a" * 64,
        "prompt_hash": "b" * 64,
        "generation_config": generation,
        "generation_config_hash": canonical_hash(generation),
        "actual_tokens": {
            "prompt": 4,
            "generated": 2,
            "total": 6,
            "prompt_token_ids": [1, 2, 3, 4],
            "generated_token_ids": [5, 6],
            "token_ids_hash": canonical_hash({
                "prompt_token_ids": [1, 2, 3, 4],
                "generated_token_ids": [5, 6],
            }),
        },
        "output_hash": "c" * 64,
        "quality": {"status": "pass", "score": 1.0},
        "serving_runtime": "fixture-serving",
        "serving": {"version": "1"},
        "hardware_id": "gpu-0",
        "hardware": {"name": "fixture"},
        "repetition_index": 0,
        "request_index": 0,
        "profiler_pass": "P0",
        "native_format": "fixture-native",
        "native_paths": ["raw_traces/fixture.raw"],
        "native_sha256": "d" * 64,
        "native_checksums": {"raw_traces/fixture.raw": "d" * 64},
        "environment_hash": "e" * 64,
        "alignment": alignment,
        "completeness": {"complete": True, "missing_fields": [], "truncated": False},
    }


class BenchmarkTraceContractTests(unittest.TestCase):
    def test_complete_record_satisfies_semantic_contract(self) -> None:
        self.assertEqual([], validate_benchmark_trace_record(record()))

    def test_actual_token_total_is_checked(self) -> None:
        value = record()
        value["actual_tokens"]["total"] = 99
        self.assertIn(
            "actual_tokens.total must equal prompt + generated",
            validate_benchmark_trace_record(value),
        )

    def test_alignment_key_detects_cross_pass_drift(self) -> None:
        value = record()
        value["alignment"]["layer_index"] = 1
        self.assertIn(
            "alignment.alignment_key does not match canonical fields",
            validate_benchmark_trace_record(value),
        )

    def test_complete_record_cannot_hide_missing_fields(self) -> None:
        value = record()
        value["completeness"]["missing_fields"] = ["quality.score"]
        self.assertIn(
            "complete record cannot list missing_fields",
            validate_benchmark_trace_record(value),
        )


if __name__ == "__main__":
    unittest.main()
