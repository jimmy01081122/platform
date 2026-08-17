from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PHASE2_ROOT = Path(__file__).resolve().parents[1]
SIM_ROOT = PHASE2_ROOT.parent
sys.path.insert(0, str(PHASE2_ROOT))
sys.path.insert(0, str(SIM_ROOT / "tools"))

from build_fixture import build_fixture  # noqa: E402
from canonical_ir import (  # noqa: E402
    CanonicalIRError,
    IR_KINDS,
    calibration_profile_id,
    load_contracts,
    read_bundle,
    semantic_hashes,
    strict_json_bytes,
    validate_records,
    write_bundle,
)
from contract_runtime import (  # noqa: E402
    canonical_bytes,
    dataset_semantic_hash,
    validate_runtime_variant,
)
import canonical_ir  # noqa: E402

FIXTURE_PATH = PHASE2_ROOT / "fixtures" / "canonical_ir_bundle.json"


def fixture() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["records"]


def by_kind(records: list[dict], kind: str) -> list[dict]:
    return [record for record in records if record["ir_kind"] == kind]


def one(records: list[dict], kind: str) -> dict:
    values = by_kind(records, kind)
    assert len(values) == 1
    return values[0]


def make_measured_formal(
    records: list[dict],
) -> dict[str, dict]:
    calibration = one(records, "CalibrationIR")["payload"]
    result = one(records, "ResultIR")["payload"]
    calibration.update(
        {
            "evidence_class": "MEASURED",
            "fidelity": "MEASURED",
            "range_status": "IN_CALIBRATION_ENVELOPE",
            "calibration_envelope": {
                "dimensions": [
                    {"name": "service_units", "lower": "1", "upper": "1"}
                ]
            },
            "evaluation_coordinate": [
                {"name": "service_units", "value": "1"}
            ],
        }
    )
    _, catalog, _ = load_contracts()

    def root(kind: str) -> str:
        return dataset_semantic_hash(
            by_kind(records, kind), catalog["descriptors"][kind]
        )[1]

    profile = {
        "schema_version": "calibration-profile-v1",
        "profile_id": "0" * 64,
        "runtime_variant_hash": calibration["runtime_variant_hash"],
        "model": {
            "record_id": calibration["model_record_id"],
            "semantic_root": root("ModelIR"),
        },
        "platform": {
            "record_id": calibration["platform_record_id"],
            "semantic_root": root("PlatformIR"),
        },
        "training_workloads": [
            {"record_id": item, "semantic_root": root("WorkloadIR")}
            for item in calibration["training_workload_record_ids"]
        ],
        "held_out_workloads": [
            {"record_id": item, "semantic_root": root("WorkloadIR")}
            for item in calibration["held_out_workload_record_ids"]
        ],
        "metric": calibration["metric"],
        "unit": calibration["unit"],
        "evidence_artifact_roots": ["e" * 64],
    }
    profile["profile_id"] = calibration_profile_id(profile)
    calibration["calibration_profile_hash"] = profile["profile_id"]
    result.update(
        {
            "formal_pass": True,
            "range_status": "IN_CALIBRATION_ENVELOPE",
            "evidence_class": "MEASURED",
            "evidence_availability": "CONFIRMED",
        }
    )
    refresh_refs(records)
    return {profile["profile_id"]: profile}


def refresh_refs(records: list[dict]) -> None:
    _, catalog, _ = load_contracts()

    def root(kind: str) -> str:
        return dataset_semantic_hash(
            by_kind(records, kind), catalog["descriptors"][kind]
        )[1]

    def ref(kind: str, record_id: str) -> dict:
        return {
            "target_ir_kind": kind,
            "target_schema_version": "canonical-ir-v1",
            "target_semantic_root": root(kind),
            "target_primary_key": {"record_id": record_id},
        }

    order = [
        "ModelIR",
        "PlatformIR",
        "WorkloadIR",
        "ClockAlignmentIR",
        "PlacementIR",
        "RoutingIR",
        "EventIR",
        "CalibrationIR",
        "ResultIR",
    ]
    for kind_in_order in order:
        for record in by_kind(records, kind_in_order):
            payload = record["payload"]
            pairs: list[tuple[str, str]] = []
            if record["ir_kind"] == "WorkloadIR":
                pairs = [("ModelIR", payload["model_record_id"])]
            elif record["ir_kind"] == "RoutingIR":
                pairs = [
                    ("ModelIR", payload["model_record_id"]),
                    ("WorkloadIR", payload["workload_record_id"]),
                ]
            elif record["ir_kind"] == "PlacementIR":
                pairs = [
                    ("ModelIR", payload["model_record_id"]),
                    ("PlatformIR", payload["platform_record_id"]),
                ]
            elif record["ir_kind"] == "ClockAlignmentIR":
                pairs = [("PlatformIR", payload["platform_record_id"])]
            elif record["ir_kind"] == "EventIR":
                pairs = [
                    ("PlatformIR", payload["platform_record_id"]),
                    ("PlacementIR", payload["placement_record_id"]),
                    ("ClockAlignmentIR", payload["alignment_record_id"]),
                ]
                if payload["workload_record_id"] is not None:
                    pairs.append(("WorkloadIR", payload["workload_record_id"]))
            elif record["ir_kind"] == "CalibrationIR":
                pairs = [
                    ("ModelIR", payload["model_record_id"]),
                    ("PlatformIR", payload["platform_record_id"]),
                    *[
                        ("WorkloadIR", record_id)
                        for record_id in payload[
                            "training_workload_record_ids"
                        ]
                    ],
                    *[
                        ("WorkloadIR", record_id)
                        for record_id in payload[
                            "held_out_workload_record_ids"
                        ]
                    ],
                ]
            elif record["ir_kind"] == "ResultIR":
                pairs = [
                    ("WorkloadIR", payload["workload_record_id"]),
                    ("ModelIR", payload["model_record_id"]),
                    ("PlatformIR", payload["platform_record_id"]),
                    ("CalibrationIR", payload["calibration_record_id"]),
                ]
            record["refs"] = [
                ref(kind, record_id) for kind, record_id in pairs
            ]


def test_fixture_has_all_nine_ir_kinds() -> None:
    records = validate_records(fixture())
    assert {record["ir_kind"] for record in records} == IR_KINDS
    assert len(records) == 11


def test_fixture_generator_is_deterministic() -> None:
    assert build_fixture() == fixture()


def test_arrow_zstd_bundle_round_trip(tmp_path: Path) -> None:
    records = fixture()
    expected_root = semantic_hashes(records)[1]
    envelope = write_bundle(tmp_path / "bundle", records)
    decoded, observed = read_bundle(
        tmp_path / "bundle" / "artifact-envelope.json"
    )
    assert decoded == validate_records(records)
    assert envelope == observed
    assert observed["artifact_id"] == expected_root
    assert len(observed["partitions"]) == 9


def test_input_order_and_batch_layout_do_not_change_semantic_root(
    tmp_path: Path,
) -> None:
    first = write_bundle(tmp_path / "one", fixture(), batch_size=1)
    second = write_bundle(
        tmp_path / "many", list(reversed(fixture())), batch_size=65_536
    )
    assert first["bundle_semantic_root"] == second["bundle_semantic_root"]
    first_event = next(
        item for item in first["partitions"] if item["ir_kind"] == "EventIR"
    )
    second_event = next(
        item for item in second["partitions"] if item["ir_kind"] == "EventIR"
    )
    assert first_event["file_sha256"] != second_event["file_sha256"]


def test_existing_bundle_target_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    write_bundle(target, fixture())
    with pytest.raises(CanonicalIRError):
        write_bundle(target, fixture())


def test_duplicate_json_key_is_rejected() -> None:
    with pytest.raises(CanonicalIRError):
        strict_json_bytes(b'{"a":1,"a":2}')
    with pytest.raises(CanonicalIRError):
        strict_json_bytes('{"é":1,"é":2}'.encode("utf-8"))


def test_noncanonical_json_and_float_are_rejected() -> None:
    with pytest.raises(CanonicalIRError):
        strict_json_bytes(b'{"b":1, "a":2}')
    records = fixture()
    one(records, "ResultIR")["payload"]["invalid"] = 0.5
    with pytest.raises(CanonicalIRError):
        validate_records(records)


def test_u128_overflow_is_rejected() -> None:
    records = fixture()
    one(records, "ResultIR")["payload"]["latency_fs"] = str(1 << 128)
    with pytest.raises(CanonicalIRError, match="unsigned bound"):
        validate_records(records)


def test_extra_or_missing_field_is_rejected() -> None:
    records = fixture()
    one(records, "ModelIR")["payload"]["unknown"] = "x"
    with pytest.raises(Exception):
        validate_records(records)
    records = fixture()
    del one(records, "ModelIR")["payload"]["layers"]
    with pytest.raises(Exception):
        validate_records(records)


def test_wrong_root_and_dangling_typed_reference_are_rejected() -> None:
    records = fixture()
    by_kind(records, "WorkloadIR")[0]["refs"][0][
        "target_semantic_root"
    ] = "0" * 64
    with pytest.raises(CanonicalIRError, match="root mismatch"):
        validate_records(records)
    records = fixture()
    workload = by_kind(records, "WorkloadIR")[0]
    workload["payload"]["model_record_id"] = "missing"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="missing"):
        validate_records(records)


def test_workload_token_identity_is_enforced() -> None:
    records = fixture()
    by_kind(records, "WorkloadIR")[0]["payload"]["tokens"][1][
        "position"
    ] = 4
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="positions"):
        validate_records(records)


def test_model_tensor_byte_conservation_is_enforced() -> None:
    records = fixture()
    one(records, "ModelIR")["payload"]["tensors"][0]["exact_bytes"] = "999"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="byte conservation"):
        validate_records(records)


def test_token_and_aggregate_routing_contracts() -> None:
    records = fixture()
    routing = one(records, "RoutingIR")["payload"]
    routing["selected_experts"] = [0]
    with pytest.raises(CanonicalIRError, match="top_k"):
        validate_records(records)
    records = fixture()
    routing = one(records, "RoutingIR")["payload"]
    routing.update(
        {
            "routing_scope": "AGGREGATE",
            "token_index": None,
                "canonical_scores": None,
                "score_tolerance_absolute": None,
                "score_tolerance_relative": None,
                "selected_experts": None,
                "k_boundary_score": None,
            "aggregate_expert_demand": ["1"] * 8,
        }
    )
    validate_records(records)


def test_routing_top_k_boundary_and_ambiguity_are_rederived() -> None:
    records = fixture()
    routing = one(records, "RoutingIR")["payload"]
    routing["selected_experts"] = [6, 7]
    with pytest.raises(CanonicalIRError, match="top-k"):
        validate_records(records)

    records = fixture()
    routing = one(records, "RoutingIR")["payload"]
    routing["ambiguity_set"] = [6, 7]
    with pytest.raises(CanonicalIRError, match="ambiguity"):
        validate_records(records)


def test_placement_capacity_and_owner_coverage_are_enforced() -> None:
    records = fixture()
    platform = one(records, "PlatformIR")["payload"]
    next(
        item for item in platform["memory_domains"] if item["domain_id"] == "vram0"
    )["capacity_bytes"] = "7999"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="capacity"):
        validate_records(records)

    records = fixture()
    placement = one(records, "PlacementIR")["payload"]
    duplicate = copy.deepcopy(placement["expert_locations"][0])
    duplicate["replica_id"] = "second-owner"
    placement["expert_locations"].append(duplicate)
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="owner coverage"):
        validate_records(records)


def test_platform_endpoint_clock_and_queue_closure() -> None:
    records = fixture()
    one(records, "PlatformIR")["payload"]["interconnects"][0][
        "destination_domain_id"
    ] = "missing"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="unknown domain"):
        validate_records(records)


def test_event_time_regression_and_cycle_are_rejected() -> None:
    records = fixture()
    events = {record["record_id"]: record for record in by_kind(records, "EventIR")}
    events["event-compute-start"]["payload"]["source_timestamp"] = "1600000"
    events["event-compute-start"]["payload"]["time_fs"] = "4001000"
    events["event-compute-start"]["payload"]["aligned_interval_fs"] = {
        "lower_fs": "4001000",
        "upper_fs": "4001000",
    }
    with pytest.raises(CanonicalIRError, match="after dependent"):
        validate_records(records)

    records = fixture()
    events = {record["record_id"]: record for record in by_kind(records, "EventIR")}
    events["event-compute-start"]["payload"]["source_timestamp"] = "1200000"
    events["event-compute-start"]["payload"]["time_fs"] = "3001000"
    events["event-compute-start"]["payload"]["aligned_interval_fs"] = {
        "lower_fs": "3001000",
        "upper_fs": "3001000",
    }
    events["event-compute-start"]["payload"]["dependencies"] = [
        "event-compute-complete"
    ]
    with pytest.raises(
        CanonicalIRError, match="cycle|same-time dependency"
    ):
        validate_records(records)


def test_alignment_rational_and_uncertainty_are_enforced() -> None:
    records = fixture()
    alignment = one(records, "ClockAlignmentIR")["payload"]
    alignment["scale_numerator"] = "10"
    alignment["scale_denominator"] = "4"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="not normalized"):
        validate_records(records)


def test_alignment_grade_and_valid_range_are_rederived() -> None:
    records = fixture()
    one(records, "ClockAlignmentIR")["payload"]["claimed_grade"] = "AGGREGATE_ONLY"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="grade"):
        validate_records(records)

    records = fixture()
    by_kind(records, "EventIR")[0]["payload"]["source_timestamp"] = "2000001"
    with pytest.raises(CanonicalIRError, match="valid time range"):
        validate_records(records)

    records = fixture()
    event = by_kind(records, "EventIR")[0]["payload"]
    event["alignment_grade"] = "ORDERING_ONLY"
    with pytest.raises(CanonicalIRError, match="grade"):
        validate_records(records)

    records = fixture()
    event = by_kind(records, "EventIR")[0]["payload"]
    event["source_timestamp"] = "999999"
    with pytest.raises(CanonicalIRError, match="lineage"):
        validate_records(records)


def test_calibration_split_and_formal_result_boundaries() -> None:
    records = fixture()
    calibration = one(records, "CalibrationIR")["payload"]
    calibration["held_out_workload_record_ids"] = list(
        calibration["training_workload_record_ids"]
    )
    _, catalog, _ = load_contracts()
    calibration_root = dataset_semantic_hash(
        by_kind(records, "CalibrationIR"),
        catalog["descriptors"]["CalibrationIR"],
    )[1]
    next(
        item
        for item in one(records, "ResultIR")["refs"]
        if item["target_ir_kind"] == "CalibrationIR"
    )["target_semantic_root"] = calibration_root
    with pytest.raises(CanonicalIRError, match="overlap"):
        validate_records(records)

    records = fixture()
    result = one(records, "ResultIR")["payload"]
    result["range_status"] = "EXTRAPOLATED"
    result["formal_pass"] = True
    with pytest.raises(CanonicalIRError, match="lineage"):
        validate_records(records)


def test_synthetic_bundle_cannot_cross_formal_claim_boundary() -> None:
    records = fixture()
    profiles = make_measured_formal(records)
    with pytest.raises(CanonicalIRError, match="formal"):
        validate_records(
            records,
            bundle_evidence_class="SYNTHETIC",
            calibration_profiles=profiles,
        )


def test_formal_result_requires_confirmed_and_eligible_range() -> None:
    valid = fixture()
    profiles = make_measured_formal(valid)
    validate_records(
        valid,
        bundle_evidence_class="MEASURED",
        calibration_profiles=profiles,
    )

    for availability in ("CONDITIONAL", "UNAVAILABLE", "NOT_APPLICABLE"):
        records = fixture()
        profiles = make_measured_formal(records)
        one(records, "ResultIR")["payload"][
            "evidence_availability"
        ] = availability
        with pytest.raises(CanonicalIRError, match="formal"):
            validate_records(
                records,
                bundle_evidence_class="MEASURED",
                calibration_profiles=profiles,
            )

    records = fixture()
    profiles = make_measured_formal(records)
    calibration = one(records, "CalibrationIR")["payload"]
    result = one(records, "ResultIR")["payload"]
    calibration["calibration_envelope"] = None
    calibration["evaluation_coordinate"] = None
    calibration["range_status"] = "RANGE_UNKNOWN"
    result["range_status"] = "RANGE_UNKNOWN"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="formal"):
        validate_records(
            records,
            bundle_evidence_class="MEASURED",
            calibration_profiles=profiles,
        )


def test_calibration_dimensions_ci_and_profile_are_fail_closed(
    tmp_path: Path,
) -> None:
    records = fixture()
    profiles = make_measured_formal(records)
    calibration = one(records, "CalibrationIR")["payload"]
    calibration["calibration_envelope"]["dimensions"].append(
        {"name": "service_units", "lower": "0", "upper": "2"}
    )
    calibration["evaluation_coordinate"].append(
        {"name": "service_units", "value": "2"}
    )
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="duplicate name"):
        validate_records(
            records,
            bundle_evidence_class="MEASURED",
            calibration_profiles=profiles,
        )

    records = fixture()
    calibration = one(records, "CalibrationIR")["payload"]
    calibration["bootstrap_ci_95"] = {"lower": "10", "upper": "1"}
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="bootstrap"):
        validate_records(records)

    records = fixture()
    profiles = make_measured_formal(records)
    with pytest.raises(CanonicalIRError, match="profile artifact"):
        validate_records(records, bundle_evidence_class="MEASURED")
    write_bundle(
        tmp_path / "measured",
        records,
        evidence_class="MEASURED",
        calibration_profiles=list(profiles.values()),
    )
    decoded, envelope = read_bundle(
        tmp_path / "measured" / "artifact-envelope.json"
    )
    assert decoded == validate_records(
        records,
        bundle_evidence_class="MEASURED",
        calibration_profiles=profiles,
    )
    profile = next(iter(profiles.values()))
    assert profile["profile_id"] in envelope["source_artifact_refs"]
    assert profile["evidence_artifact_roots"][0] in envelope[
        "source_artifact_refs"
    ]


def test_phase0_runtime_variant_contract_is_reused() -> None:
    import jsonschema

    runtime = json.loads(
        (
            PHASE2_ROOT / "contracts" / "runtime_variant_fixture.json"
        ).read_text()
    )
    schema = json.loads(
        (SIM_ROOT / "schemas" / "runtime_variant.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(runtime)
    validate_runtime_variant(runtime)


def test_schema_driven_integer_abi_rejects_u128_overflow() -> None:
    overflow = str(1 << 128)
    mutations = [
        (
            "PlatformIR",
            lambda payload: payload["bridges"][0].__setitem__(
                "reverse_latency_fs", overflow
            ),
        ),
        (
            "EventIR",
            lambda payload: payload.__setitem__("service_demand", overflow),
        ),
        (
            "PlacementIR",
            lambda payload: payload.__setitem__("valid_to_fs", overflow),
        ),
    ]
    for kind, mutate in mutations:
        records = fixture()
        mutate(by_kind(records, kind)[0]["payload"])
        if kind != "EventIR":
            refresh_refs(records)
        with pytest.raises(CanonicalIRError, match="unsigned bound"):
            validate_records(records)


def test_schema_driven_integer_abi_rejects_s128_overflow() -> None:
    for overflow in (str(1 << 127), str(-(1 << 127) - 1)):
        records = fixture()
        one(records, "ClockAlignmentIR")["payload"]["offset_fs"] = overflow
        refresh_refs(records)
        with pytest.raises(CanonicalIRError, match="signed 128 overflow"):
            validate_records(records)


def test_phase0_bridge_credit_and_progress_contract_is_reused() -> None:
    import jsonschema

    records = fixture()
    bridge = one(records, "PlatformIR")["payload"]["bridges"][0]
    bridge["protocol"] = "CREDIT"
    bridge["backpressure_policy"] = "CREDIT_BLOCK"
    phase0_schema = json.loads(
        (SIM_ROOT / "schemas" / "bridge.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(phase0_schema).validate(bridge)
    refresh_refs(records)
    validate_records(records)

    invalid_mutations = [
        lambda value: value.update(
            {"protocol": "CREDIT", "backpressure_policy": "STALL_SOURCE"}
        ),
        lambda value: value.update(
            {
                "protocol": "REQUEST_ACK",
                "backpressure_policy": "CREDIT_BLOCK",
            }
        ),
        lambda value: value.update(
            {"forward_latency_fs": "0", "receiver_sync_cycles": 0}
        ),
    ]
    for mutate in invalid_mutations:
        records = fixture()
        mutate(one(records, "PlatformIR")["payload"]["bridges"][0])
        refresh_refs(records)
        with pytest.raises(CanonicalIRError):
            validate_records(records)


def test_phase0_event_order_and_priority_contract_is_reused() -> None:
    records = fixture()
    start = by_kind(records, "EventIR")[0]
    start["payload"]["token_index"] = None
    start["payload"]["layer_index"] = None
    validate_records(records)

    for event_type, priority in (
        ("UNKNOWN_EVENT", 999),
        ("COMPUTE_START", 999),
    ):
        records = fixture()
        event = by_kind(records, "EventIR")[0]["payload"]
        event["event_type"] = event_type
        event["event_priority"] = priority
        with pytest.raises(CanonicalIRError, match="priority"):
            validate_records(records)

    for field, value in (
        ("token_index", (1 << 64) - 1),
        ("layer_index", (1 << 32) - 1),
    ):
        records = fixture()
        by_kind(records, "EventIR")[0]["payload"][field] = value
        with pytest.raises(Exception):
            validate_records(records)

    records = fixture()
    del by_kind(records, "EventIR")[0]["payload"]["token_index"]
    with pytest.raises(Exception):
        validate_records(records)


def test_embedded_runtime_and_calibration_artifacts_are_content_bound(
    tmp_path: Path,
) -> None:
    records = fixture()
    profiles = make_measured_formal(records)
    write_bundle(
        tmp_path / "base",
        records,
        evidence_class="MEASURED",
        calibration_profiles=list(profiles.values()),
    )

    runtime_tamper = tmp_path / "runtime-tamper"
    shutil.copytree(tmp_path / "base", runtime_tamper)
    runtime_envelope_path = runtime_tamper / "artifact-envelope.json"
    runtime_envelope = json.loads(runtime_envelope_path.read_text())
    runtime_envelope["runtime_variants"][0]["seed"] += 1
    runtime_envelope_path.write_bytes(canonical_bytes(runtime_envelope) + b"\n")
    with pytest.raises((CanonicalIRError, ValueError)):
        read_bundle(runtime_envelope_path)

    profile_tamper = tmp_path / "profile-tamper"
    shutil.copytree(tmp_path / "base", profile_tamper)
    profile_envelope_path = profile_tamper / "artifact-envelope.json"
    profile_envelope = json.loads(profile_envelope_path.read_text())
    profile_envelope["calibration_profiles"][0]["metric"] = "tampered"
    profile_envelope_path.write_bytes(canonical_bytes(profile_envelope) + b"\n")
    with pytest.raises(CanonicalIRError, match="profile identity"):
        read_bundle(profile_envelope_path)


def test_model_request_layer_and_runtime_lineage_are_closed() -> None:
    records = fixture()
    second_model = copy.deepcopy(one(records, "ModelIR"))
    second_model["record_id"] = "model-second"
    records.append(second_model)
    routing = one(records, "RoutingIR")["payload"]
    routing["model_record_id"] = second_model["record_id"]
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="model/workload"):
        validate_records(records)

    records = fixture()
    one(records, "RoutingIR")["payload"]["request_id"] = "wrong-request"
    with pytest.raises(CanonicalIRError, match="request/workload"):
        validate_records(records)

    records = fixture()
    one(records, "RoutingIR")["payload"]["layer_index"] = 999
    with pytest.raises(CanonicalIRError, match="layer"):
        validate_records(records)

    records = fixture()
    by_kind(records, "EventIR")[0]["payload"][
        "runtime_variant_hash"
    ] = "2" * 64
    with pytest.raises(CanonicalIRError, match="runtime"):
        validate_records(records)


def test_alignment_grading_inputs_are_content_bound() -> None:
    records = fixture()
    alignment = one(records, "ClockAlignmentIR")["payload"]
    alignment["grading_inputs"]["target_period_numerator_fs"] = "999999"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="clock grading"):
        validate_records(records)

    records = fixture()
    alignment = one(records, "ClockAlignmentIR")["payload"]
    alignment["grading_inputs"]["shortest_component_record_hash"] = "2" * 64
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="shortest-component"):
        validate_records(records)


def test_multi_tensor_expert_owner_coverage_is_exact() -> None:
    records = fixture()
    placement = one(records, "PlacementIR")["payload"]
    assert len(placement["expert_locations"]) == 16
    validate_records(records)
    placement["expert_locations"][0]["shard_bytes"] = "499"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="owner coverage"):
        validate_records(records)


def test_result_requires_exact_calibration_lineage() -> None:
    records = fixture()
    one(records, "ResultIR")["refs"] = [
        ref
        for ref in one(records, "ResultIR")["refs"]
        if ref["target_ir_kind"] != "CalibrationIR"
    ]
    with pytest.raises(CanonicalIRError, match="CalibrationIR"):
        validate_records(records)


def test_resource_and_placement_abis_are_executable() -> None:
    records = fixture()
    by_kind(records, "EventIR")[0]["payload"]["resource_id"] = "missing"
    with pytest.raises(CanonicalIRError, match="unknown resource"):
        validate_records(records)

    records = fixture()
    placement = one(records, "PlacementIR")["payload"]
    placement["state_allocations"][1]["offset_bytes"] = "8100"
    refresh_refs(records)
    with pytest.raises(CanonicalIRError, match="overlap"):
        validate_records(records)


def test_bundle_rejects_extra_missing_symlink_and_trailing_payload(
    tmp_path: Path,
) -> None:
    write_bundle(tmp_path / "base", fixture())

    extra = tmp_path / "extra"
    shutil.copytree(tmp_path / "base", extra)
    (extra / "extra.txt").write_text("x", encoding="utf-8")
    with pytest.raises(CanonicalIRError, match="closure"):
        read_bundle(extra / "artifact-envelope.json")

    missing = tmp_path / "missing"
    shutil.copytree(tmp_path / "base", missing)
    (missing / "event.arrow.zst").unlink()
    with pytest.raises(CanonicalIRError, match="closure"):
        read_bundle(missing / "artifact-envelope.json")

    linked = tmp_path / "linked"
    shutil.copytree(tmp_path / "base", linked)
    (linked / "event.arrow.zst").unlink()
    (linked / "event.arrow.zst").symlink_to("model.arrow.zst")
    with pytest.raises(CanonicalIRError, match="non-regular"):
        read_bundle(linked / "artifact-envelope.json")

    trailing = tmp_path / "trailing"
    shutil.copytree(tmp_path / "base", trailing)
    with (trailing / "event.arrow.zst").open("ab") as stream:
        stream.write(b"trailing")
    with pytest.raises(CanonicalIRError):
        read_bundle(trailing / "artifact-envelope.json")


def test_truncated_arrow_and_envelope_tamper_are_rejected(tmp_path: Path) -> None:
    write_bundle(tmp_path / "base", fixture())
    truncated = tmp_path / "truncated"
    shutil.copytree(tmp_path / "base", truncated)
    path = truncated / "event.arrow.zst"
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(CanonicalIRError):
        read_bundle(truncated / "artifact-envelope.json")

    tampered = tmp_path / "tampered"
    shutil.copytree(tmp_path / "base", tampered)
    envelope_path = tampered / "artifact-envelope.json"
    envelope = json.loads(envelope_path.read_text())
    envelope["bundle_semantic_root"] = "0" * 64
    envelope_path.write_bytes(canonical_bytes(envelope) + b"\n")
    with pytest.raises(CanonicalIRError, match="semantic root"):
        read_bundle(envelope_path)


def test_outer_zstd_is_mandatory_and_ratio_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_bundle(tmp_path / "base", fixture())
    uncompressed = tmp_path / "uncompressed"
    shutil.copytree(tmp_path / "base", uncompressed)
    path = uncompressed / "event.arrow.zst"
    path.write_bytes(b"ARROW1uncompressedARROW1")
    with pytest.raises(CanonicalIRError, match="Zstd"):
        read_bundle(uncompressed / "artifact-envelope.json")

    monkeypatch.setattr(canonical_ir, "MAX_DECOMPRESSION_RATIO", 1)
    with pytest.raises(CanonicalIRError, match="ratio"):
        read_bundle(tmp_path / "base" / "artifact-envelope.json")


def test_same_kind_multi_partition_round_trip_and_overlap_rejection(
    tmp_path: Path,
) -> None:
    envelope = write_bundle(
        tmp_path / "split", fixture(), max_rows_per_partition=1
    )
    assert len(envelope["partitions"]) == 11
    records, observed = read_bundle(
        tmp_path / "split" / "artifact-envelope.json"
    )
    assert records == validate_records(fixture())
    assert observed["bundle_semantic_root"] == semantic_hashes(fixture())[1]

    envelope_path = tmp_path / "split" / "artifact-envelope.json"
    tampered = json.loads(envelope_path.read_text())
    event_parts = [
        item for item in tampered["partitions"] if item["ir_kind"] == "EventIR"
    ]
    event_parts[1]["key_min"] = event_parts[0]["key_min"]
    envelope_path.write_bytes(canonical_bytes(tampered) + b"\n")
    with pytest.raises(CanonicalIRError, match="overlap"):
        read_bundle(envelope_path)


def test_envelope_preparse_size_and_complexity_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_bundle(tmp_path / "base", fixture())
    envelope = tmp_path / "base" / "artifact-envelope.json"
    monkeypatch.setattr(canonical_ir, "MAX_ENVELOPE_BYTES", 8)
    with pytest.raises(CanonicalIRError, match="byte limit"):
        read_bundle(envelope)
    monkeypatch.setattr(canonical_ir, "MAX_ENVELOPE_BYTES", 1_048_576)
    monkeypatch.setattr(canonical_ir, "MAX_ENVELOPE_NODES", 2)
    with pytest.raises(CanonicalIRError, match="complexity"):
        read_bundle(envelope)


def test_envelope_runtime_variant_binding_is_rederived(tmp_path: Path) -> None:
    write_bundle(tmp_path / "base", fixture())
    envelope_path = tmp_path / "base" / "artifact-envelope.json"
    envelope = json.loads(envelope_path.read_text())
    envelope["runtime_variant_hashes"] = ["2" * 64]
    envelope_path.write_bytes(canonical_bytes(envelope) + b"\n")
    with pytest.raises(CanonicalIRError, match="runtime variant"):
        read_bundle(envelope_path)

def test_invalid_bundle_never_publishes_target(tmp_path: Path) -> None:
    records = fixture()
    one(records, "ResultIR")["payload"]["formal_pass"] = True
    one(records, "ResultIR")["payload"]["execution_valid"] = False
    target = tmp_path / "never-created"
    with pytest.raises(CanonicalIRError):
        write_bundle(target, records)
    assert not target.exists()
    assert not list(tmp_path.glob(".never-created.tmp-*"))


def test_python_cpp_bundle_semantic_hash_golden(tmp_path: Path) -> None:
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is unavailable")
    records = validate_records(fixture())
    _, catalog, _ = load_contracts()
    descriptor = catalog["bundle_descriptor"]
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_bytes(canonical_bytes(descriptor))
    rows_path = tmp_path / "rows.jsonl"
    ordered = sorted(
        records,
        key=lambda item: canonical_bytes([item["ir_kind"], item["record_id"]]),
    )
    rows_path.write_bytes(
        b"\n".join(canonical_bytes(record) for record in ordered) + b"\n"
    )
    executable = tmp_path / "semantic-hash-golden"
    subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            str(PHASE2_ROOT / "semantic_hash_golden.cpp"),
            "-lcrypto",
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [str(executable), str(descriptor_path), str(rows_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == semantic_hashes(records)[1]


def test_schema_evolution_is_fail_closed() -> None:
    contract = json.loads(
        (PHASE2_ROOT / "contracts" / "schema_evolution.json").read_text()
    )
    assert contract["unknown_major"] == "REJECT"
    assert contract["unknown_minor"] == "REJECT"
    assert contract["implicit_default"] == "FORBIDDEN"
    assert contract["upgrade_contract"]["in_place_reinterpretation"] == "FORBIDDEN"
