"""P2 collector for actual dispatch records supplied by a model runner."""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable, Mapping

from adapters.models.contract import GATE_SUM_ABS_TOLERANCE, gate_sum_abs_tolerance

from .c1_common import as_mapping, generate, load_runner
from .c1_contract import (
    CollectorRequest, CollectorResult, ModelRunnerLike, RecordCallback,
    RoutingCaptureLike, build_event_key, build_execution_alignment_key,
)


NULLABLE_ROUTING_FIELDS = ("routing_weights", "router_logits")
NUM_LAYERS = 24
NUM_EXPERTS = 32
TOP_K = 8


def _normalize_dispatch(
    dispatch: Mapping[str, Any], request: CollectorRequest, alignment_key: str
) -> dict[str, Any]:
    required = (
        "token_index", "global_token_position", "layer_id", "selected_experts",
        "call_index", "input_sequence_length", "phase", "generation_step",
        "gate_dtype",
    )
    missing = [name for name in required if dispatch.get(name) is None]
    if missing:
        raise ValueError(f"actual dispatch record missing: {', '.join(missing)}")
    if dispatch.get("actual_dispatch_verified") is not True:
        raise ValueError("P2 record is not verified actual dispatch")
    experts = dispatch["selected_experts"]
    if not isinstance(experts, list) or len(experts) != TOP_K:
        raise ValueError("selected_experts must contain exactly 8 expert IDs")
    if (
        any(
            not isinstance(item, int)
            or isinstance(item, bool)
            or item < 0
            or item >= NUM_EXPERTS
            for item in experts
        )
        or len(set(experts)) != TOP_K
    ):
        raise ValueError("selected_experts must be 8 distinct IDs in [0, 32)")
    weights = dispatch.get("routing_weights")
    gate_dtype = dispatch.get("gate_dtype")
    if gate_dtype not in GATE_SUM_ABS_TOLERANCE:
        raise ValueError(
            f"gate_dtype must be one of {sorted(GATE_SUM_ABS_TOLERANCE)}"
        )
    weight_sum_tolerance = gate_sum_abs_tolerance(gate_dtype)
    if (
        not isinstance(weights, list)
        or len(weights) != TOP_K
        or any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(item)
            or item < 0
            for item in weights
        )
        or not math.isclose(
            sum(weights), 1.0,
            rel_tol=0.0,
            abs_tol=weight_sum_tolerance,
        )
    ):
        raise ValueError(
            "routing_weights must be 8 finite non-negative values summing to 1 "
            f"within {gate_dtype} tolerance {weight_sum_tolerance}"
        )
    logits = dispatch.get("router_logits")
    if (
        not isinstance(logits, list)
        or len(logits) != NUM_EXPERTS
        or any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(item)
            for item in logits
        )
    ):
        raise ValueError("router_logits must contain exactly 32 finite values")
    row: dict[str, Any] = dict(dispatch)
    row.update({
        "schema_version": "c1-routing-event-v1",
        "request_id": request.request_id,
        "execution_alignment_key": alignment_key,
        "router_module": row.get("router_module", "unknown"),
        "dispatch_index": row.get("dispatch_index", 0),
        "top_k": TOP_K,
        "actual_dispatch": True,
        "gate_dtype": gate_dtype,
    })
    unavailable = dict(row.get("unavailable_reasons") or {})
    for field in NULLABLE_ROUTING_FIELDS:
        if row.get(field) is None and not unavailable.get(field):
            raise ValueError(f"{field}=null requires unavailable_reasons.{field}")
    row["unavailable_reasons"] = unavailable
    row["event_key"] = build_event_key(row)
    return row


def _layer_id(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    text = str(value)
    digits = [part for part in text.replace("[", ".").replace("]", ".").split(".")
              if part.isdigit()]
    if not digits:
        raise ValueError(f"cannot derive layer_id from {value!r}")
    return int(digits[-1])


def _router_logits(value: Any, token: int) -> list[float] | None:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
        value = value[token] if token < len(value) else None
    if not isinstance(value, (list, tuple)):
        return None
    flattened: list[float] = []
    for item in value:
        if isinstance(item, (list, tuple)):
            flattened.extend(float(child) for child in item)
        else:
            flattened.append(float(item))
    return flattened


def _expand_adapter_dispatch(
    dispatch: Any,
    generation: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    value = asdict(dispatch) if is_dataclass(dispatch) else as_mapping(dispatch)
    if "selected_experts" in value:
        return [value]
    required = ("token_indices", "expert_indices", "gates")
    if any(name not in value for name in required):
        return [value]
    tokens = list(value["token_indices"])
    experts = list(value["expert_indices"])
    gates = list(value["gates"])
    if not (len(tokens) == len(experts) == len(gates)):
        raise ValueError("actual dispatch tuple lengths differ")
    call_index = value.get("call_index")
    sequence_length = value.get("input_sequence_length")
    input_count = (
        generation.get("input_token_count") if generation is not None
        else value.get("generation_input_token_count")
    )
    output_count = (
        generation.get("output_token_count") if generation is not None
        else value.get("generation_output_token_count")
    )
    if (
        not isinstance(call_index, int)
        or call_index < 0
        or not isinstance(sequence_length, int)
        or sequence_length <= 0
        or not isinstance(input_count, int)
        or input_count <= 0
        or not isinstance(output_count, int)
        or output_count <= 0
    ):
        raise ValueError("dispatch lacks proven generation call metadata")
    expected_length = input_count if call_index == 0 else 1
    if sequence_length != expected_length or call_index >= output_count:
        raise ValueError("dispatch input length/call index contradicts HF generate semantics")
    grouped: dict[int, list[tuple[int, float]]] = {}
    for token, expert, gate in zip(tokens, experts, gates):
        grouped.setdefault(int(token), []).append((int(expert), float(gate)))
    rows = []
    if set(grouped) != set(range(sequence_length)):
        raise ValueError("dispatch token indices do not cover the input sequence")
    phase = "prefill" if call_index == 0 else "decode"
    generation_step = 0 if call_index == 0 else call_index - 1
    for dispatch_index, (token, choices) in enumerate(sorted(grouped.items())):
        logits = _router_logits(value.get("router_logits"), token)
        global_position = (
            token
            if call_index == 0
            else input_count + call_index - 1 + token
        )
        rows.append({
            "token_index": token,
            "global_token_position": global_position,
            "layer_id": _layer_id(value.get("layer")),
            "selected_experts": [expert for expert, _ in choices],
            "routing_weights": [gate for _, gate in choices],
            "gate_dtype": value.get("gate_dtype"),
            "router_logits": logits,
            "unavailable_reasons": {},
            "router_module": str(value.get("layer", "unknown")),
            "dispatch_index": dispatch_index,
            "phase": phase,
            "call_index": call_index,
            "generation_step": generation_step,
            "input_sequence_length": sequence_length,
            "generation_input_token_count": input_count,
            "generation_output_token_count": output_count,
            "actual_dispatch_verified": value.get("actual_dispatch_verified"),
            "evidence_class": value.get("evidence_class"),
        })
    return rows


def _validate_trace_semantics(rows: list[dict[str, Any]]) -> None:
    input_counts = {row.get("generation_input_token_count") for row in rows}
    output_counts = {row.get("generation_output_token_count") for row in rows}
    if (
        len(input_counts) != 1
        or len(output_counts) != 1
        or not isinstance(next(iter(input_counts)), int)
        or not isinstance(next(iter(output_counts)), int)
    ):
        raise ValueError("routing trace lacks unique generation token counts")
    input_count = next(iter(input_counts))
    output_count = next(iter(output_counts))
    if input_count <= 0 or output_count <= 0:
        raise ValueError("routing trace token counts must be positive")
    by_layer_call: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_layer_call[(int(row["layer_id"]), int(row["call_index"]))].append(row)
    if {layer for layer, _call in by_layer_call} != set(range(NUM_LAYERS)):
        raise ValueError("routing trace must cover exactly Granite layers 0..23")
    for layer in range(NUM_LAYERS):
        calls = {
            call for observed_layer, call in by_layer_call if observed_layer == layer
        }
        if calls != set(range(output_count)):
            raise ValueError(
                f"layer {layer} must have one prefill plus {output_count - 1} decode calls"
            )
        for call in range(output_count):
            call_rows = by_layer_call[(layer, call)]
            expected_length = input_count if call == 0 else 1
            if len(call_rows) != expected_length:
                raise ValueError(
                    f"layer {layer} call {call} does not cover its input sequence"
                )
            expected_phase = "prefill" if call == 0 else "decode"
            expected_step = 0 if call == 0 else call - 1
            if any(
                row.get("phase") != expected_phase
                or row.get("generation_step") != expected_step
                or row.get("input_sequence_length") != expected_length
                for row in call_rows
            ):
                raise ValueError("routing phase/call/generation-step mapping is inconsistent")
            expected_positions = (
                set(range(input_count))
                if call == 0
                else {input_count + call - 1}
            )
            if {row["global_token_position"] for row in call_rows} != expected_positions:
                raise ValueError("routing global token positions are inconsistent")


def collect(
    runner: ModelRunnerLike,
    request: CollectorRequest,
    dispatch_records: Iterable[Mapping[str, Any]] | None = None,
    emit: RecordCallback | None = None,
) -> CollectorResult:
    result = CollectorResult("P2")
    alignment = build_execution_alignment_key(request.execution)
    captured: list[Any] = []
    generation: Mapping[str, Any] | None = None
    routing_runner = runner if isinstance(runner, RoutingCaptureLike) else None
    if dispatch_records is None and routing_runner is None:
        raise TypeError("P2 requires actual dispatch_records or routing capture callbacks")
    started = time.perf_counter_ns()
    if dispatch_records is None:
        load_runner(runner)
        routing_runner.enable_routing_capture()
        try:
            tokens = runner.tokenize(request.prompt)
            generation = as_mapping(generate(runner, tokens, request.generation_config))
            captured.extend(generation.get("routing") or [])
        finally:
            returned = routing_runner.disable_routing_capture()
            if returned is not None and not captured:
                captured.extend(returned)
        dispatch_records = captured
    expanded = [
        row
        for dispatch in dispatch_records
        for row in _expand_adapter_dispatch(dispatch, generation)
    ]
    _validate_trace_semantics(expanded)
    normalized = [_normalize_dispatch(dispatch, request, alignment) for dispatch in expanded]
    overhead_ns = time.perf_counter_ns() - started
    if not normalized:
        raise ValueError("P2 actual dispatch trace is empty")
    for row in normalized:
        row["capture_overhead_total_ns"] = overhead_ns
        result.add(row, emit)
    return result
