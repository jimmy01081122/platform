#!/usr/bin/env python3
"""Validate the D0-S2 CPU-only package without SSH, network or GPU work."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class D0S2Error(RuntimeError):
    """A package contract or evidence error."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise D0S2Error(message)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise D0S2Error(f"value is not canonical JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object, parse_float=_reject_float, parse_constant=_reject_float)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise D0S2Error(f"invalid JSON {path}: {exc}") from exc
    require(isinstance(value, dict), f"JSON root must be an object: {path}")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> None:
    raise D0S2Error(f"floating-point JSON value is forbidden: {value}")


def validate_schema(value: Any, schema: dict[str, Any], *, root: dict[str, Any] | None = None, path: str = "$") -> None:
    root = schema if root is None else root
    if "$ref" in schema:
        ref = schema["$ref"]
        require(ref.startswith("#/"), f"external schema reference is forbidden: {ref}")
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        require(isinstance(target, dict), f"schema reference is not an object: {ref}")
        validate_schema(value, target, root=root, path=path)
        return
    if "const" in schema:
        require(value == schema["const"], f"{path}: expected {schema['const']!r}")
    if "enum" in schema:
        require(value in schema["enum"], f"{path}: enum mismatch")
    if "type" in schema:
        expected = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        def matches(kind: str) -> bool:
            return {
                "object": isinstance(value, dict),
                "array": isinstance(value, list),
                "string": isinstance(value, str),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
                "null": value is None,
            }.get(kind, False)
        require(any(matches(kind) for kind in expected), f"{path}: type mismatch")
    if isinstance(value, str):
        if "minLength" in schema:
            require(len(value) >= schema["minLength"], f"{path}: too short")
        if "pattern" in schema:
            require(re.search(schema["pattern"], value) is not None, f"{path}: pattern mismatch")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema:
        require(value >= schema["minimum"], f"{path}: below minimum")
    if isinstance(value, list):
        if "minItems" in schema:
            require(len(value) >= schema["minItems"], f"{path}: too few items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], root=root, path=f"{path}[{index}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            require(key in value, f"{path}: missing {key}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in properties:
                validate_schema(child, properties[key], root=root, path=f"{path}.{key}")
            elif additional is False:
                raise D0S2Error(f"{path}: unexpected property {key}")
            elif isinstance(additional, dict):
                validate_schema(child, additional, root=root, path=f"{path}.{key}")


def parse_ledger(payload: bytes) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in payload.decode("utf-8").splitlines():
        require("  " in line, "malformed application ledger line")
        digest, member = line.split("  ", 1)
        require(SHA256_RE.fullmatch(digest) is not None, f"invalid digest for {member}")
        require(member and not member.startswith("/") and ".." not in Path(member).parts, f"unsafe ledger member {member}")
        rows.append((member, digest))
    require([member for member, _ in rows] == sorted((member for member, _ in rows), key=lambda item: item.encode()), "ledger is not lexical")
    require(len(rows) == len({member for member, _ in rows}), "ledger contains duplicate members")
    return rows


def validate_identity(identity: dict[str, Any]) -> str:
    expected = {"schema_version", "application_id", "identity_status", "identity_preimage", "application_identity_sha256"}
    require(set(identity) == expected, "application identity key closure mismatch")
    require(identity["schema_version"] == "moe-simulator-phase7-gputw-d0-s2-application-identity-v1", "identity schema mismatch")
    require(identity["application_id"] == "phase7-gputw-d0-s2-20260809", "identity ID mismatch")
    require(identity["identity_status"] == "FROZEN_CPU_ONLY_CANDIDATE", "identity status mismatch")
    digest = sha256_bytes(canonical_bytes(identity["identity_preimage"]))
    require(identity["application_identity_sha256"] == digest, "application identity semantic hash mismatch")
    return digest


def validate_package(root: Path = PACKAGE_ROOT) -> dict[str, Any]:
    root = root.resolve(strict=True)
    manifest = load_json(root / "application_manifest.json")
    require(manifest.get("schema_version") == "moe-simulator-phase7-gputw-d0-s2-application-manifest-v1", "manifest schema mismatch")
    require(manifest.get("application_id") == "phase7-gputw-d0-s2-20260809", "manifest ID mismatch")
    require(manifest.get("status") == "CPU_ONLY_DISCOVERY_PACKAGE", "manifest status mismatch")
    members = manifest.get("ledger_members")
    require(isinstance(members, list) and members == sorted(members, key=lambda item: item.encode()), "manifest members are not lexical")
    require(manifest.get("ledger_exclusions") == ["application_ledger.sha256"], "manifest exclusions changed")
    expected_files = set(members) | {"application_ledger.sha256"}
    actual_files: set[str] = set()
    for entry in root.rglob("*"):
        relative = entry.relative_to(root).as_posix()
        require(not entry.is_symlink(), f"package symlink is forbidden: {relative}")
        mode = entry.lstat().st_mode
        if stat.S_ISREG(mode):
            actual_files.add(relative)
        elif stat.S_ISDIR(mode):
            continue
        else:
            raise D0S2Error(f"package special file is forbidden: {relative}")
    require(actual_files == expected_files, f"package exact-set mismatch missing={sorted(expected_files - actual_files)} extra={sorted(actual_files - expected_files)}")
    ledger_payload = (root / "application_ledger.sha256").read_bytes()
    rows = parse_ledger(ledger_payload)
    require([member for member, _ in rows] == members, "ledger set differs from manifest")
    for member, expected in rows:
        actual = sha256_bytes((root / member).read_bytes())
        require(actual == expected, f"ledger hash mismatch: {member}")
    identity_digest = validate_identity(load_json(root / "application_identity.json"))
    overlay = load_json(root / "overlay.json")
    plan = load_json(root / "d0_s2_plan.json")
    require(overlay.get("authority") == {"d0": "NOT_AUTHORIZED", "gate_m": "NOT_AUTHORIZED", "m0": "NOT_AUTHORIZED", "gpu": "NONE"}, "overlay authority changed")
    require(plan.get("authority") == overlay.get("authority"), "plan/overlay authority mismatch")
    provenance = overlay["provenance"]
    require(provenance.get("local_cpu_probe_allowed") is True, "local CPU probe permission drift")
    require(provenance.get("provider_ssh_attempted") is False, "provider SSH prohibition drift")
    require(provenance.get("model_download_performed") is False, "model download prohibition drift")
    require(provenance.get("gpu_workload_performed") is False, "GPU workload prohibition drift")
    for schema_name in ("schemas/probe.schema.json", "schemas/classification.schema.json", "schemas/evidence_ledger.schema.json"):
        schema = load_json(root / schema_name)
        require(isinstance(schema.get("$id"), str) and schema["$id"], f"schema missing $id: {schema_name}")
    # Importability is checked without running the probe or accessing a GPU.
    import importlib.util
    for module_name in ("probe", "classifier"):
        spec = importlib.util.spec_from_file_location(f"d0_s2_{module_name}", root / f"{module_name}.py")
        require(spec is not None and spec.loader is not None, f"cannot import {module_name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return {
        "status": "PASS",
        "package_status": manifest["status"],
        "application_id": manifest["application_id"],
        "application_identity_sha256": identity_digest,
        "d0_execution": "NOT_AUTHORIZED",
        "gate_m": "NOT_AUTHORIZED",
        "m0": "NOT_AUTHORIZED",
        "gpu_authority": "NONE",
        "promotion": "NOT_PROMOTABLE_NONFORMAL_DISCOVERY",
        "network_free": True,
        "ssh_attempted": False,
        "model_accessed": False,
        "gpu_queried": False,
        "remote_writes": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, default=PACKAGE_ROOT)
    args = parser.parse_args(argv)
    try:
        require(args.package_dir.resolve() == PACKAGE_ROOT.resolve(), "validator only accepts the immutable D0-S2 package root")
        result = validate_package()
    except (D0S2Error, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
