#!/usr/bin/env python3
"""Build, publish, and replay a bounded Gate M evidence export.

The export deliberately excludes model weights and credentials.  It copies only
the approved authority projection, deployment receipt, terminal evidence,
model ledger, prompt fixture, static runtime provenance, and hash-only command
log inventory.  A marker outside the export directory is published last.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    exact_regular_file_set,
    file_sha256,
    load_json,
    load_json_bytes,
    semantic_sha256,
    validate_contract,
    verify_model_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _rename_noreplace,
    _validate_member_path,
    verify_install,
)
from explorations.moe_cycle_simulator.phase7.application.executor.materialization_driver import (  # noqa: E402
    verify_materialization_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_provenance import (  # noqa: E402
    verify_runtime_provenance,
)


EXPORT_SCHEMA = "moe-simulator-phase7-gate-m-export-manifest-v1"
TRANSPORT_SCHEMA = "moe-simulator-phase7-gate-m-export-transport-v1"
STATUS_SCHEMA = "moe-simulator-phase7-gate-m-export-status-v1"
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_TRANSPORT_BYTES = 96 * 1024 * 1024
TRANSPORT_FRAME_MAGIC = b"MOE_GATE_M_EXPORT_V1"
MAX_TRANSPORT_HEADER_BYTES = 128
MAX_TRANSPORT_FRAME_BYTES = MAX_TRANSPORT_HEADER_BYTES + MAX_TRANSPORT_BYTES


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular(path: Path, label: str) -> bytes:
    if not path.exists():
        raise M0Error(f"Gate M export source is unavailable: {label}")
    observed = path.lstat()
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or path.resolve(strict=True) != path
        or observed.st_size > MAX_MEMBER_BYTES
    ):
        raise M0Error(f"Gate M export source is unsafe or oversized: {label}")
    payload = path.read_bytes()
    after = path.lstat()
    if (
        len(payload) != observed.st_size
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
    ):
        raise M0Error(f"Gate M export source changed while read: {label}")
    return payload


def _write_fresh(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o444,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _status_payload(manifest: dict[str, Any]) -> bytes:
    return _canonical(
        {
            "schema_version": STATUS_SCHEMA,
            "status": "GATE_M_EXPORT_COMPLETE_REPLAYED",
            "manifest_sha256": manifest["manifest_sha256"],
            "member_count": manifest["member_count"],
            "total_size_bytes": manifest["total_size_bytes"],
        }
    ) + b"\n"


def _command_log_inventory(materialization_root: Path) -> dict[str, Any]:
    logs = materialization_root / "logs"
    members: list[dict[str, Any]] = []
    if logs.exists():
        if logs.is_symlink() or not logs.is_dir():
            raise M0Error("materialization log root is unsafe")
        for relative in sorted(exact_regular_file_set(logs)):
            path = logs / relative
            members.append(
                {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
            )
    value: dict[str, Any] = {
        "schema_version": "moe-simulator-phase7-command-log-inventory-v1",
        "member_count": len(members),
        "members": members,
    }
    value["ledger_sha256"] = semantic_sha256(value)
    return value


def _source_map(
    *,
    application: Path,
    receipt: Path,
    materialization_root: Path,
    runtime_provenance_root: Path,
    materialization_plan: dict[str, Any],
) -> dict[str, bytes]:
    paths = materialization_plan["paths"]
    sources = {
        "authority/materialization_authority_projection.json": application
        / "materialization_approval.template.json",
        "deployment/deployment_receipt.json": receipt,
        "model/model_ledger.json": Path(paths["model_ledger"]),
        "fixtures/capacity_prompt_fixture.json": Path(
            paths["capacity_prompt_fixture"]
        ),
    }
    for relative in sorted(exact_regular_file_set(materialization_root)):
        sources[f"materialization/{relative}"] = materialization_root / relative
    for relative in sorted(exact_regular_file_set(runtime_provenance_root)):
        sources[f"runtime/{relative}"] = runtime_provenance_root / relative
    result: dict[str, bytes] = {}
    for logical, path in sources.items():
        result[logical] = _read_regular(path, logical)
    approval = load_json(application / "materialization_approval.template.json")
    result["d0/d0_binding.json"] = _canonical(
        {
            "schema_version": "moe-simulator-phase7-gate-m-d0-binding-v1",
            "approved_d0_result_sha256": approval["approved_d0_result_sha256"],
            "approved_vault_mount_identity_sha256": approval[
                "approved_vault_mount_identity_sha256"
            ],
            "approved_ssh_host_key_sha256": approval[
                "approved_ssh_host_key_sha256"
            ],
        }
    )
    result["logs/command_log_inventory.json"] = _canonical(
        _command_log_inventory(materialization_root)
    )
    return result


def build_and_publish_export(
    *,
    application: Path,
    receipt: Path,
    materialization_root: Path,
    runtime_provenance_root: Path,
    export_root: Path,
    status_path: Path,
) -> dict[str, Any]:
    application = application.resolve(strict=True)
    contract = load_json(application / "m0_execution_contract.json")
    validate_contract(contract)
    materialization_plan = load_json(
        application / "materialization_plan.template.json"
    )
    project = Path(
        materialization_plan["storage_contract"]["persistent_project_root"]
    )
    expected_receipt = Path(materialization_plan["deployment"]["deployment_receipt"])
    if receipt != expected_receipt:
        raise M0Error("Gate M export receipt path differs from the frozen plan")
    verify_install(
        allowed_root=Path(
            materialization_plan["storage_contract"]["persistent_mount"]
        ),
        target=Path(materialization_plan["deployment"]["application_target"]),
        receipt=receipt,
    )
    materialization_ledger = verify_materialization_terminal(materialization_root)
    if materialization_ledger.get("terminal_status") != "COMPLETE_HARD_STOP":
        raise M0Error("Gate M export requires complete materialization evidence")
    runtime_ledger = verify_runtime_provenance(runtime_provenance_root)
    if runtime_ledger.get("terminal_status") not in {"COMPLETE", "BLOCKED"}:
        raise M0Error("Gate M export runtime provenance status is invalid")
    paths = materialization_plan["paths"]
    model_ledger_path = Path(paths["model_ledger"])
    fixture_path = Path(paths["capacity_prompt_fixture"])
    stage_result = load_json(materialization_root / "stage_result.json")
    model_ledger = load_json(model_ledger_path)
    fixture = load_json(fixture_path)
    verify_model_ledger(
        Path(paths["snapshot"]), model_ledger, contract=contract
    )
    if (
        stage_result.get("model_ledger_sha256") != model_ledger.get("ledger_sha256")
        or stage_result.get("capacity_prompt_fixture_sha256")
        != file_sha256(fixture_path)
        or fixture.get("model_ledger_sha256") != model_ledger.get("ledger_sha256")
        or fixture.get("token_ids_sha256")
        != semantic_sha256(fixture.get("token_ids"))
    ):
        raise M0Error("Gate M export semantic materialization binding differs")
    if (
        export_root != project / "export/gate-m"
        or status_path != project / "export/gate-m.status"
        or export_root.exists()
        or export_root.is_symlink()
        or status_path.exists()
        or status_path.is_symlink()
    ):
        raise M0Error("Gate M export targets differ or are not fresh")
    parent = export_root.parent.resolve(strict=True)
    if parent != project / "export" or parent.is_symlink():
        raise M0Error("Gate M export parent is unsafe")
    stage = parent / ".gate-m.staged"
    status_stage = parent / ".gate-m.status.staged"
    if stage.exists() or stage.is_symlink() or status_stage.exists() or status_stage.is_symlink():
        raise M0Error("Gate M export staging path is not fresh")
    stage.mkdir(mode=0o700, exist_ok=False)
    payloads = _source_map(
        application=application,
        receipt=receipt,
        materialization_root=materialization_root,
        runtime_provenance_root=runtime_provenance_root,
        materialization_plan=materialization_plan,
    )
    if not payloads or len(payloads) > MAX_MEMBERS:
        raise M0Error("Gate M export member count is outside its bound")
    total = sum(len(payload) for payload in payloads.values())
    if total > MAX_EXPORT_BYTES:
        raise M0Error("Gate M export payload exceeds its bound")
    for relative, payload in sorted(payloads.items()):
        _write_fresh(stage / relative, payload)
    members = [
        {
            "path": relative,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode_octal": "0444",
        }
        for relative, payload in sorted(payloads.items())
    ]
    manifest: dict[str, Any] = {
        "schema_version": EXPORT_SCHEMA,
        "status": "COMPLETE_REPLAYED",
        "member_count": len(members),
        "total_size_bytes": total,
        "members": members,
        "model_weights_included": False,
        "credentials_included": False,
    }
    manifest["manifest_sha256"] = semantic_sha256(manifest)
    _write_fresh(stage / "export_manifest.json", _canonical(manifest))
    for directory in sorted(
        (path for path in stage.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        _fsync_directory(directory)
        directory.chmod(0o555)
    _fsync_directory(stage)
    stage.chmod(0o555)
    _write_fresh(status_stage, _status_payload(manifest))
    _rename_noreplace(stage, export_root)
    _fsync_directory(parent)
    verify_export(export_root, status_path=None)
    _rename_noreplace(status_stage, status_path)
    _fsync_directory(parent)
    verify_export(export_root, status_path=status_path)
    return manifest


def verify_export(export_root: Path, *, status_path: Path | None) -> dict[str, Any]:
    root = export_root.resolve(strict=True)
    if (
        root.is_symlink()
        or not root.is_dir()
        or stat.S_IMODE(root.stat().st_mode) != 0o555
    ):
        raise M0Error("Gate M export root is unsafe or writable")
    manifest_path = root / "export_manifest.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or stat.S_IMODE(manifest_path.stat().st_mode) != 0o444
    ):
        raise M0Error("Gate M export manifest mode differs")
    manifest = load_json(manifest_path)
    base = dict(manifest)
    claimed = base.pop("manifest_sha256", None)
    members = manifest.get("members")
    if (
        set(manifest)
        != {
            "schema_version",
            "status",
            "member_count",
            "total_size_bytes",
            "members",
            "model_weights_included",
            "credentials_included",
            "manifest_sha256",
        }
        or manifest.get("schema_version") != EXPORT_SCHEMA
        or manifest.get("status") != "COMPLETE_REPLAYED"
        or claimed != semantic_sha256(base)
        or not isinstance(members, list)
        or manifest.get("member_count") != len(members)
        or manifest.get("model_weights_included") is not False
        or manifest.get("credentials_included") is not False
    ):
        raise M0Error("Gate M export manifest identity differs")
    paths = [item.get("path") for item in members if isinstance(item, dict)]
    actual = exact_regular_file_set(
        root, excluded_root_files={"export_manifest.json"}
    )
    if (
        len(paths) != len(members)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or set(paths) != actual
    ):
        raise M0Error("Gate M export exact-set differs")
    total = 0
    for item in members:
        if set(item) != {"path", "size_bytes", "sha256", "mode_octal"}:
            raise M0Error("Gate M export member keys differ")
        path = root / item["path"]
        if (
            item["mode_octal"] != "0444"
            or path.is_symlink()
            or not path.is_file()
            or stat.S_IMODE(path.stat().st_mode) != 0o444
            or path.stat().st_size != item["size_bytes"]
            or file_sha256(path) != item["sha256"]
        ):
            raise M0Error(f"Gate M export member differs: {item['path']}")
        total += item["size_bytes"]
    if total != manifest["total_size_bytes"] or total > MAX_EXPORT_BYTES:
        raise M0Error("Gate M export total-size differs")
    for path in (root, *root.rglob("*")):
        if path.is_dir() and stat.S_IMODE(path.stat().st_mode) != 0o555:
            raise M0Error(f"Gate M export directory mode differs: {path}")
    if status_path is not None:
        if (
            status_path.is_symlink()
            or not status_path.is_file()
            or stat.S_IMODE(status_path.stat().st_mode) != 0o444
            or status_path.read_bytes() != _status_payload(manifest)
        ):
            raise M0Error("Gate M export top-level commit marker differs")
    return manifest


def build_transport_envelope(
    *,
    export_root: Path,
    status_path: Path,
    remote_summary: dict[str, Any],
) -> bytes:
    """Serialize one verified export for the already-open SSH stdout stream.

    The envelope is canonical JSON so the local controller can validate every
    byte without a second connection.  Model weights and credentials cannot
    enter this function because ``verify_export`` closes the allowlisted export
    file set before any member is encoded.
    """

    manifest = verify_export(export_root, status_path=status_path)
    status_payload = _status_payload(manifest)
    if remote_summary.get("export_manifest_sha256") != manifest[
        "manifest_sha256"
    ] or remote_summary.get("export_commit_marker_sha256") != hashlib.sha256(
        status_payload
    ).hexdigest():
        raise M0Error("Gate M transport summary/export binding differs")
    members: list[dict[str, Any]] = []
    raw_size = len(status_payload)
    for item in manifest["members"]:
        payload = _read_regular(export_root / item["path"], item["path"])
        if (
            len(payload) != item["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != item["sha256"]
        ):
            raise M0Error(f"Gate M transport member changed: {item['path']}")
        raw_size += len(payload)
        members.append(
            {
                "path": item["path"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
                "mode_octal": item["mode_octal"],
                "payload_base64": base64.b64encode(payload).decode("ascii"),
            }
        )
    if raw_size > MAX_EXPORT_BYTES + len(status_payload):
        raise M0Error("Gate M transport raw payload exceeds its bound")
    envelope: dict[str, Any] = {
        "schema_version": TRANSPORT_SCHEMA,
        "remote_summary": remote_summary,
        "export_manifest": manifest,
        "export_status_payload_base64": base64.b64encode(status_payload).decode(
            "ascii"
        ),
        "raw_payload_size_bytes": raw_size,
        "members": members,
    }
    envelope["transport_sha256"] = semantic_sha256(envelope)
    encoded = _canonical(envelope)
    if len(encoded) > MAX_TRANSPORT_BYTES:
        raise M0Error("Gate M transport envelope exceeds its frozen bound")
    return encoded


def frame_transport_envelope(payload: bytes) -> bytes:
    """Frame an exact transport payload for one SSH stdout stream."""

    if not payload or len(payload) > MAX_TRANSPORT_BYTES:
        raise M0Error("Gate M transport payload size is outside its bound")
    header = (
        TRANSPORT_FRAME_MAGIC
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + hashlib.sha256(payload).hexdigest().encode("ascii")
        + b"\n"
    )
    if len(header) > MAX_TRANSPORT_HEADER_BYTES:
        raise M0Error("Gate M transport header exceeds its bound")
    return header + payload


def parse_transport_frame(frame: bytes) -> bytes:
    """Require one canonical header, exactly N payload bytes, and EOF."""

    if not frame or len(frame) > MAX_TRANSPORT_FRAME_BYTES:
        raise M0Error("Gate M transport frame size is outside its bound")
    header, separator, payload = frame.partition(b"\n")
    if separator != b"\n" or not header or len(header) + 1 > MAX_TRANSPORT_HEADER_BYTES:
        raise M0Error("Gate M transport header is absent or oversized")
    parts = header.split(b" ")
    if len(parts) != 3 or parts[0] != TRANSPORT_FRAME_MAGIC:
        raise M0Error("Gate M transport header magic or field count differs")
    size_text, digest = parts[1], parts[2]
    if (
        not size_text
        or size_text == b"0"
        or size_text.startswith(b"0")
        or not size_text.isdigit()
        or len(digest) != 64
        or any(character not in b"0123456789abcdef" for character in digest)
    ):
        raise M0Error("Gate M transport header fields are noncanonical")
    size = int(size_text)
    if size > MAX_TRANSPORT_BYTES or len(payload) != size:
        raise M0Error("Gate M transport payload length differs")
    if hashlib.sha256(payload).hexdigest().encode("ascii") != digest:
        raise M0Error("Gate M transport payload hash differs")
    return payload


def parse_transport_envelope(payload: bytes) -> dict[str, Any]:
    """Validate one canonical, bounded transport envelope without publishing it."""

    if not payload or len(payload) > MAX_TRANSPORT_BYTES:
        raise M0Error("Gate M transport envelope size is outside its bound")
    envelope = load_json_bytes(payload, "Gate M export transport")
    if _canonical(envelope) != payload:
        raise M0Error("Gate M export transport is not canonical JSON")
    if set(envelope) != {
        "schema_version",
        "remote_summary",
        "export_manifest",
        "export_status_payload_base64",
        "raw_payload_size_bytes",
        "members",
        "transport_sha256",
    } or envelope.get("schema_version") != TRANSPORT_SCHEMA:
        raise M0Error("Gate M export transport identity differs")
    claimed = envelope.get("transport_sha256")
    unhashed = dict(envelope)
    unhashed.pop("transport_sha256", None)
    if not isinstance(claimed, str) or claimed != semantic_sha256(unhashed):
        raise M0Error("Gate M export transport semantic hash differs")
    manifest = envelope.get("export_manifest")
    members = envelope.get("members")
    summary = envelope.get("remote_summary")
    if not isinstance(manifest, dict) or not isinstance(summary, dict) or not isinstance(
        members, list
    ):
        raise M0Error("Gate M export transport objects are malformed")
    manifest_members = manifest.get("members")
    if (
        manifest.get("schema_version") != EXPORT_SCHEMA
        or not isinstance(manifest_members, list)
        or len(members) != len(manifest_members)
        or len(members) > MAX_MEMBERS
        or summary.get("export_manifest_sha256")
        != manifest.get("manifest_sha256")
        or summary.get("export_commit_marker_sha256")
        != hashlib.sha256(_status_payload(manifest)).hexdigest()
    ):
        raise M0Error("Gate M export transport manifest binding differs")
    manifest_without_hash = dict(manifest)
    manifest_without_hash.pop("manifest_sha256", None)
    if manifest.get("manifest_sha256") != semantic_sha256(manifest_without_hash):
        raise M0Error("Gate M export transport manifest hash differs")
    try:
        status_payload = base64.b64decode(
            envelope["export_status_payload_base64"], validate=True
        )
    except (binascii.Error, ValueError, TypeError) as exc:
        raise M0Error("Gate M export status encoding is invalid") from exc
    if (
        status_payload != _status_payload(manifest)
        or base64.b64encode(status_payload).decode("ascii")
        != envelope["export_status_payload_base64"]
    ):
        raise M0Error("Gate M export status payload differs")
    decoded_members: list[dict[str, Any]] = []
    raw_size = len(status_payload)
    total_size = 0
    for transported, expected in zip(members, manifest_members, strict=True):
        if not isinstance(expected, dict) or set(expected) != {
            "path",
            "size_bytes",
            "sha256",
            "mode_octal",
        }:
            raise M0Error("Gate M export manifest member keys differ")
        if not isinstance(transported, dict) or set(transported) != {
            "path",
            "size_bytes",
            "sha256",
            "mode_octal",
            "payload_base64",
        }:
            raise M0Error("Gate M transported member keys differ")
        if {key: transported[key] for key in expected} != expected:
            raise M0Error("Gate M transported member metadata differs")
        _validate_member_path(expected.get("path"))
        if (
            expected.get("mode_octal") != "0444"
            or isinstance(expected.get("size_bytes"), bool)
            or not isinstance(expected.get("size_bytes"), int)
            or expected["size_bytes"] < 0
            or not isinstance(expected.get("sha256"), str)
            or len(expected["sha256"]) != 64
        ):
            raise M0Error("Gate M export manifest member metadata is invalid")
        try:
            decoded = base64.b64decode(transported["payload_base64"], validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise M0Error("Gate M transported member encoding is invalid") from exc
        if (
            len(decoded) != expected["size_bytes"]
            or len(decoded) > MAX_MEMBER_BYTES
            or hashlib.sha256(decoded).hexdigest() != expected["sha256"]
            or base64.b64encode(decoded).decode("ascii")
            != transported["payload_base64"]
        ):
            raise M0Error(f"Gate M transported member differs: {expected['path']}")
        total_size += len(decoded)
        raw_size += len(decoded)
        decoded_members.append({**expected, "payload": decoded})
    if (
        manifest.get("member_count") != len(decoded_members)
        or manifest.get("total_size_bytes") != total_size
        or total_size > MAX_EXPORT_BYTES
        or envelope.get("raw_payload_size_bytes") != raw_size
    ):
        raise M0Error("Gate M transported export totals differ")
    return {
        "remote_summary": summary,
        "export_manifest": manifest,
        "status_payload": status_payload,
        "members": decoded_members,
        "transport_sha256": claimed,
    }


def publish_local_replay(
    transport_payload: bytes,
    *,
    export_root: Path,
    status_path: Path,
) -> dict[str, Any]:
    """Publish a locally replayed exact-set export with marker-last semantics."""

    transport = parse_transport_envelope(transport_payload)
    if (
        export_root.exists()
        or export_root.is_symlink()
        or status_path.exists()
        or status_path.is_symlink()
        or export_root.parent != status_path.parent
    ):
        raise M0Error("local Gate M replay targets differ or are not fresh")
    parent = export_root.parent.resolve(strict=True)
    if parent != export_root.parent or parent.is_symlink():
        raise M0Error("local Gate M replay parent is unsafe")
    stage = parent / f".{export_root.name}.staged"
    status_stage = parent / f".{status_path.name}.staged"
    if stage.exists() or stage.is_symlink() or status_stage.exists() or status_stage.is_symlink():
        raise M0Error("local Gate M replay staging path is not fresh")
    stage.mkdir(mode=0o700, exist_ok=False)
    for item in transport["members"]:
        _write_fresh(stage / item["path"], item["payload"])
    _write_fresh(
        stage / "export_manifest.json",
        _canonical(transport["export_manifest"]),
    )
    for directory in sorted(
        (path for path in stage.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        _fsync_directory(directory)
        directory.chmod(0o555)
    _fsync_directory(stage)
    stage.chmod(0o555)
    _write_fresh(status_stage, transport["status_payload"])
    _rename_noreplace(stage, export_root)
    _fsync_directory(parent)
    verify_export(export_root, status_path=None)
    _rename_noreplace(status_stage, status_path)
    _fsync_directory(parent)
    observed = verify_export(export_root, status_path=status_path)
    if observed != transport["export_manifest"]:
        raise M0Error("local Gate M replay differs from transported manifest")
    return transport


def verify_export_projection(
    export_root: Path,
    *,
    status_path: Path,
    remote_summary: dict[str, Any],
) -> dict[str, Any]:
    """Re-run semantic terminal verifiers over the locally rebuilt projection."""

    manifest = verify_export(export_root, status_path=status_path)
    materialization = verify_materialization_terminal(
        export_root / "materialization"
    )
    runtime = verify_runtime_provenance(export_root / "runtime")
    stage_result = load_json(export_root / "materialization/stage_result.json")
    model_ledger = load_json(export_root / "model/model_ledger.json")
    fixture_path = export_root / "fixtures/capacity_prompt_fixture.json"
    fixture = load_json(fixture_path)
    receipt = load_json(export_root / "deployment/deployment_receipt.json")
    runtime_record = (
        export_root / "runtime/runtime_provenance.json"
        if runtime["terminal_status"] == "COMPLETE"
        else export_root / "runtime/runtime_provenance_failure.json"
    )
    expected_paths = {
        "authority/materialization_authority_projection.json",
        "deployment/deployment_receipt.json",
        "model/model_ledger.json",
        "fixtures/capacity_prompt_fixture.json",
        "d0/d0_binding.json",
        "logs/command_log_inventory.json",
        "materialization/evidence_ledger.json",
        "runtime/evidence_ledger.json",
    }
    expected_paths.update(
        f"materialization/{item['path']}" for item in materialization["members"]
    )
    expected_paths.update(f"runtime/{item['path']}" for item in runtime["members"])
    manifest_paths = {item["path"] for item in manifest["members"]}
    if (
        manifest_paths != expected_paths
        or manifest["manifest_sha256"]
        != remote_summary.get("export_manifest_sha256")
        or file_sha256(status_path)
        != remote_summary.get("export_commit_marker_sha256")
        or semantic_sha256(receipt)
        != remote_summary.get("deployment_receipt_sha256")
        or materialization.get("terminal_status") != "COMPLETE_HARD_STOP"
        or materialization.get("ledger_sha256")
        != remote_summary.get("materialization_evidence_ledger_sha256")
        or runtime.get("ledger_sha256")
        != remote_summary.get("runtime_provenance_ledger_sha256")
        or runtime.get("terminal_status")
        != remote_summary.get("runtime_provenance_status")
        or file_sha256(runtime_record)
        != remote_summary.get("runtime_provenance_record_sha256")
        or model_ledger.get("ledger_sha256")
        != remote_summary.get("model_ledger_sha256")
        or file_sha256(fixture_path)
        != remote_summary.get("capacity_prompt_fixture_sha256")
        or stage_result.get("model_ledger_sha256")
        != model_ledger.get("ledger_sha256")
        or stage_result.get("capacity_prompt_fixture_sha256")
        != file_sha256(fixture_path)
        or fixture.get("model_ledger_sha256")
        != model_ledger.get("ledger_sha256")
        or fixture.get("token_ids_sha256")
        != semantic_sha256(fixture.get("token_ids"))
    ):
        raise M0Error("local Gate M semantic projection differs from remote summary")
    return {
        "manifest": manifest,
        "materialization": materialization,
        "runtime_provenance": runtime,
        "receipt_semantic_sha256": semantic_sha256(receipt),
        "status_file_sha256": file_sha256(status_path),
    }
