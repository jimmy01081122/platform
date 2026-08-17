#!/usr/bin/env python3
"""Strict Canonical IR validation and Arrow IPC/Zstd codec."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import sys
import unicodedata
import ctypes
import ctypes.util
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

PHASE2_ROOT = Path(__file__).resolve().parent
SIM_ROOT = PHASE2_ROOT.parent
sys.path.insert(0, str(SIM_ROOT / "tools"))

from contract_runtime import (  # noqa: E402
    ContractError,
    U128_MAX,
    canonical_bytes,
    dataset_semantic_hash,
    schema_fingerprint,
    sint_string,
    transform_alignment,
    uint_string,
    validate_alignment,
    validate_bridge,
    validate_events,
    validate_runtime_variant,
)
from contract_runtime import _exact_source_dtype_decimal, _source_dtype_ulp  # noqa: E402

REQUIRED_JSONSCHEMA = "4.24.0"
REQUIRED_PYARROW = "20.0.0"
IR_KINDS = {
    "WorkloadIR",
    "ModelIR",
    "RoutingIR",
    "PlacementIR",
    "PlatformIR",
    "EventIR",
    "ClockAlignmentIR",
    "CalibrationIR",
    "ResultIR",
}
KIND_FILE_NAMES = {
    "WorkloadIR": "workload.arrow.zst",
    "ModelIR": "model.arrow.zst",
    "RoutingIR": "routing.arrow.zst",
    "PlacementIR": "placement.arrow.zst",
    "PlatformIR": "platform.arrow.zst",
    "EventIR": "event.arrow.zst",
    "ClockAlignmentIR": "clock_alignment.arrow.zst",
    "CalibrationIR": "calibration.arrow.zst",
    "ResultIR": "result.arrow.zst",
}
MAX_PARTITION_FILE_BYTES = 1_073_741_824
MAX_UNCOMPRESSED_PARTITION_BYTES = 2_147_483_648
MAX_DECOMPRESSION_RATIO = 1000
MAX_ROWS_PER_PARTITION = 10_000_000
MAX_CANONICAL_ROW_BYTES = 16_777_216
MAX_ENVELOPE_BYTES = 1_048_576
MAX_ENVELOPE_NODES = 10_000
MAX_ENVELOPE_DEPTH = 32
EXACT_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
U128_FIELDS = {
    "arrival_time_fs",
    "active_parameter_count",
    "bandwidth_bytes_per_second",
    "bootstrap_seed",
    "capacity_bytes",
    "exact_bytes",
    "expert_bytes",
    "forward_latency_fs",
    "latency_fs",
    "lower_fs",
    "phase_offset_fs",
    "shard_bytes",
    "source_timestamp",
    "time_fs",
    "total_parameter_count",
    "upper_fs",
    "valid_from_fs",
}
SIGNED_FIELDS = {
    "lower_error_fs",
    "offset_fs",
    "upper_error_fs",
}
EXACT_DECIMAL_FIELDS = {
    "lower",
    "measured_value",
    "predicted_value",
    "upper",
}


class CanonicalIRError(ValueError):
    pass


def _zstd() -> Any:
    name = ctypes.util.find_library("zstd")
    if not name:
        raise CanonicalIRError("libzstd is unavailable")
    library = ctypes.CDLL(name)
    library.ZSTD_versionString.restype = ctypes.c_char_p
    if library.ZSTD_versionString().decode("ascii") != "1.4.8":
        raise CanonicalIRError("libzstd must be exactly 1.4.8")
    library.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    library.ZSTD_compressBound.restype = ctypes.c_size_t
    library.ZSTD_compress.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
    ]
    library.ZSTD_compress.restype = ctypes.c_size_t
    library.ZSTD_decompress.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    library.ZSTD_decompress.restype = ctypes.c_size_t
    library.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    library.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
    library.ZSTD_findFrameCompressedSize.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    library.ZSTD_findFrameCompressedSize.restype = ctypes.c_size_t
    library.ZSTD_isError.argtypes = [ctypes.c_size_t]
    library.ZSTD_isError.restype = ctypes.c_uint
    return library


def _zstd_compress(raw: bytes) -> bytes:
    if len(raw) > MAX_UNCOMPRESSED_PARTITION_BYTES:
        raise CanonicalIRError("uncompressed partition exceeds limit")
    library = _zstd()
    source = ctypes.create_string_buffer(raw)
    bound = library.ZSTD_compressBound(len(raw))
    destination = ctypes.create_string_buffer(bound)
    size = library.ZSTD_compress(
        destination, bound, source, len(raw), 3
    )
    if library.ZSTD_isError(size):
        raise CanonicalIRError("Zstd compression failed")
    return destination.raw[:size]


def _zstd_decompress(compressed: bytes) -> bytes:
    if (
        not compressed.startswith(b"\x28\xb5\x2f\xfd")
        or len(compressed) > MAX_PARTITION_FILE_BYTES
    ):
        raise CanonicalIRError("Zstd frame magic or size mismatch")
    library = _zstd()
    source = ctypes.create_string_buffer(compressed)
    frame_size = library.ZSTD_findFrameCompressedSize(source, len(compressed))
    if library.ZSTD_isError(frame_size) or frame_size != len(compressed):
        raise CanonicalIRError("trailing or concatenated Zstd frame")
    content_size = library.ZSTD_getFrameContentSize(source, len(compressed))
    if (
        content_size in {(1 << 64) - 1, (1 << 64) - 2}
        or content_size > MAX_UNCOMPRESSED_PARTITION_BYTES
        or content_size > max(1, len(compressed)) * MAX_DECOMPRESSION_RATIO
    ):
        raise CanonicalIRError("Zstd content size or ratio exceeds limit")
    destination = ctypes.create_string_buffer(content_size)
    observed = library.ZSTD_decompress(
        destination, content_size, source, len(compressed)
    )
    if library.ZSTD_isError(observed) or observed != content_size:
        raise CanonicalIRError("Zstd decompression failed")
    return destination.raw[:observed]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, value in pairs:
        key = unicodedata.normalize("NFC", raw_key)
        if key in result:
            raise CanonicalIRError(
                f"duplicate key after NFC normalization: {key}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CanonicalIRError(f"non-finite JSON number: {value}")


def _reject_floats(value: Any, location: str = "$") -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalIRError(f"{location}: non-finite float")
        raise CanonicalIRError(f"{location}: JSON floats are forbidden")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f"{location}[{index}]")


def _validate_scalar_abi(value: Any, location: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            item_location = f"{location}.{key}"
            if key in U128_FIELDS and item is not None:
                try:
                    uint_string(item, item_location, U128_MAX)
                except ContractError as exc:
                    raise CanonicalIRError(str(exc)) from exc
            elif key in SIGNED_FIELDS:
                try:
                    signed = sint_string(item, item_location)
                except ContractError as exc:
                    raise CanonicalIRError(str(exc)) from exc
                if signed < -(1 << 127) or signed > (1 << 127) - 1:
                    raise CanonicalIRError(f"{item_location}: signed 128 overflow")
            elif key in EXACT_DECIMAL_FIELDS:
                if not isinstance(item, str) or not EXACT_DECIMAL_RE.fullmatch(item):
                    raise CanonicalIRError(
                        f"{item_location}: non-canonical exact decimal"
                    )
                if item.startswith("-0") and (
                    item == "-0" or item.startswith("-0.")
                ):
                    raise CanonicalIRError(
                        f"{item_location}: negative zero is forbidden"
                    )
            _validate_scalar_abi(item, item_location)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_scalar_abi(item, f"{location}[{index}]")


def _exact_decimal(value: str, location: str) -> None:
    if not EXACT_DECIMAL_RE.fullmatch(value) or value.startswith("-0"):
        raise CanonicalIRError(f"{location}: non-canonical exact decimal")


def _validate_schema_numeric_abi(
    value: Any,
    node: dict[str, Any],
    root: dict[str, Any],
    location: str,
) -> None:
    reference = node.get("$ref")
    if reference is not None:
        if reference == "#/$defs/uintString":
            try:
                uint_string(value, location, U128_MAX)
            except ContractError as exc:
                raise CanonicalIRError(str(exc)) from exc
        elif reference == "#/$defs/sintString":
            try:
                signed = sint_string(value, location)
            except ContractError as exc:
                raise CanonicalIRError(str(exc)) from exc
            if signed < -(1 << 127) or signed > (1 << 127) - 1:
                raise CanonicalIRError(f"{location}: signed 128 overflow")
        if not reference.startswith("#/$defs/"):
            return
        node = root["$defs"][reference.rsplit("/", 1)[1]]
    if "anyOf" in node:
        branch = next(
            (
                item
                for item in node["anyOf"]
                if not (
                    value is None and item.get("type") != "null"
                )
                and not (
                    value is not None and item.get("type") == "null"
                )
            ),
            None,
        )
        if branch is not None:
            _validate_schema_numeric_abi(value, branch, root, location)
        return
    if isinstance(value, dict):
        properties = node.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _validate_schema_numeric_abi(
                    item, properties[key], root, f"{location}.{key}"
                )
    elif isinstance(value, list) and "items" in node:
        for index, item in enumerate(value):
            _validate_schema_numeric_abi(
                item, node["items"], root, f"{location}[{index}]"
            )


def strict_json_bytes(value: bytes) -> dict[str, Any]:
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalIRError("canonical row is not UTF-8") from exc
    record = json.loads(
        decoded,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if not isinstance(record, dict):
        raise CanonicalIRError("canonical row must be an object")
    _reject_floats(record)
    if canonical_bytes(record) != value:
        raise CanonicalIRError("row bytes are not canonical JSON")
    return record


def load_contracts() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, str]
]:
    schema = json.loads(
        (PHASE2_ROOT / "schemas" / "canonical_ir.schema.json").read_text(
            encoding="utf-8"
        ),
        object_pairs_hook=_unique_object,
    )
    catalog = json.loads(
        (
            PHASE2_ROOT / "contracts" / "ir_descriptors.json"
        ).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
    )
    _reject_floats(schema)
    _reject_floats(catalog)
    logical_schema_hash = hashlib.sha256(canonical_bytes(schema)).hexdigest()
    invariant_contract_hash = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    for descriptor in [
        *catalog["descriptors"].values(),
        catalog["bundle_descriptor"],
    ]:
        descriptor["logical_schema_sha256"] = logical_schema_hash
        descriptor["semantic_invariant_contract_sha256"] = invariant_contract_hash
    hashes = {
        kind: schema_fingerprint(descriptor)
        for kind, descriptor in catalog["descriptors"].items()
    }
    return schema, catalog, hashes


def _schema_validator() -> Any:
    if importlib.metadata.version("jsonschema") != REQUIRED_JSONSCHEMA:
        raise CanonicalIRError(
            f"jsonschema must be exactly {REQUIRED_JSONSCHEMA}"
        )
    import jsonschema

    schema, _, _ = load_contracts()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _phase0_bridge_validator() -> Any:
    import jsonschema

    schema = json.loads(
        (SIM_ROOT / "schemas" / "bridge.schema.json").read_text(
            encoding="utf-8"
        ),
        object_pairs_hook=_unique_object,
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _event_priorities() -> dict[str, int]:
    contract = json.loads(
        (SIM_ROOT / "contracts" / "event_priorities.json").read_text(
            encoding="utf-8"
        ),
        object_pairs_hook=_unique_object,
    )
    if (
        contract.get("schema_version")
        != "moe-simulator-event-priorities-v1"
        or contract.get("unknown_priority") != "reject"
        or contract.get("duplicate_value") != "reject"
    ):
        raise CanonicalIRError("event priority registry policy mismatch")
    priorities = contract.get("priorities")
    if not isinstance(priorities, list):
        raise CanonicalIRError("event priority registry is malformed")
    try:
        mapped = {item["name"]: item["value"] for item in priorities}
        values = [item["value"] for item in priorities]
    except (KeyError, TypeError) as exc:
        raise CanonicalIRError("event priority registry is malformed") from exc
    if (
        len(mapped) != len(priorities)
        or len(values) != len(set(values))
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for name, value in mapped.items()
        )
    ):
        raise CanonicalIRError("event priority registry is not unique")
    return mapped


def validate_records(
    records: Iterable[dict[str, Any]],
    *,
    require_all_kinds: bool = True,
    bundle_evidence_class: str | None = None,
    calibration_profiles: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    values = list(records)
    if not values:
        raise CanonicalIRError("Canonical IR dataset is empty")
    validator = _schema_validator()
    schema, _, descriptor_hashes = load_contracts()
    payload_defs = {
        "WorkloadIR": "workload",
        "ModelIR": "model",
        "RoutingIR": "routing",
        "PlacementIR": "placement",
        "PlatformIR": "platform",
        "EventIR": "event",
        "ClockAlignmentIR": "clockAlignment",
        "CalibrationIR": "calibration",
        "ResultIR": "result",
    }
    seen_keys: set[tuple[str, str]] = set()
    by_kind: dict[str, dict[str, dict[str, Any]]] = {
        kind: {} for kind in IR_KINDS
    }
    for value in values:
        _reject_floats(value)
        _validate_scalar_abi(value)
        validator.validate(value)
        _validate_schema_numeric_abi(
            value["payload"],
            schema["$defs"][payload_defs[value["ir_kind"]]],
            schema,
            f"$.{value['ir_kind']}.payload",
        )
        if (
            value["semantic_descriptor_hash"]
            != descriptor_hashes[value["ir_kind"]]
        ):
            raise CanonicalIRError("semantic descriptor hash mismatch")
        key = (value["ir_kind"], value["record_id"])
        if key in seen_keys:
            raise CanonicalIRError(f"duplicate Canonical IR key: {key}")
        seen_keys.add(key)
        by_kind[value["ir_kind"]][value["record_id"]] = value
    observed = {kind for kind, items in by_kind.items() if items}
    if require_all_kinds and observed != IR_KINDS:
        raise CanonicalIRError(
            f"Canonical IR kinds mismatch: missing={sorted(IR_KINDS-observed)}"
        )
    partition_roots = _partition_roots(by_kind)
    _validate_references(by_kind, partition_roots)
    profiles = _validate_calibration_profiles(
        calibration_profiles or {}, by_kind, partition_roots
    )
    _validate_cross_ir(by_kind, bundle_evidence_class, profiles)
    return sorted(values, key=lambda item: (item["ir_kind"], item["record_id"]))


def _require_reference(
    by_kind: dict[str, dict[str, dict[str, Any]]],
    kind: str,
    record_id: str,
    owner: str,
) -> dict[str, Any]:
    try:
        return by_kind[kind][record_id]
    except KeyError as exc:
        raise CanonicalIRError(
            f"{owner} references missing {kind} {record_id}"
        ) from exc


def _partition_roots(
    by_kind: dict[str, dict[str, dict[str, Any]]]
) -> dict[str, str]:
    _, catalog, _ = load_contracts()
    roots: dict[str, str] = {}
    for kind, values in by_kind.items():
        if values:
            _, roots[kind] = dataset_semantic_hash(
                values.values(), catalog["descriptors"][kind]
            )
    return roots


def _validate_references(
    by_kind: dict[str, dict[str, dict[str, Any]]],
    partition_roots: dict[str, str],
) -> None:
    for records in by_kind.values():
        for record in records.values():
            seen: set[tuple[str, str]] = set()
            for reference in record["refs"]:
                kind = reference["target_ir_kind"]
                record_id = reference["target_primary_key"]["record_id"]
                key = (kind, record_id)
                if key in seen:
                    raise CanonicalIRError("duplicate typed reference")
                seen.add(key)
                _require_reference(by_kind, kind, record_id, record["record_id"])
                if (
                    reference["target_semantic_root"]
                    != partition_roots[kind]
                ):
                    raise CanonicalIRError("typed reference semantic root mismatch")


def _require_typed_ref(
    record: dict[str, Any], kind: str, record_id: str
) -> None:
    matches = [
        reference
        for reference in record["refs"]
        if reference["target_ir_kind"] == kind
        and reference["target_primary_key"]["record_id"] == record_id
    ]
    if len(matches) != 1:
        raise CanonicalIRError(
            f"{record['record_id']} requires exactly one typed {kind} reference"
        )


def _assert_exact_refs(
    record: dict[str, Any], expected: set[tuple[str, str]]
) -> None:
    actual = {
        (
            reference["target_ir_kind"],
            reference["target_primary_key"]["record_id"],
        )
        for reference in record["refs"]
    }
    if actual != expected:
        raise CanonicalIRError(
            f"{record['record_id']} typed reference closure mismatch"
        )


def _unique_field(records: list[dict[str, Any]], field: str, owner: str) -> None:
    values = [record[field] for record in records]
    if len(values) != len(set(values)):
        raise CanonicalIRError(f"{owner} contains duplicate {field}")


def calibration_profile_id(profile: dict[str, Any]) -> str:
    semantic = {
        key: value for key, value in profile.items() if key != "profile_id"
    }
    return hashlib.sha256(
        b"moe-calibration-profile-v1\0" + canonical_bytes(semantic)
    ).hexdigest()


def _calibration_profile_map(
    profiles: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    values = list(profiles)
    try:
        mapped = {item["profile_id"]: item for item in values}
    except (KeyError, TypeError) as exc:
        raise CanonicalIRError("malformed calibration profile artifact") from exc
    if len(mapped) != len(values):
        raise CanonicalIRError("duplicate calibration profile artifact")
    return mapped


def _validate_calibration_profiles(
    profiles: dict[str, dict[str, Any]],
    by_kind: dict[str, dict[str, dict[str, Any]]],
    partition_roots: dict[str, str],
) -> dict[str, dict[str, Any]]:
    schema = json.loads(
        (
            PHASE2_ROOT / "schemas" / "calibration_profile.schema.json"
        ).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
    )
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    validated: dict[str, dict[str, Any]] = {}
    for claimed_id, profile in profiles.items():
        _reject_floats(profile)
        validator.validate(profile)
        if (
            claimed_id != profile["profile_id"]
            or claimed_id != calibration_profile_id(profile)
            or claimed_id in validated
        ):
            raise CanonicalIRError("calibration profile identity mismatch")
        validated[claimed_id] = profile
    for record in by_kind["CalibrationIR"].values():
        payload = record["payload"]
        profile_id = payload["calibration_profile_hash"]
        if profile_id is None:
            continue
        profile = validated.get(profile_id)
        if profile is None:
            raise CanonicalIRError("calibration profile artifact is missing")
        expected_model = {
            "record_id": payload["model_record_id"],
            "semantic_root": partition_roots["ModelIR"],
        }
        expected_platform = {
            "record_id": payload["platform_record_id"],
            "semantic_root": partition_roots["PlatformIR"],
        }
        expected_training = [
            {
                "record_id": record_id,
                "semantic_root": partition_roots["WorkloadIR"],
            }
            for record_id in payload["training_workload_record_ids"]
        ]
        expected_held_out = [
            {
                "record_id": record_id,
                "semantic_root": partition_roots["WorkloadIR"],
            }
            for record_id in payload["held_out_workload_record_ids"]
        ]
        if (
            profile["runtime_variant_hash"]
            != payload["runtime_variant_hash"]
            or profile["model"] != expected_model
            or profile["platform"] != expected_platform
            or profile["training_workloads"] != expected_training
            or profile["held_out_workloads"] != expected_held_out
            or profile["metric"] != payload["metric"]
            or profile["unit"] != payload["unit"]
        ):
            raise CanonicalIRError("calibration profile lineage mismatch")
    unused = set(validated) - {
        record["payload"]["calibration_profile_hash"]
        for record in by_kind["CalibrationIR"].values()
        if record["payload"]["calibration_profile_hash"] is not None
    }
    if unused:
        raise CanonicalIRError("unreferenced calibration profile artifact")
    return validated


def _validate_cross_ir(
    by_kind: dict[str, dict[str, dict[str, Any]]],
    bundle_evidence_class: str | None,
    calibration_profiles: dict[str, dict[str, Any]],
) -> None:
    for record in by_kind["ModelIR"].values():
        payload = record["payload"]
        _assert_exact_refs(record, set())
        if payload["top_k"] > payload["experts"]:
            raise CanonicalIRError("ModelIR top_k exceeds experts")
        if int(payload["active_parameter_count"]) > int(
            payload["total_parameter_count"]
        ):
            raise CanonicalIRError("active parameters exceed total parameters")
        _unique_field(payload["operators"], "operator_id", record["record_id"])
        _unique_field(payload["tensors"], "tensor_id", record["record_id"])
        tensor_bytes = {expert_id: 0 for expert_id in range(payload["experts"])}
        for operator in payload["operators"]:
            if operator["layer_index"] >= payload["layers"]:
                raise CanonicalIRError("operator layer exceeds model layers")
            expert_id = operator["expert_id"]
            if expert_id is not None and expert_id >= payload["experts"]:
                raise CanonicalIRError("operator expert exceeds model experts")
        for tensor in payload["tensors"]:
            expert_id = tensor["expert_id"]
            if expert_id is not None:
                if expert_id >= payload["experts"]:
                    raise CanonicalIRError("tensor expert exceeds model experts")
                tensor_bytes[expert_id] += int(tensor["exact_bytes"])
        if any(
            value != int(payload["expert_bytes"])
            for value in tensor_bytes.values()
        ):
            raise CanonicalIRError("expert tensor byte conservation failed")

    for record in by_kind["WorkloadIR"].values():
        _require_reference(
            by_kind,
            "ModelIR",
            record["payload"]["model_record_id"],
            record["record_id"],
        )
        _require_typed_ref(
            record, "ModelIR", record["payload"]["model_record_id"]
        )
        _assert_exact_refs(
            record, {("ModelIR", record["payload"]["model_record_id"])}
        )
        payload = record["payload"]
        positions = [item["position"] for item in payload["tokens"]]
        if positions != list(range(len(positions))):
            raise CanonicalIRError("workload token positions are not contiguous")
        if [item["token_id"] for item in payload["tokens"]] != payload[
            "input_token_ids"
        ]:
            raise CanonicalIRError("workload token identity mismatch")
    _unique_field(
        [
            record["payload"]
            for record in by_kind["WorkloadIR"].values()
        ],
        "request_instance_id",
        "WorkloadIR",
    )

    for record in by_kind["PlatformIR"].values():
        payload = record["payload"]
        _assert_exact_refs(record, set())
        memory = payload["memory_domains"]
        compute = payload["compute_domains"]
        links = payload["interconnects"]
        _unique_field(memory, "domain_id", record["record_id"])
        _unique_field(compute, "domain_id", record["record_id"])
        _unique_field(links, "link_id", record["record_id"])
        _unique_field(payload["clocks"], "clock_id", record["record_id"])
        _unique_field(payload["bridges"], "bridge_id", record["record_id"])
        _unique_field(payload["queues"], "queue_id", record["record_id"])
        _unique_field(
            payload["calibrated_components"],
            "component_record_id",
            record["record_id"],
        )
        for component in payload["calibrated_components"]:
            expected_root = hashlib.sha256(
                canonical_bytes(
                    {
                        "component_record_id": component[
                            "component_record_id"
                        ],
                        "duration_fs": component["duration_fs"],
                    }
                )
            ).hexdigest()
            if (
                component["evidence_root"] != expected_root
                or int(component["duration_fs"]) == 0
            ):
                raise CanonicalIRError(
                    "calibrated component evidence root mismatch"
                )
        domains = {
            item["domain_id"] for item in memory
        } | {item["domain_id"] for item in compute}
        for link in links:
            if (
                link["source_domain_id"] not in domains
                or link["destination_domain_id"] not in domains
            ):
                raise CanonicalIRError("interconnect references unknown domain")
        clocks = {item["clock_id"] for item in payload["clocks"]}
        for clock in payload["clocks"]:
            numerator = int(clock["frequency_numerator_hz"])
            denominator = int(clock["frequency_denominator_hz"])
            if math.gcd(numerator, denominator) != 1:
                raise CanonicalIRError("platform clock rational is not normalized")
        for compute_domain in compute:
            if compute_domain["clock_id"] not in clocks:
                raise CanonicalIRError("compute domain references unknown clock")
            if int(compute_domain["service_rate_units_per_second"]) == 0:
                raise CanonicalIRError("compute service rate must be positive")
        for link in links:
            if int(link["bandwidth_bytes_per_second"]) == 0:
                raise CanonicalIRError("interconnect bandwidth must be positive")
        import jsonschema

        bridge_validator = _phase0_bridge_validator()
        for bridge in payload["bridges"]:
            try:
                bridge_validator.validate(bridge)
                validate_bridge(
                    bridge, {clock_id: None for clock_id in clocks}
                )
            except (ContractError, jsonschema.ValidationError) as exc:
                raise CanonicalIRError(str(exc)) from exc
        for queue in payload["queues"]:
            if queue["domain_id"] not in domains:
                raise CanonicalIRError("queue references unknown domain")

    for record in by_kind["PlacementIR"].values():
        payload = record["payload"]
        model = _require_reference(
            by_kind, "ModelIR", payload["model_record_id"], record["record_id"]
        )
        platform = _require_reference(
            by_kind,
            "PlatformIR",
            payload["platform_record_id"],
            record["record_id"],
        )
        _require_typed_ref(record, "ModelIR", payload["model_record_id"])
        _require_typed_ref(
            record, "PlatformIR", payload["platform_record_id"]
        )
        _assert_exact_refs(
            record,
            {
                ("ModelIR", payload["model_record_id"]),
                ("PlatformIR", payload["platform_record_id"]),
            },
        )
        if payload["platform_id"] != platform["payload"]["platform_id"]:
            raise CanonicalIRError("PlacementIR platform identity mismatch")
        locations = payload["expert_locations"]
        memory_ids = {
            item["domain_id"]
            for item in platform["payload"]["memory_domains"]
        }
        capacity = {
            item["domain_id"]: int(item["capacity_bytes"])
            for item in platform["payload"]["memory_domains"]
        }
        used = {domain_id: 0 for domain_id in memory_ids}
        compute_ids = {
            item["domain_id"] for item in platform["payload"]["compute_domains"]
        }
        tensors = {
            item["tensor_id"]: item for item in model["payload"]["tensors"]
        }
        owner_ranges: dict[str, list[tuple[int, int]]] = {
            tensor_id: []
            for tensor_id, tensor in tensors.items()
            if tensor["expert_id"] is not None
        }
        replica_ids: set[str] = set()
        for location in locations:
            if location["expert_id"] >= model["payload"]["experts"]:
                raise CanonicalIRError("placement expert exceeds model experts")
            if location["memory_domain_id"] not in memory_ids:
                raise CanonicalIRError("placement references unknown memory domain")
            if location["compute_domain_id"] not in compute_ids:
                raise CanonicalIRError("placement references unknown compute domain")
            tensor = tensors.get(location["tensor_id"])
            if tensor is None or tensor["expert_id"] != location["expert_id"]:
                raise CanonicalIRError("expert placement tensor identity mismatch")
            if int(location["shard_offset_bytes"]) + int(
                location["shard_bytes"]
            ) > int(tensor["exact_bytes"]):
                raise CanonicalIRError("expert shard range exceeds tensor")
            if location["replica_id"] in replica_ids:
                raise CanonicalIRError("duplicate placement replica_id")
            replica_ids.add(location["replica_id"])
            used[location["memory_domain_id"]] += int(location["shard_bytes"])
            if location["owner"]:
                owner_ranges[location["tensor_id"]].append(
                    (
                        int(location["shard_offset_bytes"]),
                        int(location["shard_offset_bytes"])
                        + int(location["shard_bytes"]),
                    )
                )
        for tensor_id, ranges in owner_ranges.items():
            ordered_tensor_ranges = sorted(ranges)
            if (
                not ordered_tensor_ranges
                or ordered_tensor_ranges[0][0] != 0
                or ordered_tensor_ranges[-1][1]
                != int(tensors[tensor_id]["exact_bytes"])
                or any(
                    left[1] != right[0]
                    for left, right in zip(
                        ordered_tensor_ranges, ordered_tensor_ranges[1:]
                    )
                )
            ):
                raise CanonicalIRError(
                    "expert tensor owner coverage is not exact"
                )
        if any(used[key] > capacity[key] for key in used):
            raise CanonicalIRError("placement exceeds memory capacity")
        allocation_ids: set[str] = set()
        nonexpert_tensor_counts = {
            tensor_id: 0
            for tensor_id, tensor in tensors.items()
            if tensor["expert_id"] is None
        }
        occupied_ranges: dict[str, list[tuple[int, int]]] = {
            domain_id: [] for domain_id in memory_ids
        }
        for location in locations:
            start_offset = int(location["memory_offset_bytes"])
            occupied_ranges[location["memory_domain_id"]].append(
                (start_offset, start_offset + int(location["shard_bytes"]))
            )
        for allocation in payload["state_allocations"]:
            if allocation["allocation_id"] in allocation_ids:
                raise CanonicalIRError("duplicate state allocation identity")
            allocation_ids.add(allocation["allocation_id"])
            if allocation["memory_domain_id"] not in memory_ids:
                raise CanonicalIRError("state allocation uses unknown memory domain")
            if allocation["compute_domain_id"] not in compute_ids:
                raise CanonicalIRError("state allocation uses unknown compute domain")
            tensor_id = allocation["tensor_id"]
            if allocation["object_class"] == "NON_EXPERT_WEIGHT":
                tensor = tensors.get(tensor_id)
                if tensor is None or tensor["expert_id"] is not None:
                    raise CanonicalIRError("non-expert weight identity mismatch")
                if int(allocation["exact_bytes"]) != int(tensor["exact_bytes"]):
                    raise CanonicalIRError("non-expert weight byte mismatch")
                nonexpert_tensor_counts[tensor_id] += 1
            elif tensor_id is not None:
                raise CanonicalIRError("runtime state cannot claim model tensor")
            domain_id = allocation["memory_domain_id"]
            start_offset = int(allocation["offset_bytes"])
            end_offset = start_offset + int(allocation["exact_bytes"])
            used[domain_id] += int(allocation["exact_bytes"])
            occupied_ranges[domain_id].append((start_offset, end_offset))
        if any(used[key] > capacity[key] for key in used):
            raise CanonicalIRError("placement state exceeds memory capacity")
        if any(count != 1 for count in nonexpert_tensor_counts.values()):
            raise CanonicalIRError(
                "non-expert tensor requires exactly one owner allocation"
            )
        for ranges in occupied_ranges.values():
            ordered_ranges = sorted(ranges)
            if any(
                left[1] > right[0]
                for left, right in zip(ordered_ranges, ordered_ranges[1:])
            ):
                raise CanonicalIRError("placement allocation ranges overlap")
        start = int(payload["valid_from_fs"])
        end = payload["valid_to_fs"]
        if end is not None and int(end) <= start:
            raise CanonicalIRError("placement validity interval is empty")

    placement_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in by_kind["PlacementIR"].values():
        payload = record["payload"]
        placement_groups.setdefault(
            (payload["model_record_id"], payload["platform_record_id"]), []
        ).append(record)
    for group in placement_groups.values():
        ordered_group = sorted(group, key=lambda item: item["payload"]["version"])
        versions = [item["payload"]["version"] for item in ordered_group]
        if len(versions) != len(set(versions)):
            raise CanonicalIRError("placement snapshot version is not unique")
        for index, record in enumerate(ordered_group):
            payload = record["payload"]
            if index == 0:
                if payload["predecessor_snapshot_id"] is not None:
                    raise CanonicalIRError("first placement has a predecessor")
            else:
                previous = ordered_group[index - 1]["payload"]
                if (
                    payload["version"] != previous["version"] + 1
                    or payload["predecessor_snapshot_id"]
                    != previous["snapshot_id"]
                    or previous["valid_to_fs"] != payload["valid_from_fs"]
                    or not payload["migration_event_ids"]
                ):
                    raise CanonicalIRError("placement transition lineage mismatch")

    for record in by_kind["RoutingIR"].values():
        payload = record["payload"]
        model = _require_reference(
            by_kind, "ModelIR", payload["model_record_id"], record["record_id"]
        )
        workload = _require_reference(
            by_kind,
            "WorkloadIR",
            payload["workload_record_id"],
            record["record_id"],
        )
        _require_typed_ref(record, "ModelIR", payload["model_record_id"])
        _require_typed_ref(
            record, "WorkloadIR", payload["workload_record_id"]
        )
        _assert_exact_refs(
            record,
            {
                ("ModelIR", payload["model_record_id"]),
                ("WorkloadIR", payload["workload_record_id"]),
            },
        )
        experts = model["payload"]["experts"]
        top_k = model["payload"]["top_k"]
        if payload["model_record_id"] != workload["payload"]["model_record_id"]:
            raise CanonicalIRError("routing model/workload lineage mismatch")
        if payload["request_id"] != workload["payload"]["request_instance_id"]:
            raise CanonicalIRError("routing request/workload lineage mismatch")
        if payload["layer_index"] >= model["payload"]["layers"]:
            raise CanonicalIRError("routing layer exceeds model layers")
        if payload["routing_scope"] == "TOKEN":
            for index, score in enumerate(payload["canonical_scores"]):
                _exact_decimal(score, f"canonical_scores[{index}]")
            for field in (
                "score_tolerance_absolute",
                "score_tolerance_relative",
                "k_boundary_score",
            ):
                _exact_decimal(payload[field], field)
            if len(payload["canonical_scores"]) != experts:
                raise CanonicalIRError("routing score count does not match experts")
            if len(payload["selected_experts"]) != top_k:
                raise CanonicalIRError("routing selected count does not match top_k")
            if any(item >= experts for item in payload["selected_experts"]):
                raise CanonicalIRError("routing expert exceeds model experts")
            decimal_scores = [
                Decimal(item) for item in payload["canonical_scores"]
            ]
            if decimal_scores != [
                _exact_source_dtype_decimal(item, payload["score_dtype"])
                for item in decimal_scores
            ]:
                raise CanonicalIRError(
                    "routing score is not exactly representable in source dtype"
                )
            relative = Decimal(payload["score_tolerance_relative"])
            if relative < 0 or relative > Decimal("0.00001"):
                raise CanonicalIRError("routing relative tolerance exceeds maximum")
            ordered_experts = sorted(
                range(experts), key=lambda item: (-decimal_scores[item], item)
            )
            boundary = decimal_scores[ordered_experts[top_k - 1]]
            if Decimal(payload["k_boundary_score"]) != boundary:
                raise CanonicalIRError("routing boundary score mismatch")
            absolute = Decimal(payload["score_tolerance_absolute"])
            if absolute != Decimal(4) * _source_dtype_ulp(
                boundary, payload["score_dtype"]
            ):
                raise CanonicalIRError("routing absolute tolerance is not four ULP")
            ambiguity = sorted(
                expert
                for expert, score in enumerate(decimal_scores)
                if abs(score - boundary)
                <= absolute + relative * max(abs(score), abs(boundary))
            )
            if payload["ambiguity_set"] != ambiguity:
                raise CanonicalIRError("routing ambiguity set mismatch")
            certain = [
                expert
                for expert in ordered_experts[:top_k]
                if expert not in ambiguity
            ]
            selected = payload["selected_experts"]
            if (
                selected[: len(certain)] != certain
                or any(item not in ambiguity for item in selected[len(certain) :])
            ):
                raise CanonicalIRError("selected experts violate canonical top-k")
        elif len(payload["aggregate_expert_demand"]) != experts:
            raise CanonicalIRError(
                "aggregate routing demand count does not match experts"
            )
        else:
            for index, demand in enumerate(payload["aggregate_expert_demand"]):
                try:
                    uint_string(demand, f"aggregate_expert_demand[{index}]")
                except ContractError as exc:
                    raise CanonicalIRError(str(exc)) from exc

    events = by_kind["EventIR"]
    for record in events.values():
        payload = record["payload"]
        workload_id = payload["workload_record_id"]
        workload = None
        if workload_id is not None:
            workload = _require_reference(
                by_kind, "WorkloadIR", workload_id, record["record_id"]
            )
            _require_typed_ref(record, "WorkloadIR", workload_id)
        for kind, field in (
            ("PlatformIR", "platform_record_id"),
            ("PlacementIR", "placement_record_id"),
            ("ClockAlignmentIR", "alignment_record_id"),
        ):
            _require_reference(
                by_kind, kind, payload[field], record["record_id"]
            )
            _require_typed_ref(record, kind, payload[field])
        expected_refs = {
            ("PlatformIR", payload["platform_record_id"]),
            ("PlacementIR", payload["placement_record_id"]),
            ("ClockAlignmentIR", payload["alignment_record_id"]),
        }
        if workload_id is not None:
            expected_refs.add(("WorkloadIR", workload_id))
        _assert_exact_refs(record, expected_refs)
        platform = by_kind["PlatformIR"][payload["platform_record_id"]][
            "payload"
        ]
        domains = {
            item["domain_id"] for item in platform["compute_domains"]
        } | {item["domain_id"] for item in platform["memory_domains"]}
        if payload["component_id"] not in domains:
            raise CanonicalIRError("event references unknown platform component")
        resources = set(domains)
        resources.update(item["queue_id"] for item in platform["queues"])
        resources.update(item["bridge_id"] for item in platform["bridges"])
        for link in platform["interconnects"]:
            resources.add(link["link_id"])
            resources.add(link["shared_resource_id"])
            if link["copy_engine_id"] is not None:
                resources.add(link["copy_engine_id"])
        if payload["resource_id"] not in resources:
            raise CanonicalIRError("event references unknown resource")
        if int(payload["quantity"]) == 0:
            raise CanonicalIRError("event resource quantity must be positive")
        placement = by_kind["PlacementIR"][payload["placement_record_id"]][
            "payload"
        ]
        if workload is not None and (
            payload["request_id"] != workload["payload"]["request_instance_id"]
            or payload["runtime_variant_hash"]
            != workload["payload"]["runtime_variant_hash"]
            or placement["model_record_id"]
            != workload["payload"]["model_record_id"]
        ):
            raise CanonicalIRError("event workload/runtime lineage mismatch")
        alignment = by_kind["ClockAlignmentIR"][
            payload["alignment_record_id"]
        ]["payload"]
        if payload["source_clock_id"] != alignment["source_clock_id"]:
            raise CanonicalIRError("event source clock/alignment mismatch")
        source_timestamp = int(payload["source_timestamp"])
        valid_range = alignment["valid_time_range"]
        if not (
            int(valid_range["source_start"])
            <= source_timestamp
            <= int(valid_range["source_end"])
        ):
            raise CanonicalIRError("event alignment is outside valid time range")
        try:
            mapped = transform_alignment(alignment, source_timestamp)
        except ContractError as exc:
            raise CanonicalIRError(str(exc)) from exc
        expected_lower = mapped + int(
            alignment["confidence_interval_95_fs"]["lower_error_fs"]
        )
        expected_upper = mapped + int(
            alignment["confidence_interval_95_fs"]["upper_error_fs"]
        )
        interval = payload["aligned_interval_fs"]
        if expected_lower < 0 or expected_upper < expected_lower or (
            int(interval["lower_fs"]) != expected_lower
            or int(interval["upper_fs"]) != expected_upper
        ):
            raise CanonicalIRError("event aligned interval lineage mismatch")
        event_time = int(payload["time_fs"])
        if event_time < int(placement["valid_from_fs"]) or (
            placement["valid_to_fs"] is not None
            and event_time >= int(placement["valid_to_fs"])
        ):
            raise CanonicalIRError("event uses placement outside validity interval")
        if not (
            int(interval["lower_fs"]) <= event_time <= int(interval["upper_fs"])
        ):
            raise CanonicalIRError("aligned interval does not contain event time")
        if (
            payload["alignment_grade"] != alignment["claimed_grade"]
        ):
            raise CanonicalIRError("event alignment grade was not rederived")
        if (
            payload["alignment_grade"] != "CYCLE_GRADE"
            and interval["lower_fs"] == interval["upper_fs"]
        ):
            raise CanonicalIRError(
                "non-cycle-grade event cannot erase alignment uncertainty"
            )
        for dependency in payload["dependencies"]:
            if dependency == record["record_id"] or dependency not in events:
                raise CanonicalIRError("event dependency is missing or self-referential")
            if int(events[dependency]["payload"]["time_fs"]) > int(
                payload["time_fs"]
            ):
                raise CanonicalIRError("event dependency occurs after dependent")

    event_groups: dict[str, list[dict[str, Any]]] = {}
    for record in events.values():
        payload = record["payload"]
        event_groups.setdefault(payload["platform_record_id"], []).append(
            {
                "event_id": record["record_id"],
                "event_type": payload["event_type"],
                "time_fs": payload["time_fs"],
                "event_priority": payload["event_priority"],
                "request_id": payload["request_id"],
                "token_index": payload["token_index"],
                "layer_index": payload["layer_index"],
                "component_id": payload["component_id"],
                "dependencies": payload["dependencies"],
                "clock_id": payload["source_clock_id"],
                "attributes": {},
            }
        )
    priorities = _event_priorities()
    for platform_id, projected_events in event_groups.items():
        clocks = {
            item["clock_id"]: None
            for item in by_kind["PlatformIR"][platform_id]["payload"]["clocks"]
        }
        try:
            validate_events(projected_events, clocks, priorities)
        except ContractError as exc:
            raise CanonicalIRError(str(exc)) from exc

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(event_id: str) -> None:
        if event_id in visiting:
            raise CanonicalIRError("event dependency graph contains a cycle")
        if event_id in visited:
            return
        visiting.add(event_id)
        for dependency in events[event_id]["payload"]["dependencies"]:
            visit(dependency)
        visiting.remove(event_id)
        visited.add(event_id)

    for event_id in events:
        visit(event_id)
    for record in by_kind["PlacementIR"].values():
        payload = record["payload"]
        for event_id in payload["migration_event_ids"]:
            event = events.get(event_id)
            if (
                event is None
                or event["payload"]["resource_action"] != "MIGRATE"
                or event["payload"]["placement_record_id"] != record["record_id"]
                or event["payload"]["time_fs"] != payload["valid_from_fs"]
            ):
                raise CanonicalIRError("placement migration event lineage mismatch")

    for kind in ("CalibrationIR", "ResultIR"):
        for record in by_kind[kind].values():
            payload = record["payload"]
            _require_reference(
                by_kind,
                "ModelIR",
                payload["model_record_id"],
                record["record_id"],
            )
            _require_reference(
                by_kind,
                "PlatformIR",
                payload["platform_record_id"],
                record["record_id"],
            )
            _require_typed_ref(record, "ModelIR", payload["model_record_id"])
            _require_typed_ref(
                record, "PlatformIR", payload["platform_record_id"]
            )
            expected_refs = {
                ("ModelIR", payload["model_record_id"]),
                ("PlatformIR", payload["platform_record_id"]),
            }
            if kind == "ResultIR":
                workload = _require_reference(
                    by_kind,
                    "WorkloadIR",
                    payload["workload_record_id"],
                    record["record_id"],
                )
                _require_typed_ref(
                    record, "WorkloadIR", payload["workload_record_id"]
                )
                expected_refs.add(
                    ("WorkloadIR", payload["workload_record_id"])
                )
                if (
                    payload["model_record_id"]
                    != workload["payload"]["model_record_id"]
                    or payload["request_id"]
                    != workload["payload"]["request_instance_id"]
                    or payload["runtime_variant_hash"]
                    != workload["payload"]["runtime_variant_hash"]
                ):
                    raise CanonicalIRError(
                        "ResultIR workload/model/runtime lineage mismatch"
                    )
                calibration_id = payload["calibration_record_id"]
                calibration = _require_reference(
                    by_kind, "CalibrationIR", calibration_id, record["record_id"]
                )
                _require_typed_ref(record, "CalibrationIR", calibration_id)
                expected_refs.add(("CalibrationIR", calibration_id))
                if (
                    calibration["payload"]["model_record_id"]
                    != payload["model_record_id"]
                    or calibration["payload"]["platform_record_id"]
                    != payload["platform_record_id"]
                    or calibration["payload"]["evidence_class"]
                    != payload["evidence_class"]
                    or calibration["payload"]["runtime_variant_hash"]
                    != payload["runtime_variant_hash"]
                    or calibration["payload"]["range_status"]
                    != payload["range_status"]
                ):
                    raise CanonicalIRError("ResultIR calibration lineage mismatch")
                if payload["formal_pass"] and (
                    not payload["execution_valid"]
                    or not payload["completed"]
                    or payload["range_status"] == "EXTRAPOLATED"
                    or payload["evidence_availability"] != "CONFIRMED"
                    or payload["range_status"]
                    not in {"IN_CALIBRATION_ENVELOPE", "INTERPOLATED"}
                    or payload["evidence_class"] == "SYNTHETIC"
                    or bundle_evidence_class in {None, "SYNTHETIC"}
                    or calibration["payload"]["fidelity"]
                    not in {"MEASURED", "CALIBRATED_SURROGATE"}
                    or calibration["payload"]["range_status"]
                    not in {"IN_CALIBRATION_ENVELOPE", "INTERPOLATED"}
                    or calibration["payload"]["calibration_profile_hash"] is None
                ):
                    raise CanonicalIRError(
                        "formal ResultIR PASS violates evidence boundary"
                    )
            else:
                training = payload["training_workload_record_ids"]
                held_out = payload["held_out_workload_record_ids"]
                if set(training) & set(held_out):
                    raise CanonicalIRError(
                        "calibration training and held-out workloads overlap"
                    )
                for workload_id in [*training, *held_out]:
                    workload = _require_reference(
                        by_kind, "WorkloadIR", workload_id, record["record_id"]
                    )
                    _require_typed_ref(record, "WorkloadIR", workload_id)
                    expected_refs.add(("WorkloadIR", workload_id))
                    if (
                        workload["payload"]["model_record_id"]
                        != payload["model_record_id"]
                        or workload["payload"]["runtime_variant_hash"]
                        != payload["runtime_variant_hash"]
                    ):
                        raise CanonicalIRError(
                            "calibration workload/runtime lineage mismatch"
                        )
                envelope = payload["calibration_envelope"]
                coordinate = payload["evaluation_coordinate"]
                _exact_decimal(
                    payload["measurement_noise_floor"],
                    "measurement_noise_floor",
                )
                ci_lower = Decimal(payload["bootstrap_ci_95"]["lower"])
                ci_upper = Decimal(payload["bootstrap_ci_95"]["upper"])
                noise_floor = Decimal(payload["measurement_noise_floor"])
                if (
                    ci_lower > ci_upper
                    or noise_floor < 0
                    or payload["sample_count"] < payload["repetitions"]
                ):
                    raise CanonicalIRError(
                        "calibration bootstrap evidence is invalid"
                    )
                if envelope is None or coordinate is None:
                    if envelope is not None or coordinate is not None:
                        raise CanonicalIRError(
                            "calibration range inputs are incomplete"
                        )
                    derived_range = "RANGE_UNKNOWN"
                else:
                    _unique_field(
                        envelope["dimensions"],
                        "name",
                        "calibration envelope",
                    )
                    _unique_field(
                        coordinate,
                        "name",
                        "calibration coordinate",
                    )
                    for dimension in envelope["dimensions"]:
                        _exact_decimal(
                            dimension["lower"], "calibration envelope lower"
                        )
                        _exact_decimal(
                            dimension["upper"], "calibration envelope upper"
                        )
                    for item in coordinate:
                        _exact_decimal(
                            item["value"], "calibration coordinate value"
                        )
                    dimensions = {
                        item["name"]: item for item in envelope["dimensions"]
                    }
                    coordinates = {
                        item["name"]: Decimal(item["value"])
                        for item in coordinate
                    }
                    if set(dimensions) != set(coordinates):
                        raise CanonicalIRError(
                            "calibration coordinate dimensions mismatch"
                        )
                    outside = False
                    on_boundary = False
                    for name, dimension in dimensions.items():
                        lower = Decimal(dimension["lower"])
                        upper = Decimal(dimension["upper"])
                        if lower > upper:
                            raise CanonicalIRError(
                                "calibration envelope is inverted"
                            )
                        value = coordinates[name]
                        outside |= value < lower or value > upper
                        on_boundary |= value in {lower, upper}
                    derived_range = (
                        "EXTRAPOLATED"
                        if outside
                        else (
                            "IN_CALIBRATION_ENVELOPE"
                            if on_boundary
                            else "INTERPOLATED"
                        )
                    )
                if payload["range_status"] != derived_range:
                    raise CanonicalIRError(
                        "calibration range status was not rederived"
                    )
                if (
                    payload["evidence_class"] == "SYNTHETIC"
                    and (
                        payload["fidelity"] != "FUNCTIONAL_ONLY"
                        or payload["range_status"] != "RANGE_UNKNOWN"
                        or payload["calibration_profile_hash"] is not None
                    )
                ):
                    raise CanonicalIRError(
                        "synthetic calibration claim exceeds scope"
                    )
            _assert_exact_refs(record, expected_refs)

    for record in by_kind["ClockAlignmentIR"].values():
        platform_id = record["payload"]["platform_record_id"]
        _require_reference(
            by_kind, "PlatformIR", platform_id, record["record_id"]
        )
        _require_typed_ref(record, "PlatformIR", platform_id)
        _assert_exact_refs(record, {("PlatformIR", platform_id)})
        platform = by_kind["PlatformIR"][platform_id]["payload"]
        clock_ids = {item["clock_id"] for item in platform["clocks"]}
        if (
            record["payload"]["source_clock_id"] not in clock_ids
            or record["payload"]["target_clock_id"] not in clock_ids
        ):
            raise CanonicalIRError("alignment references unknown platform clock")
        alignment = record["payload"]
        target_clock = next(
            item
            for item in platform["clocks"]
            if item["clock_id"] == alignment["target_clock_id"]
        )
        period_numerator = (
            10**15 * int(target_clock["frequency_denominator_hz"])
        )
        period_denominator = int(target_clock["frequency_numerator_hz"])
        common = math.gcd(period_numerator, period_denominator)
        period_numerator //= common
        period_denominator //= common
        grading = alignment["grading_inputs"]
        if (
            grading["target_clock_profile_hash"]
            != hashlib.sha256(canonical_bytes(target_clock)).hexdigest()
            or int(grading["target_period_numerator_fs"])
            != period_numerator
            or int(grading["target_period_denominator"])
            != period_denominator
        ):
            raise CanonicalIRError("alignment target clock grading binding mismatch")
        shortest = min(
            platform["calibrated_components"],
            key=lambda item: int(item["duration_fs"]),
        )
        if (
            grading["shortest_component_record_hash"]
            != shortest["evidence_root"]
            or grading["shortest_component_duration_fs"]
            != shortest["duration_fs"]
        ):
            raise CanonicalIRError(
                "alignment shortest-component grading binding mismatch"
            )
        numerator = uint_string(
            alignment["scale_numerator"], "scale_numerator", U128_MAX
        )
        denominator = uint_string(
            alignment["scale_denominator"], "scale_denominator", U128_MAX
        )
        if numerator == 0 or denominator == 0:
            raise CanonicalIRError("alignment scale must be positive")
        if math.gcd(numerator, denominator) != 1:
            raise CanonicalIRError("alignment rational is not normalized")
        try:
            validate_alignment(alignment)
        except ContractError as exc:
            raise CanonicalIRError(str(exc)) from exc


def semantic_hashes(
    records: Iterable[dict[str, Any]]
) -> tuple[list[str], str]:
    _, catalog, _ = load_contracts()
    ordered = validate_records(records)
    return dataset_semantic_hash(ordered, catalog["bundle_descriptor"])


def _runtime_variant_hashes(records: Iterable[dict[str, Any]]) -> list[str]:
    values = {
        record["payload"]["runtime_variant_hash"]
        for record in records
        if record["ir_kind"]
        in {"WorkloadIR", "EventIR", "CalibrationIR", "ResultIR"}
    }
    if not values:
        raise CanonicalIRError("runtime variant identity closure is empty")
    return sorted(values)


def _validate_runtime_variants(
    variants: list[dict[str, Any]], records: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    import jsonschema

    schema = json.loads(
        (SIM_ROOT / "schemas" / "runtime_variant.schema.json").read_text(
            encoding="utf-8"
        ),
        object_pairs_hook=_unique_object,
    )
    validator = jsonschema.Draft202012Validator(schema)
    observed: dict[str, dict[str, Any]] = {}
    for variant in variants:
        _reject_floats(variant)
        validator.validate(variant)
        try:
            validate_runtime_variant(variant)
        except ContractError as exc:
            raise CanonicalIRError(str(exc)) from exc
        if variant["variant_id"] in observed:
            raise CanonicalIRError("duplicate runtime variant manifest")
        observed[variant["variant_id"]] = variant
    required = set(_runtime_variant_hashes(records))
    if set(observed) != required:
        raise CanonicalIRError("runtime variant manifest closure mismatch")
    return [observed[key] for key in sorted(observed)]


def _partition_schema(
    kind: str,
    partition_id: str,
    descriptor_hash: str,
    semantic_root: str,
    row_count: int,
) -> Any:
    if importlib.metadata.version("pyarrow") != REQUIRED_PYARROW:
        raise CanonicalIRError(f"pyarrow must be exactly {REQUIRED_PYARROW}")
    import pyarrow as pa

    metadata = {
        b"codec": b"canonical-ir-arrow-ipc-file-outer-zstd-v1",
        b"ir_kind": kind.encode("ascii"),
        b"partition_id": partition_id.encode("ascii"),
        b"descriptor_sha256": descriptor_hash.encode("ascii"),
        b"semantic_root_sha256": semantic_root.encode("ascii"),
        b"row_count": str(row_count).encode("ascii"),
    }
    return pa.schema(
        [
            pa.field("record_id", pa.string(), nullable=False),
            pa.field(
                "semantic_descriptor_hash", pa.binary(32), nullable=False
            ),
            pa.field("canonical_json", pa.binary(), nullable=False),
        ],
        metadata=metadata,
    )


def _write_partition(
    path: Path,
    kind: str,
    records: list[dict[str, Any]],
    descriptor: dict[str, Any],
    batch_size: int,
    partition_id: str,
) -> dict[str, Any]:
    import pyarrow as pa

    if not records or len(records) > MAX_ROWS_PER_PARTITION:
        raise CanonicalIRError("partition row count is outside limits")
    ordered = sorted(records, key=lambda item: item["record_id"])
    row_hashes, semantic_root = dataset_semantic_hash(ordered, descriptor)
    descriptor_hash = schema_fingerprint(descriptor)
    schema = _partition_schema(
        kind, partition_id, descriptor_hash, semantic_root, len(ordered)
    )
    canonical_rows = [canonical_bytes(item) for item in ordered]
    if any(len(item) > MAX_CANONICAL_ROW_BYTES for item in canonical_rows):
        raise CanonicalIRError("canonical row exceeds byte limit")
    table = pa.Table.from_arrays(
        [
            pa.array([item["record_id"] for item in ordered], type=pa.string()),
            pa.array(
                [bytes.fromhex(item["semantic_descriptor_hash"]) for item in ordered],
                type=pa.binary(32),
            ),
            pa.array(canonical_rows, type=pa.binary()),
        ],
        schema=schema,
    )
    options = pa.ipc.IpcWriteOptions(
        metadata_version=pa.ipc.MetadataVersion.V5,
        compression=None,
        use_threads=False,
    )
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, schema, options=options) as writer:
        writer.write_table(table, max_chunksize=batch_size)
    compressed = _zstd_compress(sink.getvalue().to_pybytes())
    with path.open("xb") as stream:
        stream.write(compressed)
        stream.flush()
        os.fsync(stream.fileno())
    if path.stat().st_size > MAX_PARTITION_FILE_BYTES:
        raise CanonicalIRError("partition file exceeds byte limit")
    return {
        "partition_id": partition_id,
        "ir_kind": kind,
        "file_path": path.name,
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "row_count": str(len(ordered)),
        "key_min": ordered[0]["record_id"],
        "key_max": ordered[-1]["record_id"],
        "semantic_root": semantic_root,
        "row_hashes": row_hashes,
    }


def _read_partition(
    path: Path,
    kind: str,
    descriptor: dict[str, Any],
    partition_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import pyarrow as pa

    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_size > MAX_PARTITION_FILE_BYTES
    ):
        raise CanonicalIRError("partition path or size is invalid")
    raw_file = path.read_bytes()
    raw_arrow = _zstd_decompress(raw_file)
    if not raw_arrow.startswith(b"ARROW1") or not raw_arrow.endswith(b"ARROW1"):
        raise CanonicalIRError("Arrow file magic/trailing payload mismatch")
    reader = pa.ipc.open_file(pa.BufferReader(raw_arrow))
    table = reader.read_all()
    schema = table.schema
    if table.num_rows < 1 or table.num_rows > MAX_ROWS_PER_PARTITION:
        raise CanonicalIRError("partition row count is outside limits")
    metadata = schema.metadata or {}
    expected_keys = {
        b"codec",
        b"ir_kind",
        b"partition_id",
        b"descriptor_sha256",
        b"semantic_root_sha256",
        b"row_count",
    }
    if set(metadata) != expected_keys:
        raise CanonicalIRError("partition metadata set mismatch")
    descriptor_hash = schema_fingerprint(descriptor)
    claimed_root = metadata[b"semantic_root_sha256"].decode("ascii")
    expected_schema = _partition_schema(
        kind, partition_id, descriptor_hash, claimed_root, table.num_rows
    )
    if not schema.equals(expected_schema, check_metadata=True):
        raise CanonicalIRError("partition Arrow schema mismatch")
    if metadata[b"codec"] != b"canonical-ir-arrow-ipc-file-outer-zstd-v1":
        raise CanonicalIRError("partition codec mismatch")
    if metadata[b"ir_kind"].decode("ascii") != kind:
        raise CanonicalIRError("partition kind mismatch")
    if metadata[b"partition_id"].decode("ascii") != partition_id:
        raise CanonicalIRError("partition identity mismatch")
    records: list[dict[str, Any]] = []
    for index in range(table.num_rows):
        raw = table["canonical_json"][index].as_py()
        if len(raw) > MAX_CANONICAL_ROW_BYTES:
            raise CanonicalIRError("canonical row exceeds byte limit")
        record = strict_json_bytes(raw)
        if record["ir_kind"] != kind:
            raise CanonicalIRError("row kind does not match partition")
        if table["record_id"][index].as_py() != record["record_id"]:
            raise CanonicalIRError("partition record ID redundancy mismatch")
        if (
            table["semantic_descriptor_hash"][index].as_py().hex()
            != record["semantic_descriptor_hash"]
        ):
            raise CanonicalIRError("partition descriptor redundancy mismatch")
        records.append(record)
    if records != sorted(records, key=lambda item: item["record_id"]):
        raise CanonicalIRError("partition rows are not in canonical key order")
    row_hashes, root = dataset_semantic_hash(records, descriptor)
    if root != claimed_root:
        raise CanonicalIRError("partition semantic root mismatch")
    return records, {
        "partition_id": partition_id,
        "ir_kind": kind,
        "file_path": path.name,
        "file_sha256": hashlib.sha256(raw_file).hexdigest(),
        "row_count": str(len(records)),
        "key_min": records[0]["record_id"],
        "key_max": records[-1]["record_id"],
        "semantic_root": root,
        "row_hashes": row_hashes,
    }


def _load_envelope(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CanonicalIRError("artifact envelope is missing or not regular")
    if path.stat().st_size > MAX_ENVELOPE_BYTES:
        raise CanonicalIRError("artifact envelope exceeds byte limit")
    raw = path.read_bytes()
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise CanonicalIRError("artifact envelope exceeds byte limit")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalIRError("artifact envelope is not UTF-8") from exc
    value = json.loads(
        decoded,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    _reject_floats(value)
    if not isinstance(value, dict):
        raise CanonicalIRError("artifact envelope must be an object")
    if raw != canonical_bytes(value) + b"\n":
        raise CanonicalIRError("artifact envelope is not canonical JSON")

    node_count = 0

    def bound(item: Any, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_ENVELOPE_NODES or depth > MAX_ENVELOPE_DEPTH:
            raise CanonicalIRError("artifact envelope complexity exceeds limit")
        if isinstance(item, dict):
            for child in item.values():
                bound(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                bound(child, depth + 1)

    bound(value, 0)
    if importlib.metadata.version("jsonschema") != REQUIRED_JSONSCHEMA:
        raise CanonicalIRError(
            f"jsonschema must be exactly {REQUIRED_JSONSCHEMA}"
        )
    import jsonschema

    schema = json.loads(
        (
            PHASE2_ROOT / "schemas" / "artifact_envelope.schema.json"
        ).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
    )
    jsonschema.Draft202012Validator(schema).validate(value)
    return value


def _contract_hashes() -> list[str]:
    paths = [
        PHASE2_ROOT / "schemas" / "canonical_ir.schema.json",
        PHASE2_ROOT / "schemas" / "artifact_envelope.schema.json",
        PHASE2_ROOT / "schemas" / "calibration_profile.schema.json",
        PHASE2_ROOT / "contracts" / "ir_descriptors.json",
        PHASE2_ROOT / "contracts" / "codec_profile.json",
        PHASE2_ROOT / "contracts" / "schema_evolution.json",
        PHASE2_ROOT / "contracts" / "runtime_variant_fixture.json",
        SIM_ROOT / "contracts" / "architecture_decisions.json",
        SIM_ROOT / "contracts" / "event_priorities.json",
        SIM_ROOT / "schemas" / "bridge.schema.json",
        SIM_ROOT / "schemas" / "event_ir.schema.json",
        SIM_ROOT / "schemas" / "runtime_variant.schema.json",
        SIM_ROOT / "tools" / "contract_runtime.py",
    ]
    return sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in paths)


def _codec_profile_hash() -> str:
    profile = json.loads(
        (PHASE2_ROOT / "contracts" / "codec_profile.json").read_text(
            encoding="utf-8"
        ),
        object_pairs_hook=_unique_object,
    )
    return hashlib.sha256(canonical_bytes(profile)).hexdigest()


def write_bundle(
    target: Path,
    records: Iterable[dict[str, Any]],
    *,
    evidence_class: str = "SYNTHETIC",
    source_artifact_refs: list[str] | None = None,
    runtime_variants: list[dict[str, Any]] | None = None,
    calibration_profiles: list[dict[str, Any]] | None = None,
    batch_size: int = 65_536,
    max_rows_per_partition: int = MAX_ROWS_PER_PARTITION,
) -> dict[str, Any]:
    if target.exists():
        raise CanonicalIRError("bundle target already exists")
    if batch_size < 1 or batch_size > MAX_ROWS_PER_PARTITION:
        raise CanonicalIRError("batch size is outside limits")
    if max_rows_per_partition < 1 or max_rows_per_partition > MAX_ROWS_PER_PARTITION:
        raise CanonicalIRError("partition row limit is outside limits")
    profile_list = sorted(
        calibration_profiles or [], key=lambda item: item["profile_id"]
    )
    profile_map = _calibration_profile_map(profile_list)
    ordered = validate_records(
        records,
        bundle_evidence_class=evidence_class,
        calibration_profiles=profile_map,
    )
    _, catalog, _ = load_contracts()
    if runtime_variants is None:
        runtime_variants = [
            json.loads(
                (
                    PHASE2_ROOT
                    / "contracts"
                    / "runtime_variant_fixture.json"
                ).read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object,
            )
        ]
    runtime_variant_manifests = _validate_runtime_variants(
        runtime_variants, ordered
    )
    runtime_variant_hashes = [
        item["variant_id"] for item in runtime_variant_manifests
    ]
    calibration_source_refs = {
        item["profile_id"] for item in profile_list
    } | {
        root
        for item in profile_list
        for root in item["evidence_artifact_roots"]
    }
    _, bundle_root = dataset_semantic_hash(
        ordered, catalog["bundle_descriptor"]
    )
    staging = target.parent / f".{target.name}.tmp-{os.getpid()}"
    if staging.exists():
        raise CanonicalIRError("bundle staging path already exists")
    staging.mkdir(parents=False)
    try:
        partitions = []
        for kind in sorted(IR_KINDS):
            kind_records = [item for item in ordered if item["ir_kind"] == kind]
            chunks = [
                kind_records[index : index + max_rows_per_partition]
                for index in range(0, len(kind_records), max_rows_per_partition)
            ]
            for index, chunk in enumerate(chunks):
                partition_id = f"{kind.lower()}-{index:06d}"
                file_name = (
                    KIND_FILE_NAMES[kind]
                    if len(chunks) == 1
                    else f"{kind.lower()}-{index:06d}.arrow.zst"
                )
                partitions.append(
                    _write_partition(
                        staging / file_name,
                        kind,
                        chunk,
                        catalog["descriptors"][kind],
                        batch_size,
                        partition_id,
                    )
                )
        envelope = {
            "schema_version": "artifact-envelope-v1",
            "artifact_id": bundle_root,
            "bundle_semantic_root": bundle_root,
            "row_count": str(len(ordered)),
            "primary_key": ["ir_kind", "record_id"],
            "codec_profile_hash": _codec_profile_hash(),
            "producer": {
                "tool": "canonical_ir.py",
                "version": "1",
                "build_hash": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
            },
            "contract_hashes": _contract_hashes(),
            "source_artifact_refs": sorted(
                set(source_artifact_refs or [])
                | set(runtime_variant_hashes)
                | calibration_source_refs
            ),
            "runtime_variant_hashes": runtime_variant_hashes,
            "runtime_variants": runtime_variant_manifests,
            "calibration_profiles": profile_list,
            "evidence_class": evidence_class,
            "completeness": "COMPLETE",
            "status": "PASS",
            "partitions": [
                {key: value for key, value in item.items() if key != "row_hashes"}
                for item in partitions
            ],
        }
        envelope_path = staging / "artifact-envelope.json"
        with envelope_path.open("xb") as stream:
            stream.write(canonical_bytes(envelope) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        read_bundle(envelope_path)
        directory_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(staging, target)
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return envelope
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def read_bundle(envelope_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    envelope = _load_envelope(envelope_path)
    root = envelope_path.parent
    partition_paths = [item["file_path"] for item in envelope["partitions"]]
    if len(partition_paths) != len(set(partition_paths)):
        raise CanonicalIRError("duplicate partition file path")
    if any(Path(item).name != item for item in partition_paths):
        raise CanonicalIRError("unsafe partition file path")
    expected_files = {"artifact-envelope.json"} | set(partition_paths)
    actual_files: set[str] = set()
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise CanonicalIRError("bundle contains non-regular entry")
        actual_files.add(path.name)
    if actual_files != expected_files:
        raise CanonicalIRError("bundle file closure mismatch")
    if envelope["codec_profile_hash"] != _codec_profile_hash():
        raise CanonicalIRError("codec profile hash mismatch")
    if envelope["contract_hashes"] != _contract_hashes():
        raise CanonicalIRError("contract hash closure mismatch")
    _, catalog, _ = load_contracts()
    claimed_by_kind: dict[str, list[dict[str, Any]]] = {
        kind: [] for kind in IR_KINDS
    }
    partition_ids: set[str] = set()
    for item in envelope["partitions"]:
        if item["partition_id"] in partition_ids:
            raise CanonicalIRError("duplicate partition identity")
        partition_ids.add(item["partition_id"])
        claimed_by_kind[item["ir_kind"]].append(item)
    if any(not items for items in claimed_by_kind.values()):
        raise CanonicalIRError("partition kind closure mismatch")
    all_records: list[dict[str, Any]] = []
    for kind in sorted(IR_KINDS):
        partitions = sorted(
            claimed_by_kind[kind], key=lambda item: item["key_min"]
        )
        for left, right in zip(partitions, partitions[1:]):
            if left["key_max"] >= right["key_min"]:
                raise CanonicalIRError("partition key ranges overlap")
        for claimed in partitions:
            records, observed = _read_partition(
                root / claimed["file_path"],
                kind,
                catalog["descriptors"][kind],
                claimed["partition_id"],
            )
            observed.pop("row_hashes")
            if observed != claimed:
                raise CanonicalIRError("partition envelope mismatch")
            all_records.extend(records)
    profile_list = envelope["calibration_profiles"]
    profile_map = _calibration_profile_map(profile_list)
    ordered = validate_records(
        all_records,
        bundle_evidence_class=envelope["evidence_class"],
        calibration_profiles=profile_map,
    )
    runtime_variant_manifests = _validate_runtime_variants(
        envelope["runtime_variants"], ordered
    )
    runtime_variant_hashes = [
        item["variant_id"] for item in runtime_variant_manifests
    ]
    if (
        envelope["runtime_variant_hashes"] != runtime_variant_hashes
        or not set(runtime_variant_hashes).issubset(
            envelope["source_artifact_refs"]
        )
    ):
        raise CanonicalIRError("runtime variant envelope binding mismatch")
    calibration_source_refs = {
        item["profile_id"] for item in profile_list
    } | {
        evidence_root
        for item in profile_list
        for evidence_root in item["evidence_artifact_roots"]
    }
    if not calibration_source_refs.issubset(envelope["source_artifact_refs"]):
        raise CanonicalIRError("calibration profile source closure mismatch")
    _, bundle_root = dataset_semantic_hash(
        ordered, catalog["bundle_descriptor"]
    )
    if envelope["artifact_id"] != bundle_root:
        raise CanonicalIRError("artifact ID is not bundle semantic root")
    if envelope["bundle_semantic_root"] != bundle_root:
        raise CanonicalIRError("bundle semantic root mismatch")
    if int(envelope["row_count"]) != len(ordered):
        raise CanonicalIRError("bundle row count mismatch")
    if envelope["completeness"] != "COMPLETE" or envelope["status"] != "PASS":
        raise CanonicalIRError("reader accepts only complete PASS bundles")
    return ordered, envelope
