from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import torch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
M0_EVIDENCE_SKIP = "external measured evidence not bundled"

from executable_moe_benchmark import (  # noqa: E402
    JsonlWriter,
    RouterCapture,
    capacity_boundary_failure,
    model_snapshot_inventory,
    native_sample_results,
    select_smoke_samples,
    stop_reason,
    verify_cross_pass,
)


def m0_evidence_root() -> Path:
    value = os.environ.get("M0_EVIDENCE_ROOT")
    if not value:
        raise unittest.SkipTest(M0_EVIDENCE_SKIP)
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise unittest.SkipTest(f"{M0_EVIDENCE_SKIP}: {root}")
    return root


class _Mlp(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.gate = torch.nn.Linear(3, 4, bias=False)

    def forward(self, value):
        return self.gate(value)


class _Layer(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.mlp = _Mlp()


class _Backbone(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.layers = torch.nn.ModuleList([_Layer(), _Layer()])


class _FakeModel(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.model = _Backbone()


class ExecutableMoeBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config_path = (
            ROOT / "configs/executable_moe/m0_tiny_qwen2moe_v1.yaml"
        )
        cls.config = yaml.safe_load(cls.config_path.read_text())

    def test_model_and_scope_are_immutable_m0(self) -> None:
        model = self.config["model"]
        self.assertEqual(
            "f736f270816032b3c721f7422c62dea1381f49d7",
            model["revision"],
        )
        self.assertIn(model["revision"], model["snapshot_path"])
        self.assertNotIn("latest", model["snapshot_path"].lower())
        self.assertTrue(self.config["evidence_scope"]["model_is_random_tiny"])
        self.assertTrue(
            self.config["evidence_scope"]["prohibit_performance_conclusion"]
        )
        self.assertTrue(
            self.config["evidence_scope"][
                "prohibit_quality_or_generalization_conclusion"
            ]
        )

    def test_exact_four_frozen_smoke_samples_per_benchmark(self) -> None:
        selected = select_smoke_samples(
            ROOT / self.config["dataset"]["manifest"], self.config
        )
        self.assertEqual({"gsm8k", "mmlu"}, set(selected))
        self.assertEqual(4, len(selected["gsm8k"]))
        self.assertEqual(4, len(selected["mmlu"]))
        self.assertTrue(
            all(row["split"] == "smoke" for rows in selected.values() for row in rows)
        )
        self.assertEqual(
            {"gsm8k": "T1", "mmlu": "T2"},
            {
                name: spec["suite_class"]
                for name, spec in self.config["dataset"]["benchmarks"].items()
            },
        )

    def test_model_snapshot_inventory_is_deterministic(self) -> None:
        snapshot = ROOT / "tests/fixtures/model_snapshot"
        first = model_snapshot_inventory(snapshot)
        second = model_snapshot_inventory(snapshot)
        self.assertEqual(first, second)
        self.assertGreater(first["file_count"], 0)
        self.assertRegex(first["aggregate_sha256"], r"^[0-9a-f]{64}$")

    def test_router_hook_is_reconstructed_not_actual_dispatch(self) -> None:
        model = _FakeModel()
        with tempfile.TemporaryDirectory() as temporary:
            writer = JsonlWriter(Path(temporary) / "routing.jsonl")
            capture = RouterCapture(writer, self.config)
            self.assertEqual(2, capture.install(model))
            capture.set_request("request", 3)
            value = torch.tensor([
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ])
            for layer in model.model.layers:
                layer.mlp(value)
            capture.remove()
            writer.close()
            records = [
                json.loads(line)
                for line in (Path(temporary) / "routing.jsonl").read_text().splitlines()
            ]
        self.assertEqual(2, len(records))
        for record in records:
            self.assertEqual([3, 4], record["router_shape"])
            self.assertTrue(record["shape_sanity"]["valid"])
            self.assertEqual(
                "reconstructed_topk_from_gate_logits",
                record["routing_semantics"],
            )
            self.assertFalse(record["actual_dispatch_verified"])
            self.assertTrue(record["drop_overflow_unavailable"])
            self.assertEqual(
                6, sum(record["reconstructed_topk_counts"].values())
            )
            self.assertEqual(0, record["nan_count"])
            self.assertEqual(0, record["inf_count"])

    def test_cross_pass_requires_tokens_and_hashes(self) -> None:
        def result(pass_id: str, token: int = 7) -> dict:
            return {
                "pass": pass_id,
                "request_id": "gsm8k:measured:sample",
                "input_token_ids": [1, 2],
                "prompt_hash": "prompt",
                "sample_id": "sample",
                "model_id": "model",
                "model_revision": "model-revision",
                "weights_revision": "weights",
                "tokenizer_revision": "tokenizer",
                "snapshot_hash": "snapshot",
                "effective_generation_config_hash": "generation",
                "output_token_ids": [token],
                "output_hash": str(token),
            }

        passing = verify_cross_pass({
            "P0": [result("P0")],
            "P1": [result("P1")],
            "P2": [result("P2")],
            "P3": [result("P3")],
            "P5": [result("P5")],
        })
        self.assertEqual("pass", passing["status"])
        failing = verify_cross_pass({
            "P0": [result("P0")],
            "P2": [result("P2", 8)],
            "P3": [result("P3")],
        })
        self.assertEqual("fail", failing["status"])
        identity_mismatch = result("P2")
        identity_mismatch["snapshot_hash"] = "different"
        failing_identity = verify_cross_pass({
            "P0": [result("P0")],
            "P2": [identity_mismatch],
        })
        self.assertEqual("fail", failing_identity["status"])
        self.assertEqual(
            "snapshot_hash", failing_identity["mismatches"][0]["field"]
        )

    def test_capacity_boundary_preserves_output_and_cpu_is_explicit_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            partial = output / "partial.jsonl"
            partial.write_text("partial\n")
            manifest = capacity_boundary_failure(
                output, self.config, "cuda", "paid"
            )
            self.assertTrue(partial.is_file())
            self.assertEqual("capacity_boundary", manifest["status"])
            self.assertTrue(manifest["output_preserved"])
            self.assertFalse(manifest["cpu_fallback_performed"])
        command = [
            sys.executable,
            str(ROOT / "scripts/executable_moe_benchmark.py"),
            "--device", "cpu",
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("CPU execution requires", result.stderr)

    def test_stop_reason_contract(self) -> None:
        self.assertEqual("eos_token", stop_reason([1, 2], 2, 8))
        self.assertEqual("max_new_tokens", stop_reason([1] * 8, 2, 8))
        self.assertEqual("generation_ended_other", stop_reason([1], 2, 8))

    def test_measured_artifact_pass_contracts(self) -> None:
        artifact = m0_evidence_root()
        manifest = json.loads((artifact / "run_manifest.json").read_text())
        self.assertEqual("completed", manifest["status"])
        self.assertEqual("cuda", manifest["device"])
        self.assertEqual("pass", manifest["cross_pass_consistency"]["status"])
        for pass_id in ("P0", "P2", "P3"):
            loading = manifest["pass_summaries"][pass_id]["loading"]
            self.assertTrue(
                loading["checkpoint_fully_consumed_after_compatibility_remap"]
            )
            self.assertTrue(loading["compatibility_remap"]["all_exact"])
            self.assertEqual(24, loading["compatibility_remap"]["tensor_count"])
        p0 = [
            json.loads(line)
            for line in (artifact / "p0/native.jsonl").read_text().splitlines()
        ]
        p2 = [
            json.loads(line)
            for line in (artifact / "p2/native.jsonl").read_text().splitlines()
        ]
        p3 = [
            json.loads(line)
            for line in (artifact / "p3/native.jsonl").read_text().splitlines()
        ]
        p0_start = next(row for row in p0 if row["event"] == "pass_start")
        self.assertEqual(0, p0_start["hook_count_before"])
        self.assertEqual(0, p0_start["hook_count_installed"])
        self.assertFalse(any(row["event"] == "routing" for row in p0))
        routing = [row for row in p2 if row["event"] == "routing"]
        self.assertEqual(160, len(routing))
        self.assertTrue(all(row["shape_sanity"]["valid"] for row in routing))
        self.assertTrue(all(row["nan_count"] == 0 for row in routing))
        self.assertTrue(all(row["inf_count"] == 0 for row in routing))
        p3_end = next(row for row in p3 if row["event"] == "pass_end")
        self.assertIsNotNone(p3_end["allocator_before"])
        self.assertIsNotNone(p3_end["allocator_after"])
        self.assertGreater(p3_end["memory_peak"]["max_allocated_bytes"], 0)

    def test_existing_raw_passes_full_offline_identity_regression(self) -> None:
        artifact = m0_evidence_root()
        results = {
            pass_id: native_sample_results(
                artifact / pass_id.lower() / "native.jsonl"
            )
            for pass_id in ("P0", "P2", "P3")
        }
        consistency = verify_cross_pass(results)
        self.assertEqual("pass", consistency["status"])
        self.assertIn(
            "identical_effective_generation_config_hash",
            consistency["requirements"],
        )
        self.assertIn("identical_snapshot_hash", consistency["requirements"])

    def test_measured_artifact_checksums(self) -> None:
        artifact = m0_evidence_root()
        inventory = json.loads((artifact / "checksums.json").read_text())
        for item in inventory["files"]:
            path = artifact / item["path"]
            self.assertEqual(item["bytes"], path.stat().st_size)
            self.assertEqual(
                item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )

    def test_measured_artifact_suite_class_sidecar(self) -> None:
        artifact = m0_evidence_root()
        mapping = json.loads(
            (artifact / "suite_class_mapping_v1.2.0.json").read_text()
        )
        self.assertTrue(mapping["native_artifacts_unchanged"])
        self.assertEqual(
            {"gsm8k": "T1", "mmlu": "T2"},
            mapping["benchmark_id_mapping"],
        )
        self.assertEqual(8, len(mapping["samples"]))
        self.assertEqual(
            {"T1", "T2"}, {sample["suite_class"] for sample in mapping["samples"]}
        )

    def test_standard_vertical_slice_p1_p5_contracts(self) -> None:
        artifact = m0_evidence_root()
        standard = json.loads(
            (artifact / "standard_vertical_slice_manifest.json").read_text()
        )
        self.assertEqual("completed", standard["status"])
        self.assertTrue(standard["protected_raw_unchanged"])
        self.assertEqual(
            ["P0", "P1", "P2", "P3", "P5"],
            standard["cross_pass_consistency"]["passes"],
        )
        self.assertEqual("pass", standard["cross_pass_consistency"]["status"])
        p1 = standard["passes"]["P1"]
        self.assertGreater(p1["profiler_events"]["total_events"], 0)
        self.assertGreater(p1["profiler_events"]["cuda_events"], 0)
        self.assertIn("never use as P0", p1["overhead_label"])
        self.assertEqual({"gsm8k", "mmlu"}, set(p1["traces"]))
        for trace in p1["traces"].values():
            relative = Path(trace["path"]).relative_to(
                "artifacts/m0_benchmark_smoke"
            )
            self.assertTrue((artifact / relative).is_file())
            self.assertGreater(trace["events"]["total_events"], 0)
        p5 = standard["passes"]["P5"]
        self.assertGreater(p5["telemetry"]["sample_count"], 0)
        self.assertEqual(
            "insufficient_short_vertical_smoke",
            p5["telemetry"]["sampling_sufficiency"],
        )
        self.assertIn("unsupported_fields", p5["telemetry"])
        self.assertIn("never use as P0", p5["overhead_label"])
        self.assertEqual("unsupported", standard["passes"]["P4"]["status"])
        self.assertFalse(standard["passes"]["P4"]["executed"])
        self.assertEqual("optional_not_run", standard["passes"]["P6"]["status"])
        self.assertFalse(standard["passes"]["P6"]["executed"])

    def test_standard_supplement_preserved_original_raw(self) -> None:
        artifact = m0_evidence_root()
        standard = json.loads(
            (artifact / "standard_vertical_slice_manifest.json").read_text()
        )
        for pass_id in ("P0", "P2", "P3"):
            path = artifact / pass_id.lower() / "native.jsonl"
            self.assertEqual(
                standard["passes"][pass_id]["raw_sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
