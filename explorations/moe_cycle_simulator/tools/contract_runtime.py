"""Executable Phase 0 semantic contracts for clocks, traces and provenance."""
from __future__ import annotations

import hashlib
import json
import math
import struct
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
FS_PER_SECOND = 10**15
MAX_FREQUENCY_HZ = FS_PER_SECOND


class ContractError(ValueError):
    pass


def uint_string(value: Any, name: str, maximum: int = U128_MAX) -> int:
    if not isinstance(value, str) or not value or (
        value != "0" and (value.startswith("0") or not value.isdigit())
    ):
        raise ContractError(f"{name} must be a canonical unsigned decimal string")
    number = int(value)
    if number > maximum:
        raise ContractError(f"{name} exceeds its unsigned bound")
    return number


def sint_string(value: Any, name: str) -> int:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{name} must be a canonical signed decimal string")
    if value == "-0":
        raise ContractError(f"{name} has non-canonical negative zero")
    body = value[1:] if value.startswith("-") else value
    if not body.isdigit() or (body != "0" and body.startswith("0")):
        raise ContractError(f"{name} must be a canonical signed decimal string")
    return int(value)


@dataclass(frozen=True)
class Clock:
    clock_id: str
    frequency_numerator_hz: int
    frequency_denominator_hz: int
    phase_offset_fs: int
    local_cycle: int
    fractional_remainder: int

    @classmethod
    def from_record(cls, value: dict[str, Any]) -> "Clock":
        numerator = uint_string(
            value["frequency_numerator_hz"], "frequency_numerator_hz"
        )
        denominator = uint_string(
            value["frequency_denominator_hz"], "frequency_denominator_hz"
        )
        if not numerator or not denominator:
            raise ContractError("clock frequency must be positive")
        if math.gcd(numerator, denominator) != 1:
            raise ContractError("clock frequency must be gcd-normalized")
        if numerator > MAX_FREQUENCY_HZ * denominator:
            raise ContractError("clock frequency exceeds 1 PHz")
        cycle = uint_string(value["local_cycle"], "local_cycle", U64_MAX)
        phase = uint_string(value["phase_offset_fs"], "phase_offset_fs")
        remainder = uint_string(
            value["fractional_remainder"], "fractional_remainder"
        )
        expected = (cycle * FS_PER_SECOND * denominator) % numerator
        if remainder != expected or remainder >= numerator:
            raise ContractError("fractional_remainder is not canonical")
        clock = cls(
            value["clock_id"], numerator, denominator, phase, cycle, remainder
        )
        if clock.edge_time(cycle) > U128_MAX:
            raise ContractError("clock state exceeds global u128 time")
        return clock

    def edge_time(self, cycle: int) -> int:
        if cycle < 0 or cycle > U64_MAX:
            raise ContractError("cycle exceeds uint64")
        result = self.phase_offset_fs + (
            cycle * FS_PER_SECOND * self.frequency_denominator_hz
        ) // self.frequency_numerator_hz
        if result > U128_MAX:
            raise ContractError("edge time exceeds uint128")
        return result

    def remainder(self, cycle: int) -> int:
        return (
            cycle * FS_PER_SECOND * self.frequency_denominator_hz
        ) % self.frequency_numerator_hz

    def ceil_edge(self, time_fs: int) -> int:
        if time_fs <= self.phase_offset_fs:
            return 0
        delta = time_fs - self.phase_offset_fs
        numerator = delta * self.frequency_numerator_hz
        denominator = FS_PER_SECOND * self.frequency_denominator_hz
        estimate = (numerator + denominator - 1) // denominator
        while estimate and self.edge_time(estimate - 1) >= time_fs:
            estimate -= 1
        while self.edge_time(estimate) < time_fs:
            estimate += 1
        return estimate


def validate_bridge(bridge: dict[str, Any], clocks: dict[str, Clock]) -> None:
    source = bridge["source_clock_id"]
    target = bridge["target_clock_id"]
    if source not in clocks or target not in clocks:
        raise ContractError("bridge references an unknown clock")
    forward = uint_string(bridge["forward_latency_fs"], "forward_latency_fs")
    reverse = uint_string(bridge["reverse_latency_fs"], "reverse_latency_fs")
    protocol = bridge["protocol"]
    policy = bridge["backpressure_policy"]
    if protocol == "ONE_WAY" and (
        reverse or bridge["ack_sync_cycles"] != 0
    ):
        raise ContractError("ONE_WAY bridge cannot define an acknowledge path")
    if protocol == "CREDIT" and policy != "CREDIT_BLOCK":
        raise ContractError("CREDIT protocol requires CREDIT_BLOCK")
    if protocol != "CREDIT" and policy == "CREDIT_BLOCK":
        raise ContractError("CREDIT_BLOCK requires CREDIT protocol")
    if forward == 0 and bridge["receiver_sync_cycles"] == 0:
        raise ContractError("bridge request path must guarantee strict time progress")


def cdc_arrival(
    source_completion_fs: int,
    bridge: dict[str, Any],
    clocks: dict[str, Clock],
) -> int:
    validate_bridge(bridge, clocks)
    target = clocks[bridge["target_clock_id"]]
    forward = uint_string(bridge["forward_latency_fs"], "forward_latency_fs")
    capture = target.ceil_edge(source_completion_fs + forward)
    destination_cycle = capture + bridge["receiver_sync_cycles"]
    return target.edge_time(destination_cycle)


def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        raise ContractError("floats are forbidden in semantic values")
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        if normalized == "-0":
            raise ContractError("negative zero is forbidden")
        return normalized
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ContractError("object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in output:
                raise ContractError("normalization creates duplicate object keys")
            output[key] = _normalize(item)
        return output
    if value is None or isinstance(value, (bool, int)):
        return value
    raise ContractError(f"unsupported semantic type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def schema_fingerprint(descriptor: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(descriptor)).hexdigest()


def dataset_semantic_hash(
    rows: Iterable[dict[str, Any]],
    descriptor: dict[str, Any],
) -> tuple[list[str], str]:
    fingerprint = schema_fingerprint(descriptor)
    primary_key = descriptor["primary_key"]
    keyed_rows: list[tuple[bytes, dict[str, Any]]] = []
    seen: set[bytes] = set()
    for row in rows:
        normalized = _normalize(row)
        try:
            key = canonical_bytes([normalized[name] for name in primary_key])
        except KeyError as exc:
            raise ContractError(
                f"missing primary key field: {exc.args[0]}"
            ) from exc
        if key in seen:
            raise ContractError("duplicate primary key")
        seen.add(key)
        keyed_rows.append((key, normalized))
    ordered = [row for _, row in sorted(keyed_rows, key=lambda item: item[0])]
    fingerprint_bytes = bytes.fromhex(fingerprint)
    row_hashes = [
        hashlib.sha256(
            b"moe-row-v1\0"
            + fingerprint_bytes
            + b"\0"
            + canonical_bytes(row)
        ).hexdigest()
        for row in ordered
    ]
    aggregate = hashlib.sha256(
        b"moe-dataset-v1\0"
        + fingerprint_bytes
        + b"".join(bytes.fromhex(item) for item in row_hashes)
    ).hexdigest()
    return row_hashes, aggregate


def runtime_variant_hash(value: dict[str, Any]) -> str:
    semantic = {key: item for key, item in value.items() if key != "variant_id"}
    return hashlib.sha256(
        b"moe-runtime-variant-v1\0" + canonical_bytes(semantic)
    ).hexdigest()


def validate_runtime_variant(value: dict[str, Any]) -> None:
    if value["variant_id"] != runtime_variant_hash(value):
        raise ContractError("runtime variant ID does not match semantic fields")


def validate_observability(value: dict[str, Any]) -> None:
    availability = value["availability"]
    evidence = value["evidence_mode"]
    expected = value.get("expected_evidence_modes")
    if availability == "CONFIRMED" and evidence == "NONE":
        raise ContractError("confirmed observation requires evidence")
    if availability == "CONDITIONAL":
        if evidence != "NONE" or not expected:
            raise ContractError("conditional observation requires expected modes")
    elif expected is not None:
        raise ContractError("expected modes are valid only for conditional evidence")
    if availability in {"UNAVAILABLE", "NOT_APPLICABLE"} and evidence != "NONE":
        raise ContractError("unavailable/not-applicable observation has no evidence")


def validate_result_evidence(
    value: dict[str, Any], formal_stage: str | None = None
) -> None:
    fidelity = value["fidelity"]
    status = value["range_status"]
    if fidelity == "MEASURED" and status != "IN_CALIBRATION_ENVELOPE":
        raise ContractError("measured evidence must be in its measured envelope")
    if fidelity in {"UNAVAILABLE", "FUNCTIONAL_ONLY", "ANALYTIC_FIRST_ORDER"}:
        if status != "RANGE_UNKNOWN":
            raise ContractError(f"{fidelity} requires RANGE_UNKNOWN")
    if fidelity == "CALIBRATED_SURROGATE":
        required = (
            value.get("envelope_distance"),
            value.get("nearest_calibration_point"),
            value.get("calibration_profile_hash"),
        )
        if any(item is None for item in required):
            raise ContractError("calibrated surrogate lacks envelope provenance")
        distance = value["envelope_distance"]
        if not isinstance(distance, dict) or Decimal(distance["value"]) < 0:
            raise ContractError("calibrated envelope distance is invalid")
    if formal_stage in {"CALIBRATION_PASS", "BREAK_EVEN_PASS"} and status in {
        "EXTRAPOLATED",
        "RANGE_UNKNOWN",
    }:
        raise ContractError("out-of-range evidence is ineligible for formal PASS")
    if formal_stage and fidelity == "UNAVAILABLE":
        raise ContractError("unavailable evidence is ineligible for formal PASS")


def transform_alignment(value: dict[str, Any], source_time: int) -> int:
    valid = value["valid_time_range"]
    start = uint_string(valid["source_start"], "valid source start")
    end = uint_string(valid["source_end"], "valid source end")
    if not start <= source_time <= end:
        raise ContractError("alignment source time outside valid range")
    if value["transform_type"] == "PIECEWISE_AFFINE_RATIONAL":
        selected = None
        for segment in value["segments"]:
            left = uint_string(segment["source_start"], "segment start")
            right = uint_string(segment["source_end"], "segment end")
            if left <= source_time < right or (
                source_time == right and segment["end_inclusive"]
            ):
                selected = segment
                break
        if selected is None:
            raise ContractError("alignment source time falls in a segment gap")
        numerator = uint_string(selected["scale_numerator"], "scale numerator")
        denominator = uint_string(
            selected["scale_denominator"], "scale denominator"
        )
        offset = sint_string(selected["offset_fs"], "offset_fs")
    else:
        numerator = uint_string(value["scale_numerator"], "scale numerator")
        denominator = uint_string(value["scale_denominator"], "scale denominator")
        offset = sint_string(value["offset_fs"], "offset_fs")
    target = (source_time * numerator) // denominator + offset
    if target < 0 or target > U128_MAX:
        raise ContractError("alignment maps outside global time range")
    return target


def alignment_grade(value: dict[str, Any]) -> str:
    interval = value["confidence_interval_95_fs"]
    lower = sint_string(interval["lower_error_fs"], "CI lower")
    upper = sint_string(interval["upper_error_fs"], "CI upper")
    if lower > upper:
        raise ContractError("alignment confidence interval is inverted")
    half_width = (upper - lower + 1) // 2
    inputs = value["grading_inputs"]
    period_num = uint_string(
        inputs["target_period_numerator_fs"], "target period numerator"
    )
    period_den = uint_string(
        inputs["target_period_denominator"], "target period denominator"
    )
    shortest = uint_string(
        inputs["shortest_component_duration_fs"],
        "shortest component duration",
    )
    cycle_threshold = period_num // (4 * period_den)
    component_threshold = shortest // 20
    if value["transform_type"] == "PIECEWISE_AFFINE_RATIONAL" and any(
        segment["boundary_discontinuity"] for segment in value["segments"]
    ):
        return "AGGREGATE_ONLY"
    return (
        "CYCLE_GRADE"
        if half_width <= min(cycle_threshold, component_threshold)
        else "AGGREGATE_ONLY"
    )


def pair_alignment_grade(
    left: dict[str, Any],
    left_source_time: int,
    right: dict[str, Any],
    right_source_time: int,
) -> str:
    if alignment_grade(left) == alignment_grade(right) == "CYCLE_GRADE":
        return "CYCLE_GRADE"

    def interval(value: dict[str, Any], source_time: int) -> tuple[int, int]:
        center = transform_alignment(value, source_time)
        confidence = value["confidence_interval_95_fs"]
        return (
            center + sint_string(confidence["lower_error_fs"], "CI lower"),
            center + sint_string(confidence["upper_error_fs"], "CI upper"),
        )

    left_interval = interval(left, left_source_time)
    right_interval = interval(right, right_source_time)
    if left_interval[1] < right_interval[0] or right_interval[1] < left_interval[0]:
        return "ORDERING_ONLY"
    return "AGGREGATE_ONLY"


def validate_alignment(value: dict[str, Any]) -> None:
    transform = value["transform_type"]
    if transform == "IDENTITY" and (
        value["scale_numerator"] != "1"
        or value["scale_denominator"] != "1"
        or value["offset_fs"] != "0"
    ):
        raise ContractError("IDENTITY alignment must be exactly scale 1/1 offset 0")
    if transform == "PIECEWISE_AFFINE_RATIONAL":
        segments = value["segments"]
        previous_end = None
        for index, segment in enumerate(segments):
            start = uint_string(segment["source_start"], "segment start")
            end = uint_string(segment["source_end"], "segment end")
            numerator = uint_string(
                segment["scale_numerator"], "scale numerator"
            )
            denominator = uint_string(
                segment["scale_denominator"], "scale denominator"
            )
            if start >= end or math.gcd(numerator, denominator) != 1:
                raise ContractError("invalid piecewise alignment segment")
            if previous_end is not None and start < previous_end:
                raise ContractError("piecewise segments overlap")
            if index < len(segments) - 1 and segment["end_inclusive"]:
                raise ContractError("only final segment may be right-inclusive")
            previous_end = end
    else:
        numerator = uint_string(value["scale_numerator"], "scale numerator")
        denominator = uint_string(value["scale_denominator"], "scale denominator")
        if math.gcd(numerator, denominator) != 1:
            raise ContractError("alignment scale must be gcd-normalized")
    points = value["calibration_points"]
    minimum_points = 1 if transform == "IDENTITY" else 2
    if len(points) < minimum_points:
        raise ContractError("insufficient alignment calibration points")
    residual_bound = uint_string(value["residual_error_fs"], "residual_error_fs")
    segment_point_counts = [0] * len(value.get("segments", []))
    for point in points:
        source = uint_string(point["source_time"], "calibration source")
        observed = uint_string(point["target_time"], "calibration target")
        if abs(transform_alignment(value, source) - observed) > residual_bound:
            raise ContractError("alignment point exceeds residual bound")
        if transform == "PIECEWISE_AFFINE_RATIONAL":
            for index, segment in enumerate(value["segments"]):
                start = int(segment["source_start"])
                end = int(segment["source_end"])
                if start <= source < end or (
                    source == end and segment["end_inclusive"]
                ):
                    segment_point_counts[index] += 1
                    break
    if segment_point_counts and any(count < 2 for count in segment_point_counts):
        raise ContractError("each piecewise segment requires two calibration points")
    if alignment_grade(value) != value["claimed_grade"]:
        raise ContractError("claimed alignment grade was not rederived")


def validate_routing(value: dict[str, Any]) -> None:
    validate_observability(value["observability"])
    num_experts = value["num_experts"]
    top_k = value["top_k"]
    selected = value["selected_experts"]
    if top_k > num_experts or len(selected) != top_k:
        raise ContractError("routing top-k shape mismatch")
    if len(set(selected)) != len(selected) or any(
        item < 0 or item >= num_experts for item in selected
    ):
        raise ContractError("routing selected expert out of range or duplicate")
    scores = value["canonical_scores"]
    if value["observability"]["availability"] == "CONFIRMED":
        required = (
            scores,
            value["score_dtype"],
            value["score_tolerance_absolute"],
            value["score_tolerance_relative"],
            value["k_boundary_score"],
            value["ambiguity_set"],
        )
        if any(item is None for item in required):
            raise ContractError("confirmed routing lacks score evidence")
    else:
        if scores is not None:
            raise ContractError("unavailable routing cannot carry scores")
        return
    if len(scores) != num_experts:
        raise ContractError("routing score count must equal num_experts")
    relative = Decimal(value["score_tolerance_relative"])
    if relative < 0 or relative > Decimal("0.00001"):
        raise ContractError("relative routing tolerance exceeds frozen maximum")
    decimal_scores = [Decimal(item) for item in scores]
    quantized_scores = [
        _exact_source_dtype_decimal(item, value["score_dtype"])
        for item in decimal_scores
    ]
    if decimal_scores != quantized_scores:
        raise ContractError("routing score is not exactly representable in source dtype")
    ordered = sorted(range(num_experts), key=lambda i: (-decimal_scores[i], i))
    expected_selected = ordered[:top_k]
    if selected != expected_selected:
        raise ContractError("selected experts do not match stable top-k")
    boundary = decimal_scores[ordered[top_k - 1]]
    if Decimal(value["k_boundary_score"]) != boundary:
        raise ContractError("k boundary score mismatch")
    absolute = Decimal(value["score_tolerance_absolute"])
    expected_absolute = Decimal(4) * _source_dtype_ulp(
        boundary, value["score_dtype"]
    )
    if absolute != expected_absolute:
        raise ContractError("absolute routing tolerance must equal four source-dtype ULP")
    expected_ambiguity = sorted(
        i
        for i, score in enumerate(decimal_scores)
        if abs(score - boundary)
        <= absolute + relative * max(abs(score), abs(boundary))
    )
    if value["ambiguity_set"] != expected_ambiguity:
        raise ContractError("routing ambiguity set mismatch")


def _quantized_float(value: Decimal, dtype: str) -> float:
    source = float(value)
    if dtype == "float32":
        return struct.unpack(">f", struct.pack(">f", source))[0]
    if dtype == "float16":
        return struct.unpack(">e", struct.pack(">e", source))[0]
    if dtype == "bfloat16":
        bits = struct.unpack(">I", struct.pack(">f", source))[0]
        upper = bits >> 16
        return struct.unpack(">f", struct.pack(">I", upper << 16))[0]
    raise ContractError("unsupported routing score dtype")


def _exact_source_dtype_decimal(value: Decimal, dtype: str) -> Decimal:
    return Decimal.from_float(_quantized_float(value, dtype))


def _source_dtype_ulp(value: Decimal, dtype: str) -> Decimal:
    current = _quantized_float(value, dtype)
    if dtype == "float32":
        bits = struct.unpack(">I", struct.pack(">f", current))[0]
        next_value = struct.unpack(">f", struct.pack(">I", bits + 1))[0]
    elif dtype == "float16":
        bits = struct.unpack(">H", struct.pack(">e", current))[0]
        next_value = struct.unpack(">e", struct.pack(">H", bits + 1))[0]
    elif dtype == "bfloat16":
        bits = struct.unpack(">I", struct.pack(">f", current))[0] >> 16
        next_value = struct.unpack(">f", struct.pack(">I", (bits + 1) << 16))[0]
    else:
        raise ContractError("unsupported routing score dtype")
    return abs(Decimal.from_float(next_value) - Decimal.from_float(current))


def event_tie_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        uint_string(value["time_fs"], "event time"),
        value["event_priority"],
        unicodedata.normalize("NFC", value.get("request_id") or "").encode("utf-8"),
        value["token_index"] if value.get("token_index") is not None else U64_MAX,
        value["layer_index"]
        if value.get("layer_index") is not None
        else (1 << 32) - 1,
        unicodedata.normalize("NFC", value["component_id"]).encode("utf-8"),
        unicodedata.normalize("NFC", value["event_id"]).encode("utf-8"),
    )


def reject_float_tree(value: Any) -> None:
    if isinstance(value, float):
        raise ContractError("float values are forbidden")
    if isinstance(value, dict):
        for item in value.values():
            reject_float_tree(item)
    elif isinstance(value, list):
        for item in value:
            reject_float_tree(item)


def validate_events(
    events: list[dict[str, Any]],
    clocks: dict[str, Clock],
    priorities: dict[str, int],
) -> list[dict[str, Any]]:
    ids: dict[str, dict[str, Any]] = {}
    keys: set[tuple[Any, ...]] = set()
    for event in events:
        reject_float_tree(event.get("attributes", {}))
        if event["clock_id"] not in clocks:
            raise ContractError("event references unknown clock")
        expected_priority = priorities.get(event["event_type"])
        if expected_priority is None or expected_priority != event["event_priority"]:
            raise ContractError("event priority does not match the frozen table")
        if event["event_id"] in ids:
            raise ContractError("duplicate event ID")
        key = event_tie_key(event)
        if key in keys:
            raise ContractError("duplicate complete event tie key")
        keys.add(key)
        ids[event["event_id"]] = event
    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(event_id: str) -> None:
        if event_id in complete:
            return
        if event_id in visiting:
            raise ContractError("event dependency cycle")
        visiting.add(event_id)
        event = ids[event_id]
        event_time = uint_string(event["time_fs"], "event time")
        for dependency in event["dependencies"]:
            if dependency not in ids or dependency == event_id:
                raise ContractError("missing or self event dependency")
            dependency_time = uint_string(
                ids[dependency]["time_fs"], "dependency time"
            )
            if dependency_time > event_time:
                raise ContractError("event occurs before dependency")
            if (
                dependency_time == event_time
                and event_tie_key(ids[dependency]) >= event_tie_key(event)
            ):
                raise ContractError("same-time dependency sorts after its consumer")
            visit(dependency)
        visiting.remove(event_id)
        complete.add(event_id)

    for event_id in ids:
        visit(event_id)
    return sorted(events, key=event_tie_key)
