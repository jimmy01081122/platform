from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PHASE7_ROOT = Path(__file__).resolve().parents[1]
SIM_ROOT = PHASE7_ROOT.parent
sys.path.insert(0, str(PHASE7_ROOT))
sys.path.insert(0, str(SIM_ROOT / "tools"))

from adapters.vllm_mock_adapter import (  # noqa: E402
    AdapterContractError,
    adapt_fixture,
    load_strict_json,
    validate_output,
)
from contract_runtime import canonical_bytes  # noqa: E402

FIXTURE_PATH = PHASE7_ROOT / "fixtures" / "mock_vllm_trace.json"
ADAPTER_PATH = PHASE7_ROOT / "adapters" / "vllm_mock_adapter.py"


def fixture() -> dict:
    return load_strict_json(FIXTURE_PATH)


class Phase7AdapterTest(unittest.TestCase):
    def test_mock_conversion_is_deterministic_and_cpu_bounded(self) -> None:
        first = adapt_fixture(fixture())
        second = adapt_fixture(fixture())
        self.assertEqual(first, second)
        self.assertFalse(first["gpu_used"])
        self.assertFalse(first["model_downloaded"])
        self.assertFalse(first["formal_runtime_evidence"])
        self.assertEqual(first["evidence_class"], "SYNTHETIC_CPU_MOCK")
        self.assertEqual(len(first["events"]), 7)
        self.assertEqual(len(first["observations"]), 7)
        self.assertEqual(len(first["semantic_hashes"]["event_rows"]), 7)
        self.assertRegex(
            first["semantic_hashes"]["adapter_output_root"], r"^[0-9a-f]{64}$"
        )

    def test_runtime_variant_binds_adapter_and_collector(self) -> None:
        value = fixture()
        self.assertEqual(
            value["runtime_variant"]["adapter_hash"],
            hashlib.sha256(ADAPTER_PATH.read_bytes()).hexdigest(),
        )
        descriptor = load_strict_json(
            PHASE7_ROOT / "schemas" / "canonical_observation_descriptor.json"
        )
        self.assertEqual(
            value["runtime_variant"]["collector_hash"],
            hashlib.sha256(canonical_bytes(descriptor)).hexdigest(),
        )

    def test_runtime_variant_rejects_missing_extra_and_hash_drift(self) -> None:
        value = fixture()
        del value["runtime_variant"]["scheduler_policy"]
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)
        value = fixture()
        value["runtime_variant"]["unregistered"] = "forbidden"
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)
        value = fixture()
        value["runtime_variant"]["max_sequences"] = 3
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_pass_roles_cover_p0_p5_and_offline_v0(self) -> None:
        value = fixture()
        output = adapt_fixture(value)
        by_id = {item["pass_id"]: item for item in output["pass_contracts"]}
        self.assertEqual(set(by_id), {"P0", "P1", "P2", "P3", "P4", "P5", "V0"})
        self.assertEqual(by_id["P5"]["scope"], "SESSION")
        self.assertEqual(by_id["P5"]["minimum_steady_state_seconds"], 30)
        self.assertEqual(
            (by_id["V0"]["execution_role"], by_id["V0"]["gpu_execution"], by_id["V0"]["runtime_pass"]),
            ("OFFLINE_VALIDATION", False, False),
        )
        value["pass_contracts"][-1]["gpu_execution"] = True
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_observability_normalization_and_calibration_exclusion(self) -> None:
        value = fixture()
        allocation = next(
            item for item in value["raw_events"] if item["raw_event_id"] == "allocation-0"
        )
        self.assertEqual(
            allocation["observability"],
            {
                "availability": "CONDITIONAL",
                "evidence_mode": "NONE",
                "expected_evidence_modes": ["INSTRUMENTED", "DERIVED"],
            },
        )
        allocation["alignment_use"] = "CROSS_DOMAIN_CALIBRATION"
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

        value = fixture()
        value["raw_events"][0]["observability"] = {
            "availability": "UNAVAILABLE",
            "evidence_mode": "DERIVED",
        }
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_mock_measured_claim_fails_closed(self) -> None:
        value = fixture()
        value["raw_events"][0]["observability"]["evidence_mode"] = "MEASURED"
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)
        value = fixture()
        value["routing_records"][0]["observability"]["evidence_mode"] = "MEASURED"
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_alignment_grade_is_rederived_and_gates_cross_domain_use(self) -> None:
        value = fixture()
        alignment = next(
            item
            for item in value["clock_alignments"]
            if item["source_clock_id"] == "device-rank0-tick"
        )
        alignment["confidence_interval_95_fs"]["upper_error_fs"] = "20000"
        alignment["claimed_grade"] = "AGGREGATE_ONLY"
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_alignment_valid_range_is_enforced(self) -> None:
        value = fixture()
        event = next(
            item for item in value["raw_events"] if item["raw_event_id"] == "copy-0"
        )
        event["source_timestamp"] = "10001"
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_alignment_target_and_component_bindings_are_checked(self) -> None:
        value = fixture()
        value["clock_alignments"][0]["grading_inputs"][
            "target_clock_profile_hash"
        ] = "0" * 64
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)
        value = fixture()
        value["clock_alignments"][0]["grading_inputs"][
            "shortest_component_record_hash"
        ] = "0" * 64
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_routing_score_tolerance_and_ambiguity_are_rederived(self) -> None:
        output = adapt_fixture(fixture())
        routing = output["routing_records"][0]
        self.assertEqual(routing["selected_experts"], [0, 1])
        self.assertEqual(routing["ambiguity_set"], [1, 2])
        self.assertEqual(routing["score_dtype"], "float32")
        value = fixture()
        value["routing_records"][0]["ambiguity_set"] = [1]
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)
        value = fixture()
        value["routing_records"][0]["score_tolerance_relative"] = "0.00002"
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_single_and_serving_identity_contracts_are_distinct(self) -> None:
        output = adapt_fixture(fixture())
        identities = {item["mode"]: item for item in output["identity_contracts"]}
        self.assertEqual(
            identities["SINGLE_REQUEST"]["routing_comparison"],
            "TOPK_AMBIGUITY_RULE_WEIGHTS_TOLERANCE_BOUND",
        )
        self.assertEqual(
            set(identities["SERVING_REPLAY"]["observational_fields"]),
            {"batch_formation", "schedule", "stream_ordering", "event_timing"},
        )
        value = fixture()
        serving = next(
            item
            for item in value["identity_contracts"]
            if item["mode"] == "SERVING_REPLAY"
        )
        serving["blocking_fields"].append("schedule")
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)

    def test_event_dependencies_and_semantic_hash_are_fail_closed(self) -> None:
        value = fixture()
        value["raw_events"][1]["dependencies"] = ["missing-event"]
        with self.assertRaises(AdapterContractError):
            adapt_fixture(value)
        output = adapt_fixture(fixture())
        tampered = copy.deepcopy(output)
        tampered["semantic_hashes"]["event_root"] = "0" * 64
        with self.assertRaises(AdapterContractError):
            validate_output(tampered)

    def test_strict_loader_rejects_duplicate_key_and_float(self) -> None:
        text = FIXTURE_PATH.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            duplicate = Path(directory) / "duplicate.json"
            duplicate.write_text(
                text.replace(
                    '"fixture_id": "phase7-vllm-shape-contract-cpu-mock",',
                    '"fixture_id": "a", "fixture_id": "b",',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AdapterContractError):
                load_strict_json(duplicate)
            floating = Path(directory) / "float.json"
            floating.write_text('{"value":0.5}\n', encoding="utf-8")
            with self.assertRaises(AdapterContractError):
                load_strict_json(floating)

    def test_cli_conversion_and_replay_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            converted = subprocess.run(
                [
                    sys.executable,
                    str(ADAPTER_PATH),
                    "--input",
                    str(FIXTURE_PATH),
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(converted.returncode, 0, converted.stderr)
            validated = subprocess.run(
                [
                    sys.executable,
                    str(ADAPTER_PATH),
                    "--input",
                    str(FIXTURE_PATH),
                    "--validate-output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertIn("PASS", validated.stdout)

    def test_schema_documents_are_strict_json_objects(self) -> None:
        for path in sorted((PHASE7_ROOT / "schemas").glob("*.json")):
            value = load_strict_json(path)
            self.assertIsInstance(value, dict)
            if (
                path.name != "canonical_observation_descriptor.json"
                and value.get("type") == "object"
            ):
                self.assertEqual(value.get("additionalProperties"), False)


if __name__ == "__main__":
    unittest.main()
