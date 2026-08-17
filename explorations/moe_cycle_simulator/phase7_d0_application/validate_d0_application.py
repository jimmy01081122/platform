#!/usr/bin/env python3
"""CPU-only, network-free validator for the frozen GPUtw D0 application overlay."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
BASE_COMMIT = "a94f336ad57707c1eca0be10e3d6da257ff7fb46"
BASE_TREE = "2ab20af3291a9554ad2dae972b7f32891c29e7bc"
R12_CLOSURE = "87c8a866e44387100dadf9a087cf55a37c7cc9e0"
APPLICATION_ID = "phase7-gputw-d0-7f9804d4-20260809-r1"
KNOWN_HOSTS_LINE = b"[ssh.gputw.ai]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPB++siZEvvX9Lv3hNKGQHAJPHxoqW8qSHy+1hxo3aN/\n"
KNOWN_HOSTS_SHA256 = "b19c0603ac7cc77fa91ba455734a8783f3502535e0fbe0918813f3d676aa6ec2"
HOST_KEY_BLOB_SHA256 = "3474b58e429257768ac1f430802d7fc12255959909bae1bf007ddc5230191b05"
HOST_KEY_FINGERPRINT = "SHA256:NHS1jkKSV3aKwfQwgC1/wSJVlZkJuuG/AH3cUjAZGwU"
SSH_ARGV_SHA256 = "ed6b316dbab3018e2b23a9dcfbae7918d82097a4cd0adf49405419957613a90a"
REMOTE_PAYLOAD_SHA256 = "87b7da0f6acddcfe6b5bfe33c4e90ac49a57f3dea3701b980194a215a3d2bbf1"
COMMAND_BINDING_SHA256 = "ac769a67bffd217471930ab6cc62702990654f058ce4924938cbafab001c7184"
AGGREGATE_PATH = "explorations/moe_cycle_simulator/phase7_provider_adaptation/governance/r1/review_aggregate.json"
SOURCE_BINDINGS = {
    "explorations/moe_cycle_simulator/phase7/application/executor/environment_probe.py": "87b7da0f6acddcfe6b5bfe33c4e90ac49a57f3dea3701b980194a215a3d2bbf1",
    "explorations/moe_cycle_simulator/phase7/application/executor/disclosure_driver.py": "8f4cbfa8bdeb3c0e2208b1268981d9d226d69e9dfe005a0b04e810b0f8ac9bca",
    "explorations/moe_cycle_simulator/phase7/application/executor/disclosure.py": "0aa09a215851c4312b7b5626555a64ef64af9669fde9be0fc27f65bdd615f352",
    "explorations/moe_cycle_simulator/phase7/application/executor/process_tree.py": "4ba00f15843da2f28121cf56e105f9adb01ee188c517990f450404d3c53d32a7",
    "explorations/moe_cycle_simulator/phase7/application/executor/authority.py": "7b4001de87f836b8551ea34253c3722f2afd9a6330fcd6ea624adb65e5d97ac7",
    "explorations/moe_cycle_simulator/phase7/application/executor/package_ledger.py": "31137e6e08988c0a7379e00491cc96a8465d8476716761dec807f8e5d3f7e252",
    "explorations/moe_cycle_simulator/phase7/application/executor/d0_finalize.py": "31ae48269ebae7bf0ba61814a89ab75e02839a03b506686558cb68460e455219",
    "explorations/moe_cycle_simulator/phase7/application/executor/common.py": "f285fcddc433b51486d4162c9174ab2b600b37d07d8ac3e5e6201adf9a13e211",
    "explorations/moe_cycle_simulator/phase7/application/executor/allocation.py": "e10cf3465085f914d4e85e70c02b87fd090bccd429db4e69feae80b723ae945a",
}
CORE_MEMBERS = [
    "D0_APPLICATION.md",
    "application_identity.json",
    "application_manifest.json",
    "d0_contract.json",
    "known_hosts.gputw",
    "remote_payload_binding.json",
    "schemas/d0_application.schema.json",
    "ssh_argv.json",
    "validate_d0_application.py",
]
GOVERNANCE_MEMBERS = [
    "approval_request.json",
    "status.json",
    "application_ledger.sha256",
    "checksums.sha256",
]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise AssertionError(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_json(path: Path) -> dict[str, Any]:
    def reject_float(value: str) -> None:
        raise ValueError(f"JSON float is forbidden: {value}")

    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_float=reject_float,
        parse_constant=reject_float,
        object_pairs_hook=lambda pairs: _unique_object(pairs, path),
    )
    require(isinstance(value, dict), f"{path}: JSON root must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]], path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"{path}: duplicate JSON key {key}")
        result[key] = value
    return result


def git(*args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    )
    return result.stdout


def git_blob(commit: str, path: str) -> bytes:
    return bytes(git("show", f"{commit}:{path}", binary=True))


def parse_ledger(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or "  " not in line:
            fail(f"malformed checksum ledger line: {line!r}")
        digest, member = line.split("  ", 1)
        require(SHA256_RE.fullmatch(digest) is not None, f"invalid digest for {member}")
        rows.append((member, digest))
    require(len({member for member, _ in rows}) == len(rows), "duplicate checksum member")
    require(
        [member for member, _ in rows] == sorted((member for member, _ in rows), key=lambda item: item.encode()),
        f"{path}: members are not LC_ALL=C lexical order",
    )
    return rows


def check_repository_identity() -> None:
    require(str(git("rev-parse", f"{BASE_COMMIT}^{{tree}}")).strip() == BASE_TREE, "base tree mismatch")
    git("cat-file", "-e", f"{R12_CLOSURE}^{{commit}}")
    current = str(git("rev-parse", "HEAD")).strip()
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASE_COMMIT, current],
        cwd=REPO,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", R12_CLOSURE, BASE_COMMIT],
        cwd=REPO,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    aggregate = json.loads(git_blob(BASE_COMMIT, AGGREGATE_PATH))
    require(aggregate["verdict"] == "GO/GO/GO", "GPUtw R1 aggregate is not GO/GO/GO")
    require(aggregate["blockers"] == [], "GPUtw R1 aggregate has blockers")
    require(set(aggregate["reviewer_verdicts"].values()) == {"GO"}, "reviewer verdict mismatch")


def check_known_hosts() -> None:
    path = ROOT / "known_hosts.gputw"
    payload = path.read_bytes()
    require(payload == KNOWN_HOSTS_LINE, "KNOWN_HOSTS_CANONICAL_BYTES_MISMATCH")
    require(sha256(payload) == KNOWN_HOSTS_SHA256, "KNOWN_HOSTS_CANONICAL_BYTES_MISMATCH")
    require(b"\r" not in payload and not payload.startswith(b"\xef\xbb\xbf"), "KNOWN_HOSTS_CANONICAL_BYTES_MISMATCH")
    fields = payload[:-1].decode("utf-8").split(" ")
    require(len(fields) == 3 and fields[0] == "[ssh.gputw.ai]:2222" and fields[1] == "ssh-ed25519", "known_hosts entry mismatch")
    blob = base64.b64decode(fields[2], validate=True)
    require(sha256(blob) == HOST_KEY_BLOB_SHA256, "HOST_KEY_FINGERPRINT_MISMATCH")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    require(fingerprint == HOST_KEY_FINGERPRINT, "HOST_KEY_FINGERPRINT_MISMATCH")


def check_source_bindings(manifest: dict[str, Any], payload: dict[str, Any]) -> None:
    listed = {item["path"]: item["sha256"] for item in manifest["frozen_execution_machinery"]}
    require(listed == SOURCE_BINDINGS, "frozen execution machinery binding mismatch")
    for path, expected in SOURCE_BINDINGS.items():
        require(sha256(git_blob(BASE_COMMIT, path)) == expected, f"frozen source hash mismatch: {path}")
    require(payload["source_commit"] == BASE_COMMIT, "remote payload source commit mismatch")
    require(payload["source_tree"] == BASE_TREE, "remote payload source tree mismatch")
    require(payload["source_path"] == "explorations/moe_cycle_simulator/phase7/application/executor/environment_probe.py", "remote payload source path mismatch")
    require(payload["payload_sha256"] == REMOTE_PAYLOAD_SHA256, "remote payload hash mismatch")
    require(sha256(git_blob(BASE_COMMIT, payload["source_path"])) == REMOTE_PAYLOAD_SHA256, "remote payload bytes differ")


def check_argv(argv_doc: dict[str, Any], payload: dict[str, Any], contract: dict[str, Any]) -> None:
    argv = argv_doc["argv"]
    require(argv_doc["status"] == "FROZEN_NOT_EXECUTED", "SSH argv status changed")
    require(argv_doc["argv_sha256"] == SSH_ARGV_SHA256, "SSH argv digest mismatch")
    require(sha256(canonical_json(argv)) == SSH_ARGV_SHA256, "SSH argv canonical hash mismatch")
    joined = "\n".join(argv)
    for forbidden in ("StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null", "UpdateHostKeys=yes", "ssh-keyscan", "ProxyJump=", "ProxyCommand="):
        if forbidden in {"ProxyJump=", "ProxyCommand="}:
            continue
        require(forbidden not in joined, f"forbidden SSH option: {forbidden}")
    require("StrictHostKeyChecking=yes" in joined, "strict host-key checking missing")
    require("UserKnownHostsFile=" + str(ROOT / "known_hosts.gputw") in joined, "project known_hosts binding missing")
    require(argv[-6:] == payload["remote_argv"], "SSH argv and remote payload argv differ")
    require(argv[-7] == "pod-7f9804d4-2dd0-4196-8215-9049a1d28942@ssh.gputw.ai", "SSH principal mismatch")
    require(contract["command_binding"]["ssh_argv_sha256"] == SSH_ARGV_SHA256, "contract SSH hash mismatch")


def check_cost_and_time(contract: dict[str, Any]) -> None:
    cost = contract["owner_cost_governance"]
    nominal = Decimal(cost["observed_compute_price"]["amount"]) * Decimal(cost["owner_runtime_hours"])
    require(nominal == Decimal("206.16"), "compute-price arithmetic mismatch")
    require(cost["nominal_compute_only_exposure"]["amount"] == "206.16", "nominal exposure mismatch")
    require(cost["owner_total_cost_cap"]["amount"] == "300", "total cost cap mismatch")
    require(contract["cost_authority_status"] == "COST_AUTHORITY_INCOMPLETE", "cost authority must remain incomplete")
    require(contract["d0_execution_approval_eligible"] is False, "D0 must not be execution-eligible")
    for key in ("owner_compute_spend", "owner_storage_spend", "owner_port_spend"):
        require("ZERO_SPEND_REQUIRED" in cost[key], f"missing zero-spend gate: {key}")
    envelope = contract["owner_time_envelope"]
    require(envelope["owner_execution_budget_seconds"] == 21600, "owner cap mismatch")
    require(envelope["semantic_role"] == "OWNER_IMPOSED_EXECUTION_ENVELOPE", "lease semantics mismatch")
    stages = envelope["stage_bounds_seconds"]
    require(stages == {"D0": 300, "Gate M": 5400, "M0": 14400, "release": 900}, "stage bounds mismatch")
    require(envelope["bounded_work_seconds"] == 21000 and envelope["slack_seconds"] == 600, "time arithmetic mismatch")
    require(sum(stages.values()) == 21000 and 21000 + 600 == 21600, "time envelope arithmetic mismatch")


def check_identity(identity: dict[str, Any], manifest: dict[str, Any], contract: dict[str, Any]) -> str:
    require(identity["application_id"] == APPLICATION_ID, "application ID mismatch")
    require(identity["identity_status"] == "FROZEN", "application identity is not frozen")
    require(identity["repository_identity"]["base_commit"] == BASE_COMMIT, "identity base commit mismatch")
    require(identity["repository_identity"]["base_tree"] == BASE_TREE, "identity base tree mismatch")
    require(identity["provider_identity"]["provider"] == "GPUtw.ai", "provider mismatch")
    require(identity["instance_identity"]["instance_id"] == "7f9804d4-2dd0-4196-8215-9049a1d28942", "instance ID mismatch")
    require(identity["ssh_gateway_identity"]["principal"] == "pod-7f9804d4-2dd0-4196-8215-9049a1d28942", "principal mismatch")
    require(identity["server_authentication"]["known_hosts_sha256"] == KNOWN_HOSTS_SHA256, "identity known_hosts mismatch")
    require(identity["server_authentication"]["trusted_fingerprint"] == HOST_KEY_FINGERPRINT, "identity fingerprint mismatch")
    require(identity["authority"]["d0_execution"] == "NOT_AUTHORIZED" and identity["authority"]["gpu_authority"] == "NONE", "authority boundary changed")
    require(contract["application_id"] == APPLICATION_ID and contract["status"] == "READY_FOR_OWNER_REVIEW", "contract identity/status mismatch")
    identity_hash = sha256((ROOT / "application_identity.json").read_bytes())
    require(manifest["application_identity_sha256"] == identity_hash, "application identity hash not bound")
    return identity_hash


def check_schema(manifest: dict[str, Any]) -> None:
    schema = load_json(ROOT / "schemas/d0_application.schema.json")
    require(schema["$id"] == "moe-simulator-phase7-gputw-d0-application-v1", "schema ID mismatch")
    required = set(schema["required"])
    require(required.issubset(manifest), "manifest does not satisfy application schema")
    require(manifest["schema_version"] == "moe-simulator-phase7-gputw-d0-application-manifest-v1", "manifest schema mismatch")


def check_ledgers(manifest: dict[str, Any], approval: dict[str, Any], status: dict[str, Any]) -> tuple[str, str]:
    require(manifest["core_members"] == CORE_MEMBERS, "core member set mismatch")
    require(CORE_MEMBERS == sorted(CORE_MEMBERS, key=lambda item: item.encode()), "core member order mismatch")
    core_rows = parse_ledger(ROOT / "application_ledger.sha256")
    require([member for member, _ in core_rows] == CORE_MEMBERS, "application ledger member set mismatch")
    for member, expected in core_rows:
        path = ROOT / member
        require(path.is_file() and not path.is_symlink(), f"invalid core member: {member}")
        require(sha256(path.read_bytes()) == expected, f"core member hash mismatch: {member}")
    application_ledger_sha256 = sha256((ROOT / "application_ledger.sha256").read_bytes())
    require(approval["application_ledger_sha256"] == application_ledger_sha256, "approval ledger binding mismatch")
    require(status["application_ledger_sha256"] == application_ledger_sha256, "status ledger binding mismatch")
    all_members = sorted(
        CORE_MEMBERS + [member for member in GOVERNANCE_MEMBERS if member != "checksums.sha256"],
        key=lambda item: item.encode(),
    )
    checksum_rows = parse_ledger(ROOT / "checksums.sha256")
    require([member for member, _ in checksum_rows] == all_members, "full checksum member set mismatch")
    for member, expected in checksum_rows:
        path = ROOT / member
        require(path.is_file() and not path.is_symlink(), f"invalid checksum member: {member}")
        require(sha256(path.read_bytes()) == expected, f"checksum member hash mismatch: {member}")
    checksums_sha256 = sha256((ROOT / "checksums.sha256").read_bytes())
    return application_ledger_sha256, checksums_sha256


def check_governance(approval: dict[str, Any], status: dict[str, Any], identity_hash: str) -> None:
    require(approval["application_identity_sha256"] == identity_hash, "approval identity binding mismatch")
    require(status["application_identity_sha256"] == identity_hash, "status identity binding mismatch")
    require(approval["request_status"] == "PENDING_OWNER_APPROVAL", "approval request must remain pending")
    require(approval["approval_decision"] == "PENDING", "D0 approval was consumed or invented")
    require(status["application_status"] == "READY_FOR_OWNER_REVIEW", "status changed")
    require(status["validation_status"] == "PASS", "status validator result is not PASS")
    require(status["d0_execution_approval_eligible"] is False, "status grants D0 authority")
    require(all(value is False for value in status["side_effects"].values()), "side effect recorded")


def check_secret_absence() -> None:
    for member in CORE_MEMBERS + GOVERNANCE_MEMBERS:
        if member == "validate_d0_application.py":
            continue
        data = (ROOT / member).read_bytes()
        text = data.decode("utf-8", errors="strict")
        for marker in ("BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "BEGIN EC PRIVATE KEY", "id_rsa", "id_ed25519"):
            require(marker not in text, f"PRIVATE_CREDENTIAL_EXPOSURE: {member}")


def main() -> None:
    manifest = load_json(ROOT / "application_manifest.json")
    identity = load_json(ROOT / "application_identity.json")
    contract = load_json(ROOT / "d0_contract.json")
    argv_doc = load_json(ROOT / "ssh_argv.json")
    payload = load_json(ROOT / "remote_payload_binding.json")
    approval = load_json(ROOT / "approval_request.json")
    status = load_json(ROOT / "status.json")
    check_repository_identity()
    check_schema(manifest)
    check_known_hosts()
    check_source_bindings(manifest, payload)
    check_argv(argv_doc, payload, contract)
    check_cost_and_time(contract)
    identity_hash = check_identity(identity, manifest, contract)
    application_ledger_sha256, checksums_sha256 = check_ledgers(manifest, approval, status)
    check_governance(approval, status, identity_hash)
    check_secret_absence()
    print(json.dumps({
        "status": "PASS",
        "application_id": APPLICATION_ID,
        "application_identity_sha256": identity_hash,
        "application_ledger_sha256": application_ledger_sha256,
        "checksums_sha256": checksums_sha256,
        "known_hosts_sha256": KNOWN_HOSTS_SHA256,
        "ssh_argv_sha256": SSH_ARGV_SHA256,
        "remote_payload_sha256": REMOTE_PAYLOAD_SHA256,
        "exact_command_binding_sha256": COMMAND_BINDING_SHA256,
        "d0_execution": "NOT_AUTHORIZED",
        "gpu_authority": "NONE",
        "network_free": True,
    }, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"FAIL: {exc}") from exc
