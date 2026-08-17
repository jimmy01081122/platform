from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_quality import (  # noqa: E402
    evaluate_choice,
    evaluate_chinese,
    evaluate_code_static,
    evaluate_gsm8k,
    evaluate_instruction,
    evaluate_record,
)
from freeze_benchmark_suite import freeze_root, merkle_root  # noqa: E402
from generate_sample_manifest import canonical_json, digest_text  # noqa: E402


class BenchmarkSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = ROOT / "configs" / "test_suites"
        cls.suite = yaml.safe_load(
            (cls.config / "moe_trace_suite_v1.yaml").read_text()
        )
        cls.registry = yaml.safe_load(
            (cls.config / "benchmark_registry.yaml").read_text()
        )

    def test_all_tasks_have_versioned_definitions(self) -> None:
        self.assertEqual(
            {f"T{index}" for index in range(9)}, set(self.suite["tasks"])
        )
        for task_id, task in self.suite["tasks"].items():
            self.assertEqual(f"{task_id}-v1", task["version"])
        self.assertEqual("deterministic_micro_fixtures", self.suite["tasks"]["T0"]["name"])
        self.assertEqual("gsm8k_exact_math", self.suite["tasks"]["T1"]["name"])
        self.assertEqual("mmlu_subject_stratified", self.suite["tasks"]["T2"]["name"])
        self.assertEqual("humaneval_static_code", self.suite["tasks"]["T3"]["name"])
        self.assertEqual("ceval_chinese", self.suite["tasks"]["T5"]["name"])

    def test_external_revisions_are_immutable_sha_and_stratified(self) -> None:
        for name in ("gsm8k", "mmlu", "humaneval", "ceval"):
            revision = self.registry["datasets"][name]["dataset_revision"]
            self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertEqual(
            "subject", self.registry["datasets"]["mmlu"]["stratification"]
        )
        self.assertEqual(
            {"gsm8k": "T1", "mmlu": "T2", "humaneval": "T3", "ceval": "T5"},
            {
                name: self.registry["datasets"][name]["suite_class"]
                for name in ("gsm8k", "mmlu", "humaneval", "ceval")
            },
        )
        domain_axis = yaml.safe_load(
            (self.config / "splits" / "v1.4.0" / "domain_split.yaml").read_text()
        )
        self.assertTrue(domain_axis["assignment"]["mmlu"]["domain_holdout_subjects"])
        self.assertTrue(domain_axis["assignment"]["ceval"]["domain_holdout_subjects"])

    def test_generator_is_reproducible_and_closes_external_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out1 = Path(temporary) / "one.jsonl"
            out2 = Path(temporary) / "two.jsonl"
            gate1 = Path(temporary) / "gate-one.json"
            gate2 = Path(temporary) / "gate-two.json"
            command = [sys.executable, str(ROOT / "scripts/generate_sample_manifest.py")]
            subprocess.run(
                command + ["--output", str(out1), "--gate-report", str(gate1)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                command + ["--output", str(out2), "--gate-report", str(gate2)],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(out1.read_bytes(), out2.read_bytes())
            rows = [json.loads(line) for line in out1.read_text().splitlines()]
            gates = json.loads(gate1.read_text())
            self.assertEqual(810, len(rows))
            self.assertEqual(0, gates["unresolved_gate_count"])
            self.assertEqual([], gates["gates"])
            self.assertEqual(
                {
                    "gsm8k": 132,
                    "mmlu": 369,
                    "humaneval": 132,
                    "ceval": 152,
                },
                {
                    name: details["selected_rows"]
                    for name, details in gates["materialized"].items()
                },
            )
            for row in rows:
                self.assertRegex(row["raw_sample_hash"], r"^[0-9a-f]{64}$")
                self.assertEqual(digest_text(row["prompt"]), row["prompt_hash"])
                self.assertTrue(row["role"])
                self.assertTrue(row["enabled_models"])
                self.assertNotIn("model_holdout", row)
                self.assertNotIn("hardware_holdout", row)

    def test_external_rows_match_snapshot_raw_hashes_and_quotas(self) -> None:
        import pyarrow.parquet as parquet

        rows = [
            json.loads(line)
            for line in (self.config / "sample_manifest_v1.jsonl")
            .read_text().splitlines()
        ]
        tables = {}
        external = [
            row for row in rows
            if row["task_id"] in {"T1", "T2", "T3", "T5"}
        ]
        for row in external:
            source = row["source"]
            path = ROOT / source["snapshot_path"]
            if path not in tables:
                tables[path] = parquet.read_table(path).to_pylist()
            raw = tables[path][source["row_index"]]
            self.assertEqual(
                digest_text(canonical_json(raw)), row["raw_sample_hash"]
            )
        by_benchmark_split = {}
        for row in external:
            key = (row["metadata"]["benchmark"], row["split"])
            by_benchmark_split[key] = by_benchmark_split.get(key, 0) + 1
        for benchmark in ("gsm8k", "mmlu", "humaneval", "ceval"):
            self.assertEqual(4, by_benchmark_split[(benchmark, "smoke")])
            self.assertEqual(32, by_benchmark_split[(benchmark, "calibration")])
            self.assertEqual(32, by_benchmark_split[(benchmark, "validation")])
            self.assertEqual(64, by_benchmark_split[(benchmark, "sample_holdout")])
        self.assertEqual(237, by_benchmark_split[("mmlu", "domain_holdout")])
        self.assertEqual(20, by_benchmark_split[("ceval", "domain_holdout")])
        self.assertEqual(
            {
                "T1": {"math"},
                "T2": {"multiple_choice"},
                "T3": {"code"},
                "T5": {"chinese_multiple_choice"},
            },
            {
                task_id: {
                    row["role"] for row in external if row["task_id"] == task_id
                }
                for task_id in ("T1", "T2", "T3", "T5")
            },
        )

    def test_snapshot_inventory_checksums_and_provenance(self) -> None:
        inventory = json.loads(
            (ROOT / "datasets/snapshots/snapshot_inventory_v1.json").read_text()
        )
        self.assertEqual(16, inventory["file_count"])
        for item in inventory["files"]:
            path = ROOT / item["path"]
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"]
            )
            self.assertRegex(item["dataset_revision"], r"^[0-9a-f]{40}$")
            self.assertTrue(item["license"])
            self.assertIn(item["dataset_revision"], item["source_url"])

    def test_split_hashes_are_pairwise_disjoint(self) -> None:
        rows = [
            json.loads(line)
            for line in (self.config / "sample_manifest_v1.jsonl")
            .read_text().splitlines()
        ]
        for field in ("sample_id", "raw_sample_hash", "prompt_hash"):
            by_split: dict[str, set[str]] = {}
            for row in rows:
                by_split.setdefault(row["split"], set()).add(row[field])
            names = sorted(by_split)
            for index, left in enumerate(names):
                for right in names[index + 1:]:
                    self.assertTrue(
                        by_split[left].isdisjoint(by_split[right]),
                        f"{field}: {left} overlaps {right}",
                    )

    def test_v1_2_task_counts_preserve_all_samples(self) -> None:
        rows = [
            json.loads(line)
            for line in (self.config / "sample_manifest_v1.jsonl")
            .read_text().splitlines()
        ]
        counts = {}
        for row in rows:
            counts[row["task_id"]] = counts.get(row["task_id"], 0) + 1
        self.assertEqual(
            {
                "T0": 4,
                "T1": 132,
                "T2": 369,
                "T3": 132,
                "T4": 4,
                "T5": 152,
                "T6": 5,
                "T7": 4,
                "T8": 8,
            },
            counts,
        )
        self.assertEqual(810, len(rows))

    def test_t0_t4_t6_t8_contracts(self) -> None:
        rows = [
            json.loads(line)
            for line in (self.config / "sample_manifest_v1.jsonl")
            .read_text().splitlines()
        ]
        t0 = [row for row in rows if row["task_id"] == "T0"]
        self.assertEqual(4, len(t0))
        self.assertTrue(all(row["role"] == "micro_fixture" for row in t0))
        self.assertTrue(all(
            row["metadata"]["token_contract"]
            == "artificial_fixture_ids_not_model_tokenizer_ids"
            for row in t0
        ))
        t4 = [row for row in rows if row["task_id"] == "T4"]
        self.assertEqual(4, len(t4))
        self.assertTrue(all(row["metadata"]["fixed_content"] for row in t4))
        self.assertEqual(
            {128, 512, 2048, 4096, 8192},
            {row["metadata"]["bucket"] for row in rows if row["task_id"] == "T6"},
        )
        for row in rows:
            if row["task_id"] == "T6":
                self.assertEqual(
                    row["metadata"]["bucket"], len(row["prompt"].split())
                )
        self.assertEqual(
            set(self.suite["tasks"]["T8"]["patterns"]),
            {
                row["metadata"]["stress_pattern"]
                for row in rows if row["task_id"] == "T8"
            },
        )

    def test_freeze_merkle_and_refuses_revision_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "frozen"
            command = [
                sys.executable, str(ROOT / "scripts/freeze_benchmark_suite.py"),
                "--output-root", str(output_root), "--revision", "test-v1",
            ]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, first.returncode, first.stderr)
            inventory_path = output_root / "test-v1" / "inventory.json"
            inventory = json.loads(inventory_path.read_text())
            frozen_lines = [
                line.encode()
                for line in (output_root / "test-v1" / "sample_manifest.jsonl")
                .read_text().splitlines()
            ]
            manifest_root = merkle_root(frozen_lines)
            self.assertEqual(manifest_root, inventory["merkle"]["manifest_root"])
            self.assertEqual(
                freeze_root(manifest_root, inventory["source_files"]),
                inventory["merkle"]["root"],
            )
            required = {
                "configs/test_suites/prompt_templates/v1.yaml",
                "configs/test_suites/generation_configs/v1.yaml",
                "configs/test_suites/serving_schedules/v1.yaml",
                "configs/test_suites/model_benchmark_matrix.yaml",
                "datasets/snapshots/snapshot_inventory_v1.json",
                "configs/test_suites/splits/v1.4.0/sample_split.yaml",
                "configs/test_suites/splits/v1.4.0/domain_split.yaml",
                "configs/test_suites/splits/v1.4.0/model_holdout.yaml",
                "configs/test_suites/splits/v1.4.0/hardware_holdout.yaml",
            }
            self.assertTrue(required.issubset(inventory["source_files"]))
            modified = dict(inventory["source_files"])
            modified[next(iter(required))] = "0" * 64
            self.assertNotEqual(
                inventory["merkle"]["root"], freeze_root(manifest_root, modified)
            )
            self.assertEqual(
                {"sample", "domain", "model", "hardware"},
                set(inventory["split_axes"]),
            )
            self.assertFalse(inventory["split_axes"]["hardware"]["active"])
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(0, second.returncode)
            self.assertIn("refusing to overwrite", second.stderr)

    def test_quality_evaluators_are_non_executing(self) -> None:
        self.assertTrue(
            evaluate_gsm8k("work\n#### 1,234.50", "1234.5")["correctness"]
        )
        self.assertTrue(evaluate_choice("Answer: (C)", 2)["correctness"])
        code = "def solve():\n    raise RuntimeError('must not execute')\n"
        self.assertTrue(evaluate_code_static(code, "solve")["validity"])
        self.assertIsNone(evaluate_code_static(code, "solve")["correctness"])
        self.assertTrue(evaluate_instruction("One sentence.")["validity"])
        self.assertTrue(evaluate_chinese("這是一段有效的中文回答。")["validity"])
        ceval = evaluate_record({
            "task_id": "T5", "prediction": "A", "reference": "A",
            "prompt": "下列哪一個選項正確？只回答字母。",
        })
        self.assertTrue(ceval["correctness"])
        self.assertTrue(ceval["language_validity"]["validity"])
        fixture = evaluate_record({
            "task_id": "T0", "prediction_token_ids": [7], "reference": [7],
        })
        self.assertTrue(fixture["validity"])
        self.assertTrue(fixture["correctness"])

    def test_tiny_random_m0_has_exactly_eight_pipeline_pairings(self) -> None:
        model_id = "tiny_random_qwen2moe_m0"
        rows = [
            json.loads(line)
            for line in (self.config / "sample_manifest_v1.jsonl")
            .read_text().splitlines()
        ]
        eligible = [row for row in rows if model_id in row["enabled_models"]]
        self.assertEqual(8, len(eligible))
        self.assertEqual({"smoke"}, {row["split"] for row in eligible})
        self.assertEqual({"T1", "T2"}, {row["task_id"] for row in eligible})
        matrix = yaml.safe_load(
            (self.config / "model_benchmark_matrix.yaml").read_text()
        )
        self.assertFalse(matrix["models"][model_id]["release_eligible"])
        registry = yaml.safe_load(
            (ROOT / "configs/model_registry.yaml").read_text()
        )
        self.assertEqual("M0_pipeline_only", registry["models"][model_id]["identity"])

    def test_holdout_axes_are_orthogonal_and_hardware_is_unassigned(self) -> None:
        split_root = self.config / "splits" / "v1.4.0"
        model_axis = yaml.safe_load((split_root / "model_holdout.yaml").read_text())
        hardware_axis = yaml.safe_load((split_root / "hardware_holdout.yaml").read_text())
        self.assertEqual("model_id", model_axis["assignment_unit"])
        self.assertEqual(
            {"deepseek_r1", "qwen3_235b"},
            set(model_axis["cohorts"]["large_moe_blind"]["model_ids"]),
        )
        self.assertEqual(
            "unassigned_pending_future_decision", hardware_axis["status"]
        )
        self.assertFalse(hardware_axis["active_split"])
        self.assertEqual([], hardware_axis["assignments"])
        self.assertFalse(
            hardware_axis["historical_retired_assignments"][0][
                "participates_in_active_split"
            ]
        )

    def test_canonical_hash_contract(self) -> None:
        left = {"b": 2, "a": 1}
        right = {"a": 1, "b": 2}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(
            digest_text(canonical_json(left)), digest_text(canonical_json(right))
        )


if __name__ == "__main__":
    unittest.main()
