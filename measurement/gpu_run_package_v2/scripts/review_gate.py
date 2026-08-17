#!/usr/bin/env python3
"""Enforce second-decision-layer approval before paid GPU execution."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def evaluate_review(
    root: Path,
    profile_id: str,
    matrix_path: Path | None,
    approval_path: Path | None,
    *,
    local_pipeline_smoke: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy = yaml.safe_load(
        (root / "configs/gpu_execution_review.yaml").read_text(encoding="utf-8")
    )
    profiles = yaml.safe_load(
        (root / "configs/gpu_profiles.yaml").read_text(encoding="utf-8")
    ).get("profiles", {})
    failures: list[str] = []
    current = now or datetime.now(timezone.utc)

    if profile_id not in profiles:
        failures.append(f"unknown GPU profile: {profile_id}")

    exceptions = policy.get("local_exceptions", [])
    local_exception = next(
        (
            item for item in exceptions
            if item.get("gpu_profile_id") == profile_id
            and "benchmark-smoke" in item.get("allowed_commands", [])
        ),
        None,
    )
    if local_pipeline_smoke:
        if local_exception is None:
            failures.append("requested local pipeline-smoke exception is not allowed")
        return {
            "schema_version": "gpu-execution-review-result-v1",
            "status": "pass" if not failures else "no_go",
            "paid_execution": False,
            "exception_id": (
                local_exception.get("exception_id") if local_exception else None
            ),
            "gpu_profile_id": profile_id,
            "failures": failures,
        }

    decision = policy.get("governing_decision", {})
    if (
        decision.get("decision_id") == "D-062"
        and decision.get("status") == "accepted"
        and not decision.get("superseded_by")
    ):
        failures.append("D-062 is active and has not been superseded")

    approval: dict[str, Any] = {}
    if matrix_path is None or not matrix_path.is_file():
        failures.append("paid execution requires an existing frozen matrix")
    else:
        try:
            matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
            if not isinstance(matrix, dict) or matrix.get("frozen") is not True:
                failures.append("paid execution matrix must declare frozen=true")
        except Exception as exc:
            failures.append(f"paid execution matrix is invalid: {exc}")
    if approval_path is None or not approval_path.is_file():
        failures.append("paid execution requires a second-layer approval file")
    else:
        try:
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            schema_path = root / policy["paid_execution"]["approval_schema"]
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.validate(
                approval,
                schema,
                format_checker=jsonschema.FormatChecker(),
            )
        except Exception as exc:
            failures.append(f"approval schema validation failed: {exc}")

    if approval:
        expected = {
            "package_id": policy.get("package_id"),
            "package_checksums_sha256": sha256_file(root / "checksums.txt"),
            "gpu_profile_id": profile_id,
        }
        if matrix_path is not None and matrix_path.is_file():
            expected["matrix_sha256"] = sha256_file(matrix_path)
        for field, value in expected.items():
            if approval.get(field) != value:
                failures.append(f"approval {field} does not match selected execution")
        reviewers = approval.get("reviewers", [])
        required_roles = set(
            policy["paid_execution"]["required_distinct_reviewer_roles"]
        )
        reviewer_ids = {
            str(item.get("reviewer_id", "")).strip().casefold()
            for item in reviewers if isinstance(item, dict)
            and str(item.get("reviewer_id", "")).strip()
        }
        reviewer_roles = {
            item.get("role") for item in reviewers if isinstance(item, dict)
        }
        if reviewer_roles != required_roles:
            failures.append(
                "approval requires exactly the architecture_system, "
                "model_benchmark, and trace_statistics reviewer roles"
            )
        if len(reviewer_ids) != len(required_roles):
            failures.append("approval reviewer roles require distinct reviewer identities")
        superseding_decision = decision.get("superseded_by")
        if (
            superseding_decision
            and approval.get("superseding_decision_id") != superseding_decision
        ):
            failures.append(
                "approval superseding_decision_id does not match governing decision"
            )
        if approval.get("blockers") != []:
            failures.append("approval blockers must be exactly []")
        try:
            approved = parse_utc(approval["approved_utc"])
            expires = parse_utc(approval["expires_utc"])
            deadline = parse_utc(approval["deadline_utc"])
            if approved > current:
                failures.append("approval approved_utc is in the future")
            if expires <= current:
                failures.append("approval has expired")
            if deadline <= current:
                failures.append("execution deadline has passed")
            if expires > deadline:
                failures.append("approval expiry must not exceed execution deadline")
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(f"approval timestamp invalid: {exc}")

    return {
        "schema_version": "gpu-execution-review-result-v1",
        "status": "pass" if not failures else "no_go",
        "paid_execution": True,
        "gpu_profile_id": profile_id,
        "governing_decision": decision,
        "matrix_sha256": (
            sha256_file(matrix_path)
            if matrix_path is not None and matrix_path.is_file()
            else None
        ),
        "package_checksums_sha256": sha256_file(root / "checksums.txt"),
        "approval_id": approval.get("approval_id"),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PACKAGE_ROOT)
    parser.add_argument("--gpu-profile", required=True)
    parser.add_argument("--matrix", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--local-pipeline-smoke", action="store_true")
    args = parser.parse_args()
    result = evaluate_review(
        args.root.resolve(),
        args.gpu_profile,
        args.matrix,
        args.approval,
        local_pipeline_smoke=args.local_pipeline_smoke,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 20


if __name__ == "__main__":
    raise SystemExit(main())
