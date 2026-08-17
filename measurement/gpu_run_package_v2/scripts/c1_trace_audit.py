#!/usr/bin/env python3
"""Audit every suite sample × repetition × pass work unit."""
from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from typing import Any

try:
    from scripts.c1_quality import compare_cross_pass_evidence
except ModuleNotFoundError:
    from c1_quality import compare_cross_pass_evidence

MANDATORY_PASSES = ("P0", "P1", "P2", "P3", "P5_BASIC")
COMPLETE = {"COMPLETE", "complete"}
UNAVAILABLE = {"UNAVAILABLE", "unavailable_due_to_environment"}


def _load(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError("non-JSON suite plan requires PyYAML") from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("suite plan must be an object")
    return value


def _samples(plan: dict[str, Any]) -> list[dict[str, str]]:
    raw = plan.get("samples", plan.get("suite", {}).get("samples", []))
    if isinstance(raw, dict):
        samples = []
        for benchmark, identifiers in sorted(raw.items()):
            if not isinstance(identifiers, list):
                raise ValueError(
                    f"samples.{benchmark} must list frozen sample IDs, not a count"
                )
            samples.extend(
                {"benchmark_id": benchmark, "sample_id": str(sample)}
                for sample in identifiers
            )
        return samples
    return [
        {"benchmark_id": str(item["benchmark_id"]), "sample_id": str(item["sample_id"])}
        for item in raw
    ]


def work_unit_id(sample: dict[str, str], repetition: int, pass_id: str) -> str:
    return f"{sample['benchmark_id']}__{sample['sample_id']}__r{repetition}__{pass_id}"


def _manifest(root: Path, unit_id: str) -> Path | None:
    candidates = (
        root / "complete" / unit_id / "PASS_MANIFEST.json",
        root / "complete" / unit_id / "pass_manifest.json",
        root / "work_units" / unit_id / "PASS_MANIFEST.json",
        root / "work_units" / unit_id / "pass_manifest.json",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def audit(plan_path: Path, session_root: Path) -> tuple[int, dict[str, Any]]:
    plan = _load(plan_path)
    suite_id = str(plan.get("suite_id", plan.get("suite", {}).get("suite_id", "")))
    repetitions = int(plan.get("repetitions", plan.get("suite", {}).get("repetitions", 1)))
    passes = tuple(plan.get("mandatory_passes", plan.get("pass_plan", MANDATORY_PASSES)))
    samples = _samples(plan)
    if not suite_id or not samples or repetitions < 1 or not passes:
        raise ValueError("suite plan requires suite_id, samples, repetitions, and passes")
    units = []
    missing = []
    alignment_by_execution: dict[tuple[str, str, int], dict[str, str]] = {}
    output_by_execution: dict[tuple[str, str, int], dict[str, str]] = {}
    formal_by_execution: dict[
        tuple[str, str, int], dict[str, dict[str, Any]]
    ] = {}
    for sample in samples:
        for repetition in range(repetitions):
            execution_key = (
                sample["benchmark_id"], sample["sample_id"], repetition
            )
            for pass_id in passes:
                unit_id = work_unit_id(sample, repetition, pass_id)
                manifest_path = _manifest(session_root, unit_id)
                status = "MISSING"
                reason = None
                if manifest_path is not None:
                    manifest = _load(manifest_path)
                    status = str(manifest.get("status", "MISSING"))
                    reason = manifest.get("unavailable_reason") or manifest.get("failure_reason")
                    if status in UNAVAILABLE and not reason:
                        status = "INVALID_UNAVAILABLE"
                    if status in COMPLETE:
                        alignment = manifest.get("execution_alignment_key")
                        if (
                            not isinstance(alignment, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", alignment)
                        ):
                            status = "INVALID_ALIGNMENT"
                        else:
                            alignment_by_execution.setdefault(
                                execution_key, {}
                            )[str(pass_id)] = alignment
                        generation_path = manifest_path.parent / "generation_results.jsonl"
                        if generation_path.is_file():
                            generation_rows = [
                                json.loads(line)
                                for line in generation_path.read_text(
                                    encoding="utf-8"
                                ).splitlines()
                                if line.strip()
                            ]
                            if (
                                len(generation_rows) == 1
                                and isinstance(
                                    generation_rows[0].get("output_hash"), str
                                )
                            ):
                                output_by_execution.setdefault(
                                    execution_key, {}
                                )[str(pass_id)] = generation_rows[0]["output_hash"]
                                quality_path = (
                                    manifest_path.parent / "quality_results.jsonl"
                                )
                                if quality_path.is_file():
                                    quality_rows = [
                                        json.loads(line)
                                        for line in quality_path.read_text(
                                            encoding="utf-8"
                                        ).splitlines()
                                        if line.strip()
                                    ]
                                    if (
                                        len(quality_rows) == 1
                                        and quality_rows[0].get("schema_version")
                                        == "c1-quality-v2"
                                    ):
                                        formal_by_execution.setdefault(
                                            execution_key, {}
                                        )[str(pass_id)] = {
                                            "generation": generation_rows[0],
                                            "quality": quality_rows[0],
                                        }
                command = shlex.join([
                    "projectctl", "trace", "run", "--suite", suite_id,
                    "--benchmark-id", sample["benchmark_id"],
                    "--sample-id", sample["sample_id"],
                    "--repetition", str(repetition), "--pass", str(pass_id),
                    "--resume",
                ])
                row = {
                    "work_unit_id": unit_id,
                    **sample,
                    "repetition_id": repetition,
                    "pass_id": pass_id,
                    "status": status,
                    "manifest": (
                        manifest_path.relative_to(session_root).as_posix()
                        if manifest_path else None
                    ),
                    "supplement_command": command,
                }
                units.append(row)
                satisfied = (
                    status in COMPLETE
                    or (pass_id == "P1" and status in UNAVAILABLE and bool(reason))
                )
                if not satisfied:
                    missing.append(row)
    cross_pass_findings = []
    for identity, alignments in sorted(alignment_by_execution.items()):
        if len(set(alignments.values())) > 1:
            cross_pass_findings.append({
                "kind": "execution_alignment_drift",
                "identity": {
                    "benchmark_id": identity[0],
                    "sample_id": identity[1],
                    "repetition_id": identity[2],
                },
                "execution_alignment_keys": alignments,
            })
    for identity, output_hashes in sorted(output_by_execution.items()):
        if identity in formal_by_execution:
            continue
        comparable = output_hashes
        if len(comparable) > 1 and len(set(comparable.values())) > 1:
            cross_pass_findings.append({
                "kind": "output_hash_drift",
                "identity": {
                    "benchmark_id": identity[0],
                    "sample_id": identity[1],
                    "repetition_id": identity[2],
                },
                "output_hashes": comparable,
            })
    for identity, evidence in sorted(formal_by_execution.items()):
        for finding in compare_cross_pass_evidence(
            evidence, expected_passes=passes
        ):
            cross_pass_findings.append({
                "kind": finding["kind"],
                "identity": {
                    "benchmark_id": identity[0],
                    "sample_id": identity[1],
                    "repetition_id": identity[2],
                },
                "detail": finding,
            })
    failed = bool(missing or cross_pass_findings)
    report = {
        "schema_version": "c1-trace-audit-v1",
        "suite_id": suite_id,
        "expected_work_unit_count": len(units),
        "satisfied_work_unit_count": len(units) - len(missing),
        "status": "failed" if failed else "pass",
        "work_units": units,
        "missing_work_units": missing,
        "cross_pass_findings": cross_pass_findings,
        "supplement_commands": [item["supplement_command"] for item in missing],
    }
    return (1 if failed else 0), report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-plan", required=True, type=Path)
    parser.add_argument("--session-root", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    code, report = audit(args.suite_plan, args.session_root.resolve())
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
