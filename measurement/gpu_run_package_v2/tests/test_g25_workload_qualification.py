from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from scheduler.g25_historical_evidence import (
    ARCHIVE_PATH,
    EXPECTED_RECORDS,
    HistoricalEvidenceError,
    verify_historical_evidence_archive,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/test_suites/granite_c1/g25_workload_qualification_v1.json"
SCHEMA = ROOT / "schemas/g25_workload_qualification.schema.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class G25WorkloadQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_contract_matches_schema(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(self.contract)

    def test_scope_is_all_frozen_c1a_t1_candidates(self) -> None:
        manifest = ROOT / self.contract["frozen_inputs"]["candidate_manifest_path"]
        selected = {
            row["instance_id"]
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
            for row in [json.loads(line)]
            if row["suite_id"] == "granite_c1_smoke_v1"
            and row["task_id"] == "T1"
        }
        self.assertEqual(
            selected, set(self.contract["candidate_scope"]["instances"])
        )
        self.assertEqual(
            "all_frozen_candidates_no_per_sample_exception",
            self.contract["candidate_scope"]["selection_policy"],
        )

    def test_frozen_input_hashes_match_disk(self) -> None:
        frozen = self.contract["frozen_inputs"]
        for name in ("model_config", "candidate_manifest", "quality_contract"):
            self.assertEqual(
                frozen[f"{name}_sha256"], digest(ROOT / frozen[f"{name}_path"])
            )

    def test_pilot_is_bounded_and_cannot_choose_per_sample_ceiling(self) -> None:
        controls = self.contract["execution_controls"]
        self.assertEqual([256, 384, 512], controls["token_ceiling_candidates"])
        self.assertEqual(480, controls["per_unit_timeout_seconds"])
        self.assertEqual(["eos_token"], controls["legal_stop_reasons"])
        self.assertEqual("disabled", controls["routing_collector"])
        rule = self.contract["common_ceiling_rule"]
        self.assertEqual("forbidden", rule["per_sample_ceiling_selection"])
        self.assertEqual("R5_NOT_ELIGIBLE", rule["no_common_ceiling_disposition"])
        self.assertEqual(12, rule["expected_cells"])
        self.assertEqual("forbidden", rule["early_success_stop"])
        self.assertEqual(
            "complete_full_cartesian_matrix_all_instances_at_all_ceilings",
            rule["schedule"],
        )
        self.assertEqual(
            "fresh_non_G3_no_R4_cell_reuse", rule["session_evidence"]
        )

    def test_qualification_cannot_be_used_as_quality_or_formal_pass(self) -> None:
        boundary = self.contract["claim_boundary"]
        self.assertEqual(
            "bounded_execution_compatibility_only", boundary["establishes"]
        )
        prohibited = set(boundary["does_not_establish"])
        self.assertTrue(
            {"task_correctness", "model_capability", "collector_correctness",
             "formal_c1_acceptance", "cross_pass_alignment"}.issubset(prohibited)
        )
        self.assertFalse(
            self.contract["eligibility_rule"]
            ["answer_correctness_used_for_eligibility"]
        )

    def test_r4_is_hash_bound_and_never_resumable(self) -> None:
        r4 = self.contract["r4_immutability"]
        self.assertEqual("FINAL_FAIL_IMMUTABLE", r4["status"])
        self.assertEqual("EXECUTION_TRUNCATED", r4["failure_class"])
        self.assertEqual("forbidden", r4["resume"])
        self.assertEqual("forbidden", r4["retry_failed"])
        historical = verify_historical_evidence_archive()
        self.assertEqual(
            r4["session_sha256"],
            historical["r4_session_sha256"],
        )
        self.assertEqual(set(EXPECTED_RECORDS), set(historical))

    def test_historical_failure_archive_rejects_metadata_and_blob_tamper(self) -> None:
        original = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
        mutations = []

        metadata = json.loads(json.dumps(original))
        metadata["records"][0]["original_bytes"] += 1
        mutations.append(metadata)

        blob = json.loads(json.dumps(original))
        encoded = blob["records"][0]["gzip_base64"]
        replacement = "A" if encoded[0] != "A" else "B"
        blob["records"][0]["gzip_base64"] = replacement + encoded[1:]
        mutations.append(blob)

        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "archive.json"
                path.write_text(
                    json.dumps(mutation, sort_keys=True), encoding="utf-8"
                )
                with self.assertRaises(HistoricalEvidenceError):
                    verify_historical_evidence_archive(path)

    def test_contract_does_not_authorize_gpu_or_r5(self) -> None:
        self.assertEqual(
            {"gpu_execution": "not_authorized", "paid_gpu": "not_authorized",
             "formal_r5": "not_authorized"},
            self.contract["authority"],
        )

    def test_s4_application_is_cpu_mock_complete_but_gpu_remains_gated(self) -> None:
        self.assertEqual(22, len(self.contract["implementation_status"]))
        self.assertIn(
            "exact_12_cell_ledger_and_minimal_common_ceiling_selector_implemented",
            self.contract["implementation_status"],
        )
        self.assertEqual(
            5, len(self.contract["remaining_gpu_application_conditions"])
        )
        self.assertIn(
            "5_6sol_evaluation_GO_with_empty_blockers",
            self.contract["remaining_gpu_application_conditions"],
        )
        self.assertIn(
            "single_model_load_and_actual_bf16_evidence_enforced",
            self.contract["implementation_status"],
        )
        self.assertIn(
            "terminal_PASS_requires_complete_clean_audits_and_post_audit_deadline",
            self.contract["implementation_status"],
        )


if __name__ == "__main__":
    unittest.main()
