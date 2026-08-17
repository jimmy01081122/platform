#!/usr/bin/env python3
"""Validate the additive D0-R2 CPU-only package without network or SSH."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from explorations.moe_cycle_simulator.phase7_d0_r2.controller import (  # noqa: E402
    D0R2Error,
    PACKAGE_ROOT,
    load_package,
    validate_schema,
    _load_schema,
)


def validate_package() -> dict[str, object]:
    package = load_package(PACKAGE_ROOT)
    for schema_name in (
        "schemas/approval.schema.json",
        "schemas/probe.schema.json",
        "schemas/result.schema.json",
        "schemas/failure.schema.json",
        "schemas/terminal_ledger.schema.json",
    ):
        schema = _load_schema(schema_name)
        if not isinstance(schema, dict) or not schema.get("$id"):
            raise D0R2Error(f"schema is not a complete JSON Schema document: {schema_name}")
    return {
        "status": "PASS",
        "package_status": package.overlay["status"],
        "application_id": package.overlay["application_id"],
        "application_identity_sha256": package.application_identity_sha256,
        "application_ledger_sha256": package.application_ledger_sha256,
        "d0_execution": "NOT_AUTHORIZED",
        "gate_m": "NOT_AUTHORIZED",
        "m0": "NOT_AUTHORIZED",
        "gpu_authority": "NONE",
        "promotion": "NOT_PROMOTABLE_DISCOVERY_ONLY",
        "blockers": [
            "OWNER_ENVIRONMENT_CONFIRMATION_PENDING",
            "FRESH_TIME_ORIGIN_AND_DEADLINE_REQUIRED",
            "CONTAINER_DIGEST_UNOBSERVED_DISCOVERY_ONLY",
            "FRESH_HOST_KEY_SOURCE_AND_CLIENT_KEY_SELECTOR_REQUIRED",
            "D0_R2_SAME_HASH_REVIEW_PENDING",
        ],
        "network_free": True,
        "ssh_attempted": False,
        "gpu_queried": False,
        "model_accessed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, default=PACKAGE_ROOT)
    args = parser.parse_args(argv)
    try:
        if args.package_dir.resolve() != PACKAGE_ROOT.resolve():
            raise D0R2Error("this validator only accepts the immutable D0-R2 package root")
        result = validate_package()
    except (D0R2Error, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
