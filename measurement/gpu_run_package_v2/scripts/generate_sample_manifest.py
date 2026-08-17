#!/usr/bin/env python3
"""Build the v1 benchmark manifest without downloading or inventing dataset rows."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "test_suites"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def make_row(
    task_id: str,
    version: str,
    source: dict,
    prompt: str,
    role: str,
    split: str,
    enabled_models: list[str],
    metadata: dict | None = None,
    raw_sample_hash: str | None = None,
    reference: Any = None,
) -> dict:
    raw_hash = raw_sample_hash or digest_text(canonical_json(source))
    locator = source.get("stable_locator", source)
    sample_id = f"{task_id.lower()}-{digest_text(canonical_json(locator))[:20]}"
    result = {
        "schema_version": "benchmark-sample-v1",
        "sample_id": sample_id,
        "task_id": task_id,
        "task_version": version,
        "role": role,
        "split": split,
        "raw_sample_hash": raw_hash,
        "prompt_hash": digest_text(prompt),
        "prompt": prompt,
        "source": source,
        "enabled_models": list(enabled_models),
        "metadata": metadata or {},
    }
    if reference is not None:
        result["reference"] = reference
    return result


def fixed_rows(suite: dict, templates: dict) -> list[dict]:
    models = suite["enabled_models"]
    rows: list[dict] = []
    for index, item in enumerate(templates["fixed_T4"]):
        prompt = (
            canonical_json(item["messages"])
            if "messages" in item else item["content"]
        )
        source = {
            "kind": "fixed_contract",
            "template_revision": templates["template_revision"],
            "row_index": index,
            "literal": item,
        }
        rows.append(make_row(
            "T4", "T4-v1", source, prompt, item["role"], item["split"], models,
            {"fixed_content": True},
        ))
    return rows


def micro_fixture_rows(suite: dict, templates: dict) -> list[dict]:
    rows = []
    for index, item in enumerate(templates["fixed_T0"]):
        source = {
            "kind": "deterministic_micro_fixture",
            "template_revision": templates["template_revision"],
            "row_index": index,
            "literal": item,
        }
        rows.append(make_row(
            "T0", "T0-v1", source, item["prompt"], "micro_fixture",
            item["split"], suite["enabled_models"],
            {
                "fixture_name": item["name"],
                "input_token_ids": item["input_token_ids"],
                "expected_output_token_ids": item["expected_output_token_ids"],
                "expected_semantics": item["expected"],
                "token_contract": "artificial_fixture_ids_not_model_tokenizer_ids",
            },
            reference=item["expected_output_token_ids"],
        ))
    return rows


def context_rows(suite: dict) -> list[dict]:
    rows = []
    splits = ["calibration", "validation", "sample_holdout", "domain_holdout"]
    for index, bucket in enumerate(suite["tasks"]["T6"]["buckets"]):
        prefix = "Read the deterministic context and return its final token. Context:"
        tokens = [
            f"ctx{position:05d}"
            for position in range(bucket - len(prefix.split()))
        ]
        prompt = prefix + " " + " ".join(tokens)
        source = {
            "kind": "deterministic_context_generator",
            "generator_revision": "T6-v1",
            "row_index": index,
            "bucket": bucket,
            "seed": suite["seed"],
        }
        rows.append(make_row(
            "T6", "T6-v1", source, prompt, "context_length",
            splits[index % len(splits)], suite["enabled_models"],
            {
                "bucket": bucket,
                "bucket_unit": "whitespace_token_proxy",
                "generated_token_count": len(prompt.split()),
            },
        ))
    return rows


def schedule_rows(suite: dict, schedules: dict) -> list[dict]:
    rows = []
    splits = ["calibration", "validation", "sample_holdout", "domain_holdout"]
    for index, (name, schedule) in enumerate(sorted(schedules["schedules"].items())):
        source = {
            "kind": "serving_schedule",
            "schedule_revision": schedules["schedule_revision"],
            "row_index": index,
            "name": name,
            "schedule": schedule,
        }
        prompt = f"Deterministic serving request for schedule {name}."
        rows.append(make_row(
            "T7", "T7-v1", source, prompt, "serving_schedule",
            splits[index], suite["enabled_models"], {"schedule": name},
        ))
    return rows


def stress_rows(suite: dict) -> list[dict]:
    rows = []
    splits = ["calibration", "validation", "sample_holdout", "domain_holdout"]
    for index, pattern in enumerate(suite["tasks"]["T8"]["patterns"]):
        source = {
            "kind": "moe_stress_generator",
            "generator_revision": "T8-v1",
            "row_index": index,
            "pattern": pattern,
            "seed": suite["seed"],
        }
        prompt = (
            f"Generate a deterministic routing trace descriptor for pattern={pattern}; "
            f"seed={suite['seed']}; experts=64; tokens=256."
        )
        rows.append(make_row(
            "T8", "T8-v1", source, prompt, "router_stress",
            splits[index % len(splits)], suite["enabled_models"],
            {"stress_pattern": pattern, "experts": 64, "tokens": 256},
        ))
    return rows


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def subject_round_robin(candidates: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate["subject"], []).append(candidate)
    for values in grouped.values():
        values.sort(key=lambda item: item["selection_key"])
    ordered: list[dict] = []
    names = sorted(grouped)
    while any(grouped.values()):
        for name in names:
            if grouped[name]:
                ordered.append(grouped[name].pop(0))
    return ordered


def render_external(dataset_key: str, row: dict, templates: dict) -> tuple[str, str]:
    if dataset_key == "gsm8k":
        return templates["gsm8k"]["literal"].format(question=row["question"]), "math"
    if dataset_key in {"mmlu", "ceval"}:
        choices = row.get("choices")
        if not isinstance(choices, list):
            choices = [row[key] for key in ("A", "B", "C", "D")]
        role = "multiple_choice" if dataset_key == "mmlu" else "chinese_multiple_choice"
        return templates[dataset_key]["literal"].format(
            question=row["question"], choice_a=choices[0], choice_b=choices[1],
            choice_c=choices[2], choice_d=choices[3],
        ), role
    if dataset_key == "humaneval":
        return templates["humaneval"]["literal"].format(prompt=row["prompt"]), "code"
    raise ValueError(f"unsupported dataset: {dataset_key}")


def external_rows(registry: dict, suite: dict, templates: dict,
                  sample_axis: dict, domain_axis: dict) -> tuple[list[dict], list[dict], dict]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError(
            "pyarrow is required; run with PYTHONPATH=.benchmark-runtime"
        ) from exc
    task_by_dataset = {
        "gsm8k": "T1",
        "mmlu": "T2",
        "humaneval": "T3",
        "ceval": "T5",
    }
    rows: list[dict] = []
    gates: list[dict] = []
    materialized: dict[str, dict] = {}
    inventory_path = ROOT / "datasets/snapshots/snapshot_inventory_v1.json"
    if not inventory_path.is_file():
        return [], [{
            "gate": "SNAPSHOT_INVENTORY_REQUIRED",
            "status": "unresolved",
            "missing_path": str(inventory_path.relative_to(ROOT)),
        }], {}
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory_by_path = {item["path"]: item for item in inventory["files"]}
    quotas = sample_axis["assignment"]["quotas_per_dataset"]
    for dataset_key, spec in registry["datasets"].items():
        candidates: list[dict] = []
        dataset_failed = False
        for file_spec in spec["files"]:
            path = ROOT / spec["snapshot_root"] / file_spec["path"]
            relative = str(path.relative_to(ROOT))
            expected = inventory_by_path.get(relative)
            if not path.is_file() or expected is None:
                gates.append({
                    "gate": "SNAPSHOT_FILE_REQUIRED",
                    "dataset": dataset_key,
                    "dataset_revision": spec["dataset_revision"],
                    "missing_path": relative,
                    "status": "unresolved",
                })
                dataset_failed = True
                continue
            actual_sha = file_sha256(path)
            if actual_sha != expected["sha256"]:
                gates.append({
                    "gate": "SNAPSHOT_CHECKSUM_MISMATCH",
                    "dataset": dataset_key,
                    "path": relative,
                    "expected": expected["sha256"],
                    "actual": actual_sha,
                    "status": "unresolved",
                })
                dataset_failed = True
                continue
            raw_rows = parquet.read_table(path).to_pylist()
            subject = file_spec["config"]
            for index, raw in enumerate(raw_rows):
                raw_hash = digest_text(canonical_json(raw))
                locator = [
                    spec["dataset_id"], spec["dataset_revision"], subject,
                    file_spec["split"], index,
                ]
                candidates.append({
                    "raw": raw,
                    "raw_hash": raw_hash,
                    "row_index": index,
                    "subject": subject,
                    "domain": file_spec.get("domain", dataset_key),
                    "file_spec": file_spec,
                    "path": relative,
                    "file_sha256": actual_sha,
                    "stable_locator": locator,
                    "selection_key": digest_text(canonical_json(
                        [sample_axis["seed"], *locator]
                    )),
                })
        if dataset_failed:
            materialized[dataset_key] = {"status": "unresolved_snapshot"}
            continue
        reserved = set(
            domain_axis["assignment"].get(dataset_key, {})
            .get("domain_holdout_subjects", [])
        )
        regular = subject_round_robin([
            item for item in candidates if item["subject"] not in reserved
        ])
        domain_holdout = [
            item for item in candidates if item["subject"] in reserved
        ]
        required = sum(int(value) for value in quotas.values())
        if len(regular) < required:
            gates.append({
                "gate": "INSUFFICIENT_ROWS_FOR_CONVERGENCE",
                "dataset": dataset_key,
                "required_regular_rows": required,
                "available_regular_rows": len(regular),
                "status": "unresolved",
            })
        selected: list[tuple[str, dict]] = []
        offset = 0
        split_counts: dict[str, int] = {}
        for split in ("smoke", "calibration", "validation", "sample_holdout"):
            count = min(int(quotas[split]), max(0, len(regular) - offset))
            selected.extend((split, item) for item in regular[offset:offset + count])
            split_counts[split] = count
            offset += count
        selected.extend(("domain_holdout", item) for item in domain_holdout)
        split_counts["domain_holdout"] = len(domain_holdout)
        materialized[dataset_key] = {
            "status": "materialized",
            "available_rows": len(candidates),
            "selected_rows": len(selected),
            "unused_rows": len(regular) - offset,
            "by_split": split_counts,
            "subjects": sorted({item["subject"] for item in candidates}),
        }
        for split, item in selected:
            raw = item["raw"]
            prompt, role = render_external(dataset_key, raw, templates)
            task_id = task_by_dataset[dataset_key]
            file_spec = item["file_spec"]
            source = {
                "kind": "pinned_dataset_row",
                "dataset_id": spec["dataset_id"],
                "dataset_revision": spec["dataset_revision"],
                "config": item["subject"],
                "split": file_spec["split"],
                "row_index": item["row_index"],
                "snapshot_path": item["path"],
                "snapshot_file_sha256": item["file_sha256"],
                "stable_locator": item["stable_locator"],
            }
            reference = None
            metadata = {
                "benchmark": dataset_key,
                "subject": item["subject"],
                "domain": item["domain"],
            }
            if dataset_key == "gsm8k":
                reference = raw["answer"]
            elif dataset_key == "mmlu":
                reference = "ABCD"[int(raw["answer"])]
            elif dataset_key == "ceval":
                reference = raw["answer"]
            elif dataset_key == "humaneval":
                metadata["entry_point"] = raw["entry_point"]
            rows.append(make_row(
                task_id, f"{task_id}-v1", source, prompt, role, split,
                suite["enabled_models"], metadata,
                raw_sample_hash=item["raw_hash"], reference=reference,
            ))
    return rows, gates, materialized


def validate(rows: list[dict], suite: dict) -> None:
    required = set(suite["required_sample_fields"])
    for row in rows:
        missing = required - row.keys()
        if missing:
            raise ValueError(f"{row.get('sample_id')} missing {sorted(missing)}")
    for field in ("sample_id", "raw_sample_hash", "prompt_hash"):
        values = [row[field] for row in rows]
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate {field}; split leakage contract violated")
    patterns = {
        row["metadata"].get("stress_pattern")
        for row in rows if row["task_id"] == "T8"
    }
    if patterns != set(suite["tasks"]["T8"]["patterns"]):
        raise ValueError("not all T8 stress patterns were generated")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=CONFIG / "sample_manifest_v1.jsonl")
    parser.add_argument("--gate-report", type=Path,
                        default=CONFIG / "unresolved_gates_v1.json")
    parser.add_argument("--require-resolved", action="store_true")
    args = parser.parse_args()
    registry = load_yaml(CONFIG / "benchmark_registry.yaml")
    suite = load_yaml(CONFIG / "moe_trace_suite_v1.yaml")
    templates = load_yaml(CONFIG / "prompt_templates" / "v1.yaml")
    split_cfg = load_yaml(CONFIG / "splits" / "v1.yaml")
    split_axes = {
        name: load_yaml(ROOT / details["manifest"])
        for name, details in split_cfg["axes"].items()
    }
    schedules = load_yaml(CONFIG / "serving_schedules" / "v1.yaml")
    external, gates, materialized = external_rows(
        registry, suite, templates["templates"],
        split_axes["sample"], split_axes["domain"],
    )
    rows = micro_fixture_rows(suite, templates)
    rows += fixed_rows(suite, templates)
    rows += context_rows(suite) + schedule_rows(suite, schedules)
    rows += stress_rows(suite) + external
    for model_id, policy in suite.get("model_eligibility_overrides", {}).items():
        for row in rows:
            eligible = (
                row["task_id"] in policy["tasks"]
                and row["split"] in policy["splits"]
            )
            if eligible and model_id not in row["enabled_models"]:
                row["enabled_models"].append(model_id)
            elif not eligible and model_id in row["enabled_models"]:
                row["enabled_models"].remove(model_id)
            row["enabled_models"].sort()
        actual = sum(model_id in row["enabled_models"] for row in rows)
        if actual != int(policy["expected_pairings"]):
            raise ValueError(
                f"{model_id} expected {policy['expected_pairings']} eligible "
                f"pairings, got {actual}"
            )
    rows.sort(key=lambda row: row["sample_id"])
    validate(rows, suite)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8"
    )
    gate_report = {
        "schema_version": "benchmark-unresolved-gates-v1",
        "suite_revision": suite["suite_revision"],
        "frozen_sample_count": len(rows),
        "unresolved_gate_count": len(gates),
        "gates": gates,
        "materialized": materialized,
    }
    args.gate_report.write_text(
        json.dumps(gate_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(canonical_json(gate_report))
    return 2 if gates and args.require_resolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
