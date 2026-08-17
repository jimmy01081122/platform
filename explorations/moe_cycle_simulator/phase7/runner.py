#!/usr/bin/env python3
"""CPU-mock-only dry-run CLI for Phase 7 promotion preflight."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from explorations.moe_cycle_simulator.phase7.promotion import (  # noqa: E402
    build_artifact_ledger,
    ContractError,
    load_strict_json,
    new_state,
    preflight,
    validate_plan,
    verify_artifact_ledger,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a Phase 7 CPU mock plan. This CLI never executes a workload."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-plan")
    validate.add_argument("--plan", required=True, type=Path)

    initialize = subparsers.add_parser("init-dry-run")
    initialize.add_argument("--plan", required=True, type=Path)
    initialize.add_argument("--session-registry", required=True, type=Path)

    check = subparsers.add_parser("preflight")
    check.add_argument("--plan", required=True, type=Path)
    check.add_argument("--state", required=True, type=Path)
    check.add_argument("--approval", required=True, type=Path)
    check.add_argument("--stage", required=True, choices=("M0", "M1", "M2", "M3", "M4"))
    check.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="required safety latch; no command is executed",
    )

    ledger = subparsers.add_parser("build-ledger")
    ledger.add_argument("--root", required=True, type=Path)
    ledger.add_argument(
        "--stage", required=True, choices=("M0", "M1", "M2", "M3", "M4", "V0")
    )
    ledger.add_argument("--session-id", required=True)
    ledger.add_argument("--artifact", required=True, action="append")

    verify = subparsers.add_parser("verify-ledger")
    verify.add_argument("--root", required=True, type=Path)
    verify.add_argument("--ledger", required=True, type=Path)
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build-ledger":
            _emit(
                build_artifact_ledger(
                    args.root,
                    args.artifact,
                    stage=args.stage,
                    session_id=args.session_id,
                )
            )
            return 0
        if args.command == "verify-ledger":
            ledger = load_strict_json(args.ledger)
            verify_artifact_ledger(args.root, ledger)
            _emit(
                {
                    "status": "PASS",
                    "action": "LEDGER_VERIFICATION_ONLY",
                    "execution_performed": False,
                    "gpu_authority": "NONE",
                    "ledger_sha256": ledger["ledger_sha256"],
                }
            )
            return 0
        plan = load_strict_json(args.plan)
        if args.command == "validate-plan":
            result = validate_plan(plan)
            _emit(
                {
                    "status": "PASS",
                    "action": "VALIDATION_ONLY",
                    "execution_performed": False,
                    "gpu_authority": "NONE",
                    **result,
                }
            )
            return 0
        if args.command == "init-dry-run":
            registry = load_strict_json(args.session_registry)
            if (
                not isinstance(registry, dict)
                or set(registry) != {"schema_version", "session_ids"}
                or registry["schema_version"]
                != "moe-simulator-phase7-session-registry-v1"
                or not isinstance(registry["session_ids"], list)
                or any(not isinstance(item, str) for item in registry["session_ids"])
                or len(registry["session_ids"]) != len(set(registry["session_ids"]))
            ):
                raise ContractError("invalid session registry")
            state = new_state(plan, registry["session_ids"])
            _emit(
                {
                    "status": "PASS",
                    "action": "INIT_DRY_RUN_ONLY",
                    "state_was_written": False,
                    "registry_was_written": False,
                    "gpu_authority": "NONE",
                    "prospective_state": state,
                }
            )
            return 0

        state = load_strict_json(args.state)
        approval = load_strict_json(args.approval)
        result = preflight(plan, approval, state, stage=args.stage)
        _emit({"status": "PASS", **result.as_dict()})
        return 0
    except ContractError as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "failure_class": "CONTRACT_REJECTED",
                    "reason": str(exc),
                    "execution_performed": False,
                    "gpu_authority": "NONE",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
