#!/usr/bin/env python3
"""Retain exact approval, consumption, and package bytes inside sealed evidence."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    file_sha256,
    load_json,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _rename_noreplace,
)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_exact_new(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise M0Error("authority evidence parent must be an existing real directory")
    try:
        descriptor = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
        )
    except FileExistsError as exc:
        raise M0Error(f"refusing to overwrite retained authority bytes: {path}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def retain_authority(
    *,
    application: Path,
    approval_path: Path,
    registry_path: Path,
    evidence_root: Path,
    expected_application_ledger_sha256: str,
    approval_bytes: bytes | None = None,
    package_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    application = application.resolve(strict=True)
    approval_path = approval_path.resolve(strict=True)
    registry_path = registry_path.resolve(strict=True)
    evidence_root = evidence_root.resolve(strict=True)
    if any(path.is_symlink() for path in (approval_path, registry_path, evidence_root)):
        raise M0Error("authority evidence source/root symlink is forbidden")

    if approval_bytes is None:
        approval_bytes = approval_path.read_bytes()
    registry_bytes = registry_path.read_bytes()
    authority_dir = evidence_root / "authority"
    staged_authority = evidence_root / ".authority.staged"
    if authority_dir.exists() or authority_dir.is_symlink():
        raise M0Error("authority evidence directory already exists")
    try:
        staged_authority.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise M0Error("stale staged authority evidence exists") from exc
    retained_approval = staged_authority / "approval.json"
    write_exact_new(retained_approval, approval_bytes)
    retained_registry = staged_authority / "consumption_record.json"
    write_exact_new(retained_registry, registry_bytes)

    # Exact authority bytes must be retained before any live-package comparison.
    # A consumed approval therefore remains replayable even when the package has
    # drifted and this function deliberately raises below.
    approval = load_json(retained_approval)
    registry = load_json(retained_registry)
    approval_hash = file_sha256(retained_approval)
    if (
        registry.get("approval_id") != approval.get("approval_id")
        or registry.get("approval_token_sha256")
        != approval.get("approval_token_sha256")
        or registry.get("approval_file_sha256") != approval_hash
    ):
        raise M0Error("retained approval and one-shot consumption record differ")

    if package_ledger is None:
        package_ledger = build_application_ledger(application)
    retained_package = staged_authority / "application_package_ledger.json"
    write_new_json(retained_package, package_ledger)
    package_matches = (
        package_ledger["ledger_sha256"] == expected_application_ledger_sha256
    )
    record = {
        "schema_version": "moe-simulator-phase7-authority-evidence-v1",
        "approval_id": approval["approval_id"],
        "approval_file_sha256": approval_hash,
        "approval_semantic_sha256": semantic_sha256(approval),
        "consumption_record_sha256": file_sha256(retained_registry),
        "application_package_ledger_file_sha256": file_sha256(retained_package),
        "application_package_ledger_sha256": package_ledger["ledger_sha256"],
        "expected_application_package_ledger_sha256":
            expected_application_ledger_sha256,
        "package_verification": "PASS" if package_matches else "FAIL",
        "member_count": package_ledger["member_count"],
        "retained_paths": {
            "approval": "authority/approval.json",
            "consumption_record": "authority/consumption_record.json",
            "application_package_ledger": "authority/application_package_ledger.json",
        },
    }
    write_new_json(staged_authority / "authority_evidence.json", record)
    _fsync_directory(staged_authority)
    _rename_noreplace(staged_authority, authority_dir)
    _fsync_directory(evidence_root)
    validate_retained_authority(
        evidence_root=evidence_root,
        require_package_match=package_matches,
    )
    if not package_matches:
        raise M0Error("live recursive application package differs from approval")
    return record


def _validate_package_ledger(ledger: dict[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "root_name",
        "member_count",
        "members",
        "ledger_sha256",
        "excluded_mutable_approval_files",
    }
    members = ledger.get("members")
    if (
        set(ledger) != expected_keys
        or ledger.get("schema_version")
        != "moe-simulator-phase7-application-ledger-v2"
        or ledger.get("root_name") != "application"
        or not isinstance(members, list)
        or not members
        or ledger.get("member_count") != len(members)
        or ledger.get("excluded_mutable_approval_files")
        != [
            "approval.template.json",
            "environment_disclosure_approval.template.json",
            "materialization_approval.template.json",
        ]
    ):
        raise M0Error("retained application package ledger structure differs")
    paths: list[str] = []
    rows: list[bytes] = []
    for member in members:
        if (
            not isinstance(member, dict)
            or set(member) != {"path", "size_bytes", "sha256"}
            or not isinstance(member["path"], str)
            or not member["path"]
            or member["path"].startswith("/")
            or ".." in Path(member["path"]).parts
            or not isinstance(member["size_bytes"], int)
            or member["size_bytes"] < 0
            or not isinstance(member["sha256"], str)
            or len(member["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in member["sha256"])
        ):
            raise M0Error("invalid retained application package member")
        paths.append(member["path"])
        rows.append(f"{member['sha256']}  {member['path']}\n".encode("utf-8"))
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise M0Error("retained application package members are not sorted/unique")
    if hashlib.sha256(b"".join(rows)).hexdigest() != ledger.get("ledger_sha256"):
        raise M0Error("retained application package ledger root differs")


def validate_retained_authority(
    *, evidence_root: Path, require_package_match: bool
) -> dict[str, Any]:
    """Revalidate exact retained authority immediately before terminal sealing."""

    root = evidence_root.resolve(strict=True)
    authority_dir = root / "authority"
    if not authority_dir.is_dir() or authority_dir.is_symlink():
        raise M0Error("retained authority directory is absent or unsafe")
    expected_names = {
        "approval.json",
        "consumption_record.json",
        "application_package_ledger.json",
        "authority_evidence.json",
    }
    actual_names: set[str] = set()
    for path in authority_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise M0Error(f"unsafe retained authority member: {path.name}")
        actual_names.add(path.name)
    if actual_names != expected_names:
        raise M0Error("retained authority exact-set closure differs")

    approval_path = authority_dir / "approval.json"
    registry_path = authority_dir / "consumption_record.json"
    package_path = authority_dir / "application_package_ledger.json"
    record_path = authority_dir / "authority_evidence.json"
    approval = load_json(approval_path)
    registry = load_json(registry_path)
    package = load_json(package_path)
    record = load_json(record_path)
    expected_record_keys = {
        "schema_version",
        "approval_id",
        "approval_file_sha256",
        "approval_semantic_sha256",
        "consumption_record_sha256",
        "application_package_ledger_file_sha256",
        "application_package_ledger_sha256",
        "expected_application_package_ledger_sha256",
        "package_verification",
        "member_count",
        "retained_paths",
    }
    if (
        set(record) != expected_record_keys
        or record.get("schema_version")
        != "moe-simulator-phase7-authority-evidence-v1"
        or record.get("retained_paths")
        != {
            "approval": "authority/approval.json",
            "consumption_record": "authority/consumption_record.json",
            "application_package_ledger":
                "authority/application_package_ledger.json",
        }
    ):
        raise M0Error("retained authority evidence record differs")
    _validate_package_ledger(package)
    actual_package = package["ledger_sha256"]
    expected_package = record["expected_application_package_ledger_sha256"]
    verification = "PASS" if actual_package == expected_package else "FAIL"
    if (
        record["approval_id"] != approval.get("approval_id")
        or registry.get("approval_id") != approval.get("approval_id")
        or registry.get("approval_token_sha256")
        != approval.get("approval_token_sha256")
        or registry.get("approval_file_sha256") != file_sha256(approval_path)
        or record["approval_file_sha256"] != file_sha256(approval_path)
        or record["approval_semantic_sha256"] != semantic_sha256(approval)
        or record["consumption_record_sha256"] != file_sha256(registry_path)
        or record["application_package_ledger_file_sha256"]
        != file_sha256(package_path)
        or record["application_package_ledger_sha256"] != actual_package
        or approval.get("application_ledger_sha256") != expected_package
        or record["package_verification"] != verification
        or record["member_count"] != package["member_count"]
    ):
        raise M0Error("retained authority hashes or cross-bindings differ")
    if require_package_match and verification != "PASS":
        raise M0Error("terminal PASS requires matching application authority")
    return record
