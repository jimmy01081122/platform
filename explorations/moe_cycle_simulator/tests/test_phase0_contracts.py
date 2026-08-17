from __future__ import annotations

import importlib.util
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_phase0", ROOT / "tools" / "validate_phase0.py"
)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)
sys.path.insert(0, str(ROOT / "tools"))
import contract_runtime as runtime


class Phase0ContractTests(unittest.TestCase):
    def test_schema_documents(self) -> None:
        validator.validate_schema_documents(ROOT)

    def test_architecture_contract(self) -> None:
        validator.validate_architecture(ROOT)

    def test_model_and_benchmark_contract(self) -> None:
        validator.validate_model_and_benchmarks(ROOT)

    def test_semantic_fixtures(self) -> None:
        validator.validate_semantic_fixtures(ROOT)

    def test_checksum_ledger(self) -> None:
        validator.validate_ledger(ROOT)


class SemanticNegativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(
            (ROOT / "fixtures" / "phase0_semantic_fixtures.json").read_text()
        )

    def test_clock_rejects_noncanonical_remainder(self) -> None:
        value = copy.deepcopy(self.fixture["clocks"][1])
        value["fractional_remainder"] = "1"
        with self.assertRaises(runtime.ContractError):
            runtime.Clock.from_record(value)

    def test_clock_rejects_noncanonical_frequency_and_uint64_overflow(self) -> None:
        value = copy.deepcopy(self.fixture["clocks"][1])
        value["frequency_numerator_hz"] = "4800000000"
        value["frequency_denominator_hz"] = "2"
        with self.assertRaises(runtime.ContractError):
            runtime.Clock.from_record(value)
        value = copy.deepcopy(self.fixture["clocks"][0])
        value["local_cycle"] = str(1 << 64)
        with self.assertRaises(runtime.ContractError):
            runtime.Clock.from_record(value)

    def test_clock_has_zero_drift_at_large_cycle(self) -> None:
        clock = runtime.Clock.from_record(self.fixture["clocks"][1])
        cycle = 10**12
        self.assertEqual(
            clock.edge_time(cycle),
            cycle * runtime.FS_PER_SECOND // 2_400_000_000,
        )
        self.assertEqual(
            clock.remainder(cycle),
            cycle * runtime.FS_PER_SECOND % 2_400_000_000,
        )

    def test_ceil_edge_exact_and_adjacent(self) -> None:
        clock = runtime.Clock.from_record(self.fixture["clocks"][1])
        edge = clock.edge_time(3)
        self.assertEqual(clock.ceil_edge(edge), 3)
        self.assertEqual(clock.ceil_edge(edge + 1), 4)

    def test_bridge_rejects_zero_progress_cycle(self) -> None:
        clocks = {
            item["clock_id"]: runtime.Clock.from_record(item)
            for item in self.fixture["clocks"]
        }
        value = copy.deepcopy(self.fixture["bridges"][0])
        value.update(
            source_clock_id="cpu",
            target_clock_id="cpu",
            forward_latency_fs="0",
            receiver_sync_cycles=0,
        )
        with self.assertRaises(runtime.ContractError):
            runtime.validate_bridge(value, clocks)

    def test_bridge_rejects_protocol_policy_mismatch(self) -> None:
        clocks = {
            item["clock_id"]: runtime.Clock.from_record(item)
            for item in self.fixture["clocks"]
        }
        value = copy.deepcopy(self.fixture["bridges"][0])
        value["protocol"] = "CREDIT"
        with self.assertRaises(runtime.ContractError):
            runtime.validate_bridge(value, clocks)

    def test_bridge_rejects_cross_domain_zero_progress(self) -> None:
        clocks = {
            item["clock_id"]: runtime.Clock.from_record(item)
            for item in self.fixture["clocks"]
        }
        value = copy.deepcopy(self.fixture["bridges"][0])
        value["forward_latency_fs"] = "0"
        value["receiver_sync_cycles"] = 0
        with self.assertRaises(runtime.ContractError):
            runtime.validate_bridge(value, clocks)

    def test_alignment_rejects_false_grade(self) -> None:
        value = copy.deepcopy(self.fixture["alignments"][0])
        value["claimed_grade"] = "AGGREGATE_ONLY"
        with self.assertRaises(runtime.ContractError):
            runtime.validate_alignment(value)

    def test_identity_rejects_non_identity_scale(self) -> None:
        value = copy.deepcopy(self.fixture["alignments"][0])
        value["scale_numerator"] = "2"
        with self.assertRaises(runtime.ContractError):
            runtime.validate_alignment(value)

    def test_alignment_rejects_inverted_ci_and_negative_target(self) -> None:
        value = copy.deepcopy(self.fixture["alignments"][0])
        value["confidence_interval_95_fs"] = {
            "lower_error_fs": "10",
            "upper_error_fs": "-10",
        }
        with self.assertRaises(runtime.ContractError):
            runtime.validate_alignment(value)
        value = copy.deepcopy(self.fixture["alignments"][0])
        value["offset_fs"] = "-1"
        with self.assertRaises(runtime.ContractError):
            runtime.validate_alignment(value)

    def test_alignment_rejects_out_of_range(self) -> None:
        value = self.fixture["alignments"][0]
        with self.assertRaises(runtime.ContractError):
            runtime.transform_alignment(value, 10_000_001)

    def test_affine_alignment(self) -> None:
        value = copy.deepcopy(self.fixture["alignments"][0])
        value.update(
            transform_type="AFFINE_RATIONAL",
            scale_numerator="2",
            scale_denominator="1",
            offset_fs="10",
            calibration_points=[
                {"source_time": "0", "target_time": "10"},
                {"source_time": "100", "target_time": "210"},
            ],
        )
        runtime.validate_alignment(value)
        self.assertEqual(runtime.transform_alignment(value, 50), 110)

    def test_piecewise_alignment_and_overlap_rejection(self) -> None:
        value = copy.deepcopy(self.fixture["alignments"][0])
        value.pop("scale_numerator")
        value.pop("scale_denominator")
        value.pop("offset_fs")
        value.update(
            transform_type="PIECEWISE_AFFINE_RATIONAL",
            segments=[
                {
                    "source_start": "0", "source_end": "100",
                    "end_inclusive": False, "scale_numerator": "1",
                    "scale_denominator": "1", "offset_fs": "0",
                    "boundary_discontinuity": False,
                },
                {
                    "source_start": "100", "source_end": "200",
                    "end_inclusive": True, "scale_numerator": "2",
                    "scale_denominator": "1", "offset_fs": "-100",
                    "boundary_discontinuity": False,
                },
            ],
            calibration_points=[
                {"source_time": "0", "target_time": "0"},
                {"source_time": "50", "target_time": "50"},
                {"source_time": "100", "target_time": "100"},
                {"source_time": "150", "target_time": "200"},
            ],
            valid_time_range={"source_start": "0", "source_end": "200"},
        )
        runtime.validate_alignment(value)
        self.assertEqual(runtime.transform_alignment(value, 150), 200)
        value["segments"][1]["source_start"] = "99"
        with self.assertRaises(runtime.ContractError):
            runtime.validate_alignment(value)

    def test_piecewise_discontinuity_cannot_be_cycle_grade(self) -> None:
        value = copy.deepcopy(self.fixture["alignments"][0])
        value.pop("scale_numerator")
        value.pop("scale_denominator")
        value.pop("offset_fs")
        value.update(
            transform_type="PIECEWISE_AFFINE_RATIONAL",
            segments=[
                {
                    "source_start": "0", "source_end": "100",
                    "end_inclusive": False, "scale_numerator": "1",
                    "scale_denominator": "1", "offset_fs": "0",
                    "boundary_discontinuity": True,
                },
                {
                    "source_start": "100", "source_end": "200",
                    "end_inclusive": True, "scale_numerator": "1",
                    "scale_denominator": "1", "offset_fs": "0",
                    "boundary_discontinuity": False,
                },
            ],
            calibration_points=[
                {"source_time": "0", "target_time": "0"},
                {"source_time": "50", "target_time": "50"},
                {"source_time": "100", "target_time": "100"},
                {"source_time": "150", "target_time": "150"},
            ],
            valid_time_range={"source_start": "0", "source_end": "200"},
            claimed_grade="CYCLE_GRADE",
        )
        with self.assertRaises(runtime.ContractError):
            runtime.validate_alignment(value)

    def test_observability_rejects_empty_conditional_modes(self) -> None:
        value = {
            "availability": "CONDITIONAL",
            "evidence_mode": "NONE",
            "expected_evidence_modes": [],
        }
        with self.assertRaises(runtime.ContractError):
            runtime.validate_observability(value)

    def test_formal_gate_rejects_extrapolation(self) -> None:
        value = copy.deepcopy(self.fixture["result_evidence"][1])
        value["range_status"] = "EXTRAPOLATED"
        with self.assertRaises(runtime.ContractError):
            runtime.validate_result_evidence(value, "BREAK_EVEN_PASS")

    def test_routing_rejects_wrong_top_k(self) -> None:
        value = copy.deepcopy(self.fixture["routing"][0])
        value["selected_experts"] = [0, 2]
        with self.assertRaises(runtime.ContractError):
            runtime.validate_routing(value)

    def test_routing_rejects_wrong_ambiguity_set(self) -> None:
        value = copy.deepcopy(self.fixture["routing"][0])
        value["ambiguity_set"] = [1]
        with self.assertRaises(runtime.ContractError):
            runtime.validate_routing(value)

    def test_routing_unavailable_cannot_carry_scores(self) -> None:
        value = copy.deepcopy(self.fixture["routing"][0])
        value["observability"] = {
            "schema_version": "observability-v1",
            "availability": "UNAVAILABLE",
            "evidence_mode": "NONE",
            "reason": "router scores are not exposed",
        }
        with self.assertRaises(runtime.ContractError):
            runtime.validate_routing(value)

    def test_routing_rejects_tolerance_widening_and_rounded_score(self) -> None:
        value = copy.deepcopy(self.fixture["routing"][0])
        value["score_tolerance_relative"] = "1"
        value["ambiguity_set"] = list(range(8))
        with self.assertRaises(runtime.ContractError):
            runtime.validate_routing(value)
        value = copy.deepcopy(self.fixture["routing"][0])
        value["canonical_scores"][1] = "0.9"
        value["k_boundary_score"] = "0.9"
        with self.assertRaises(runtime.ContractError):
            runtime.validate_routing(value)

    def test_events_reject_dependency_cycle(self) -> None:
        values = copy.deepcopy(self.fixture["events"])
        values[0]["dependencies"] = ["compute-01"]
        clocks = {
            item["clock_id"]: runtime.Clock.from_record(item)
            for item in self.fixture["clocks"]
        }
        priorities = {
            item["name"]: item["value"]
            for item in json.loads(
                (ROOT / "contracts" / "event_priorities.json").read_text()
            )["priorities"]
        }
        with self.assertRaises(runtime.ContractError):
            runtime.validate_events(values, clocks, priorities)

    def test_event_order_is_shuffle_invariant(self) -> None:
        values = self.fixture["events"]
        clocks = {
            item["clock_id"]: runtime.Clock.from_record(item)
            for item in self.fixture["clocks"]
        }
        priorities = {
            item["name"]: item["value"]
            for item in json.loads(
                (ROOT / "contracts" / "event_priorities.json").read_text()
            )["priorities"]
        }
        forward = runtime.validate_events(copy.deepcopy(values), clocks, priorities)
        reverse = runtime.validate_events(
            list(reversed(copy.deepcopy(values))), clocks, priorities
        )
        self.assertEqual(
            [item["event_id"] for item in forward],
            [item["event_id"] for item in reverse],
        )

    def test_event_attributes_reject_float(self) -> None:
        values = copy.deepcopy(self.fixture["events"])
        values[0]["attributes"]["forbidden"] = 1.0
        clocks = {
            item["clock_id"]: runtime.Clock.from_record(item)
            for item in self.fixture["clocks"]
        }
        priorities = {
            item["name"]: item["value"]
            for item in json.loads(
                (ROOT / "contracts" / "event_priorities.json").read_text()
            )["priorities"]
        }
        with self.assertRaises(runtime.ContractError):
            runtime.validate_events(values, clocks, priorities)

    def test_same_time_dependency_must_sort_before_consumer(self) -> None:
        values = copy.deepcopy(self.fixture["events"])
        values[1]["time_fs"] = "0"
        values[1]["event_type"] = "RESOURCE_RELEASE"
        values[1]["event_priority"] = 10
        clocks = {
            item["clock_id"]: runtime.Clock.from_record(item)
            for item in self.fixture["clocks"]
        }
        priorities = {
            item["name"]: item["value"]
            for item in json.loads(
                (ROOT / "contracts" / "event_priorities.json").read_text()
            )["priorities"]
        }
        with self.assertRaises(runtime.ContractError):
            runtime.validate_events(values, clocks, priorities)

    def test_runtime_variant_hash_drift(self) -> None:
        value = copy.deepcopy(self.fixture["runtime_variants"][0])
        runtime.validate_runtime_variant(value)
        value["collector_hash"] = "5" * 64
        with self.assertRaises(runtime.ContractError):
            runtime.validate_runtime_variant(value)

    def test_semantic_hash_rejects_float(self) -> None:
        with self.assertRaises(runtime.ContractError):
            runtime.canonical_bytes({"value": 1.0})

    def test_schema_rejects_unknown_field(self) -> None:
        validators = validator._schema_validators(ROOT)
        value = copy.deepcopy(self.fixture["clocks"][0])
        value["unknown"] = "forbidden"
        with self.assertRaises(Exception):
            validators["clock_domain.schema.json"].validate(value)

    def test_loader_rejects_duplicate_json_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"key":1,"key":2}', encoding="utf-8")
            with self.assertRaises(validator.ValidationFailure):
                validator.load_json(path)

    def test_semantic_hash_rejects_duplicate_primary_key(self) -> None:
        semantic = self.fixture["semantic_hash"]
        rows = [semantic["rows"][0], copy.deepcopy(semantic["rows"][0])]
        with self.assertRaises(runtime.ContractError):
            runtime.dataset_semantic_hash(rows, semantic["descriptor"])

    def test_cpp_semantic_hash_golden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "semantic_hash_golden"
            subprocess.run(
                [
                    "c++", "-std=c++20",
                    str(ROOT / "tools" / "semantic_hash_golden.cpp"),
                    "-lcrypto", "-o", str(binary),
                ],
                check=True,
            )
            completed = subprocess.run(
                [str(binary)], check=True, text=True, capture_output=True
            )
            self.assertIn("PASS", completed.stdout)

    def test_arrow_physical_layout_does_not_change_semantic_hash(self) -> None:
        import pyarrow as pa
        import pyarrow.ipc as ipc

        semantic = self.fixture["semantic_hash"]
        rows = semantic["rows"]
        expected = semantic["expected_aggregate_hash"]
        table = pa.Table.from_pylist(rows)
        for compression in (None, "zstd"):
            sink = pa.BufferOutputStream()
            options = ipc.IpcWriteOptions(compression=compression)
            with ipc.new_stream(sink, table.schema, options=options) as writer:
                writer.write_table(table, max_chunksize=1)
            recovered = ipc.open_stream(sink.getvalue()).read_all().to_pylist()
            _, actual = runtime.dataset_semantic_hash(
                recovered, semantic["descriptor"]
            )
            self.assertEqual(actual, expected)
        dictionary_table = table.set_column(
            table.schema.get_field_index("label"),
            "label",
            table["label"].dictionary_encode(),
        )
        recovered = dictionary_table.combine_chunks().to_pylist()
        _, actual = runtime.dataset_semantic_hash(
            recovered, semantic["descriptor"]
        )
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
