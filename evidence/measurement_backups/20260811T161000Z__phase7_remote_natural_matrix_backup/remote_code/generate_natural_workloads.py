#!/usr/bin/env python3
"""Generate frozen, project-authored Phase 7 natural workload fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_text(path: Path, text: str) -> dict[str, Any]:
    raw = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": str(path), "bytes": len(raw), "sha256": sha256_bytes(raw)}


def token_count(tokenizer: Any, text: str) -> int:
    return len(tokenizer.encode(text, add_special_tokens=True))


def build_to_target(
    tokenizer: Any,
    header: str,
    line_factory: Callable[[int], str],
    footer: str,
    target_tokens: int,
) -> tuple[str, int, int]:
    lines: list[str] = []
    index = 1
    while True:
        candidate = header + "".join(lines) + footer
        count = token_count(tokenizer, candidate)
        if count >= target_tokens:
            return candidate, count, len(lines)
        lines.append(line_factory(index))
        index += 1


def record_line(index: int, namespace: str) -> str:
    stations = ("Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir", "Grove", "Hazel")
    colors = ("amber", "blue", "copper", "green", "indigo", "silver", "violet", "white")
    station = stations[index % len(stations)]
    color = colors[(index * 3) % len(colors)]
    quantity = 73 + ((index * 97) % 901)
    delay = (index * 13) % 47
    code = f"{namespace}-{index:05d}-{(index * 7919) % 100000:05d}"
    return (
        f"Record {index:05d}: station {station}; shipment code {code}; marker {color}; "
        f"quantity {quantity}; observed delay {delay} minutes; validation state accepted.\n"
    )


def w0_prompt() -> str:
    return (
        "A warehouse starts with 1,240 components. During the morning it ships 18 crates with "
        "27 components in each crate. At noon, 135 returned components pass inspection and are "
        "placed back in stock. During the afternoon, three production lines each consume 146 "
        "components. Finally, 8 damaged components are removed. Compute the final inventory. "
        "Show a concise sequence of arithmetic steps and end with a line formatted exactly as "
        "FINAL INVENTORY: <integer>.\n"
    )


def w1_prompt(tokenizer: Any) -> tuple[str, int, int]:
    header = (
        "The following synthetic operations ledger is a project-authored retrieval and summarization "
        "fixture. Every record is authoritative for this prompt. Read the complete ledger before "
        "answering; do not use outside facts.\n\n"
    )
    footer = (
        "\nTask: Report the shipment code and delay for records 00037, 00113, and 00251. Then summarize "
        "in two sentences how quantities and delays vary across the ledger. End with a compact table "
        "whose columns are record, shipment_code, and delay_minutes.\n"
    )
    return build_to_target(
        tokenizer,
        header,
        lambda index: record_line(index, "LONGCTX"),
        footer,
        target_tokens=16_200,
    )


def w2_constraint(index: int) -> str:
    resource = ("DMA", "compute", "router", "memory", "network")[index % 5]
    return (
        f"Constraint {index:03d}: operation {index} uses resource '{resource}', depends on every "
        f"operation j where j < {index} and ({index} - j) is divisible by {(index % 7) + 2}, and "
        "must retain stable input ordering when multiple operations become ready together.\n"
    )


def w2_prompt(tokenizer: Any) -> tuple[str, int, int]:
    header = (
        "Write a complete Python 3 implementation of `schedule_operations(operations)`. Each input "
        "operation is a dictionary with string `id`, integer `duration`, string `resource`, and a list "
        "of dependency IDs. Return a deterministic list of operation IDs in legal execution order. "
        "Reject duplicate IDs, missing dependencies, negative durations, and cycles with descriptive "
        "ValueError messages. Among simultaneously ready operations choose the smallest tuple "
        "(resource, duration, original_input_index). Do not use third-party packages. Include type "
        "hints, a docstring, and six executable assert-based tests. The detailed stress constraints "
        "below are specification text, not additional function arguments.\n\n"
    )
    footer = (
        "\nReturn only one self-contained Python code block. The implementation must be iterative and "
        "must not mutate the caller's dictionaries or dependency lists.\n"
    )
    return build_to_target(tokenizer, header, w2_constraint, footer, target_tokens=2_000)


def w3_prompt(tokenizer: Any, position: int, target_tokens: int) -> tuple[str, int, int]:
    header = (
        f"Serial workload request {position:02d}. This is a project-authored mixed-length context. "
        "Use only the records below. Preserve record identifiers when answering.\n\n"
    )
    footer = (
        f"\nTask for request {position:02d}: identify the record with the largest quantity among the "
        "final five records, state its shipment code, and give one sentence comparing its delay with "
        "the first record.\n"
    )
    return build_to_target(
        tokenizer,
        header,
        lambda index: record_line(index, f"SERIAL{position:02d}"),
        footer,
        target_tokens=target_tokens,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        str(args.model_path),
        local_files_only=True,
        trust_remote_code=False,
    )
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    fixtures: list[dict[str, Any]] = []

    w0 = w0_prompt()
    w0_record = write_text(output / "W0_arithmetic_reasoning.txt", w0)
    fixtures.append(
        {
            "workload_id": "W0",
            "kind": "fixed arithmetic/reasoning",
            "input_token_count": token_count(tokenizer, w0),
            "forced_output_tokens": 256,
            **w0_record,
        }
    )

    w1, w1_tokens, w1_records = w1_prompt(tokenizer)
    w1_record = write_text(output / "W1_long_context_retrieval.txt", w1)
    fixtures.append(
        {
            "workload_id": "W1",
            "kind": "fixed project-authored long-context retrieval/summarization",
            "input_token_count": w1_tokens,
            "generated_record_count": w1_records,
            "forced_output_tokens": 256,
            **w1_record,
        }
    )

    w2, w2_tokens, w2_constraints = w2_prompt(tokenizer)
    w2_record = write_text(output / "W2_code_generation.txt", w2)
    fixtures.append(
        {
            "workload_id": "W2",
            "kind": "fixed project-authored code generation",
            "input_token_count": w2_tokens,
            "generated_constraint_count": w2_constraints,
            "forced_output_tokens": 1024,
            **w2_record,
        }
    )

    target_lengths = [128] * 8 + [512] * 8 + [2048] * 8 + [8192] * 4 + [16384] * 4
    output_lengths = [32] * 8 + [64] * 8 + [256] * 8 + [32, 64, 256, 512] + [32, 256, 512, 1024]
    w3_requests = []
    for position, (target, output_tokens) in enumerate(zip(target_lengths, output_lengths, strict=True), start=1):
        prompt, actual_tokens, generated_records = w3_prompt(tokenizer, position, target)
        relative = Path("W3_prompts") / f"W3_{position:02d}.txt"
        file_record = write_text(output / relative, prompt)
        request_id = f"W3-{position:02d}-in{target}-out{output_tokens}"
        w3_requests.append(
            {
                "request_id": request_id,
                "prompt_file": str(relative),
                "prompt_sha256": file_record["sha256"],
                "target_input_tokens": target,
                "preflight_input_token_count": actual_tokens,
                "generated_record_count": generated_records,
                "output_tokens": output_tokens,
            }
        )
    plan = {
        "schema_version": "phase7-request-plan-v1",
        "plan_id": "W3-fixed-32-request-mixed-length-serial-v1",
        "repeat_count": 3,
        "requests": w3_requests,
    }
    plan_raw = json.dumps(plan, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    (output / "W3_request_plan.json").write_bytes(plan_raw)

    generator_path = Path(__file__).resolve()
    manifest = {
        "schema_version": "phase7-natural-workloads-v1",
        "provenance_class": "project-authored synthetic natural prompts; not a public benchmark",
        "model_path_used_for_preflight_tokenization": str(args.model_path),
        "tokenizer_type": type(tokenizer).__name__,
        "generator_path": str(generator_path),
        "generator_sha256": sha256_bytes(generator_path.read_bytes()),
        "fixtures": fixtures,
        "w3": {
            "plan_path": str(output / "W3_request_plan.json"),
            "plan_sha256": sha256_bytes(plan_raw),
            "request_count": len(w3_requests),
            "repeat_count": 3,
            "requests": w3_requests,
        },
        "freeze_rule": "Prompt identity, order, lengths, and output ceilings are frozen before any model output is observed.",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
