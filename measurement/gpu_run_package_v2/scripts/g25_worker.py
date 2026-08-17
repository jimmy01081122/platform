#!/usr/bin/env python3
"""Execute exactly one frozen G2.5 cell and write evidence to a parent-owned path."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from scheduler.store import atomic_json  # noqa: E402
from scripts.c1_worker import construct_adapter, preflight  # noqa: E402
from scripts.g25_qualification import (  # noqa: E402
    canonical_hash,
    cell_identity,
    execute_generation_core,
    load_profile_map,
    resolve_task_profile,
    validate_schema,
)


def load_descriptor(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("worker descriptor must be an object")
    validate_schema("g25_worker_descriptor.schema.json", value)
    selection = value["selection"]
    sample = value["sample"]
    config = value["generation_config"]
    ceiling = value["ceiling"]
    if (
        selection.get("instance_id") != value["instance_id"]
        or selection.get("sample_id") != value["sample_id"]
        or sample.get("sample_id") != value["sample_id"]
        or sample.get("task_id") != "T1"
        or selection.get("prompt_hash") != value["prompt_hash"]
        or hashlib.sha256(str(sample.get("prompt", "")).encode("utf-8")).hexdigest()
        != value["prompt_hash"]
        or config != resolve_task_profile(load_profile_map(), "T1", ceiling=ceiling)
        or canonical_hash(config) != value["generation_config_sha256"]
        or value["cell_id"] != cell_identity(
            value["session_id"], value["instance_id"], value["sample_id"],
            ceiling, value["generation_profile_sha256"],
            value["generation_config_sha256"],
        )
    ):
        raise ValueError("worker descriptor identity differs from frozen cell")
    return value


def execute_descriptor(
    descriptor: Mapping[str, Any],
    model_snapshot: Path,
    *,
    runtime_closure_verifier: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    adapter = construct_adapter(str(model_snapshot))
    try:
        preflight(adapter, str(model_snapshot))
        execution = {
            "suite_version": "granite-c1-v1.1.0",
            "model_revision": "0da7a48b0276d500ce5922fd2b33944091fc6c09",
            "tokenizer_revision": "0da7a48b0276d500ce5922fd2b33944091fc6c09",
            "benchmark_id": descriptor["selection"].get("benchmark_id"),
            "sample_id": descriptor["sample_id"],
            "prompt_hash": descriptor["prompt_hash"],
            "generation_config_hash": descriptor["generation_config_sha256"],
            "seed": descriptor["generation_config"]["seed"],
            "repetition_id": 0,
            "hardware_session_id": descriptor["session_id"],
            "device_identity": dict(descriptor["device_identity"]),
        }
        return execute_generation_core(
            adapter,
            execution=execution,
            prompt=descriptor["sample"]["prompt"],
            sample=descriptor["sample"],
            generation_config=descriptor["generation_config"],
            request_id=descriptor["cell_id"],
            runtime_closure_verifier=runtime_closure_verifier,
        )
    finally:
        cleanup = getattr(adapter, "cleanup", None)
        if callable(cleanup):
            cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell-descriptor", type=Path, required=True)
    parser.add_argument("--evidence-out", type=Path, required=True)
    parser.add_argument("--model-snapshot", type=Path, required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runtime_closure_verifier: Callable[[str], dict[str, Any]] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    descriptor_path = args.cell_descriptor.resolve(strict=True)
    model_snapshot = args.model_snapshot.resolve(strict=True)
    evidence_out = args.evidence_out.resolve()
    if evidence_out.exists() or evidence_out.parent.is_symlink():
        raise FileExistsError("worker evidence output must be a fresh regular path")
    descriptor = load_descriptor(descriptor_path)
    evidence = execute_descriptor(
        descriptor,
        model_snapshot,
        runtime_closure_verifier=runtime_closure_verifier,
    )
    atomic_json(evidence_out, evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
