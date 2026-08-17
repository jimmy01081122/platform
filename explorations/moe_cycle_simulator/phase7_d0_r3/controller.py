#!/usr/bin/env python3
"""Fail-closed CPU-only D0-R3 controller and terminal evidence sealer.

The controller has no ambient execution path.  Its live entry point requires a
complete owner approval and an explicit second factor; all command material is
derived from the captured approval and plan rather than accepted as raw argv.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parent
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
UNSAFE_SECRET_MARKERS = (
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
)
ALLOWED_HOST_KEY_PROVENANCE = {
    "OFFICIAL_PROVIDER_CONFIRMATION",
    "AUTHENTICATED_PROVIDER_DASHBOARD",
    "INDEPENDENTLY_AUTHENTICATED_PROVIDER_CHANNEL",
}
TERMINAL_MARKERS = {
    "COMPLETE": "D0_R3_COMPLETE_AUDITED\n",
    "FAILED": "D0_R3_FAILED_IMMUTABLE_NO_RETRY\n",
    "INCOMPLETE": "D0_R3_INCOMPLETE_IMMUTABLE_NO_RETRY\n",
}


class D0R2Error(RuntimeError):
    """A fail-closed D0-R3 contract or evidence error."""


# Kept as an explicit compatibility alias so old helper code cannot silently
# import the prospective package as if it were the immutable R2 package.
D0R3Error = D0R2Error


def require(condition: bool, message: str) -> None:
    if not condition:
        raise D0R2Error(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise D0R2Error(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise D0R2Error(f"JSON floating-point value is forbidden: {value}")


def load_json_bytes(payload: bytes, label: str = "<bytes>") -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise D0R2Error(f"invalid strict JSON in {label}: {exc}") from exc
    require(isinstance(value, dict), f"JSON root must be an object: {label}")
    return value


def load_json(path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> tuple[dict[str, Any], bytes]:
    payload = read_once(path, max_bytes=max_bytes)
    return load_json_bytes(payload, str(path)), payload


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise D0R2Error(f"value cannot be canonicalized: {exc}") from exc


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_bytes(read_once(path, max_bytes=2**63 - 1))


def semantic_sha256(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def read_once(path: Path, *, max_bytes: int) -> bytes:
    """Read a regular non-symlink file once and never follow a replacement."""

    path = Path(path)
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise D0R2Error(f"cannot open captured input {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        require(stat.S_ISREG(metadata.st_mode), f"captured input is not regular: {path}")
        output = bytearray()
        while True:
            block = os.read(descriptor, min(1024 * 1024, max_bytes - len(output) + 1))
            if not block:
                break
            output.extend(block)
            require(len(output) <= max_bytes, f"captured input exceeds bound: {path}")
        return bytes(output)
    finally:
        os.close(descriptor)


def write_exclusive(path: Path, payload: bytes, *, mode: int = 0o400) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
        )
    except OSError as exc:
        raise D0R2Error(f"refusing to overwrite {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_json_exclusive(path: Path, value: Any) -> bytes:
    payload = canonical_bytes(value)
    write_exclusive(path, payload)
    return payload


def _type_matches(value: Any, type_name: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(type_name, False)


def _resolve_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not reference:
        return schema
    require(reference.startswith("#/"), f"external schema reference is forbidden: {reference}")
    value: Any = root
    for component in reference[2:].split("/"):
        value = value[component.replace("~1", "/").replace("~0", "~")]
    require(isinstance(value, dict), f"schema reference is not an object: {reference}")
    return value


def validate_schema(value: Any, schema: dict[str, Any], *, root: dict[str, Any] | None = None, path: str = "$") -> None:
    root = schema if root is None else root
    schema = _resolve_ref(schema, root)
    if "oneOf" in schema:
        successes = 0
        for option in schema["oneOf"]:
            try:
                validate_schema(value, option, root=root, path=path)
            except D0R2Error:
                continue
            successes += 1
        require(successes == 1, f"{path}: oneOf validation failed")
        return
    if "const" in schema:
        require(value == schema["const"], f"{path}: expected const {schema['const']!r}")
    if "enum" in schema:
        require(value in schema["enum"], f"{path}: value is outside enum")
    if "type" in schema:
        expected = schema["type"]
        names = expected if isinstance(expected, list) else [expected]
        require(any(_type_matches(value, name) for name in names), f"{path}: type mismatch")
    if isinstance(value, str):
        if "minLength" in schema:
            require(len(value) >= schema["minLength"], f"{path}: string is too short")
        if "pattern" in schema:
            require(re.search(schema["pattern"], value) is not None, f"{path}: pattern mismatch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema:
            require(value >= schema["minimum"], f"{path}: value is below minimum")
    if isinstance(value, list):
        if "minItems" in schema:
            require(len(value) >= schema["minItems"], f"{path}: array is too short")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], root=root, path=f"{path}[{index}]")
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            require(key in value, f"{path}: missing required property {key}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in properties:
                validate_schema(child, properties[key], root=root, path=f"{path}.{key}")
            elif additional is False:
                raise D0R2Error(f"{path}: unexpected property {key}")
            elif isinstance(additional, dict):
                validate_schema(child, additional, root=root, path=f"{path}.{key}")


def _contains_private_material(value: Any, location: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = key.casefold()
            if any(token in lowered for token in ("private_key", "password", "secret")):
                if child not in (
                    None,
                    "",
                    False,
                    "EXCLUDED",
                    "FORBIDDEN_NOT_RECORDED",
                    "FORBIDDEN_AND_NOT_RECORDED",
                    "FORBIDDEN_IN_REPOSITORY_VAULT_WORKSPACE_LOGS_AND_ARGV",
                ):
                    found.append(f"{location}.{key}")
            found.extend(_contains_private_material(child, f"{location}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_contains_private_material(child, f"{location}[{index}]"))
    elif isinstance(value, str) and any(marker in value for marker in UNSAFE_SECRET_MARKERS):
        found.append(location)
    return found


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    require(actual == expected, f"{label} key closure mismatch; missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")


def _load_schema(name: str) -> dict[str, Any]:
    schema, _ = load_json(PACKAGE_ROOT / name)
    return schema


def _parse_ledger(payload: bytes, label: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in payload.decode("utf-8", errors="strict").splitlines():
        require("  " in line, f"{label}: malformed ledger line")
        digest, member = line.split("  ", 1)
        require(SHA256_RE.fullmatch(digest) is not None, f"{label}: invalid digest")
        require(member and not member.startswith("/") and ".." not in Path(member).parts, f"{label}: unsafe member path")
        rows.append((member, digest))
    require(len(rows) == len({member for member, _ in rows}), f"{label}: duplicate member")
    require([member for member, _ in rows] == sorted((member for member, _ in rows), key=lambda item: item.encode()), f"{label}: members are not lexical")
    return rows


def _validate_application_identity(root: Path, identity: dict[str, Any]) -> str:
    _require_exact_keys(identity, {"schema_version", "application_id", "identity_status", "identity_preimage", "application_identity_sha256"}, "application identity")
    require(identity["schema_version"] == "moe-simulator-phase7-gputw-d0-r3-application-identity-v1", "application identity schema mismatch")
    require(identity["identity_status"] == "FROZEN_CPU_ONLY", "application identity status mismatch")
    digest = semantic_sha256(identity["identity_preimage"])
    require(identity["application_identity_sha256"] == digest, "application identity hash mismatch")
    require(identity["application_id"] == "phase7-gputw-d0-7f9804d4-20260809-r3", "application identity ID mismatch")
    return digest


def _validate_dependency_manifest(package_bytes: Mapping[str, bytes], dependency: dict[str, Any]) -> None:
    _require_exact_keys(dependency, {"schema_version", "closure_status", "members", "live_inputs", "private_credentials"}, "dependency manifest")
    require(dependency["private_credentials"] == "EXCLUDED", "private credentials are not excluded")
    for item in dependency["members"]:
        require(item["path"] in package_bytes, f"dependency member is outside package ledger: {item['path']}")
        require(item["sha256"] == sha256_bytes(package_bytes[item["path"]]), f"transitive dependency drift: {item['path']}")


@dataclass(frozen=True)
class PackageInputs:
    root: Path
    overlay: dict[str, Any]
    owner_authority: dict[str, Any]
    plan: dict[str, Any]
    manifest: dict[str, Any]
    identity: dict[str, Any]
    application_identity_sha256: str
    application_ledger_sha256: str
    package_bytes: dict[str, bytes]
    probe_source: bytes


def load_package(root: Path = PACKAGE_ROOT) -> PackageInputs:
    root = Path(root).resolve(strict=True)
    require(root == PACKAGE_ROOT.resolve(), "D0-R3 package must be validated from its prospective package root")
    manifest, _ = load_json(root / "application_manifest.json")
    _require_exact_keys(manifest, {"schema_version", "application_id", "status", "application_ledger_path", "ledger_exclusions", "ledger_members", "mutable_external_inputs", "historical_artifacts_untouched", "execution_authority"}, "application manifest")
    require(manifest["schema_version"] == "moe-simulator-phase7-gputw-d0-r3-application-manifest-v1", "application manifest schema mismatch")
    require(manifest["status"] == "CPU_ONLY_DISCOVERY_PACKAGE", "application manifest status mismatch")
    require(manifest["application_id"] == "phase7-gputw-d0-7f9804d4-20260809-r3", "application manifest ID mismatch")
    members = manifest["ledger_members"]
    require(isinstance(members, list) and members == sorted(members, key=lambda item: item.encode()), "application manifest members are not lexical")
    require(manifest["ledger_exclusions"] == ["application_ledger.sha256", "approval.template.json"], "application ledger exclusions changed")
    package_bytes: dict[str, bytes] = {}
    for member in members:
        path = root / member
        require(not path.is_symlink() and path.is_file(), f"application member missing or unsafe: {member}")
        package_bytes[member] = read_once(path, max_bytes=32 * 1024 * 1024)
    ledger_bytes = read_once(root / manifest["application_ledger_path"], max_bytes=4 * 1024 * 1024)
    rows = _parse_ledger(ledger_bytes, "application ledger")
    require([member for member, _ in rows] == members, "application ledger member set mismatch")
    for member, expected in rows:
        require(sha256_bytes(package_bytes[member]) == expected, f"application ledger hash mismatch: {member}")
    overlay, _ = load_json(root / "overlay.json")
    validate_schema(overlay, _load_schema("d0_r3.schema.json"))
    owner_authority, _ = load_json(root / "owner_authority.json")
    plan, _ = load_json(root / "d0_plan.json")
    identity, _ = load_json(root / "application_identity.json")
    application_identity_sha256 = _validate_application_identity(root, identity)
    require(overlay["application_id"] == manifest["application_id"] == identity["application_id"], "application ID closure mismatch")
    require(overlay["authority"] == {"d0_execution": "NOT_AUTHORIZED", "gate_m": "NOT_AUTHORIZED", "m0": "NOT_AUTHORIZED", "gpu_authority": "NONE"}, "authority boundary changed")
    require(overlay["promotion"]["promotable"] is False and overlay["promotion"]["execution_mode"] == "DISCOVERY_ONLY", "D0-R3 package became promotable")
    require(overlay["environment"]["container_digest"] is None, "a live container digest was invented")
    require(owner_authority["decision_status"] == "PENDING_OWNER_CONFIRMATION", "owner authority status changed")
    require(owner_authority["authority"] == {"d0": "NOT_AUTHORIZED", "gate_m": "NOT_AUTHORIZED", "m0": "NOT_AUTHORIZED", "gpu": "NONE"}, "owner authority boundary changed")
    require(owner_authority["cost_authority"]["maximum_additional_spend"] == {"amount": "0", "currency": "TWD"}, "owner maximum spend changed")
    require(owner_authority["cost_authority"]["top_up"] is False and owner_authority["cost_authority"]["extension"] is False, "top-up or extension became allowed")
    require(plan["execution_mode"] == "DISCOVERY_ONLY" and plan["retry_allowed"] is False and plan["resume_allowed"] is False, "D0-R3 plan authority changed")
    require(plan["outer_timeout_seconds"] == 300 and plan["inner_probe_timeout_seconds"] == 120, "D0 timeout contract changed")
    probe_source = package_bytes["probe.py"]
    require(plan["source_binding"]["probe_sha256"] == sha256_bytes(probe_source), "probe source binding mismatch")
    dependency, _ = load_json(root / "dependency_manifest.json")
    _validate_dependency_manifest(package_bytes, dependency)
    for value in (overlay, owner_authority, plan, manifest, identity, dependency):
        require(not _contains_private_material(value), "private credential material is present in package JSON")
    ledger_hash = sha256_bytes(ledger_bytes)
    return PackageInputs(root, overlay, owner_authority, plan, manifest, identity, application_identity_sha256, ledger_hash, package_bytes, probe_source)


def _utc_datetime(value: str, label: str) -> datetime:
    require(isinstance(value, str) and value.endswith("Z"), f"{label} must be UTC ISO-8601")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise D0R2Error(f"{label} is not a timestamp") from exc
    require(result.tzinfo is not None and result.utcoffset() == timezone.utc.utcoffset(result), f"{label} is not UTC")
    return result


def _validate_fresh_directory_target(path: Path, *, label: str) -> None:
    """Validate the parent before any one-shot authority is consumed."""

    require(path.is_absolute() and not str(path).startswith("PENDING_"), f"{label} is not an explicit absolute path")
    parent = path.parent
    require(parent.is_absolute() and parent.exists() and not parent.is_symlink() and parent.is_dir(), f"{label} parent is not a real directory")
    require(not path.exists() and not path.is_symlink(), f"{label} already exists or is a symlink")


def _fresh_now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _validate_session_id(value: str) -> None:
    require(SESSION_RE.fullmatch(value) is not None, "invalid fresh session ID")


def build_ssh_argv(approval: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    endpoint = approval["endpoint"]
    host_key = approval["host_key"]
    credential = approval["credential"]
    runtime = approval["runtime"]
    digest = runtime["approved_container_digest"] or "UNOBSERVED"
    remote_argv = ["env", f"MOE_PHASE7_CONTAINER_DIGEST={digest}", "python3", "-I", "-B", "-"]
    argv = [
        approval["local_ssh"]["executable_path"],
        "-F", "/dev/null", "-T", "-p", str(endpoint["port"]),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={host_key['known_hosts_path']}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"IdentityAgent={credential['selector']}",
        "-o", "ClearAllForwardings=yes",
        "-o", "ForwardAgent=no",
        "-o", "ProxyCommand=none",
        "-o", "ProxyJump=none",
        "-o", "RequestTTY=no",
        "-o", "ControlMaster=no",
        "-o", "LogLevel=ERROR",
        "--",
        f"{endpoint['username']}@{endpoint['host']}",
        *remote_argv,
    ]
    return argv, {"SSH_AUTH_SOCK": credential["selector"]}


def _validate_known_hosts(approval: dict[str, Any]) -> bytes:
    descriptor = approval["host_key"]
    raw_path = Path(descriptor["known_hosts_path"])
    require(raw_path.is_absolute() and not raw_path.is_symlink(), "known_hosts path is not a real file path")
    path = raw_path.resolve(strict=True)
    raw = read_once(path, max_bytes=64 * 1024)
    require(sha256_bytes(raw) == descriptor["known_hosts_sha256"], "host-key source bytes drifted")
    require(raw.endswith(b"\n") and raw.count(b"\n") == 1 and b"\r" not in raw, "known_hosts bytes are not canonical")
    fields = raw[:-1].decode("utf-8", errors="strict").split(" ")
    require(len(fields) == 3, "known_hosts must contain exactly one canonical entry")
    endpoint = approval["endpoint"]
    require(fields[0] == f"[{endpoint['host']}]:{endpoint['port']}", "known_hosts endpoint mismatch")
    require(fields[1].startswith("ssh-"), "known_hosts algorithm is not an SSH host-key algorithm")
    try:
        blob = base64.b64decode(fields[2], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise D0R2Error("known_hosts host-key blob is invalid") from exc
    require(sha256_bytes(blob) == descriptor["host_key_blob_sha256"], "host-key blob hash mismatch")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    require(fingerprint == descriptor["fingerprint"], "host-key fingerprint mismatch")
    require(descriptor["provenance"] in ALLOWED_HOST_KEY_PROVENANCE, "host-key provenance is not authenticated")
    provenance_path = Path(descriptor["provenance_artifact_path"])
    require(provenance_path.is_absolute() and not provenance_path.is_symlink(), "host-key provenance artifact path is not explicit")
    provenance_bytes = read_once(provenance_path.resolve(strict=True), max_bytes=256 * 1024)
    require(sha256_bytes(provenance_bytes) == descriptor["provenance_artifact_sha256"], "host-key provenance artifact drifted")
    provenance = load_json_bytes(provenance_bytes, "host-key provenance artifact")
    _require_exact_keys(provenance, {"schema_version", "source_id", "confirmed_at_utc", "endpoint", "known_hosts_sha256", "host_key_blob_sha256", "fingerprint"}, "host-key provenance artifact")
    require(provenance["schema_version"] == "gpu-tw-host-key-confirmation-v1", "host-key provenance schema mismatch")
    require(provenance["endpoint"] == f"[{endpoint['host']}]:{endpoint['port']}", "host-key provenance endpoint mismatch")
    require(provenance["known_hosts_sha256"] == descriptor["known_hosts_sha256"], "host-key provenance source mismatch")
    require(provenance["host_key_blob_sha256"] == descriptor["host_key_blob_sha256"], "host-key provenance blob mismatch")
    require(provenance["fingerprint"] == descriptor["fingerprint"], "host-key provenance fingerprint mismatch")
    _utc_datetime(provenance["confirmed_at_utc"], "host-key provenance confirmation time")
    return raw


def _ssh_public_key_fingerprints(listing: bytes) -> set[str]:
    fingerprints: set[str] = set()
    for line in listing.decode("utf-8", errors="strict").splitlines():
        fields = line.split()
        require(len(fields) >= 2 and fields[0].startswith("ssh-"), "SSH agent returned a non-public-key line")
        try:
            blob = base64.b64decode(fields[1], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise D0R2Error("SSH agent returned an invalid public-key blob") from exc
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        fingerprints.add(fingerprint)
    require(fingerprints, "SSH agent returned no public keys")
    return fingerprints


def _capture_agent_listing(approval: dict[str, Any]) -> bytes:
    credential = approval["credential"]
    executable_path = Path(credential["agent_query_executable_path"])
    require(executable_path.is_absolute() and not executable_path.is_symlink(), "SSH agent query executable path is not explicit")
    executable = executable_path.resolve(strict=True)
    metadata = executable.stat()
    require(stat.S_ISREG(metadata.st_mode), "SSH agent query executable is not a regular file")
    executable_bytes = read_once(executable, max_bytes=64 * 1024 * 1024)
    require(sha256_bytes(executable_bytes) == credential["agent_query_executable_sha256"], "SSH agent query executable hash drifted")
    environment = os.environ.copy()
    environment["SSH_AUTH_SOCK"] = credential["selector"]
    try:
        result = subprocess.run(
            [str(executable), "-L"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise D0R2Error("unable to query the approved SSH agent") from exc
    require(result.returncode == 0, "approved SSH agent query failed")
    _ssh_public_key_fingerprints(result.stdout)
    return result.stdout


def _validate_local_ssh(approval: dict[str, Any], *, agent_listing_provider: Callable[[dict[str, Any]], bytes] | None = None) -> bytes:
    descriptor = approval["local_ssh"]
    raw_path = Path(descriptor["executable_path"])
    require(raw_path.is_absolute() and not raw_path.is_symlink(), "local SSH executable path is not a real file path")
    path = raw_path.resolve(strict=True)
    metadata = path.stat()
    require(stat.S_ISREG(metadata.st_mode) and not path.is_symlink(), "local SSH executable is not a regular file")
    raw = read_once(path, max_bytes=64 * 1024 * 1024)
    require(sha256_bytes(raw) == descriptor["executable_sha256"], "local SSH executable hash drifted")
    credential = approval["credential"]
    selector = Path(credential["selector"])
    require(selector.is_absolute() and not str(selector).startswith("PENDING_"), "credential selector is not explicit")
    fingerprint = credential["client_public_key_fingerprint"]
    require(isinstance(fingerprint, str) and fingerprint.startswith("SHA256:"), "client public-key fingerprint is missing")
    require(credential["private_key_material"] == "FORBIDDEN_NOT_RECORDED", "private key material was recorded")
    listing = (agent_listing_provider or _capture_agent_listing)(approval)
    require(fingerprint in _ssh_public_key_fingerprints(listing), "approved client public-key fingerprint is not present in the selected agent")
    return raw


def build_command_preimage(package: PackageInputs, approval: dict[str, Any], *, known_hosts: bytes, ssh_executable: bytes) -> dict[str, Any]:
    argv, env_overrides = build_ssh_argv(approval)
    return {
        "schema_version": "moe-simulator-phase7-gputw-d0-r3-command-preimage-v1",
        "application_id": package.overlay["application_id"],
        "application_identity_sha256": package.application_identity_sha256,
        "application_ledger_sha256": package.application_ledger_sha256,
        "reviewed_commit": approval["reviewed_commit"],
        "reviewed_tree": approval["reviewed_tree"],
        "owner_authority_sha256": approval["owner_authority_sha256"],
        "approval_id": approval["approval_id"],
        "ssh": {
            "executable_path": approval["local_ssh"]["executable_path"],
            "executable_sha256": sha256_bytes(ssh_executable),
            "argv": argv,
            "env_overrides": env_overrides,
        },
        "host_key": {
            "known_hosts_path": approval["host_key"]["known_hosts_path"],
            "known_hosts_sha256": sha256_bytes(known_hosts),
            "host_key_blob_sha256": approval["host_key"]["host_key_blob_sha256"],
            "provenance": approval["host_key"]["provenance"],
            "provenance_artifact_path": approval["host_key"]["provenance_artifact_path"],
            "provenance_artifact_sha256": approval["host_key"]["provenance_artifact_sha256"],
        },
        "credential": {
            "selector_kind": approval["credential"]["selector_kind"],
            "selector": approval["credential"]["selector"],
            "client_public_key_fingerprint": approval["credential"]["client_public_key_fingerprint"],
            "agent_query_executable_path": approval["credential"]["agent_query_executable_path"],
            "agent_query_executable_sha256": approval["credential"]["agent_query_executable_sha256"],
        },
        "remote_payload_sha256": sha256_bytes(package.probe_source),
        "remote_argv": argv[-6:],
        "outer_timeout_seconds": package.plan["outer_timeout_seconds"],
        "inner_probe_timeout_seconds": package.plan["inner_probe_timeout_seconds"],
        "max_stdout_bytes": package.plan["max_stdout_bytes"],
        "max_stderr_bytes": package.plan["max_stderr_bytes"],
        "process_tree_cleanup": package.plan["process_cleanup"],
    }


def _validate_approval(
    package: PackageInputs,
    approval: dict[str, Any],
    *,
    evidence_root: Path,
    registry_path: Path,
    now_utc: datetime | None = None,
    agent_listing_provider: Callable[[dict[str, Any]], bytes] | None = None,
) -> tuple[bytes, bytes, bytes, dict[str, Any], str]:
    validate_schema(approval, _load_schema("schemas/approval.schema.json"))
    require(approval["decision"] == "APPROVE", "owner approval is not APPROVE")
    require(approval["execution_mode"] == "DISCOVERY_ONLY", "D0-R3 overlay permits discovery-only execution only")
    require(approval["application_id"] == package.overlay["application_id"], "approval application identity mismatch")
    require(approval["application_identity_sha256"] == package.application_identity_sha256, "approval application identity hash mismatch")
    require(approval["application_ledger_sha256"] == package.application_ledger_sha256, "approval application ledger hash mismatch")
    owner_input = approval["owner_authority_input"]
    owner_path = Path(owner_input["path"])
    require(owner_path.is_absolute() and not owner_path.is_symlink() and not str(owner_path).startswith("PENDING_"), "owner authority input path is not explicit")
    owner_bytes = read_once(owner_path.resolve(strict=True), max_bytes=256 * 1024)
    owner_hash = sha256_bytes(owner_bytes)
    require(approval["owner_authority_sha256"] == owner_hash == owner_input["sha256"], "owner authority hash mismatch")
    owner_document = load_json_bytes(owner_bytes, "owner authority input")
    validate_schema(owner_document, _load_schema("schemas/owner_authority.schema.json"))
    _validate_session_id(approval["session_id"])
    require(evidence_root.is_absolute() and evidence_root.name == approval["session_id"], "evidence root is not the approved fresh session root")
    approved_evidence_root = Path(approval["evidence"]["evidence_root"])
    require(approved_evidence_root.is_absolute() and not approved_evidence_root.is_symlink(), "approved evidence root is not an explicit path")
    require(approved_evidence_root == evidence_root, "evidence root differs from the approval binding")
    require(registry_path.is_absolute() and registry_path == Path(approval["evidence"]["one_shot_registry_path"]), "one-shot registry path mismatch")
    _validate_fresh_directory_target(evidence_root, label="fresh evidence root")
    require(not registry_path.exists(), "one-shot approval registry already exists")
    control_start = _utc_datetime(approval["lease"]["control_plane_start_utc"], "control-plane start")
    lease_start = _utc_datetime(approval["lease"]["lease_start_utc"], "lease start")
    deadline = _utc_datetime(approval["lease"]["lease_deadline_utc"], "lease deadline")
    require(control_start == lease_start, "control-plane start and lease start differ")
    require(deadline - lease_start == timedelta(seconds=21600), "lease window is not exactly six hours")
    current = _fresh_now_utc() if now_utc is None else now_utc
    require(lease_start <= current < deadline, "approval lease is not currently active")
    required_remaining = package.plan["outer_timeout_seconds"] + 60
    require((deadline - current).total_seconds() >= required_remaining, "approval lease lacks the required D0 reserve")
    require(approval["lease"]["maximum_additional_spend_amount"] == "0" and approval["lease"]["maximum_additional_spend_currency"] == "TWD", "approval cost authority changed")
    require(approval["lease"]["extension_allowed"] is False and approval["lease"]["top_up_allowed"] is False and approval["lease"]["provider_grace_credit_seconds"] == 0, "approval lifecycle/cost authority changed")
    require(owner_document["time_origin"]["lease_start_utc"] == approval["lease"]["lease_start_utc"], "owner/approval lease start mismatch")
    require(owner_document["time_origin"]["lease_deadline_utc"] == approval["lease"]["lease_deadline_utc"], "owner/approval lease deadline mismatch")
    require(owner_document["cost_authority"]["maximum_additional_spend"] == {"amount": "0", "currency": "TWD"}, "owner authority spend changed")
    known_hosts = _validate_known_hosts(approval)
    ssh_executable = _validate_local_ssh(approval, agent_listing_provider=agent_listing_provider)
    require(approval["remote_payload_sha256"] == sha256_bytes(package.probe_source), "remote payload hash mismatch")
    require(approval["runtime"]["approved_container_digest"] is None and approval["runtime"]["digest_status"] == "UNOBSERVED_DISCOVERY_ONLY", "discovery-only digest policy changed")
    command_preimage = build_command_preimage(package, approval, known_hosts=known_hosts, ssh_executable=ssh_executable)
    command_hash = semantic_sha256(command_preimage)
    require(approval["command_binding_sha256"] == command_hash, "COMMAND_PREIMAGE_DRIFT")
    return known_hosts, ssh_executable, owner_bytes, command_preimage, command_hash


def one_shot_registry_bytes(approval: dict[str, Any], approval_bytes: bytes) -> bytes:
    payload = {
        "schema_version": "moe-simulator-phase7-gputw-d0-r3-used-approval-v1",
        "approval_id": approval["approval_id"],
        "session_id": approval["session_id"],
        "approval_sha256": sha256_bytes(approval_bytes),
    }
    return canonical_bytes(payload)


def consume_one_shot_registry(approval: dict[str, Any], approval_bytes: bytes, expected_bytes: bytes | None = None) -> tuple[bytes, str]:
    registry_path = Path(approval["evidence"]["one_shot_registry_path"])
    parent = registry_path.parent
    require(parent.is_absolute() and parent.exists() and not parent.is_symlink(), "one-shot registry parent is not a real directory")
    encoded = one_shot_registry_bytes(approval, approval_bytes)
    if expected_bytes is not None:
        require(encoded == expected_bytes, "one-shot registry preimage drift")
    write_exclusive(registry_path, encoded)
    return encoded, sha256_bytes(encoded)


def _descendants(root_pid: int) -> set[int]:
    descendants: set[int] = set()
    changed = True
    while changed:
        changed = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat_line = (entry / "stat").read_text(encoding="ascii")
                after_name = stat_line.rsplit(") ", 1)[1].split()
                parent = int(after_name[1])
                pid = int(entry.name)
            except (OSError, ValueError, IndexError):
                continue
            if parent == root_pid or parent in descendants:
                if pid != root_pid and pid not in descendants:
                    descendants.add(pid)
                    changed = True
    return descendants


def _signal_known_pids(pids: set[int], sig: signal.Signals) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _live_pid(pid: int) -> bool:
    """Return false for a reaped process or a zombie, without following links."""

    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
    except (FileNotFoundError, OSError, IndexError):
        return False
    return bool(fields) and fields[0] != "Z"


def _terminate_tree(process: subprocess.Popen[bytes], *, term_grace: float, kill_grace: float) -> None:
    """Terminate the process group and descendants, including fast parent exit.

    A detached child can outlive the process-group leader immediately after the
    leader receives SIGTERM.  We therefore retain the initial descendant set,
    refresh it while the leader is alive, and hard-kill every known PID at the
    term boundary even when ``wait()`` would otherwise return successfully.
    """

    known = _descendants(process.pid)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _signal_known_pids(known, signal.SIGTERM)
    term_deadline = time.monotonic() + term_grace
    while time.monotonic() < term_deadline and process.poll() is None:
        known.update(_descendants(process.pid))
        time.sleep(0.01)

    # Capture one final descendant snapshot before the group leader can be
    # reparented, then hard-kill the original and refreshed sets unconditionally.
    known.update(_descendants(process.pid))
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _signal_known_pids(known, signal.SIGKILL)
    kill_deadline = time.monotonic() + kill_grace
    try:
        process.wait(timeout=kill_grace)
    except subprocess.TimeoutExpired as exc:
        raise D0R2Error("process tree survived kill boundary") from exc

    # A child may have ignored SIGTERM and been reparented after the leader
    # exited; known PIDs remain the only safe bounded set to revisit.
    survivors = {pid for pid in known if _live_pid(pid)}
    while survivors and time.monotonic() < kill_deadline:
        _signal_known_pids(survivors, signal.SIGKILL)
        time.sleep(0.01)
        survivors = {pid for pid in known if _live_pid(pid)}
    require(not survivors, f"process tree survived kill boundary: {sorted(survivors)}")


@dataclass(frozen=True)
class TransportResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_limited: bool
    elapsed_monotonic_ns: int


def run_bounded_process(argv: list[str], payload: bytes, *, env_overrides: dict[str, str], timeout_seconds: int, max_stdout_bytes: int, max_stderr_bytes: int) -> TransportResult:
    started = time.monotonic_ns()
    environment = os.environ.copy()
    environment.update(env_overrides)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    streams = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    limits = {stdout_fd: max_stdout_bytes, stderr_fd: max_stderr_bytes}
    os.set_blocking(process.stdin.fileno(), False)
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    input_offset = 0
    timed_out = False
    output_limited = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_tree(process, term_grace=5, kill_grace=5)
                break
            events = selector.select(remaining)
            if not events:
                timed_out = True
                _terminate_tree(process, term_grace=5, kill_grace=5)
                break
            for key, mask in events:
                stream = key.fileobj
                if key.data == "stdin" and mask & selectors.EVENT_WRITE:
                    if input_offset < len(payload):
                        try:
                            input_offset += os.write(stream.fileno(), payload[input_offset:input_offset + 65536])
                        except BlockingIOError:
                            continue
                    if input_offset >= len(payload):
                        selector.unregister(stream)
                        stream.close()
                elif key.data in {"stdout", "stderr"} and mask & selectors.EVENT_READ:
                    try:
                        block = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    buffer = streams[stream.fileno()]
                    buffer.extend(block)
                    if len(buffer) > limits[stream.fileno()]:
                        output_limited = True
                        _terminate_tree(process, term_grace=5, kill_grace=5)
                        break
            if timed_out or output_limited:
                break
        if process.poll() is None:
            _terminate_tree(process, term_grace=5, kill_grace=5)
        returncode = process.wait(timeout=1)
    finally:
        for key in list(selector.get_map().values()):
            try:
                selector.unregister(key.fileobj)
            except Exception:
                pass
            try:
                key.fileobj.close()
            except Exception:
                pass
        selector.close()
    return TransportResult(returncode, bytes(streams[stdout_fd]), bytes(streams[stderr_fd]), timed_out, output_limited, time.monotonic_ns() - started)


def _result_identity_preimage(*, package: PackageInputs, approval: dict[str, Any], approval_bytes: bytes, registry_bytes: bytes, command_binding_sha256: str, retained_input_hashes: dict[str, str], probe_result_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": "moe-simulator-phase7-gputw-d0-r3-result-evidence-identity-preimage-v1",
        "application_id": package.overlay["application_id"],
        "application_identity_sha256": package.application_identity_sha256,
        "application_ledger_sha256": package.application_ledger_sha256,
        "reviewed_commit": approval["reviewed_commit"],
        "reviewed_tree": approval["reviewed_tree"],
        "command_binding_sha256": command_binding_sha256,
        "owner_authority_sha256": approval["owner_authority_sha256"],
        "owner_approval_sha256": sha256_bytes(approval_bytes),
        "session_id": approval["session_id"],
        "one_shot_registry_sha256": sha256_bytes(registry_bytes),
        "retained_input_hashes": dict(sorted(retained_input_hashes.items())),
        "probe_input_sha256": sha256_bytes(package.probe_source),
        "probe_result_sha256": probe_result_sha256,
    }


def _terminal_ledger(evidence_root: Path, outcome: str, *, application_identity_sha256: str, result_identity_sha256: str) -> dict[str, Any]:
    excluded = {"terminal_ledger.json", "terminal_ledger.json.staged", "terminal_status.txt", "terminal_status.txt.staged"}
    members: list[dict[str, Any]] = []
    for path in sorted(evidence_root.rglob("*")):
        if path.is_symlink():
            raise D0R2Error(f"terminal evidence symlink is forbidden: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(evidence_root).as_posix()
        require(relative not in excluded, f"stale terminal artifact: {relative}")
        members.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": file_sha256(path)})
    status = TERMINAL_MARKERS[outcome].encode()
    members.append({"path": "terminal_status.txt", "size_bytes": len(status), "sha256": sha256_bytes(status)})
    members.sort(key=lambda item: item["path"].encode())
    ledger: dict[str, Any] = {
        "schema_version": "moe-simulator-phase7-gputw-d0-r3-terminal-ledger-v1",
        "terminal_status": outcome,
        "terminal_marker": TERMINAL_MARKERS[outcome].strip(),
        "member_count": len(members),
        "members": members,
        "application_identity_sha256": application_identity_sha256,
        "result_evidence_identity_sha256": result_identity_sha256,
    }
    ledger["ledger_sha256"] = semantic_sha256(ledger)
    return ledger


def verify_terminal(evidence_root: Path) -> dict[str, Any]:
    ledger, _ = load_json(evidence_root / "terminal_ledger.json")
    validate_schema(ledger, _load_schema("schemas/terminal_ledger.schema.json"))
    digest_input = dict(ledger)
    claimed = digest_input.pop("ledger_sha256")
    require(claimed == semantic_sha256(digest_input), "terminal ledger digest mismatch")
    paths = [item["path"] for item in ledger["members"]]
    require(paths == sorted(paths, key=lambda item: item.encode()) and len(paths) == len(set(paths)), "terminal ledger paths are not exact lexical set")
    actual: set[str] = set()
    for path in evidence_root.rglob("*"):
        if path.is_file() and path.name != "terminal_ledger.json":
            actual.add(path.relative_to(evidence_root).as_posix())
    require(set(paths) == actual and "terminal_status.txt" in actual, "terminal ledger exact set mismatch")
    for item in ledger["members"]:
        path = evidence_root / item["path"]
        require(path.is_file() and not path.is_symlink(), f"terminal member missing: {item['path']}")
        require(path.stat().st_size == item["size_bytes"] and file_sha256(path) == item["sha256"], f"terminal member drift: {item['path']}")
        require(path.stat().st_mode & 0o222 == 0, f"terminal member remains writable: {item['path']}")
    require((evidence_root / "terminal_status.txt").read_text(encoding="utf-8") == TERMINAL_MARKERS[ledger["terminal_status"]], "terminal marker mismatch")
    return ledger


def _publish_terminal(evidence_root: Path, outcome: str, *, application_identity_sha256: str, result_identity_sha256: str) -> dict[str, Any]:
    ledger = _terminal_ledger(evidence_root, outcome, application_identity_sha256=application_identity_sha256, result_identity_sha256=result_identity_sha256)
    ledger_bytes = canonical_bytes(ledger)
    write_exclusive(evidence_root / "terminal_ledger.json.staged", ledger_bytes)
    write_exclusive(evidence_root / "terminal_status.txt.staged", TERMINAL_MARKERS[outcome].encode())
    descriptor = os.open(str(evidence_root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    for staged, final in (("terminal_ledger.json.staged", "terminal_ledger.json"), ("terminal_status.txt.staged", "terminal_status.txt")):
        try:
            os.link(evidence_root / staged, evidence_root / final)
            (evidence_root / staged).unlink()
        except FileExistsError as exc:
            raise D0R2Error(f"terminal artifact already exists: {final}") from exc
    for path in [evidence_root, *evidence_root.rglob("*")]:
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            path.chmod(0o444)
    verify_terminal(evidence_root)
    return ledger


def _write_package_inputs(evidence_root: Path, package: PackageInputs, approval_bytes: bytes, registry_bytes: bytes, owner_authority_bytes: bytes, known_hosts: bytes, ssh_executable: bytes, command_preimage: dict[str, Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative, payload in sorted(package.package_bytes.items()):
        target = evidence_root / "inputs" / "package" / relative
        write_exclusive(target, payload)
        hashes[target.relative_to(evidence_root).as_posix()] = sha256_bytes(payload)
    approval = load_json_bytes(approval_bytes, "captured approval")
    provenance_bytes = read_once(Path(approval["host_key"]["provenance_artifact_path"]).resolve(strict=True), max_bytes=256 * 1024)
    agent_query_bytes = read_once(Path(approval["credential"]["agent_query_executable_path"]).resolve(strict=True), max_bytes=64 * 1024 * 1024)
    static_inputs = {
        "inputs/approval.json": approval_bytes,
        "inputs/one_shot_registry.json": registry_bytes,
        "inputs/owner_authority.json": owner_authority_bytes,
        "inputs/known_hosts": known_hosts,
        "inputs/local_ssh_identity.json": canonical_bytes({"path": "CAPTURED_FROM_APPROVAL", "sha256": sha256_bytes(ssh_executable), "private_key_material": "FORBIDDEN_NOT_RECORDED"}),
        "inputs/host_key_provenance.json": provenance_bytes,
        "inputs/agent_query_executable": agent_query_bytes,
        "inputs/command_preimage.json": canonical_bytes(command_preimage),
        "inputs/probe_source.py": package.probe_source,
    }
    for relative, payload in static_inputs.items():
        target = evidence_root / relative
        write_exclusive(target, payload)
        hashes[relative] = sha256_bytes(payload)
    return dict(sorted(hashes.items()))


def _base_terminal_fields(package: PackageInputs, approval: dict[str, Any], approval_bytes: bytes, registry_bytes: bytes, command_hash: str, retained: dict[str, str], probe_result_hash: str, start_utc: str, end_utc: str, elapsed_ns: int, timed_out: bool) -> dict[str, Any]:
    identity_preimage = _result_identity_preimage(package=package, approval=approval, approval_bytes=approval_bytes, registry_bytes=registry_bytes, command_binding_sha256=command_hash, retained_input_hashes=retained, probe_result_sha256=probe_result_hash)
    return {
        "application_id": package.overlay["application_id"],
        "application_identity_sha256": package.application_identity_sha256,
        "application_ledger_sha256": package.application_ledger_sha256,
        "reviewed_commit": approval["reviewed_commit"],
        "reviewed_tree": approval["reviewed_tree"],
        "command_binding_sha256": command_hash,
        "owner_authority_sha256": approval["owner_authority_sha256"],
        "owner_approval_sha256": sha256_bytes(approval_bytes),
        "session_id": approval["session_id"],
        "one_shot_registry_sha256": sha256_bytes(registry_bytes),
        "retained_input_hashes": retained,
        "probe_input_sha256": sha256_bytes(package.probe_source),
        "probe_result_sha256": probe_result_hash,
        "result_evidence_identity_sha256": semantic_sha256(identity_preimage),
        "timing": {
            "controller_start_utc": start_utc,
            "controller_end_utc": end_utc,
            "elapsed_monotonic_ns": elapsed_ns,
            "outer_timeout_seconds": 300,
            "timed_out": timed_out,
        },
    }


def _probe_eligibility_findings(probe: dict[str, Any], approval: dict[str, Any]) -> list[str]:
    findings: list[str] = []

    def missing(label: str, value: Any) -> None:
        if value in (None, ""):
            findings.append(f"PROBE_FIELD_UNOBSERVED:{label}")

    for label, value in (
        ("provider.instance_id", probe["provider"]["instance_id"]),
        ("instance.principal", probe["instance"]["principal"]),
        ("host.hostname", probe["host"]["hostname"]),
        ("host.os_release", probe["host"]["os_release"]),
        ("host.kernel_release", probe["host"]["kernel_release"]),
        ("host.boot_id", probe["host"]["boot_id"]),
        ("host.python.path", probe["host"]["python"]["path"]),
        ("host.python.sha256", probe["host"]["python"]["sha256"]),
        ("host.timeout.path", probe["host"]["timeout"]["path"]),
        ("host.timeout.sha256", probe["host"]["timeout"]["sha256"]),
        ("runtime.container_image", probe["runtime"]["container_image"]),
        ("runtime.container_digest", probe["runtime"]["container_digest"]),
        ("runtime.cuda", probe["runtime"]["cuda"]),
        ("runtime.driver", probe["runtime"]["driver"]),
        ("storage.vault.mount_identity_sha256", probe["storage"]["vault"]["mount_identity_sha256"]),
        ("storage.vault.free_bytes", probe["storage"]["vault"]["free_bytes"]),
    ):
        missing(label, value)
    if probe["gpu"]["query_status"] != "COMPLETE" or probe["gpu"]["count"] != 1:
        findings.append("GPU_IDENTITY_OR_QUERY_INCOMPLETE")
    vllm = probe["runtime"]["vllm"]
    if not vllm["present"]:
        findings.append("VLLM_NOT_PRESENT")
    else:
        missing("runtime.vllm.version", vllm["version"])
        missing("runtime.vllm.path", vllm["path"])
        missing("runtime.vllm.distribution_sha256", vllm["distribution_sha256"])
    if probe["runtime"]["container_digest"] in (None, "", "UNOBSERVED"):
        findings.insert(0, "CONTAINER_DIGEST_UNOBSERVED")
    if approval["runtime"]["approved_container_digest"] is None:
        findings.append("OWNER_D0_R3_IS_DISCOVERY_ONLY")
    return list(dict.fromkeys(findings))


Runner = Callable[[list[str], bytes, dict[str, str], int, int, int], TransportResult]


def run_session(
    package_dir: Path,
    approval_path: Path,
    evidence_root: Path,
    *,
    runner: Runner | None = None,
    now_utc: datetime | None = None,
    agent_listing_provider: Callable[[dict[str, Any]], bytes] | None = None,
) -> dict[str, Any]:
    package = load_package(package_dir)
    approval, approval_raw = load_json(approval_path)
    registry_path = Path(approval.get("evidence", {}).get("one_shot_registry_path", ""))
    known_hosts, ssh_executable, owner_authority_bytes, command_preimage, command_hash = _validate_approval(
        package,
        approval,
        evidence_root=evidence_root,
        registry_path=registry_path,
        now_utc=now_utc,
        agent_listing_provider=agent_listing_provider,
    )
    # Establish the fresh evidence root and capture every deterministic input
    # before consuming the one-shot authority.  After consumption, all work is
    # inside the terminal-sealing try block, so a failure cannot strand a used
    # approval without a terminal result.
    evidence_root.mkdir(mode=0o700, parents=False)
    planned_registry_bytes = one_shot_registry_bytes(approval, approval_raw)
    retained = _write_package_inputs(evidence_root, package, approval_raw, planned_registry_bytes, owner_authority_bytes, known_hosts, ssh_executable, command_preimage)
    argv, env_overrides = build_ssh_argv(approval)
    transport_runner = runner or (lambda a, p, e, t, so, se: run_bounded_process(a, p, env_overrides=e, timeout_seconds=t, max_stdout_bytes=so, max_stderr_bytes=se))
    registry_bytes = b""
    registry_consumed = False
    transport = TransportResult(-1, b"", b"", False, False, 0)
    outcome = "COMPLETE"
    failure_type = ""
    failure_message = ""
    start_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        registry_bytes, _registry_hash = consume_one_shot_registry(approval, approval_raw, expected_bytes=planned_registry_bytes)
        registry_consumed = True
        transport = transport_runner(argv, package.probe_source, env_overrides, package.plan["outer_timeout_seconds"], package.plan["max_stdout_bytes"], package.plan["max_stderr_bytes"])
        write_exclusive(evidence_root / "transport" / "stdout.bin", transport.stdout)
        write_exclusive(evidence_root / "transport" / "stderr.bin", transport.stderr)
        probe_result_hash = sha256_bytes(transport.stdout)
        if transport.timed_out:
            outcome, failure_type, failure_message = "INCOMPLETE", "D0_OUTER_TIMEOUT", "bounded D0 controller exceeded 300-second outer envelope"
        elif transport.output_limited:
            outcome, failure_type, failure_message = "FAILED", "D0_OUTPUT_LIMIT", "D0 stdout or stderr exceeded the bounded output contract"
        elif transport.returncode != 0:
            outcome, failure_type, failure_message = "FAILED", "D0_TRANSPORT", f"exact transport returned {transport.returncode}"
        else:
            probe = load_json_bytes(transport.stdout, "D0 probe stdout")
            validate_schema(probe, _load_schema("schemas/probe.schema.json"))
            findings = _probe_eligibility_findings(probe, approval)
            eligibility = "READY_FOR_MATERIALIZATION_APPLICATION" if not findings else "NOT_READY"
            end_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            fields = _base_terminal_fields(package, approval, approval_raw, registry_bytes, command_hash, retained, probe_result_hash, start_utc, end_utc, transport.elapsed_monotonic_ns, transport.timed_out)
            result = {
                "schema_version": "moe-simulator-phase7-gputw-d0-r3-result-v1",
                "terminal_status": "COMPLETE",
                **fields,
                "environment_eligibility": eligibility,
                "eligibility_findings": findings,
                "transport": {"returncode": transport.returncode, "stdout_sha256": sha256_bytes(transport.stdout), "stderr_sha256": sha256_bytes(transport.stderr), "stdout_bytes": len(transport.stdout), "stderr_bytes": len(transport.stderr), "output_limited": False},
                "next_legal_action": "CREATE_NEW_PROMOTABLE_D0_APPLICATION" if findings else "STOP_AND_REQUEST_SEPARATE_MATERIALIZATION_APPROVAL",
            }
            validate_schema(result, _load_schema("schemas/result.schema.json"))
            write_json_exclusive(evidence_root / "d0_result.json", result)
            _publish_terminal(evidence_root, "COMPLETE", application_identity_sha256=package.application_identity_sha256, result_identity_sha256=result["result_evidence_identity_sha256"])
            return result
    except (D0R2Error, OSError) as exc:
        if not registry_consumed:
            raise
        outcome, failure_type, failure_message = "FAILED", "D0_EVIDENCE", str(exc)
        probe_result_hash = sha256_bytes(transport.stdout)
    end_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fields = _base_terminal_fields(package, approval, approval_raw, registry_bytes, command_hash, retained, probe_result_hash, start_utc, end_utc, transport.elapsed_monotonic_ns, transport.timed_out)
    failure = {
        "schema_version": "moe-simulator-phase7-gputw-d0-r3-failure-v1",
        "terminal_status": outcome,
        "failure_type": failure_type or "D0_FAILURE",
        "failure": failure_message or "D0 controller failed",
        **fields,
        "retry_allowed": False,
        "resume_allowed": False,
        "gpu_workload_performed": False,
    }
    validate_schema(failure, _load_schema("schemas/failure.schema.json"))
    write_json_exclusive(evidence_root / "d0_failure.json", failure)
    _publish_terminal(evidence_root, outcome, application_identity_sha256=package.application_identity_sha256, result_identity_sha256=failure["result_evidence_identity_sha256"])
    return failure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, default=PACKAGE_ROOT)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--verify-terminal", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.verify_terminal is not None:
            print(json.dumps(verify_terminal(args.verify_terminal), sort_keys=True))
            return 0
        require(args.approval is not None and args.evidence_root is not None, "live controller requires --approval and --evidence-root")
        require(os.environ.get("MOE_PHASE7_D0_R3_UNLOCK") == "OWNER_APPROVED_EXACT_D0_R3_COMMAND", "missing exact D0-R3 second-factor unlock")
        result = run_session(args.package_dir, args.approval, args.evidence_root)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (D0R2Error, OSError, ValueError) as exc:
        print(f"HARD-STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
