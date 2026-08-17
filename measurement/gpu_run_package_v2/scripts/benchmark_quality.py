#!/usr/bin/env python3
"""Offline quality evaluators; generated code is never executed by this process."""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def extract_gsm8k_answer(text: str) -> str | None:
    marked = re.findall(r"####\s*([-+]?(?:\d[\d,]*)(?:\.\d+)?)", text)
    candidates = marked or re.findall(
        r"[-+]?(?:\d[\d,]*)(?:\.\d+)?", text.replace("$", "")
    )
    if not candidates:
        return None
    value = candidates[-1].replace(",", "")
    try:
        return format(Decimal(value).normalize(), "f")
    except InvalidOperation:
        return None


def evaluate_gsm8k(prediction: str, reference: Any) -> dict:
    predicted = extract_gsm8k_answer(prediction)
    expected = extract_gsm8k_answer(str(reference))
    return {
        "validity": predicted is not None,
        "correctness": predicted is not None and predicted == expected,
        "predicted": predicted,
        "expected": expected,
    }


def extract_choice(text: str) -> str | None:
    match = re.search(
        r"(?:^|\b)(?:answer\s*[:：]?\s*)?\(?([ABCD])\)?(?:\b|$)",
        text.strip(), re.IGNORECASE,
    )
    return match.group(1).upper() if match else None


def evaluate_choice(prediction: str, reference: Any) -> dict:
    predicted = extract_choice(prediction)
    if isinstance(reference, int):
        expected = "ABCD"[reference] if 0 <= reference < 4 else None
    else:
        expected = extract_choice(str(reference))
    return {
        "validity": predicted is not None,
        "correctness": predicted is not None and predicted == expected,
        "predicted": predicted,
        "expected": expected,
    }


def evaluate_code_static(prediction: str, entry_point: str | None = None) -> dict:
    """Check syntax/shape only. This function deliberately performs no execution."""
    try:
        tree = ast.parse(prediction)
    except SyntaxError as exc:
        return {"validity": False, "correctness": None, "syntax_error": str(exc)}
    functions = [
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return {
        "validity": bool(functions) and (entry_point is None or entry_point in functions),
        "correctness": None,
        "mode": "static_structure_only",
        "functions": sorted(functions),
        "entry_point_present": entry_point in functions if entry_point else None,
    }


def run_isolated_adapter(adapter: Path, payload: dict) -> dict:
    """Delegate execution to an explicit isolation boundary, never eval/exec here."""
    if not adapter.is_file() or not adapter.stat().st_mode & 0o111:
        raise ValueError("isolated adapter must be an executable file")
    forbidden = {"python", "python3", "bash", "sh", "node"}
    if adapter.name.lower() in forbidden:
        raise ValueError("interpreter is not an isolation adapter")
    probe = subprocess.run(
        [str(adapter), "--capabilities"], text=True, capture_output=True,
        timeout=10, check=True,
    )
    capabilities = json.loads(probe.stdout)
    if capabilities.get("execution_boundary") not in {
        "container", "microvm", "sandboxed_worker"
    }:
        raise ValueError("adapter did not attest an accepted isolation boundary")
    result = subprocess.run(
        [str(adapter), "--evaluate-json"], input=json.dumps(payload), text=True,
        capture_output=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)


def evaluate_instruction(prediction: str, constraints: dict | None = None) -> dict:
    stripped = prediction.strip()
    failures = []
    constraints = constraints or {}
    if not stripped:
        failures.append("empty")
    sentence_count = len(re.findall(r"[.!?。！？](?:\s|$)", stripped))
    if "exact_sentences" in constraints and sentence_count != constraints["exact_sentences"]:
        failures.append("sentence_count")
    if constraints.get("numbered_list"):
        numbered = re.findall(r"(?m)^\s*\d+[.)]\s+\S", stripped)
        expected = constraints.get("list_items")
        if not numbered or (expected is not None and len(numbered) != expected):
            failures.append("numbered_list")
    return {"validity": not failures, "correctness": None, "failures": failures}


def evaluate_chinese(prediction: str) -> dict:
    stripped = prediction.strip()
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", stripped))
    visible = len(re.findall(r"\S", stripped))
    ratio = cjk / visible if visible else 0.0
    return {
        "validity": visible >= 2 and cjk >= 1 and ratio >= 0.2,
        "correctness": None,
        "cjk_characters": cjk,
        "cjk_visible_ratio": ratio,
    }


def evaluate_record(record: dict, adapter: Path | None = None) -> dict:
    task = record["task_id"]
    prediction = str(record.get("prediction", ""))
    reference = record.get("reference")
    if task == "T0":
        predicted_tokens = record.get("prediction_token_ids")
        result = {
            "validity": (
                isinstance(predicted_tokens, list)
                and all(isinstance(value, int) for value in predicted_tokens)
            ),
            "correctness": predicted_tokens == reference,
            "predicted_token_ids": predicted_tokens,
            "expected_token_ids": reference,
            "mode": "deterministic_micro_fixture_exact_tokens",
        }
    elif task == "T1":
        result = evaluate_gsm8k(prediction, reference)
    elif task in {"T2", "T5"} and reference is not None:
        result = evaluate_choice(prediction, reference)
        if task == "T5":
            # C-Eval requests a one-letter answer, so language validity belongs
            # to the benchmark prompt rather than to the answer token.
            result["language_validity"] = evaluate_chinese(
                str(record.get("prompt", ""))
            )
    elif task == "T3":
        result = (
            run_isolated_adapter(adapter, record) if adapter
            else evaluate_code_static(prediction, record.get("entry_point"))
        )
    elif task == "T4":
        result = evaluate_instruction(prediction, record.get("constraints"))
        if record.get("language") == "zh":
            result["language_validity"] = evaluate_chinese(prediction)
    elif task in {"T6", "T7", "T8"}:
        result = evaluate_instruction(prediction, record.get("constraints"))
    else:
        raise ValueError(f"unsupported task_id: {task}")
    return {"sample_id": record.get("sample_id"), "task_id": task, **result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True,
                        help="JSONL records containing sample_id, task_id, prediction")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--isolated-adapter", type=Path)
    args = parser.parse_args()
    records = [
        json.loads(line)
        for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    results = [evaluate_record(record, args.isolated_adapter) for record in records]
    payload = {
        "schema_version": "benchmark-quality-v1",
        "record_count": len(results),
        "valid_count": sum(bool(result.get("validity")) for result in results),
        "correct_count": sum(
            result.get("correctness") is True for result in results
        ),
        "results": results,
        "code_execution_policy": "static_only_unless_attested_isolated_adapter",
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
