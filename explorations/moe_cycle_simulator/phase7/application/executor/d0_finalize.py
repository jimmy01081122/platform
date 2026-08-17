#!/usr/bin/env python3
"""Stage, publish, seal, and independently verify D0 terminal evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    validate_retained_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    exact_regular_file_set,
    SHA256_RE,
    file_sha256,
    load_json,
    semantic_sha256,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _rename_noreplace,
)


D0Outcome = Literal["COMPLETE", "FAILED", "INCOMPLETE"]
STATUS_PAYLOAD = {
    "COMPLETE": "D0_COMPLETE_AUDITED\n",
    "FAILED": "D0_FAILED_IMMUTABLE_NO_RETRY\n",
    "INCOMPLETE": "D0_INCOMPLETE_IMMUTABLE_NO_RETRY\n",
}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o400,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _terminal_record(root: Path, outcome: D0Outcome) -> dict[str, Any]:
    if outcome == "COMPLETE":
        if (root / "d0_failure.json").exists():
            raise M0Error("D0 COMPLETE cannot coexist with failure evidence")
        record = load_json(root / "d0_result.json")
        expected = {
            "schema_version",
            "application_id",
            "disclosure_session_id",
            "disclosure_status",
            "environment_eligibility",
            "eligibility_findings",
            "authority_evidence_sha256",
            "plan_file_sha256",
            "approval_file_sha256",
            "probe_file_sha256",
            "exact_ssh_argv_sha256",
            "timing",
            "ssh",
            "probe_result_sha256",
            "vault_mount_identity_sha256",
            "prohibitions",
            "next_legal_action",
        }
        if (
            set(record) != expected
            or record.get("schema_version")
            != "moe-simulator-phase7-d0-result-v1"
            or record.get("disclosure_status") != "COMPLETE"
            or record.get("environment_eligibility")
            not in {"READY_FOR_MATERIALIZATION_APPLICATION", "NOT_READY"}
        ):
            raise M0Error("D0 result is not a complete disclosure")
        for field in (
            "authority_evidence_sha256",
            "plan_file_sha256",
            "approval_file_sha256",
            "probe_file_sha256",
            "exact_ssh_argv_sha256",
            "probe_result_sha256",
            "vault_mount_identity_sha256",
        ):
            if (
                not isinstance(record.get(field), str)
                or SHA256_RE.fullmatch(record[field]) is None
            ):
                raise M0Error(f"D0 result has invalid {field}")
        timing = record.get("timing")
        ssh = record.get("ssh")
        if (
            not isinstance(timing, dict)
            or set(timing)
            != {
                "controller_start_utc",
                "controller_end_utc",
                "elapsed_monotonic_ns",
                "lease_start_utc",
                "lease_deadline_utc",
                "ssh_elapsed_monotonic_ns",
            }
            or any(
                isinstance(timing[field], bool)
                or not isinstance(timing[field], int)
                or timing[field] < 0
                for field in ("elapsed_monotonic_ns", "ssh_elapsed_monotonic_ns")
            )
            or not isinstance(ssh, dict)
            or set(ssh)
            != {
                "endpoint",
                "host_public_key_blob_sha256",
                "returncode",
                "stdout_sha256",
                "stderr_sha256",
            }
            or not isinstance(ssh.get("endpoint"), dict)
            or set(ssh["endpoint"]) != {"host", "port", "username"}
            or any(
                not isinstance(ssh.get(field), str)
                or SHA256_RE.fullmatch(ssh[field]) is None
                for field in (
                    "host_public_key_blob_sha256",
                    "stdout_sha256",
                    "stderr_sha256",
                )
            )
        ):
            raise M0Error("D0 result timing or SSH closure differs")
    else:
        if (root / "d0_result.json").exists():
            raise M0Error("D0 failure cannot coexist with a complete result")
        record = load_json(root / "d0_failure.json")
        expected = {
            "schema_version",
            "disclosure_status",
            "failure_type",
            "failure",
            "controller_start_utc",
            "controller_end_utc",
            "elapsed_monotonic_ns",
            "authority_evidence_sha256",
            "retry_allowed",
            "resume_allowed",
            "gpu_workload_performed",
        }
        if (
            set(record) != expected
            or record.get("schema_version")
            != "moe-simulator-phase7-d0-failure-v1"
            or record.get("disclosure_status") != outcome
            or record.get("retry_allowed") is not False
            or record.get("resume_allowed") is not False
        ):
            raise M0Error("D0 failure evidence differs from terminal outcome")
        authority_hash = record.get("authority_evidence_sha256")
        if (
            authority_hash is not None
            and (
                not isinstance(authority_hash, str)
                or SHA256_RE.fullmatch(authority_hash) is None
            )
        ):
            raise M0Error("D0 failure authority hash is invalid")
    authority_dir = root / "authority"
    if outcome == "COMPLETE" and not authority_dir.exists():
        raise M0Error("D0 COMPLETE requires retained authority")
    if authority_dir.exists():
        authority = validate_retained_authority(
            evidence_root=root,
            require_package_match=outcome == "COMPLETE",
        )
        if record.get("authority_evidence_sha256") != semantic_sha256(authority):
            raise M0Error("D0 terminal record does not bind retained authority")
    elif record.get("authority_evidence_sha256") is not None:
        raise M0Error("D0 terminal record names absent authority")
    return record


def build_d0_ledger(root: Path, outcome: D0Outcome) -> dict[str, Any]:
    excluded = {
        "evidence_ledger.json",
        "d0_status.txt",
        ".evidence_ledger.json.staged",
        ".d0_status.txt.staged",
    }
    members: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise M0Error(f"D0 evidence symlink is forbidden: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"evidence_ledger.json", "d0_status.txt"}:
            raise M0Error("D0 terminal artifact exists before publication")
        if relative in excluded:
            raise M0Error(f"stale D0 staging artifact: {relative}")
        if not path.is_file():
            raise M0Error(f"D0 evidence is not a regular file: {relative}")
        members.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    status = STATUS_PAYLOAD[outcome].encode("utf-8")
    members.append(
        {
            "path": "d0_status.txt",
            "size_bytes": len(status),
            "sha256": hashlib.sha256(status).hexdigest(),
        }
    )
    members.sort(key=lambda item: item["path"])
    ledger: dict[str, Any] = {
        "schema_version": "moe-simulator-phase7-d0-evidence-ledger-v2",
        "terminal_status": outcome,
        "terminal_marker": STATUS_PAYLOAD[outcome].strip(),
        "member_count": len(members),
        "members": members,
    }
    ledger["ledger_sha256"] = semantic_sha256(ledger)
    return ledger


def seal_d0_terminal(root: Path, outcome: D0Outcome) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if outcome not in STATUS_PAYLOAD:
        raise M0Error("invalid D0 terminal outcome")
    _terminal_record(root, outcome)
    ledger = build_d0_ledger(root, outcome)
    staged_ledger = root / ".evidence_ledger.json.staged"
    staged_status = root / ".d0_status.txt.staged"
    final_ledger = root / "evidence_ledger.json"
    final_status = root / "d0_status.txt"
    payload = (
        json.dumps(ledger, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    try:
        _write_new(staged_ledger, payload)
        _write_new(staged_status, STATUS_PAYLOAD[outcome].encode("utf-8"))
        _fsync_directory(root)
        for path in sorted(root.rglob("*"), reverse=True):
            if path in {staged_ledger, staged_status}:
                continue
            if path.is_symlink():
                raise M0Error(f"D0 evidence symlink is forbidden: {path}")
            path.chmod(0o444 if path.is_file() else 0o555)
        _rename_noreplace(staged_ledger, final_ledger)
        _fsync_directory(root)
        _rename_noreplace(staged_status, final_status)
        _fsync_directory(root)
        root.chmod(0o555)
    except Exception:
        root.chmod(0o700)
        for path in (staged_ledger, staged_status, final_ledger, final_status):
            _remove(path)
        _fsync_directory(root)
        raise
    verify_d0_terminal(root)
    return ledger


def verify_d0_terminal(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    ledger = load_json(root / "evidence_ledger.json")
    expected_keys = {
        "schema_version",
        "terminal_status",
        "terminal_marker",
        "member_count",
        "members",
        "ledger_sha256",
    }
    if set(ledger) != expected_keys:
        raise M0Error("D0 ledger key closure mismatch")
    digest_input = dict(ledger)
    observed_digest = digest_input.pop("ledger_sha256", None)
    if (
        ledger["schema_version"] != "moe-simulator-phase7-d0-evidence-ledger-v2"
        or observed_digest != semantic_sha256(digest_input)
        or ledger["terminal_status"] not in STATUS_PAYLOAD
        or ledger["terminal_marker"]
        != STATUS_PAYLOAD[ledger["terminal_status"]].strip()
    ):
        raise M0Error("D0 ledger identity or root differs")
    members = ledger["members"]
    paths = [item["path"] for item in members]
    if (
        ledger["member_count"] != len(members)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or "evidence_ledger.json" in paths
    ):
        raise M0Error("D0 ledger member closure differs")
    actual = exact_regular_file_set(
        root, excluded_root_files={"evidence_ledger.json"}
    )
    if set(paths) != actual:
        raise M0Error("D0 ledger is not an exact file set")
    for item in members:
        path = root / item["path"]
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item["size_bytes"]
            or file_sha256(path) != item["sha256"]
            or path.stat().st_mode & 0o222
        ):
            raise M0Error(f"D0 ledger member differs or is writable: {item['path']}")
    if root.stat().st_mode & 0o222:
        raise M0Error("D0 terminal root remains writable")
    marker = (root / "d0_status.txt").read_text(encoding="utf-8")
    if marker != STATUS_PAYLOAD[ledger["terminal_status"]]:
        raise M0Error("D0 terminal marker differs")
    _terminal_record(root, ledger["terminal_status"])
    return ledger


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-root", type=Path, required=True)
    args = parser.parse_args()
    print(verify_d0_terminal(args.session_root)["ledger_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
