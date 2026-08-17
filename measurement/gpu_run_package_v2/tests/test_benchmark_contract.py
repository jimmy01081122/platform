from __future__ import annotations

import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))

from benchmark import build_evaluation_points, make_record_id, summary  # noqa: E402
from validate_package import validate_result_contract  # noqa: E402


def measured_row(split: str, operation: str, case: str, **fields: object) -> dict:
    samples = [1.0, 1.1, 0.9]
    return {
        "record_id": make_record_id(split, operation, case),
        "operation": operation,
        "case": case,
        "warmup": 2,
        "outer_repeats": 3,
        "inner_iterations": 10,
        "minimum_inner_seconds": 0.01,
        "repeats_ms": samples,
        "statistics": summary(samples, "ms"),
        **fields,
    }


def result_base(split: str, rows: list[dict]) -> dict:
    return {
        "schema_version": "gpu-benchmark-result-v1",
        "status": "measured",
        "evidence": "measured",
        "metric_scope": "MoE-replay; not full-model TPOT/throughput",
        "split": split,
        "experiment": {"split": split},
        "seed": 20260718,
        "command": ["python3", "scripts/benchmark.py"],
        "package_id": "fixture",
        "package_revision": "fixture-v1",
        "package_manifest_sha256": "a" * 64,
        "checksums_sha256": "b" * 64,
        "device": {"name": "CPU-only contract fixture"},
        "runtime": {"python": sys.version.split()[0]},
        "timestamp_utc": "2026-07-18T00:00:00Z",
        "raw_profiler_output": "profiler.json",
        "raw_benchmarks": rows,
    }


class BenchmarkContractTests(unittest.TestCase):
    def test_record_id_is_deterministic_and_split_scoped(self) -> None:
        first = make_record_id("calibration", "h2d_pinned", "bytes=1024,streams=1")
        self.assertEqual(
            first,
            make_record_id("calibration", "h2d_pinned", "bytes=1024,streams=1"),
        )
        self.assertNotEqual(
            first,
            make_record_id("validation", "h2d_pinned", "bytes=1024,streams=1"),
        )

    def test_calibration_minimal_probe_contract(self) -> None:
        split = "calibration"
        rows = [
            measured_row(
                split, "cpu_runtime", "call",
                calibration_role="cpu_runtime",
            ),
            measured_row(
                split, "selected_expert", "shape",
                calibration_role="gpu_service", phase="decode", concurrency=1,
            ),
            measured_row(
                split, "h2d_pinned", "h2d-1",
                direction="h2d", bytes=1024, copy_streams=1,
                calibration_role="pcie_transfer",
            ),
            measured_row(
                split, "d2h_pinned", "d2h-1",
                direction="d2h", bytes=1024, copy_streams=1,
                calibration_role="pcie_transfer",
            ),
            measured_row(
                split, "h2d_pinned", "h2d-2",
                direction="h2d", bytes=1024, copy_streams=2,
                calibration_role="copy_engine",
            ),
            measured_row(
                split, "device_memory", "memory-1",
                bytes=1024, calibration_role="memory",
            ),
            measured_row(
                split, "device_memory", "memory-2",
                bytes=2048, calibration_role="memory",
            ),
            measured_row(
                split, "queue_depth", "queue-1",
                queue_depth=1, calibration_role="queueing",
            ),
            measured_row(
                split, "queue_depth", "queue-4",
                queue_depth=4, calibration_role="queueing",
            ),
            measured_row(
                split, "contention_fixed_shape", "contention-1",
                probe_family="contention", concurrency=1,
                fixed_expert_tokens=32,
            ),
            measured_row(
                split, "contention_fixed_shape", "contention-4",
                probe_family="contention", concurrency=4,
                fixed_expert_tokens=32, calibration_role="contention",
                base_service_ms=1.0,
            ),
        ]
        self.assertEqual([], validate_result_contract(result_base(split, rows)))

    def test_evaluation_points_use_source_repeat_means(self) -> None:
        split = "validation"
        transfer = measured_row(
            split, "h2d_pinned", "transfer",
            direction="h2d", bytes=1024, copy_streams=1,
            calibration_role="pcie_transfer",
        )
        component = measured_row(
            split, "selected_expert", "component",
            phase="decode", concurrency=1,
        )
        replay = measured_row(
            split, "window_replay", "replay",
            tokens=8, cpu_calls=2,
            gpu_operations={"grouped_gemm": 2, "gather_scatter": 2},
            memory_bytes=4096, queue_depth=1, transfers=[],
            phase="decode", concurrency=1,
            repeats_tokens_per_second=[8000.0, 8100.0, 7900.0],
            throughput_statistics=summary(
                [8000.0, 8100.0, 7900.0], "tokens/s"
            ),
        )
        rows = [transfer, component, replay]
        result = result_base(split, rows)
        result["evaluation_points"] = build_evaluation_points(
            split, rows, "fixture"
        )
        self.assertEqual([], validate_result_contract(result))

        result["evaluation_points"][0]["measured"] = 12345.0
        failures = validate_result_contract(result)
        self.assertTrue(
            any("measured not from source mean" in failure for failure in failures),
            failures,
        )


if __name__ == "__main__":
    unittest.main()
