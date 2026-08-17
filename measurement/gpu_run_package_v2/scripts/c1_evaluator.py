#!/usr/bin/env python3
"""Pure, replayable evaluators for the frozen C1 T0/T1/T2 contracts."""
from __future__ import annotations

import re
from typing import Any, Mapping

from scripts.benchmark_quality import evaluate_choice, evaluate_gsm8k


T0_TOKEN_CONTRACT = "artificial_fixture_ids_not_model_tokenizer_ids"
T0_SEMANTICS = frozenset({"identity", "ordered_copy", "reverse", "integer_sum"})
TASK_EVALUATORS = {
    "T0": "t0_integer_semantics_v1",
    "T1": "gsm8k_last_number",
    "T2": "mmlu_choice",
}


def parse_integer_sequence(text: str) -> list[int] | None:
    """Parse an integer-only answer with minimal balanced punctuation."""
    value = text.strip()
    if not value:
        return None
    if value[-1:] in {".", "!", "?"}:
        value = value[:-1].rstrip()
    pairs = {"[": "]", "(": ")", "{": "}", '"': '"', "'": "'"}
    if value[:1] in pairs:
        if value[-1:] != pairs[value[0]]:
            return None
        value = value[1:-1].strip()
    integer = r"[+-]?\d+"
    sequence = rf"{integer}(?:(?:\s*[,;]\s*|\s+){integer})*"
    if re.fullmatch(sequence, value) is None:
        return None
    return [int(item) for item in re.findall(integer, value)]


def t0_expected_from_semantics(
    metadata: Mapping[str, Any],
) -> tuple[list[int] | None, str | None]:
    if metadata.get("token_contract") != T0_TOKEN_CONTRACT:
        return None, "unsupported or missing T0 token_contract"
    semantic = metadata.get("expected_semantics")
    if semantic not in T0_SEMANTICS:
        return None, "unsupported or missing T0 expected_semantics"
    inputs = metadata.get("input_token_ids")
    if (
        not isinstance(inputs, list)
        or not inputs
        or any(not isinstance(value, int) or isinstance(value, bool) for value in inputs)
    ):
        return None, "T0 input_token_ids must be a non-empty integer list"
    if semantic in {"identity", "ordered_copy"}:
        expected = list(inputs)
    elif semantic == "reverse":
        expected = list(reversed(inputs))
    else:
        expected = [sum(inputs)]
    return expected, None


def evaluate_frozen_sample(text: Any, sample: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one frozen C1 sample without model, GPU, or mutable adapter state."""
    rendered = text if isinstance(text, str) else ""
    task = sample.get("task_id")
    reference = sample.get("reference")
    if task == "T1":
        result = evaluate_gsm8k(rendered, reference)
        return {"evaluator": TASK_EVALUATORS[task], **result, "details": result}
    if task == "T2":
        result = evaluate_choice(rendered, reference)
        return {"evaluator": TASK_EVALUATORS[task], **result, "details": result}
    if task == "T0":
        metadata = sample.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        semantic_expected, contract_error = t0_expected_from_semantics(metadata)
        reference_valid = (
            isinstance(reference, list)
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in reference
            )
        )
        parsed = parse_integer_sequence(rendered)
        validity = contract_error is None and reference_valid and parsed is not None
        correctness = bool(
            validity
            and semantic_expected == list(reference)
            and parsed == list(reference)
        )
        return {
            "evaluator": TASK_EVALUATORS[task],
            "validity": validity,
            "correctness": correctness,
            "details": {
                "token_contract": metadata.get("token_contract"),
                "expected_semantics": metadata.get("expected_semantics"),
                "parsed_integers": parsed,
                "contract_error": contract_error,
                "reference_matches_semantics": bool(
                    reference_valid and semantic_expected == list(reference)
                ),
            },
        }
    return {
        "evaluator": "unknown",
        "validity": False,
        "correctness": None,
        "details": {"contract_error": "unknown task"},
    }
