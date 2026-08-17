#!/usr/bin/env python3
"""Validate the immutable Gate M parent required before any M0 GPU entry."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    SHA256_RE,
    file_sha256,
    load_json,
    load_json_bytes,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    verify_install,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment import (  # noqa: E402
    validate_gate_m_remote_summary,
)
from explorations.moe_cycle_simulator.phase7.application.executor.gate_m_export import (  # noqa: E402
    verify_export_projection,
)
from explorations.moe_cycle_simulator.phase7.application.executor.materialization_driver import (  # noqa: E402
    verify_materialization_terminal,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_provenance import (  # noqa: E402
    verify_runtime_provenance,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_finalize import (  # noqa: E402
    verify_deployment_terminal,
)
PARENT_SCHEMA = "moe-simulator-phase7-gate-m-parent-evidence-v1"
SESSION_RE = re.compile(r"^phase7-gate-m-deploy-[a-z0-9][a-z0-9._-]{7,80}$")
GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_PARENT_BYTES = 16 * 1024 * 1024


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise M0Error(f"{label} key closure differs")


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise M0Error(f"{label} must be a lowercase SHA-256 digest")
    return value


def _git_object(value: Any, label: str) -> str:
    if not isinstance(value, str) or GIT_OBJECT_RE.fullmatch(value) is None:
        raise M0Error(f"{label} must be a lowercase 40-hex Git object ID")
    return value


def _absolute(value: Any, label: str) -> Path:
    if not isinstance(value, str):
        raise M0Error(f"{label} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or "\\" in value or ".." in path.parts or str(path) != value:
        raise M0Error(f"{label} must be a canonical absolute path")
    return path


def validate_gate_m_parent(parent: Mapping[str, Any], *, verify_live: bool) -> dict[str, Any]:
    _exact(
        parent,
        {
            "schema_version",
            "status",
            "gate_m_session_id",
            "source",
            "local_terminal",
            "remote",
            "remote_summary",
            "gpu_workload_performed",
            "claim_boundary",
        },
        "Gate M parent",
    )
    if (
        parent["schema_version"] != PARENT_SCHEMA
        or parent["status"] != "COMPLETE_M0_ELIGIBLE"
        or SESSION_RE.fullmatch(parent["gate_m_session_id"]) is None
        or parent["gpu_workload_performed"] is not False
        or parent["claim_boundary"]
        != "GATE_M_CPU_IO_PROVENANCE_ONLY_NOT_M0_CAPACITY_PASS"
    ):
        raise M0Error("Gate M parent identity or claim boundary differs")
    source = parent["source"]
    _exact(
        source,
        {
            "commit_sha1",
            "tree_sha1",
            "application_ledger_sha256",
            "same_hash_review_aggregate_sha256",
            "architecture_system",
            "model_benchmark",
            "trace_provenance",
            "blockers",
        },
        "Gate M source/review",
    )
    _git_object(source["commit_sha1"], "source commit")
    _git_object(source["tree_sha1"], "source tree")
    for field in ("application_ledger_sha256", "same_hash_review_aggregate_sha256"):
        _hash(source[field], field)
    if (
        source["architecture_system"] != "GO"
        or source["model_benchmark"] != "GO"
        or source["trace_provenance"] != "GO"
        or source["blockers"] != []
    ):
        raise M0Error("Gate M parent lacks same-hash GO/GO/GO")
    local = parent["local_terminal"]
    _exact(
        local,
        {
            "evidence_ledger_file_sha256",
            "evidence_ledger_sha256",
            "deployment_result_file_sha256",
            "transport_sha256",
            "export_manifest_sha256",
            "export_status_sha256",
            "local_replay_status",
        },
        "Gate M local terminal",
    )
    for field in (
        "evidence_ledger_file_sha256",
        "evidence_ledger_sha256",
        "deployment_result_file_sha256",
        "transport_sha256",
        "export_manifest_sha256",
        "export_status_sha256",
    ):
        _hash(local[field], field)
    if local["local_replay_status"] != "COMPLETE_REPLAYED":
        raise M0Error("Gate M local export was not replayed")
    remote = parent["remote"]
    _exact(
        remote,
        {
            "project_root",
            "application_target",
            "deployment_receipt_path",
            "deployment_bundle_sha256",
            "deployment_receipt_sha256",
            "materialization_evidence_root",
            "runtime_provenance_root",
            "export_root",
            "export_status_path",
            "materialization_evidence_ledger_sha256",
            "runtime_provenance_ledger_sha256",
            "runtime_provenance_record_sha256",
            "model_ledger_sha256",
            "capacity_prompt_fixture_sha256",
            "export_manifest_sha256",
            "export_status_sha256",
        },
        "Gate M remote parent",
    )
    project = _absolute(remote["project_root"], "Gate M project root")
    application = _absolute(remote["application_target"], "Gate M application target")
    receipt = _absolute(remote["deployment_receipt_path"], "Gate M receipt")
    materialization_root = _absolute(
        remote["materialization_evidence_root"], "Gate M materialization evidence"
    )
    runtime_root = _absolute(
        remote["runtime_provenance_root"], "Gate M runtime provenance"
    )
    export_root = _absolute(remote["export_root"], "Gate M export")
    export_status = _absolute(remote["export_status_path"], "Gate M export status")
    if (
        project.parent != Path("/vault")
        or materialization_root != project / "evidence/materialization"
        or runtime_root != project / "evidence/runtime-provenance"
        or export_root != project / "export/gate-m"
        or export_status != project / "export/gate-m.status"
        or receipt != project / "packages/materialization/deployment_receipt.json"
        or application
        != project
        / "packages/materialization/repo/explorations/moe_cycle_simulator/phase7/application"
    ):
        raise M0Error("Gate M remote path binding differs")
    for field in (
        "deployment_bundle_sha256",
        "deployment_receipt_sha256",
        "materialization_evidence_ledger_sha256",
        "runtime_provenance_ledger_sha256",
        "runtime_provenance_record_sha256",
        "model_ledger_sha256",
        "capacity_prompt_fixture_sha256",
        "export_manifest_sha256",
        "export_status_sha256",
    ):
        _hash(remote[field], field)
    summary = parent["remote_summary"]
    if not isinstance(summary, dict):
        raise M0Error("Gate M parent remote summary is not an object")
    summary = validate_gate_m_remote_summary(
        summary,
        bundle_sha256=remote["deployment_bundle_sha256"],
        application_ledger_sha256=source["application_ledger_sha256"],
        deployment_receipt_sha256=remote["deployment_receipt_sha256"],
    )
    if (
        summary.get("status") != "REMOTE_COMPLETE_PROVENANCE_ELIGIBLE"
        or summary.get("runtime_provenance_status") != "COMPLETE"
        or summary.get("export_status") != "REMOTE_COMPLETE_LOCAL_REPLAY_REQUIRED"
        or summary.get("materialization_evidence_ledger_sha256")
        != remote["materialization_evidence_ledger_sha256"]
        or summary.get("runtime_provenance_ledger_sha256")
        != remote["runtime_provenance_ledger_sha256"]
        or summary.get("runtime_provenance_record_sha256")
        != remote["runtime_provenance_record_sha256"]
        or summary.get("model_ledger_sha256") != remote["model_ledger_sha256"]
        or summary.get("capacity_prompt_fixture_sha256")
        != remote["capacity_prompt_fixture_sha256"]
        or summary.get("export_manifest_sha256")
        != remote["export_manifest_sha256"]
        or summary.get("export_commit_marker_sha256")
        != remote["export_status_sha256"]
        or local["transport_sha256"] == "0" * 64
        or local["export_manifest_sha256"] != remote["export_manifest_sha256"]
        or local["export_status_sha256"] != remote["export_status_sha256"]
    ):
        raise M0Error("Gate M parent summary/local/remote binding differs")
    if not verify_live:
        return {"status": "COMPLETE_M0_ELIGIBLE", "remote_summary": summary}
    receipt_value = verify_install(
        allowed_root=Path("/vault"), target=application, receipt=receipt
    )
    materialization = verify_materialization_terminal(materialization_root)
    runtime = verify_runtime_provenance(runtime_root)
    projection = verify_export_projection(
        export_root,
        status_path=export_status,
        remote_summary=summary,
    )
    if (
        receipt_value["bundle_sha256"] != remote["deployment_bundle_sha256"]
        or semantic_sha256(receipt_value) != remote["deployment_receipt_sha256"]
        or summary["deployment_receipt_sha256"]
        != remote["deployment_receipt_sha256"]
        or materialization["terminal_status"] != "COMPLETE_HARD_STOP"
        or materialization["ledger_sha256"]
        != remote["materialization_evidence_ledger_sha256"]
        or runtime["terminal_status"] != "COMPLETE"
        or runtime["ledger_sha256"]
        != remote["runtime_provenance_ledger_sha256"]
        or projection["manifest"]["manifest_sha256"]
        != remote["export_manifest_sha256"]
        or file_sha256(export_status) != remote["export_status_sha256"]
    ):
        raise M0Error("live Gate M parent evidence differs")
    return {"status": "COMPLETE_M0_ELIGIBLE", "remote_summary": summary}


def _capture_parent_bytes(path: Path) -> bytes:
    """Capture one stable, non-symlink parent inode through one descriptor."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise M0Error(f"cannot stat Gate M parent: {exc}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > MAX_PARENT_BYTES
    ):
        raise M0Error("Gate M parent must be a bounded real regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise M0Error(f"cannot safely open Gate M parent: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise M0Error("Gate M parent identity changed while opening")
        blocks: list[bytes] = []
        observed = 0
        while True:
            block = os.read(
                descriptor,
                min(1024 * 1024, MAX_PARENT_BYTES + 1 - observed),
            )
            if not block:
                break
            blocks.append(block)
            observed += len(block)
            if observed > MAX_PARENT_BYTES:
                raise M0Error("Gate M parent exceeds its byte bound")
        after = os.fstat(descriptor)
        if (
            observed != opened.st_size
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            )
        ):
            raise M0Error("Gate M parent changed while being captured")
        return b"".join(blocks)
    finally:
        os.close(descriptor)


def validate_parent_file(
    path: Path,
    *,
    verify_live: bool,
    expected_file_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate and return the exact parent value captured from one file read."""

    payload = _capture_parent_bytes(path)
    if expected_file_sha256 is not None and (
        not isinstance(expected_file_sha256, str)
        or SHA256_RE.fullmatch(expected_file_sha256) is None
        or hashlib.sha256(payload).hexdigest() != expected_file_sha256
    ):
        raise M0Error("M0 approval does not bind the Gate M parent evidence")
    parent = load_json_bytes(payload, str(path))
    validate_gate_m_parent(parent, verify_live=verify_live)
    return parent


def validate_m0_model_binding(
    parent: Mapping[str, Any], runtime: Mapping[str, Any]
) -> None:
    """Require M0 to consume the exact model ledger/fixture exported by Gate M."""

    remote = parent.get("remote")
    model = runtime.get("model")
    if not isinstance(remote, Mapping) or not isinstance(model, Mapping):
        raise M0Error("Gate M/M0 model binding objects are missing")
    if (
        remote.get("model_ledger_sha256")
        != model.get("model_file_ledger_sha256")
        or remote.get("capacity_prompt_fixture_sha256")
        != model.get("capacity_prompt_fixture_sha256")
    ):
        raise M0Error("Gate M/M0 model ledger or capacity fixture binding differs")


def build_parent_from_local_terminal(
    *,
    evidence_root: Path,
    same_hash_review_aggregate: Path,
) -> dict[str, Any]:
    """Project one sealed eligible local Gate M result into the M0 parent."""

    root = evidence_root.resolve(strict=True)
    ledger = verify_deployment_terminal(root)
    if ledger["terminal_status"] != "COMPLETE":
        raise M0Error("Gate M parent requires a COMPLETE local terminal")
    result_path = root / "deployment_result.json"
    summary_path = root / "gate_m_remote_summary.json"
    result = load_json(result_path)
    summary = load_json(summary_path)
    if (
        result.get("gate_m_status") != "COMPLETE_M0_ELIGIBLE"
        or result.get("gate_m_remote_status")
        != "REMOTE_COMPLETE_PROVENANCE_ELIGIBLE"
        or result.get("runtime_provenance_status") != "COMPLETE"
        or result.get("export_status") != "COMPLETE_REPLAYED"
        or result.get("gpu_workload_performed") is not False
        or result.get("next_legal_action") != "REQUEST_NEW_M0_APPLICATION"
    ):
        raise M0Error("local Gate M terminal is not M0-eligible")
    review_path = same_hash_review_aggregate.resolve(strict=True)
    if review_path.is_symlink() or not review_path.is_file():
        raise M0Error("same-hash review aggregate must be a real file")
    review = load_json(review_path)
    roles = review.get("roles")
    if (
        roles
        != {
            "Architecture/System": "GO",
            "Model/Benchmark": "GO",
            "Trace/Provenance": "GO",
        }
        or review.get("verdict") != "GO"
        or review.get("blockers") != []
        or review.get("application_ledger_sha256")
        != result["application_ledger_sha256"]
    ):
        raise M0Error("same-hash review aggregate does not authorize this Gate M source")
    commit = _git_object(review.get("source_commit_sha1"), "review source commit")
    tree = _git_object(review.get("source_tree_sha1"), "review source tree")
    plan = load_json(root / "deployment_inputs/plan.json")
    project = _absolute(plan["storage"]["project_root"], "Gate M project root")
    parent = {
        "schema_version": PARENT_SCHEMA,
        "status": "COMPLETE_M0_ELIGIBLE",
        "gate_m_session_id": result["gate_m_session_id"],
        "source": {
            "commit_sha1": commit,
            "tree_sha1": tree,
            "application_ledger_sha256": result["application_ledger_sha256"],
            "same_hash_review_aggregate_sha256": file_sha256(review_path),
            "architecture_system": "GO",
            "model_benchmark": "GO",
            "trace_provenance": "GO",
            "blockers": [],
        },
        "local_terminal": {
            "evidence_ledger_file_sha256": file_sha256(
                root / "evidence_ledger.json"
            ),
            "evidence_ledger_sha256": ledger["ledger_sha256"],
            "deployment_result_file_sha256": file_sha256(result_path),
            "transport_sha256": result["gate_m_transport_sha256"],
            "export_manifest_sha256": result["local_export_manifest_sha256"],
            "export_status_sha256": result["local_export_status_sha256"],
            "local_replay_status": "COMPLETE_REPLAYED",
        },
        "remote": {
            "project_root": str(project),
            "application_target": plan["storage"]["application_target"],
            "deployment_receipt_path": plan["storage"]["deployment_receipt"],
            "deployment_bundle_sha256": result["bundle_sha256"],
            "deployment_receipt_sha256": result[
                "expected_deployment_receipt_sha256"
            ],
            "materialization_evidence_root": str(
                project / "evidence/materialization"
            ),
            "runtime_provenance_root": str(
                project / "evidence/runtime-provenance"
            ),
            "export_root": str(project / "export/gate-m"),
            "export_status_path": str(project / "export/gate-m.status"),
            "materialization_evidence_ledger_sha256": result[
                "remote_materialization_evidence_ledger_sha256"
            ],
            "runtime_provenance_ledger_sha256": result[
                "remote_runtime_provenance_ledger_sha256"
            ],
            "runtime_provenance_record_sha256": result[
                "remote_runtime_provenance_record_sha256"
            ],
            "model_ledger_sha256": result["remote_model_ledger_sha256"],
            "capacity_prompt_fixture_sha256": result[
                "remote_capacity_prompt_fixture_sha256"
            ],
            "export_manifest_sha256": result[
                "remote_export_manifest_sha256"
            ],
            "export_status_sha256": result[
                "remote_export_commit_marker_sha256"
            ],
        },
        "remote_summary": summary,
        "gpu_workload_performed": False,
        "claim_boundary": "GATE_M_CPU_IO_PROVENANCE_ONLY_NOT_M0_CAPACITY_PASS",
    }
    validate_gate_m_parent(parent, verify_live=False)
    return parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--verify-live", action="store_true")
    parser.add_argument("--build-from-evidence", type=Path)
    parser.add_argument("--same-hash-review-aggregate", type=Path)
    args = parser.parse_args()
    if args.build_from_evidence is not None:
        if args.verify_live or args.same_hash_review_aggregate is None:
            raise M0Error(
                "parent build requires one review aggregate and forbids --verify-live"
            )
        parent = build_parent_from_local_terminal(
            evidence_root=args.build_from_evidence,
            same_hash_review_aggregate=args.same_hash_review_aggregate,
        )
        write_new_json(args.parent, parent)
        print(file_sha256(args.parent))
        return 0
    result = validate_parent_file(args.parent.resolve(strict=True), verify_live=args.verify_live)
    print(result["status"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
