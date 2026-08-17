"""Small helpers for model-independent C1 collector adapters."""
from __future__ import annotations

import math
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from adapters.models.contract import GenerationRequest

from .c1_contract import CollectorRequest, ModelRunnerLike
from .trace_contract import canonical_hash


def as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"runner result must be a mapping or dataclass, got {type(value).__name__}")


def generation_request(config: Mapping[str, Any]) -> GenerationRequest:
    values = dict(config)
    extra = dict(values.pop("extra", {}))
    for ignored in ("batch_size", "compile", "speculative_decoding"):
        values.pop(ignored, None)
    known = {"max_new_tokens", "do_sample", "num_beams", "use_cache", "seed"}
    extra.update({key: values.pop(key) for key in list(values) if key not in known})
    if "max_new_tokens" not in values:
        raise ValueError("generation config requires max_new_tokens")
    return GenerationRequest(**values, extra=extra)


def load_runner(runner: ModelRunnerLike) -> None:
    try:
        runner.load_model(local_files_only=True)
    except TypeError:
        runner.load_model()


def generate(runner: ModelRunnerLike, tokens: Any, config: Mapping[str, Any]) -> Any:
    try:
        return runner.generate(tokens, generation_request(config))
    except TypeError:
        return runner.generate(tokens, **dict(config))


def run_generation(
    runner: ModelRunnerLike, request: CollectorRequest
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    load_started = time.perf_counter_ns()
    load_runner(runner)
    loaded = time.perf_counter_ns()
    tokens = runner.tokenize(request.prompt)
    started = time.perf_counter_ns()
    output = generate(runner, tokens, request.generation_config)
    ended = time.perf_counter_ns()
    return as_mapping(output), {
        "load_ns": loaded - load_started,
        "generation_ns": ended - started,
        "e2e_ns": ended - load_started,
    }


def integer_count(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return len(value)
    except TypeError:
        return 0


def output_identity(output: Mapping[str, Any]) -> tuple[list[int], str]:
    ids = output.get("output_token_ids", [])
    if not isinstance(ids, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in ids
    ):
        raise ValueError("runner output_token_ids must be non-negative integers")
    return ids, canonical_hash(ids)


def finite_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    return None
