#!/usr/bin/env python3
"""Build the exact 28,672-token M0 input fixture from the sealed tokenizer."""

from __future__ import annotations

import argparse
import importlib.metadata
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    load_json,
    require_materialization_unlock,
    semantic_sha256,
    validate_contract,
    validate_materialization_plan,
    verify_model_ledger,
    write_new_json,
)


SEED_TEXT = "Phase 7 bounded BF16 capacity qualification. "


def repeat_seed(tokenizer: Any, count: int) -> list[int]:
    seed = tokenizer.encode(SEED_TEXT, add_special_tokens=False)
    if (
        not isinstance(seed, list)
        or not seed
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in seed
        )
    ):
        raise M0Error("tokenizer returned an invalid prompt seed")
    return (seed * ((count + len(seed) - 1) // len(seed)))[:count]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--model-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contract = load_json(args.contract)
    plan = load_json(args.plan)
    ledger = load_json(args.model_ledger)
    validate_contract(contract)
    require_materialization_unlock(contract)
    validate_materialization_plan(plan, contract)
    snapshot = Path(plan["paths"]["snapshot"]).resolve(strict=True)
    verify_model_ledger(snapshot, ledger, contract=contract)
    actual_version = importlib.metadata.version("transformers")
    if actual_version != plan["tokenizer_builder"]["version"]:
        raise M0Error("transformers version differs from materialization plan")
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
    )
    token_ids = repeat_seed(tokenizer, contract["probe"]["input_tokens"])
    fixture = {
        "schema_version": "moe-simulator-phase7-capacity-prompt-v1",
        "model_id": contract["model"]["model_id"],
        "repository_commit": contract["model"]["repository_commit"],
        "model_ledger_sha256": ledger["ledger_sha256"],
        "tokenizer_builder": {
            "library": "transformers",
            "version": actual_version,
            "method": plan["tokenizer_builder"]["method"],
        },
        "seed_text": SEED_TEXT,
        "add_special_tokens": False,
        "token_count": len(token_ids),
        "token_ids": token_ids,
        "token_ids_sha256": semantic_sha256(token_ids),
        "tokenizer_config_sha256": ledger["snapshot_structure"][
            "tokenizer_config_sha256"
        ],
        "tokenizer_sha256": ledger["snapshot_structure"]["tokenizer_sha256"],
    }
    write_new_json(args.output, fixture)
    print(fixture["token_ids_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
