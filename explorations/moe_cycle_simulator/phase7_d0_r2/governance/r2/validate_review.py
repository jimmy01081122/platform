#!/usr/bin/env python3
"""Validate the immutable D0-R2 candidate identity and CPU-only review closure."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
REVIEW_ROOT = ROOT / "explorations/moe_cycle_simulator/phase7_d0_r2/governance/r2"
REQUIRED_REVIEWERS = {
    "Architecture/System": "review_architecture_system.json",
    "Model/Benchmark": "review_model_benchmark.json",
    "Trace/Provenance": "review_trace_provenance.json",
}
SIDE_EFFECT_KEYS = (
    "ssh",
    "provider_network_probe",
    "instance_creation",
    "model_download",
    "gpu_query",
    "gpu_workload",
)


def git(*args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    )
    return result.stdout


def read_json(name: str) -> dict:
    with (REVIEW_ROOT / name).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"{name}: expected JSON object")
    return value


def read_ledger(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or "  " not in line:
            raise AssertionError(f"malformed ledger line: {line!r}")
        digest, member = line.split("  ", 1)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise AssertionError(f"invalid SHA-256 digest for {member!r}")
        if not member or member.startswith("/") or ".." in Path(member).parts:
            raise AssertionError(f"unsafe candidate path: {member!r}")
        rows.append((member, digest))
    if len({member for member, _ in rows}) != len(rows):
        raise AssertionError("duplicate candidate path")
    if [member for member, _ in rows] != sorted(
        (member for member, _ in rows), key=lambda member: member.encode("utf-8")
    ):
        raise AssertionError("candidate ledger is not LC_ALL=C lexical order")
    return rows


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_candidate(policy: dict, manifest: dict) -> dict:
    commit = policy["reviewed_commit"]
    tree = policy["reviewed_tree"]
    members = policy["candidate_paths"]
    if members != sorted(members, key=lambda member: member.encode("utf-8")):
        raise AssertionError("candidate policy is not LC_ALL=C lexical order")
    if len(members) != len(set(members)):
        raise AssertionError("candidate policy contains duplicate paths")
    if any("governance/r2" in member for member in members):
        raise AssertionError("review artifact entered the implementation candidate")
    ledger_path = ROOT / policy["candidate_ledger"]
    rows = read_ledger(ledger_path)
    row_members = [member for member, _ in rows]
    if row_members != members:
        raise AssertionError("candidate policy and ledger member sets differ")
    if len(members) != policy["candidate_member_count"]:
        raise AssertionError("candidate member count mismatch")
    if sha256(ledger_path.read_bytes()) != policy["candidate_ledger_sha256"]:
        raise AssertionError("candidate ledger SHA-256 mismatch")
    if manifest["candidate_id"] != policy["candidate_id"]:
        raise AssertionError("candidate ID mismatch between policy and manifest")
    if manifest["reviewed_commit"] != commit or manifest["reviewed_tree"] != tree:
        raise AssertionError("candidate identity mismatch between policy and manifest")
    if manifest["candidate_ledger_sha256"] != policy["candidate_ledger_sha256"]:
        raise AssertionError("candidate ledger root mismatch between policy and manifest")

    actual_tree = str(git("rev-parse", f"{commit}^{{tree}}")).strip()
    if actual_tree != tree:
        raise AssertionError(f"candidate tree mismatch: {actual_tree} != {tree}")

    changed = []
    for line in str(git("diff-tree", "--root", "--no-commit-id", "--name-status", "-r", commit)).splitlines():
        status, member = line.split("\t", 1)
        if status != "A":
            raise AssertionError(f"candidate commit has non-additive change: {line}")
        changed.append(member)
    if changed != members:
        raise AssertionError("candidate set does not equal the implementation commit change boundary")

    for member, expected_digest in rows:
        tree_entry = str(git("ls-tree", commit, "--", member)).strip()
        metadata, entry_path = tree_entry.split("\t", 1)
        mode, entry_type, _object_id = metadata.split()
        if entry_type != "blob" or mode not in {"100644", "100755"} or entry_path != member:
            raise AssertionError(f"candidate member is not a regular file: {member}")
        actual_digest = sha256(git("show", f"{commit}:{member}", binary=True))
        if actual_digest != expected_digest:
            raise AssertionError(f"candidate member hash mismatch: {member}")

    return {
        "candidate_id": policy["candidate_id"],
        "reviewed_commit": commit,
        "reviewed_tree": tree,
        "candidate_member_count": len(members),
        "candidate_ledger_sha256": policy["candidate_ledger_sha256"],
    }


def check_side_effects(value: object, label: str) -> None:
    if not isinstance(value, dict) or set(value) != set(SIDE_EFFECT_KEYS) or any(value.values()):
        raise AssertionError(f"{label}: side effects are not all false")


def check_archive_replay(identity: dict) -> None:
    replay = read_json("archive_replay.json")
    for key, identity_key in (
        ("candidate_id", "candidate_id"),
        ("reviewed_commit", "reviewed_commit"),
        ("reviewed_tree", "reviewed_tree"),
        ("candidate_ledger_sha256", "candidate_ledger_sha256"),
    ):
        if replay.get(key) != identity[identity_key]:
            raise AssertionError("archive replay candidate identity mismatch")
    if replay.get("archive_has_git_metadata") is not False:
        raise AssertionError("archive replay includes git metadata")
    if replay.get("d0_r2_validator") != "PASS" or replay.get("d0_r2_focused_pytest") != "9 passed":
        raise AssertionError("archive replay CPU checks did not pass")
    if replay.get("authority_boundary") != {
        "d0_execution": "NOT_AUTHORIZED",
        "gate_m": "NOT_AUTHORIZED",
        "m0": "NOT_AUTHORIZED",
        "gpu_authority": "NONE",
        "ssh": "NOT_AUTHORIZED",
    }:
        raise AssertionError("archive replay authority boundary changed")
    if replay.get("network_free") is not True or replay.get("side_effects") is not False:
        raise AssertionError("archive replay side-effect boundary changed")


def check_review_outputs(identity: dict) -> None:
    seen_roles = set()
    for role, filename in REQUIRED_REVIEWERS.items():
        review = read_json(filename)
        seen_roles.add(review.get("role"))
        for key in (
            "role",
            "candidate_id",
            "reviewed_commit",
            "reviewed_tree",
            "reviewed_candidate_ledger_sha256",
            "candidate_member_count",
            "verdict",
            "blockers",
            "non_blocking_findings",
            "commands_executed",
            "claim_boundary",
            "side_effects",
        ):
            if key not in review:
                raise AssertionError(f"{filename}: missing {key}")
        if review["role"] != role:
            raise AssertionError(f"{filename}: unexpected role")
        for key, identity_key in (
            ("candidate_id", "candidate_id"),
            ("reviewed_commit", "reviewed_commit"),
            ("reviewed_tree", "reviewed_tree"),
            ("reviewed_candidate_ledger_sha256", "candidate_ledger_sha256"),
            ("candidate_member_count", "candidate_member_count"),
        ):
            if review[key] != identity[identity_key]:
                raise AssertionError(f"{filename}: candidate identity mismatch")
        if review["verdict"] != "GO" or review["blockers"] != []:
            raise AssertionError(f"{filename}: review verdict is not unconditional GO")
        if not isinstance(review["commands_executed"], list) or not review["commands_executed"]:
            raise AssertionError(f"{filename}: commands_executed must be non-empty")
        if not isinstance(review["claim_boundary"], str) or not review["claim_boundary"]:
            raise AssertionError(f"{filename}: claim_boundary must be a non-empty string")
        check_side_effects(review["side_effects"], filename)

    if seen_roles != set(REQUIRED_REVIEWERS):
        raise AssertionError("reviewer role set mismatch")

    aggregate = read_json("review_aggregate.json")
    if aggregate.get("candidate_identity") != identity:
        raise AssertionError("aggregate candidate identity mismatch")
    if aggregate.get("reviewer_verdicts") != {role: "GO" for role in REQUIRED_REVIEWERS}:
        raise AssertionError("aggregate reviewer verdicts are not exactly GO")
    if aggregate.get("verdict") != "GO/GO/GO" or aggregate.get("blockers") != []:
        raise AssertionError("aggregate is not unconditional GO/GO/GO")
    if aggregate.get("identity_equality") != "PASS":
        raise AssertionError("aggregate identity check did not pass")
    if aggregate.get("cpu_evidence_replay") != "PASS" or aggregate.get("clean_archive_replay") != "PASS":
        raise AssertionError("aggregate CPU/archive replay did not pass")
    if aggregate.get("historical_integrity") != "PASS":
        raise AssertionError("aggregate historical integrity check did not pass")
    if not isinstance(aggregate.get("external_execution_blockers"), list) or not aggregate["external_execution_blockers"]:
        raise AssertionError("aggregate must retain external execution blockers")
    if aggregate.get("execution_authority") != {
        "d0_execution": "NOT_AUTHORIZED",
        "gate_m": "NOT_AUTHORIZED",
        "m0": "NOT_AUTHORIZED",
        "gpu_authority": "NONE",
        "ssh": "NOT_AUTHORIZED",
    }:
        raise AssertionError("aggregate execution authority boundary changed")
    if aggregate.get("side_effects") is not False:
        raise AssertionError("aggregate side_effects must be false")


def check_review_ledger() -> None:
    ledger = REVIEW_ROOT / "review_checksums.sha256"
    rows = read_ledger(ledger)
    for member, expected_digest in rows:
        path = ROOT / member
        if not path.is_file() or path.is_symlink():
            raise AssertionError(f"review artifact is not a regular file: {member}")
        if sha256(path.read_bytes()) != expected_digest:
            raise AssertionError(f"review artifact hash mismatch: {member}")


def main() -> None:
    policy = read_json("candidate_set_policy.json")
    manifest = read_json("candidate_manifest.json")
    identity = check_candidate(policy, manifest)
    check_archive_replay(identity)
    check_review_outputs(identity)
    check_review_ledger()
    print(json.dumps({"status": "PASS", **identity}, sort_keys=True))


if __name__ == "__main__":
    main()
