from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import projectctl
from scripts.c1_quality import (
    build_quality_artifact,
    canonical_hash,
    compare_cross_pass_evidence,
    quality_binding_document,
    validate_quality_artifact,
)
from scripts.c1_evaluator import evaluate_frozen_sample
from scripts.c1_worker import main as worker_main


def sample(task_id: str = "T1") -> dict:
    references = {"T0": [7], "T1": 42, "T2": "A"}
    value = {
        "sample_id": "source-1",
        "task_id": task_id,
        "prompt": "fixture prompt",
        "reference": references.get(task_id),
    }
    if task_id == "T0":
        value["metadata"] = {
            "token_contract": "artificial_fixture_ids_not_model_tokenizer_ids",
            "expected_semantics": "identity",
            "input_token_ids": [7],
        }
    return value


def suite(task_id: str = "T1") -> dict:
    source = sample(task_id)
    value = {
        "stage": "C1-A",
        "suite_id": "quality-v2-fixture",
        "suite_revision": "fixture-v1",
        "model": {
            "id": "fixture-granite",
            "revision": "fixture-rev",
            "tokenizer_revision": "fixture-rev",
        },
        "models": ["fixture-granite"],
        "samples": [{
            "sample_id": "instance-1",
            "source_sample_id": "source-1",
            "benchmark_id": "fixture",
            "prompt_hash": hashlib.sha256(source["prompt"].encode()).hexdigest(),
            "source_sample": source,
        }],
        "repetitions": 1,
        "logical_passes": ["P0"],
        "generation_config": {
            "max_new_tokens": 2,
            "do_sample": False,
            "num_beams": 1,
            "use_cache": True,
            "seed": 7,
        },
    }
    return projectctl.bind_quality_contract(value)


def generation(stop_reason: str = "eos_token", text: str = "42") -> dict:
    ids = [9, 10]
    return {
        "input_token_ids": [1, 2],
        "input_token_count": 2,
        "output_token_ids": ids,
        "output_token_count": len(ids),
        "output_hash": canonical_hash(ids),
        "text": text,
        "stop_reason": stop_reason,
        "execution_alignment_key": "a" * 64,
    }


class C1QualityV2Tests(unittest.TestCase):
    def artifact(
        self,
        task_id: str,
        *,
        correctness: bool,
        validity: bool = True,
        stop_reason: str = "eos_token",
    ) -> tuple[dict, dict, dict, dict]:
        document = suite(task_id)
        source = document["samples"][0]["source_sample"]
        contract = projectctl.load_structured(
            projectctl.PACKAGE_ROOT / projectctl.QUALITY_CONTRACT_PATH
        )
        answer_text = {
            ("T0", True): "7", ("T0", False): "99",
            ("T1", True): "42", ("T1", False): "41",
            ("T2", True): "A", ("T2", False): "B",
        }.get((task_id, correctness), "fixture")
        if not validity:
            answer_text = "unparseable"
        gen = generation(stop_reason, answer_text)
        evaluated = evaluate_frozen_sample(answer_text, source)
        artifact = build_quality_artifact(
            contract=contract,
            contract_binding=document["quality_contract"],
            sample=source,
            quality={
                "evaluator": evaluated["evaluator"],
                "validity": evaluated["validity"],
                "correctness": evaluated["correctness"],
            },
            generation=gen,
            records=[{"latency_ns": 1}],
            alignment=gen["execution_alignment_key"],
            pass_id="P0",
        )
        return artifact, contract, document, gen

    def test_t1_parseable_wrong_is_nonblocking_and_explicit(self):
        for task_id in ("T1", "T2"):
            with self.subTest(task_id=task_id):
                artifact, contract, document, gen = self.artifact(
                    task_id, correctness=False
                )
                self.assertEqual("pass", artifact["blocking_status"])
                self.assertEqual("incorrect", artifact["task_outcome"])
                self.assertEqual("recorded_nonblocking", artifact["qg4"])
                self.assertEqual("observational", artifact["quality_role"])
                self.assertEqual("not_inferred", artifact["task_capability"])
                validate_quality_artifact(
                    artifact,
                    contract=contract,
                    contract_binding=document["quality_contract"],
                    sample=document["samples"][0]["source_sample"],
                    generation=gen,
                )

    def test_t2_contract_matches_adapter_evaluator_name(self):
        artifact, *_ = self.artifact("T2", correctness=True)
        self.assertEqual("parseable", artifact["parser_outcome"])
        contract = projectctl.load_structured(
            projectctl.PACKAGE_ROOT / projectctl.QUALITY_CONTRACT_PATH
        )
        document = suite("T2")
        source = document["samples"][0]["source_sample"]
        mismatched = build_quality_artifact(
            contract=contract,
            contract_binding=document["quality_contract"],
            sample=source,
            quality={
                "evaluator": "mmlu_exact_choice",
                "validity": True,
                "correctness": True,
            },
            generation=generation(),
            records=[{"latency_ns": 1}],
            alignment="a" * 64,
            pass_id="P0",
        )
        self.assertEqual("evaluator_error", mismatched["parser_outcome"])
        self.assertEqual("fail", mismatched["blocking_status"])

    def test_t0_wrong_and_max_new_tokens_are_blocking(self):
        t0, *_ = self.artifact("T0", correctness=False)
        truncated, *_ = self.artifact(
            "T1", correctness=True, stop_reason="max_new_tokens"
        )
        self.assertEqual("fail", t0["blocking_status"])
        self.assertEqual("fail", t0["qg3"])
        self.assertEqual("fail", truncated["blocking_status"])
        self.assertFalse(truncated["legal_stop"])
        unknown_stop, *_ = self.artifact(
            "T1", correctness=True, stop_reason="backend_mystery"
        )
        self.assertEqual("fail", unknown_stop["blocking_status"])

    def test_unknown_task_is_fail_closed(self):
        artifact, *_ = self.artifact("TX", correctness=True)
        self.assertEqual("unknown", artifact["task_class"])
        self.assertEqual("fail", artifact["blocking_status"])

    def test_coherent_semantic_tamper_is_rejected_by_replay(self):
        artifact, contract, document, gen = self.artifact(
            "T1", correctness=False
        )
        artifact["task_outcome"] = "correct"
        # Recompute the public binding exactly; replay must still reject the lie.
        artifact["quality_binding_sha256"] = canonical_hash(
            quality_binding_document(artifact)
        )
        with self.assertRaisesRegex(ValueError, "replayed evaluator"):
            validate_quality_artifact(
                artifact,
                contract=contract,
                contract_binding=document["quality_contract"],
                sample=document["samples"][0]["source_sample"],
                generation=gen,
            )

    def test_evaluator_and_quality_engine_source_hashes_are_verified(self):
        for field in ("evaluator_sha256", "quality_engine_sha256"):
            with self.subTest(field=field):
                document = suite("T1")
                document["quality_contract"][field] = "0" * 64
                with self.assertRaisesRegex(ValueError, "verified source"):
                    projectctl.verify_quality_contract_source(document)

    def test_contract_governance_fields_are_machine_enforced(self):
        contract = projectctl.load_structured(
            projectctl.PACKAGE_ROOT / projectctl.QUALITY_CONTRACT_PATH
        )
        mutations = (
            lambda row: row.update({"prospective_only": False}),
            lambda row: row["historical_decisions"].update({"G3-R3": "PASS"}),
            lambda row: row.update({"legal_stop_reasons": ["eos_token", "length"]}),
            lambda row: row["quality_gates"]["QG-4"].update(
                {"disposition": "blocking"}
            ),
        )
        from scripts.c1_quality import validate_contract_definition
        for mutate in mutations:
            candidate = json.loads(json.dumps(contract))
            mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_contract_definition(candidate)

    def test_shared_evaluator_uses_canonical_numeric_and_choice_parsers(self):
        t1 = evaluate_frozen_sample("The answer is 100.0", {
            "task_id": "T1", "reference": 100,
        })
        self.assertTrue(t1["validity"])
        self.assertTrue(t1["correctness"])
        t2 = evaluate_frozen_sample("Reasoning first; answer: C", {
            "task_id": "T2", "reference": "C",
        })
        self.assertTrue(t2["validity"])
        self.assertTrue(t2["correctness"])
        self.assertFalse(evaluate_frozen_sample("Because", {
            "task_id": "T2", "reference": "B",
        })["validity"])

    def test_all_passes_compare_to_p0_and_detect_classification_drift(self):
        baseline = {
            "generation": generation(),
            "quality": {
                "evaluator": "gsm8k_last_number",
                "parser_outcome": "parseable",
                "task_outcome": "incorrect",
                "quality_binding_sha256": "b" * 64,
            },
        }
        evidence = {
            pass_id: {
                "generation": dict(baseline["generation"]),
                "quality": dict(baseline["quality"]),
            }
            for pass_id in ("P0", "P1", "P2", "P3", "P5_BASIC")
        }
        self.assertEqual([], compare_cross_pass_evidence(
            evidence,
            expected_passes=["P0", "P1", "P2", "P3", "P5_BASIC"],
        ))
        evidence["P3"]["quality"]["parser_outcome"] = "unparseable"
        findings = compare_cross_pass_evidence(
            evidence,
            expected_passes=["P0", "P1", "P2", "P3", "P5_BASIC"],
        )
        self.assertIn({
            "kind": "classification_drift",
            "pass_id": "P3",
            "field": "parser_outcome",
        }, findings)
        evidence["P3"]["quality"] = dict(baseline["quality"])
        evidence["P5_BASIC"]["quality"]["quality_binding_sha256"] = "c" * 64
        self.assertIn({
            "kind": "classification_drift",
            "pass_id": "P5_BASIC",
            "field": "quality_binding_sha256",
        }, compare_cross_pass_evidence(
            evidence,
            expected_passes=["P0", "P1", "P2", "P3", "P5_BASIC"],
        ))
        evidence["P5_BASIC"]["quality"] = dict(baseline["quality"])
        for pass_id in ("P1", "P3", "P5_BASIC"):
            evidence[pass_id]["generation"]["output_token_ids"] = [int(pass_id[1])]
            self.assertTrue(any(
                finding.get("pass_id") == pass_id
                for finding in compare_cross_pass_evidence(
                    evidence,
                    expected_passes=["P0", "P1", "P2", "P3", "P5_BASIC"],
                )
            ))

    def test_worker_completes_parseable_t1_wrong_under_v2(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "suite.json"
            snapshot.write_text(json.dumps(suite("T1")), encoding="utf-8")
            output = root / "output"
            environment = {
                "C1_ADAPTER_FACTORY":
                    "tests.fake_c1_adapter:FakeT1QualityFailureAdapter",
                "PROJECTCTL_SUITE_SNAPSHOT": str(snapshot),
                "PROJECTCTL_SAMPLE_ID": "instance-1",
                "PROJECTCTL_REPETITION": "0",
                "PROJECTCTL_SESSION_ID": "fixture-session",
                "PROJECTCTL_HARDWARE_SESSION_ID": "fixture-hardware",
                "PROJECTCTL_LOGICAL_PASS": "P0",
                "PROJECTCTL_WORK_UNIT_ID": "fixture-unit",
                "PROJECTCTL_MODEL_ID": "fixture-granite",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(0, worker_main(["--output-dir", str(output)]))
            artifact = json.loads(
                (output / "quality_results.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual("pass", artifact["blocking_status"])
            self.assertEqual("incorrect", artifact["task_outcome"])
            self.assertEqual(
                "COMPLETE",
                json.loads((output / "pass_manifest.json").read_text())["status"],
            )

    def test_worker_blocks_t0_wrong_and_truncation_with_failure_evidence(self):
        cases = (
            ("T0", "tests.fake_c1_adapter:FakeQualityFailureAdapter"),
            ("T1", "tests.fake_c1_adapter:FakeMaxNewTokensAdapter"),
        )
        for task_id, adapter in cases:
            with self.subTest(task_id=task_id), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                snapshot = root / "suite.json"
                snapshot.write_text(json.dumps(suite(task_id)), encoding="utf-8")
                output = root / "output"
                environment = {
                    "C1_ADAPTER_FACTORY": adapter,
                    "PROJECTCTL_SUITE_SNAPSHOT": str(snapshot),
                    "PROJECTCTL_SAMPLE_ID": "instance-1",
                    "PROJECTCTL_REPETITION": "0",
                    "PROJECTCTL_SESSION_ID": "fixture-session",
                    "PROJECTCTL_HARDWARE_SESSION_ID": "fixture-hardware",
                    "PROJECTCTL_LOGICAL_PASS": "P0",
                    "PROJECTCTL_WORK_UNIT_ID": "fixture-unit",
                    "PROJECTCTL_MODEL_ID": "fixture-granite",
                }
                with patch.dict(os.environ, environment, clear=False):
                    self.assertEqual(
                        1, worker_main(["--output-dir", str(output)])
                    )
                quality = json.loads(
                    (output / "failure_quality_results.jsonl").read_text()
                )
                self.assertEqual("fail", quality["blocking_status"])
                self.assertTrue(
                    (output / "failure_generation_results.jsonl").is_file()
                )


if __name__ == "__main__":
    unittest.main()
