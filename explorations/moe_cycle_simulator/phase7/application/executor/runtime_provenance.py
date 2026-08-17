#!/usr/bin/env python3
"""Capture CPU-only static vLLM runtime provenance for Gate M.

The collector hashes installed files and owner-pinned provider artifacts.  It
does not import vLLM, Torch, CUDA, instantiate an engine, load model weights, or
claim which backend a future M0 process will select.
"""

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
    exact_regular_file_set,
    file_sha256,
    load_json,
    semantic_sha256,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.deployment_bundle import (  # noqa: E402
    _rename_noreplace,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_attestation import (  # noqa: E402
    build_installed_distribution_manifest,
    validate_distribution_manifest,
    validate_sbom_vllm_component,
)


RESULT_SCHEMA = "moe-simulator-phase7-static-runtime-provenance-v1"
LEDGER_SCHEMA = "moe-simulator-phase7-runtime-provenance-ledger-v1"
SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
STATUS_PAYLOAD = {
    "COMPLETE": "STATIC_RUNTIME_PROVENANCE_COMPLETE\n",
    "BLOCKED": "STATIC_RUNTIME_PROVENANCE_BLOCKED_IMMUTABLE\n",
}


class ProvenanceUnavailable(M0Error):
    """A required provider artifact is unavailable without evidence tamper."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular(path: Path, expected_sha256: str, label: str) -> Path:
    if not path.exists():
        raise ProvenanceUnavailable(f"{label} is unavailable")
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve(strict=True) != path
        or SHA256_RE.fullmatch(expected_sha256) is None
        or file_sha256(path) != expected_sha256
    ):
        raise M0Error(f"{label} file/hash identity differs")
    return path


def build_source_tree_ledger(root: Path) -> dict[str, Any]:
    if not root.exists():
        raise ProvenanceUnavailable("vLLM source tree is unavailable")
    root = root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise M0Error("vLLM source root is unsafe")
    paths = sorted(exact_regular_file_set(root))
    if not paths:
        raise M0Error("vLLM source tree is empty")
    members: list[dict[str, Any]] = []
    rows: list[bytes] = []
    for relative in paths:
        path = root / relative
        observed = path.lstat()
        if not stat.S_ISREG(observed.st_mode):
            raise M0Error(f"vLLM source member is not regular: {relative}")
        digest = file_sha256(path)
        members.append(
            {
                "path": relative,
                "size_bytes": observed.st_size,
                "sha256": digest,
            }
        )
        rows.append(f"{digest}  {relative}\n".encode("utf-8"))
    return {
        "schema_version": "moe-simulator-phase7-source-tree-ledger-v1",
        "member_count": len(members),
        "total_size_bytes": sum(item["size_bytes"] for item in members),
        "members": members,
        "ledger_sha256": hashlib.sha256(b"".join(rows)).hexdigest(),
    }


def capture_static_provenance(
    *,
    vllm_version: str,
    source_commit: str,
    source_tree: Path,
    expected_source_tree_ledger_sha256: str,
    wheel: Path,
    expected_wheel_sha256: str,
    build_environment_ledger: Path,
    expected_build_environment_ledger_sha256: str,
    container_sbom: Path,
    expected_container_sbom_sha256: str,
    container_image: str,
    container_digest: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        not vllm_version
        or SOURCE_COMMIT_RE.fullmatch(source_commit) is None
        or not container_image
        or re.fullmatch(r"sha256:[0-9a-f]{64}", container_digest) is None
    ):
        raise M0Error("static runtime identity fields are invalid")
    source_ledger = build_source_tree_ledger(source_tree)
    if (
        SHA256_RE.fullmatch(expected_source_tree_ledger_sha256) is None
        or source_ledger["ledger_sha256"] != expected_source_tree_ledger_sha256
    ):
        raise M0Error("vLLM source tree ledger differs from owner-pinned identity")
    wheel_path = _regular(wheel, expected_wheel_sha256, "vLLM wheel")
    build_path = _regular(
        build_environment_ledger,
        expected_build_environment_ledger_sha256,
        "build environment ledger",
    )
    sbom_path = _regular(
        container_sbom,
        expected_container_sbom_sha256,
        "container SBOM",
    )
    load_json(build_path)
    sbom = load_json(sbom_path)
    validate_sbom_vllm_component(sbom, vllm_version)
    installed = build_installed_distribution_manifest("vllm")
    validate_distribution_manifest(installed)
    if installed["distribution_version"] != vllm_version:
        raise M0Error("installed vLLM version differs from frozen provenance")
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "status": "COMPLETE",
        "vllm": {
            "version": vllm_version,
            "source_repository": "https://github.com/vllm-project/vllm",
            "source_commit": source_commit,
            "source_tree_ledger_sha256": source_ledger["ledger_sha256"],
            "wheel_path": str(wheel_path),
            "wheel_sha256": expected_wheel_sha256,
            "installed_distribution_ledger_sha256": installed["ledger_sha256"],
        },
        "build_environment": {
            "ledger_path": str(build_path),
            "ledger_sha256": expected_build_environment_ledger_sha256,
        },
        "container": {
            "image": container_image,
            "digest": container_digest,
            "sbom_path": str(sbom_path),
            "sbom_sha256": expected_container_sbom_sha256,
        },
        "capability_boundary": {
            "inspection_mode": "STATIC_FILES_AND_DISTRIBUTION_METADATA_ONLY",
            "vllm_imported": False,
            "torch_imported": False,
            "cuda_context_created": False,
            "model_loaded": False,
            "runtime_selected_backend_observation": "PENDING_M0_R1_STARTUP",
            "api_capability_validation": "REQUIRED_AT_M0_PACKAGE_FREEZE",
        },
        "m0_provenance_eligible": True,
    }
    result["result_sha256"] = semantic_sha256(result)
    return result, {"source_tree": source_ledger, "installed_distribution": installed}


def _build_ledger(root: Path, status: str) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise M0Error(f"runtime provenance symlink is forbidden: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {
            "evidence_ledger.json",
            "runtime_provenance_status.txt",
            ".evidence_ledger.json.staged",
            ".runtime_provenance_status.txt.staged",
        }:
            raise M0Error(f"runtime provenance terminal path is not fresh: {relative}")
        if not path.is_file():
            raise M0Error(f"runtime provenance special file is forbidden: {relative}")
        members.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    marker = STATUS_PAYLOAD[status].encode("utf-8")
    members.append(
        {
            "path": "runtime_provenance_status.txt",
            "size_bytes": len(marker),
            "sha256": hashlib.sha256(marker).hexdigest(),
        }
    )
    members.sort(key=lambda item: item["path"])
    ledger: dict[str, Any] = {
        "schema_version": LEDGER_SCHEMA,
        "terminal_status": status,
        "terminal_marker": STATUS_PAYLOAD[status].strip(),
        "member_count": len(members),
        "members": members,
    }
    ledger["ledger_sha256"] = semantic_sha256(ledger)
    return ledger


def seal_runtime_provenance(root: Path, status: str) -> dict[str, Any]:
    if status not in STATUS_PAYLOAD:
        raise M0Error("invalid runtime provenance terminal status")
    root = root.resolve(strict=True)
    ledger = _build_ledger(root, status)
    staged_ledger = root / ".evidence_ledger.json.staged"
    staged_status = root / ".runtime_provenance_status.txt.staged"
    write_new_json(staged_ledger, ledger)
    staged_status.write_text(STATUS_PAYLOAD[status], encoding="utf-8")
    with staged_status.open("rb") as stream:
        os.fsync(stream.fileno())
    staged_ledger.chmod(0o444)
    staged_status.chmod(0o444)
    _fsync_directory(root)
    for path in sorted(root.rglob("*"), reverse=True):
        if path in {staged_ledger, staged_status}:
            continue
        if path.is_symlink():
            raise M0Error(f"runtime provenance symlink is forbidden: {path}")
        path.chmod(0o444 if path.is_file() else 0o555)
    _rename_noreplace(staged_ledger, root / "evidence_ledger.json")
    _fsync_directory(root)
    _rename_noreplace(staged_status, root / "runtime_provenance_status.txt")
    _fsync_directory(root)
    root.chmod(0o555)
    verify_runtime_provenance(root)
    return ledger


def verify_runtime_provenance(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    ledger = load_json(root / "evidence_ledger.json")
    base = dict(ledger)
    claimed = base.pop("ledger_sha256", None)
    status = ledger.get("terminal_status")
    members = ledger.get("members")
    if (
        set(ledger)
        != {
            "schema_version",
            "terminal_status",
            "terminal_marker",
            "member_count",
            "members",
            "ledger_sha256",
        }
        or ledger.get("schema_version") != LEDGER_SCHEMA
        or status not in STATUS_PAYLOAD
        or ledger.get("terminal_marker") != STATUS_PAYLOAD[status].strip()
        or claimed != semantic_sha256(base)
        or not isinstance(members, list)
        or ledger.get("member_count") != len(members)
    ):
        raise M0Error("runtime provenance ledger identity differs")
    paths = [item.get("path") for item in members if isinstance(item, dict)]
    actual = exact_regular_file_set(
        root, excluded_root_files={"evidence_ledger.json"}
    )
    if (
        len(paths) != len(members)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or set(paths) != actual
        or "runtime_provenance_status.txt" not in paths
    ):
        raise M0Error("runtime provenance exact-set differs")
    for item in members:
        if set(item) != {"path", "size_bytes", "sha256"}:
            raise M0Error("runtime provenance member keys differ")
        path = root / item["path"]
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item["size_bytes"]
            or file_sha256(path) != item["sha256"]
            or path.stat().st_mode & 0o222
        ):
            raise M0Error(f"runtime provenance member differs: {item['path']}")
    for path in (root, *root.rglob("*")):
        if path.is_dir() and path.stat().st_mode & 0o222:
            raise M0Error(f"runtime provenance directory is writable: {path}")
    if (root / "runtime_provenance_status.txt").read_text(
        encoding="utf-8"
    ) != STATUS_PAYLOAD[status]:
        raise M0Error("runtime provenance terminal marker differs")
    terminal = (
        root / "runtime_provenance.json"
        if status == "COMPLETE"
        else root / "runtime_provenance_failure.json"
    )
    record = load_json(terminal)
    if record.get("status") != status:
        raise M0Error("runtime provenance terminal record differs")
    return ledger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--vllm-version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", type=Path, required=True)
    parser.add_argument("--source-tree-ledger-sha256", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--build-environment-ledger", type=Path, required=True)
    parser.add_argument("--build-environment-ledger-sha256", required=True)
    parser.add_argument("--container-sbom", type=Path, required=True)
    parser.add_argument("--container-sbom-sha256", required=True)
    parser.add_argument("--container-image", required=True)
    parser.add_argument("--container-digest", required=True)
    args = parser.parse_args()
    root = args.output_root
    if not root.is_absolute() or root.exists() or root.is_symlink():
        raise M0Error("runtime provenance output root must be absolute and fresh")
    parent = root.parent.resolve(strict=True)
    if parent != root.parent or parent.is_symlink():
        raise M0Error("runtime provenance output parent is unsafe")
    root.mkdir(mode=0o700, exist_ok=False)
    try:
        result, manifests = capture_static_provenance(
            vllm_version=args.vllm_version,
            source_commit=args.source_commit,
            source_tree=args.source_tree,
            expected_source_tree_ledger_sha256=args.source_tree_ledger_sha256,
            wheel=args.wheel,
            expected_wheel_sha256=args.wheel_sha256,
            build_environment_ledger=args.build_environment_ledger,
            expected_build_environment_ledger_sha256=(
                args.build_environment_ledger_sha256
            ),
            container_sbom=args.container_sbom,
            expected_container_sbom_sha256=args.container_sbom_sha256,
            container_image=args.container_image,
            container_digest=args.container_digest,
        )
        write_new_json(root / "runtime_provenance.json", result)
        write_new_json(root / "source_tree_ledger.json", manifests["source_tree"])
        write_new_json(
            root / "installed_distribution.json",
            manifests["installed_distribution"],
        )
        ledger = seal_runtime_provenance(root, "COMPLETE")
    except ProvenanceUnavailable as exc:
        write_new_json(
            root / "runtime_provenance_failure.json",
            {
                "schema_version": RESULT_SCHEMA,
                "status": "BLOCKED",
                "failure_class": "REQUIRED_PROVIDER_PROVENANCE_UNAVAILABLE",
                "failure": str(exc),
                "m0_provenance_eligible": False,
                "gpu_workload_performed": False,
                "retry_allowed": False,
                "resume_allowed": False,
            },
        )
        ledger = seal_runtime_provenance(root, "BLOCKED")
    print(ledger["ledger_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
