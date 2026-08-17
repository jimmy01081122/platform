from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path

import yaml

from scripts.validate_package import excluded_from_source_integrity

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/test_suites/granite_c1"
SOURCE = ROOT / "configs/test_suites/frozen/v1.4.0/sample_manifest.jsonl"


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class GraniteC1SuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = yaml.safe_load((CONFIG / "suite.yaml").read_text())
        cls.selected = rows(CONFIG / "sample_manifest.jsonl")
        cls.source = {row["sample_id"]: row for row in rows(SOURCE)}

    def test_c1_a_and_c1_b_instance_quotas(self) -> None:
        counts = Counter(
            (row["suite_id"], row["task_id"]) for row in self.selected
        )
        self.assertEqual(4, counts[("granite_c1_smoke_v1", "T0")])
        self.assertEqual(4, counts[("granite_c1_smoke_v1", "T1")])
        self.assertEqual(8, counts[("granite_c1_integration_v1", "T0")])
        self.assertEqual(8, counts[("granite_c1_integration_v1", "T1")])
        self.assertEqual(8, counts[("granite_c1_integration_v1", "T2")])
        c1b_t0 = [
            row for row in self.selected
            if row["suite_id"] == "granite_c1_integration_v1"
            and row["task_id"] == "T0"
        ]
        self.assertEqual(4, len({row["sample_id"] for row in c1b_t0}))
        self.assertEqual({2}, set(Counter(row["sample_id"] for row in c1b_t0).values()))

    def test_every_selection_matches_frozen_or_c1_extension_provenance(self) -> None:
        for row in self.selected:
            source = self.source.get(row["sample_id"])
            if source is not None:
                self.assertEqual("v1.4.0", row["source_manifest_revision"])
                self.assertEqual(source["raw_sample_hash"], row["raw_sample_hash"])
                if row["task_id"] == "T0":
                    self.assertEqual(
                        hashlib.sha256(row["prompt_override"].encode()).hexdigest(),
                        row["prompt_hash"],
                    )
                    self.assertEqual(
                        "granite-c1-t0-override-v1",
                        row["prompt_template_revision"],
                    )
                    self.assertNotEqual(source["prompt_hash"], row["prompt_hash"])
                else:
                    self.assertEqual(source["prompt_hash"], row["prompt_hash"])
                self.assertEqual(source["source"]["row_index"], row["dataset_row_index"])
                if row["task_id"] in {"T1", "T2"}:
                    self.assertEqual(
                        source["source"]["dataset_revision"],
                        row["benchmark_revision"],
                    )
            else:
                self.assertEqual(
                    "granite-c1-v1.1.0", row["source_manifest_revision"]
                )
            self.assertRegex(row["raw_sample_hash"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["prompt_hash"], r"^[0-9a-f]{64}$")

    def test_mmlu_is_balanced_across_four_required_strata(self) -> None:
        selected = [
            row for row in self.selected
            if row["suite_id"] == "granite_c1_integration_v1"
            and row["task_id"] == "T2"
        ]
        self.assertEqual(
            {
                "mathematics_or_statistics": 2,
                "computer_science": 2,
                "other_stem": 2,
                "humanities_or_social_science": 2,
            },
            Counter(row["stratum"] for row in selected),
        )
        self.assertEqual(
            {"abstract_algebra"},
            {
                row["subject"] for row in selected
                if row["stratum"] == "mathematics_or_statistics"
            },
        )
        self.assertEqual(
            {"college_computer_science"},
            {
                row["subject"] for row in selected
                if row["stratum"] == "computer_science"
            },
        )
        self.assertEqual(
            {"college_physics"},
            {
                row["subject"] for row in selected
                if row["stratum"] == "other_stem"
            },
        )

    def test_c1_b_is_eligible_with_all_strata_present(self) -> None:
        integration = self.suite["suites"]["granite_c1_integration_v1"]
        self.assertTrue(integration["enabled"])
        self.assertTrue(integration["eligible"])
        self.assertEqual([], integration["eligibility_blockers"])
        self.assertEqual(
            {"present"},
            set(integration["mmlu_required_subject_groups"].values()),
        )

    def test_manifest_hash_is_stable(self) -> None:
        self.assertEqual("granite-c1-v1.1.0", self.suite["suite_revision"])
        digest = hashlib.sha256(
            (CONFIG / "sample_manifest.jsonl").read_bytes()
        ).hexdigest()
        self.assertEqual(
            "f32aa23823c2b88f7f2fab2a11cbbafe39c10adc54d5570ca7474b27996cd2bd",
            digest,
        )
        self.assertEqual(self.suite["selection_manifest_sha256"], digest)

    def test_extension_rows_match_parquet_and_deterministic_selection(self) -> None:
        import pyarrow.parquet as parquet

        selected = [
            row for row in self.selected
            if row.get("selection_method") == "two_smallest_raw_sample_hash"
        ]
        self.assertEqual(4, len(selected))
        for subject in ("college_computer_science", "college_physics"):
            subject_rows = [row for row in selected if row["subject"] == subject]
            self.assertEqual(2, len(subject_rows))
            path = ROOT / subject_rows[0]["snapshot_path"]
            self.assertEqual(
                subject_rows[0]["snapshot_sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            table = parquet.read_table(path).to_pylist()
            ranked = sorted(
                (digest_text(canonical_json(raw)), index, raw)
                for index, raw in enumerate(table)
            )
            self.assertEqual(
                [(raw_hash, index) for raw_hash, index, _raw in ranked[:2]],
                sorted(
                    (row["raw_sample_hash"], row["dataset_row_index"])
                    for row in subject_rows
                ),
            )
            for row in subject_rows:
                raw = table[row["dataset_row_index"]]
                choices = raw["choices"]
                prompt = (
                    f"{raw['question']}\nA. {choices[0]}\nB. {choices[1]}"
                    f"\nC. {choices[2]}\nD. {choices[3]}"
                    "\nAnswer with one letter only."
                )
                locator = [
                    "cais/mmlu",
                    row["benchmark_revision"],
                    subject,
                    "test",
                    row["dataset_row_index"],
                ]
                self.assertEqual(prompt, row["prompt"])
                self.assertEqual(digest_text(prompt), row["prompt_hash"])
                self.assertEqual(
                    "ABCD"[int(raw["answer"])], row["reference"]
                )
                self.assertEqual(
                    f"t2-{digest_text(canonical_json(locator))[:20]}",
                    row["sample_id"],
                )

    def test_c1_snapshot_inventory_is_stable_and_complete(self) -> None:
        inventory_path = ROOT / self.suite["snapshot_inventory"]
        digest = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
        self.assertEqual(self.suite["snapshot_inventory_sha256"], digest)
        inventory = json.loads(inventory_path.read_text())
        self.assertEqual("granite-c1-snapshots-v1.1.0", inventory["inventory_revision"])
        self.assertEqual(
            {"college_computer_science", "college_physics"},
            {item["config"] for item in inventory["extensions"]},
        )
        for item in inventory["extensions"]:
            self.assertEqual(
                item["sha256"],
                hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest(),
            )

    def test_model_and_generation_are_pinned_without_dense_claim(self) -> None:
        model = yaml.safe_load((CONFIG / "model_config.yaml").read_text())
        generation = yaml.safe_load((CONFIG / "generation_config.yaml").read_text())
        self.assertEqual(
            "0da7a48b0276d500ce5922fd2b33944091fc6c09",
            model["model_revision"],
        )
        self.assertEqual("GraniteMoeForCausalLM", model["architecture"])
        self.assertEqual("granite-c1-adapter-v8", model["adapter_version"])
        self.assertEqual("pinned_chat_template", model["tokenization_mode"])
        self.assertEqual(
            "08962c2f15d56767854b46dfc4070b37f4c443551833bba65b417191735f3187",
            model["chat_template_sha256"],
        )
        self.assertFalse(model["claims"]["dense_architecture_description_used"])
        self.assertEqual("4.47.0", model["transformers_version"])
        self.assertEqual(1, generation["common"]["num_beams"])
        self.assertFalse(generation["common"]["compile"])
        self.assertFalse(generation["common"]["speculative_decoding"])
        self.assertEqual(
            "granite-c1-generation-v3",
            generation["generation_config_revision"],
        )
        self.assertEqual(256, generation["profiles"]["c1_a"]["max_new_tokens"])
        self.assertEqual(256, generation["profiles"]["c1_b"]["max_new_tokens"])
        determinism = generation["determinism"]
        self.assertTrue(determinism["torch_deterministic_algorithms"])
        self.assertEqual(
            ":4096:8", determinism["cublas_workspace_config"]
        )
        self.assertTrue(determinism["cuda_launch_blocking"])
        self.assertFalse(determinism["cuda_matmul_allow_tf32"])
        self.assertFalse(determinism["cudnn_allow_tf32"])
        self.assertFalse(determinism["cudnn_benchmark"])
        self.assertTrue(determinism["cudnn_deterministic"])
        self.assertFalse(
            determinism[
                "cuda_matmul_allow_bf16_reduced_precision_reduction"
            ]
        )
        self.assertFalse(
            determinism[
                "cuda_matmul_allow_fp16_reduced_precision_reduction"
            ]
        )

    def test_adapter_models_are_source_but_top_level_models_are_private(self):
        self.assertFalse(
            excluded_from_source_integrity(
                "adapters/models/granite_moe/adapter.py"
            )
        )
        self.assertTrue(
            excluded_from_source_integrity(
                "models/granite-3.1-1b-a400m/model.safetensors"
            )
        )

    def test_t0_prompt_overrides_are_exact_and_source_identity_is_unchanged(self):
        expected = {
            "t0-26831bcc3798ede7cfe1": (
                "Return exactly this text and nothing else: 3 5",
                "7d946257c3f3c2d5d7b68c7b803ffa16252fccb3e78fb54563a0c49109ab922b",
            ),
            "t0-7fdb4d724ff4d56bbaea": (
                "Return exactly this text and nothing else: 7",
                "3fd469a534c95ffb5a243ef6627b05cb1515012b4f482940218b8367c6b511c3",
            ),
            "t0-bd2edd690ce0791d9f2b": (
                "Return exactly this text and nothing else: 6 4 2",
                "b5e01922b7d3ad4e131d4ce89af4a28f0cd7fa4b61c15d52709d855924af3062",
            ),
            "t0-c072b8e86d735470bd00": (
                "Return exactly this text and nothing else: 21",
                "5087cd16760f4aa9ff165a73dec76f7eb6f1d4a92401d92f639b66af7a382bd9",
            ),
        }
        t0 = [row for row in self.selected if row["task_id"] == "T0"]
        self.assertEqual(12, len(t0))
        for row in t0:
            prompt, prompt_hash = expected[row["sample_id"]]
            source = self.source[row["sample_id"]]
            self.assertEqual(prompt, row["prompt_override"])
            self.assertEqual(prompt_hash, row["prompt_hash"])
            self.assertEqual(source["raw_sample_hash"], row["raw_sample_hash"])
            self.assertEqual(
                row["sample_id"],
                source["sample_id"],
            )
        self.assertEqual(
            "4de9eda6a8eabb5e49c897563033e2fa9d9a8b62db7b81790bb9a4c871f5621e",
            hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        )

    def test_gsm8k_prompts_are_not_overridden(self):
        for row in self.selected:
            if row["task_id"] != "T1":
                continue
            source = self.source[row["sample_id"]]
            self.assertNotIn("prompt_override", row)
            self.assertEqual(source["prompt"], row.get("prompt", source["prompt"]))
            self.assertEqual(source["prompt_hash"], row["prompt_hash"])

    def test_package_manifest_tracks_c1_v1_1_contract(self):
        package = json.loads((ROOT / "package_manifest.json").read_text())
        granite = package["granite_c1"]
        self.assertEqual("granite-c1-v1.1.0", granite["suite_revision"])
        self.assertEqual("granite-c1-adapter-v8", granite["adapter_version"])
        self.assertEqual(
            self.suite["selection_manifest_sha256"],
            granite["selection_manifest_sha256"],
        )
        self.assertEqual(
            "08962c2f15d56767854b46dfc4070b37f4c443551833bba65b417191735f3187",
            granite["chat_template_sha256"],
        )

    def test_model_inventory_is_metadata_only_with_apache_attribution(self) -> None:
        model = yaml.safe_load((CONFIG / "model_config.yaml").read_text())
        inventory_path = ROOT / model["weight_inventory"]
        inventory = json.loads(inventory_path.read_text())
        self.assertEqual("metadata_only_no_weight_payload", inventory["inventory_kind"])
        self.assertEqual("Apache-2.0", inventory["license"])
        weight = next(
            item for item in inventory["files"]
            if item["path"] == "model.safetensors"
        )
        self.assertFalse(weight["present_in_package"])
        self.assertRegex(weight["lfs_sha256"], r"^[0-9a-f]{64}$")
        attribution = (inventory_path.parent / "ATTRIBUTION").read_text()
        self.assertIn("Apache License 2.0", attribution)

    def test_c1_c_is_definition_only(self) -> None:
        regression = self.suite["suites"]["granite_c1_regression_v1"]
        self.assertFalse(regression["enabled"])
        self.assertEqual("definition_only_do_not_execute", regression["status"])


if __name__ == "__main__":
    unittest.main()
