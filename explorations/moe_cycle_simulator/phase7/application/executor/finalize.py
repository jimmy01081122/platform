#!/usr/bin/env python3
"""Seal every terminal M0 session after mutable evidence is closed."""

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

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    exact_regular_file_set,
    file_sha256,
    load_json,
    require_unlock,
    semantic_sha256,
    validate_contract,
    validate_session_id,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    validate_retained_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _rename_noreplace,
)


TerminalOutcome = Literal["PASS", "FAIL", "INCOMPLETE"]

STATUS_BY_OUTCOME = {
    "PASS": "M0_PASS_AUDITED\n",
    "FAIL": "M0_FAIL_IMMUTABLE_NO_RESUME\n",
    "INCOMPLETE": "M0_INCOMPLETE_IMMUTABLE_NO_RESUME\n",
}


def build_session_ledger(
    root: Path, session_id: str, outcome: TerminalOutcome
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    excluded = {
        "evidence_ledger.json",
        ".evidence_ledger.json.staged",
        ".session_status.txt.staged",
    }
    members = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise M0Error(f"session symlink is forbidden: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "session_status.txt":
            raise M0Error("terminal status must not exist before staged sealing")
        if relative in excluded:
            raise M0Error(f"stale terminal staging artifact is forbidden: {relative}")
        if not path.is_file():
            raise M0Error(f"non-regular session artifact: {relative}")
        members.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    status_payload = STATUS_BY_OUTCOME[outcome].encode("utf-8")
    members.append(
        {
            "path": "session_status.txt",
            "size_bytes": len(status_payload),
            "sha256": hashlib.sha256(status_payload).hexdigest(),
        }
    )
    members.sort(key=lambda item: item["path"])
    if not members:
        raise M0Error("session contains no evidence")
    ledger: dict[str, Any] = {
        "schema_version": "moe-simulator-phase7-m0-session-ledger-v2",
        "session_id": session_id,
        "terminal_outcome": outcome,
        "terminal_status": STATUS_BY_OUTCOME[outcome].strip(),
        "member_count": len(members),
        "members": members,
    }
    ledger["ledger_sha256"] = semantic_sha256(ledger)
    return ledger


def _validate_terminal_evidence(
    root: Path, session_id: str, outcome: TerminalOutcome
) -> dict[str, Any]:
    if outcome == "PASS":
        result = load_json(root / "m0_result.json")
        if (
            result.get("session_id") != session_id
            or result.get("verdict") != "PASS"
            or result.get("findings") != []
        ):
            raise M0Error("only a complete audited M0 PASS can be sealed as PASS")
        terminal = load_json(root / "driver_commands.json")
    else:
        terminal = load_json(root / "driver_failure.json")
        if (
            terminal.get("session_id") != session_id
            or terminal.get("terminal_outcome") != outcome
            or terminal.get("resume_allowed") is not False
            or terminal.get("retry_allowed") is not False
        ):
            raise M0Error(f"driver failure evidence cannot be sealed as {outcome}")

    authority_dir = root / "authority"
    if outcome == "PASS" or authority_dir.exists():
        record = validate_retained_authority(
            evidence_root=root,
            require_package_match=outcome == "PASS",
        )
        if terminal.get("authority_evidence_sha256") != semantic_sha256(record):
            raise M0Error("terminal evidence does not bind retained authority")
        return record
    if terminal.get("authority_evidence_sha256") is not None:
        raise M0Error("terminal evidence names absent retained authority")
    return {}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_bytes(path: Path, payload: bytes, mode: int) -> None:
    try:
        descriptor = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
        )
    except FileExistsError as exc:
        raise M0Error(f"refusing stale terminal staging artifact: {path.name}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _stage_terminal_artifacts(
    root: Path, ledger: dict[str, Any], outcome: TerminalOutcome
) -> tuple[Path, Path, Path, Path]:
    staged_ledger = root / ".evidence_ledger.json.staged"
    staged_status = root / ".session_status.txt.staged"
    final_ledger = root / "evidence_ledger.json"
    final_status = root / "session_status.txt"
    ledger_payload = (
        json.dumps(
            ledger,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    status_payload = STATUS_BY_OUTCOME[outcome].encode("utf-8")
    _write_new_bytes(staged_ledger, ledger_payload, 0o400)
    _write_new_bytes(staged_status, status_payload, 0o400)
    _fsync_directory(root)
    return staged_ledger, staged_status, final_ledger, final_status


def verify_terminal_session(root: Path) -> dict[str, Any]:
    """Independently replay one sealed M0 terminal exact set."""

    root = root.resolve(strict=True)
    ledger = load_json(root / "evidence_ledger.json")
    expected_keys = {
        "schema_version",
        "session_id",
        "terminal_outcome",
        "terminal_status",
        "member_count",
        "members",
        "ledger_sha256",
    }
    if set(ledger) != expected_keys:
        raise M0Error("M0 terminal ledger key closure mismatch")
    digest_input = dict(ledger)
    claimed_digest = digest_input.pop("ledger_sha256", None)
    outcome = ledger.get("terminal_outcome")
    if (
        ledger.get("schema_version")
        != "moe-simulator-phase7-m0-session-ledger-v2"
        or outcome not in STATUS_BY_OUTCOME
        or ledger.get("terminal_status") != STATUS_BY_OUTCOME[outcome].strip()
        or claimed_digest != semantic_sha256(digest_input)
        or root.name != ledger.get("session_id")
    ):
        raise M0Error("M0 terminal ledger identity or digest differs")
    members = ledger.get("members")
    if not isinstance(members, list):
        raise M0Error("M0 terminal ledger members are not a list")
    paths = [item.get("path") for item in members if isinstance(item, dict)]
    if (
        len(paths) != len(members)
        or ledger.get("member_count") != len(members)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or "evidence_ledger.json" in paths
        or "session_status.txt" not in paths
    ):
        raise M0Error("M0 terminal ledger member closure differs")
    actual = exact_regular_file_set(
        root, excluded_root_files={"evidence_ledger.json"}
    )
    if set(paths) != actual:
        raise M0Error("M0 terminal ledger is not an exact file set")
    for item in members:
        if set(item) != {"path", "size_bytes", "sha256"}:
            raise M0Error("M0 terminal ledger member keys differ")
        path = root / item["path"]
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item["size_bytes"]
            or file_sha256(path) != item["sha256"]
            or path.stat().st_mode & 0o222
        ):
            raise M0Error(f"M0 terminal member differs or is writable: {item['path']}")
    for path in (root, *root.rglob("*")):
        if path.is_dir() and path.stat().st_mode & 0o222:
            raise M0Error(f"M0 terminal directory remains writable: {path}")
    if (root / "session_status.txt").read_text(encoding="utf-8") != STATUS_BY_OUTCOME[outcome]:
        raise M0Error("M0 terminal marker differs")
    _validate_terminal_evidence(root, ledger["session_id"], outcome)
    return ledger


def seal_terminal_session(
    root: Path, session_id: str, outcome: TerminalOutcome
) -> dict[str, Any]:
    """Write terminal status and ledger, then make the complete tree read-only."""

    validate_session_id(session_id)
    root = root.resolve(strict=True)
    if root.name != session_id:
        raise M0Error("session root/name binding mismatch")
    if outcome not in STATUS_BY_OUTCOME:
        raise M0Error(f"unsupported terminal outcome: {outcome}")
    if (root / "session_status.txt").exists() or (root / "evidence_ledger.json").exists():
        raise M0Error("terminal session artifacts already exist")
    _validate_terminal_evidence(root, session_id, outcome)
    ledger = build_session_ledger(root, session_id, outcome)
    try:
        (
            staged_ledger,
            staged_status,
            final_ledger,
            final_status,
        ) = _stage_terminal_artifacts(root, ledger, outcome)
        for path in sorted(root.rglob("*"), reverse=True):
            if path in {staged_ledger, staged_status}:
                continue
            if path.is_symlink():
                raise M0Error(f"session symlink is forbidden: {path}")
            if path.is_file():
                path.chmod(0o444)
            elif path.is_dir():
                path.chmod(0o555)
        _rename_noreplace(staged_ledger, final_ledger)
        _fsync_directory(root)
        # A reader can only observe an authoritative terminal state after every
        # pre-existing member is immutable and the ledger is durable.
        _rename_noreplace(staged_status, final_status)
        _fsync_directory(root)
        root.chmod(0o555)
    except Exception:
        # A failed seal must never leave an authoritative terminal marker.
        root.chmod(0o700)
        _remove_if_present(root / "session_status.txt")
        _remove_if_present(root / "evidence_ledger.json")
        _remove_if_present(root / ".session_status.txt.staged")
        _remove_if_present(root / ".evidence_ledger.json.staged")
        _fsync_directory(root)
        raise
    verify_terminal_session(root)
    return ledger


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--session-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--outcome", choices=("PASS", "FAIL", "INCOMPLETE"), required=True
    )
    args = parser.parse_args()
    contract = load_json(args.contract)
    require_unlock(contract)
    validate_session_id(args.session_id)
    root = args.session_root.resolve(strict=True)
    ledger = seal_terminal_session(root, args.session_id, args.outcome)
    print(ledger["ledger_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
