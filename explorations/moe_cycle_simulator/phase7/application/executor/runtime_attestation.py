#!/usr/bin/env python3
"""Build and verify complete vLLM installation/build/container attestations."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.metadata
import os
import re
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
    semantic_sha256,
    write_new_json,
)


DIST_SCHEMA = "moe-simulator-phase7-installed-distribution-ledger-v1"
BUILD_SCHEMA = "moe-simulator-phase7-vllm-build-attestation-v1"
SOURCE_REPOSITORY = "https://github.com/vllm-project/vllm"
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
LOADED_MODULE_SCHEMA = "moe-simulator-phase7-loaded-vllm-modules-v1"


def build_installed_distribution_manifest(name: str = "vllm") -> dict[str, Any]:
    distribution = importlib.metadata.distribution(name)
    declared_files = distribution.files
    if declared_files is None:
        raise M0Error(f"{name} distribution has no installed-file inventory")
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    for declared in sorted(declared_files, key=lambda item: str(item)):
        relative = str(declared).replace(os.sep, "/")
        if relative in seen:
            raise M0Error(f"duplicate installed distribution path: {relative}")
        seen.add(relative)
        unresolved_path = Path(distribution.locate_file(declared))
        if unresolved_path.is_symlink():
            raise M0Error(
                f"installed distribution member is a symlink: {relative}"
            )
        try:
            path = unresolved_path.resolve(strict=True)
        except OSError as exc:
            raise M0Error(f"installed distribution member is missing: {relative}") from exc
        if not path.is_file():
            raise M0Error(f"installed distribution member is not a regular file: {relative}")
        members.append(
            {
                "declared_path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    if not members or not any(
        item["declared_path"].endswith(".dist-info/RECORD") for item in members
    ):
        raise M0Error("installed distribution inventory lacks its RECORD closure")
    value: dict[str, Any] = {
        "schema_version": DIST_SCHEMA,
        "distribution_name": name,
        "distribution_version": distribution.version,
        "member_count": len(members),
        "total_size_bytes": sum(item["size_bytes"] for item in members),
        "members": members,
    }
    value["ledger_sha256"] = semantic_sha256(value)
    return value


def validate_distribution_manifest(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "schema_version",
        "distribution_name",
        "distribution_version",
        "member_count",
        "total_size_bytes",
        "members",
        "ledger_sha256",
    }:
        raise M0Error("installed distribution manifest key closure mismatch")
    base = dict(value)
    claimed = base.pop("ledger_sha256")
    members = value["members"]
    if (
        value["schema_version"] != DIST_SCHEMA
        or value["distribution_name"] != "vllm"
        or not isinstance(value["distribution_version"], str)
        or not value["distribution_version"]
        or not isinstance(members, list)
        or not members
        or value["member_count"] != len(members)
        or not isinstance(claimed, str)
        or not SHA256_RE.fullmatch(claimed)
        or semantic_sha256(base) != claimed
    ):
        raise M0Error("installed distribution manifest content/hash mismatch")
    paths: list[str] = []
    for item in members:
        if (
            not isinstance(item, dict)
            or set(item) != {"declared_path", "size_bytes", "sha256"}
            or not isinstance(item["declared_path"], str)
            or not item["declared_path"]
            or not isinstance(item["size_bytes"], int)
            or item["size_bytes"] < 0
            or not isinstance(item["sha256"], str)
            or not SHA256_RE.fullmatch(item["sha256"])
        ):
            raise M0Error("invalid installed distribution member")
        paths.append(item["declared_path"])
    if value["total_size_bytes"] != sum(item["size_bytes"] for item in members):
        raise M0Error("installed distribution total-size mismatch")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise M0Error("installed distribution members are not sorted and unique")
    if not any(path.endswith(".dist-info/RECORD") for path in paths):
        raise M0Error("installed distribution manifest omits RECORD")


def attest_loaded_distribution_modules(
    frozen_manifest: Mapping[str, Any],
    *,
    distribution_name: str = "vllm",
    module_prefix: str = "vllm",
    modules: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind every loaded vLLM Python/native module to the frozen distribution.

    The registered distribution version is insufficient: an unbound import path
    can shadow it.  Resolve each loaded module origin, require it to be one of the
    exact frozen distribution members, and retain all loaded extension modules as
    an explicit native-code subset.
    """

    validate_distribution_manifest(frozen_manifest)
    if (
        frozen_manifest["distribution_name"] != distribution_name
        or not module_prefix
    ):
        raise M0Error("loaded-module distribution identity mismatch")
    distribution = importlib.metadata.distribution(distribution_name)
    if distribution.version != frozen_manifest["distribution_version"]:
        raise M0Error("loaded-module distribution version mismatch")

    frozen_by_path: dict[Path, Mapping[str, Any]] = {}
    for member in frozen_manifest["members"]:
        unresolved = Path(distribution.locate_file(Path(member["declared_path"])))
        if unresolved.is_symlink():
            raise M0Error(
                f"frozen distribution member became a symlink: {member['declared_path']}"
            )
        try:
            resolved = unresolved.resolve(strict=True)
        except OSError as exc:
            raise M0Error(
                f"frozen distribution member is missing: {member['declared_path']}"
            ) from exc
        if resolved in frozen_by_path:
            raise M0Error("frozen distribution resolves duplicate member paths")
        if (
            not resolved.is_file()
            or resolved.stat().st_size != member["size_bytes"]
            or file_sha256(resolved) != member["sha256"]
        ):
            raise M0Error(
                f"frozen distribution member drifted: {member['declared_path']}"
            )
        frozen_by_path[resolved] = member

    observed_modules = modules if modules is not None else sys.modules
    loaded: list[dict[str, Any]] = []
    for module_name in sorted(observed_modules):
        if module_name != module_prefix and not module_name.startswith(
            module_prefix + "."
        ):
            continue
        module = observed_modules[module_name]
        file_value = getattr(module, "__file__", None)
        spec = getattr(module, "__spec__", None)
        spec_origin = getattr(spec, "origin", None)
        origins = [
            value
            for value in (file_value, spec_origin)
            if value not in {None, "built-in", "frozen"}
        ]
        if not origins:
            raise M0Error(
                f"loaded module has no attested file origin: {module_name}"
            )
        try:
            origin_paths = [Path(value).resolve(strict=True) for value in origins]
        except (OSError, TypeError) as exc:
            raise M0Error(f"loaded module origin is unavailable: {module_name}") from exc
        if len(set(origin_paths)) != 1:
            raise M0Error(f"loaded module __file__/spec origin differ: {module_name}")
        origin = origin_paths[0]
        member = frozen_by_path.get(origin)
        if member is None:
            raise M0Error(
                f"loaded module is outside the frozen distribution: {module_name}"
            )
        observed_hash = file_sha256(origin)
        if (
            origin.stat().st_size != member["size_bytes"]
            or observed_hash != member["sha256"]
        ):
            raise M0Error(f"loaded module differs from frozen ledger: {module_name}")
        binary = origin.name.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES))
        loaded.append(
            {
                "module_name": module_name,
                "declared_path": member["declared_path"],
                "resolved_path": str(origin),
                "size_bytes": member["size_bytes"],
                "sha256": observed_hash,
                "binary": binary,
            }
        )
    names = [item["module_name"] for item in loaded]
    if module_prefix not in names:
        raise M0Error("loaded vLLM root module origin is absent")
    binary_modules = [
        item["module_name"] for item in loaded if item["binary"] is True
    ]
    evidence: dict[str, Any] = {
        "schema_version": LOADED_MODULE_SCHEMA,
        "distribution_name": distribution_name,
        "distribution_version": frozen_manifest["distribution_version"],
        "distribution_ledger_sha256": frozen_manifest["ledger_sha256"],
        "module_prefix": module_prefix,
        "loaded_module_count": len(loaded),
        "loaded_modules": loaded,
        "binary_module_count": len(binary_modules),
        "binary_modules": binary_modules,
    }
    evidence["evidence_sha256"] = semantic_sha256(evidence)
    return evidence


def validate_sbom_vllm_component(sbom: Mapping[str, Any], version: str) -> None:
    candidates: list[tuple[Any, Any]] = []
    if sbom.get("bomFormat") == "CycloneDX":
        components = sbom.get("components", [])
        if isinstance(components, list):
            candidates.extend(
                (item.get("name"), item.get("version"))
                for item in components
                if isinstance(item, dict)
            )
    if isinstance(sbom.get("spdxVersion"), str):
        packages = sbom.get("packages", [])
        if isinstance(packages, list):
            candidates.extend(
                (item.get("name"), item.get("versionInfo"))
                for item in packages
                if isinstance(item, dict)
            )
    if not any(
        isinstance(name, str)
        and name.casefold() == "vllm"
        and candidate_version == version
        for name, candidate_version in candidates
    ):
        raise M0Error("container SBOM lacks the exact installed vLLM component")


def validate_build_attestation(
    value: Mapping[str, Any],
    *,
    runtime: Mapping[str, Any],
    verify_files: bool = True,
    verify_installed: bool = True,
) -> dict[str, Any]:
    if set(value) != {
        "schema_version",
        "status",
        "package",
        "source",
        "wheel",
        "build",
        "installed_distribution",
        "container",
        "provenance",
    }:
        raise M0Error("vLLM build attestation key closure mismatch")
    rt = runtime["runtime"]
    if value["schema_version"] != BUILD_SCHEMA or value["status"] != "FROZEN":
        raise M0Error("vLLM build attestation is not frozen")
    if value["package"] != {"name": "vllm", "version": rt["version"]}:
        raise M0Error("vLLM build package version mismatch")
    source = value["source"]
    if not isinstance(source, dict) or value["source"] != {
        "repository": SOURCE_REPOSITORY,
        "git_commit": rt["git_commit"],
        "tree_sha256": source.get("tree_sha256"),
    }:
        raise M0Error("vLLM declared source commit mismatch")
    if (
        not isinstance(source["git_commit"], str)
        or not GIT_COMMIT_RE.fullmatch(source["git_commit"])
        or not isinstance(source["tree_sha256"], str)
        or not SHA256_RE.fullmatch(source["tree_sha256"])
    ):
        raise M0Error("invalid full vLLM source commit/tree identity")
    wheel = value["wheel"]
    build = value["build"]
    installed = value["installed_distribution"]
    container = value["container"]
    for label, entry in (
        ("wheel", wheel),
        ("build environment ledger", build),
        ("installed distribution", installed),
        ("container SBOM", container),
    ):
        if not isinstance(entry, dict):
            raise M0Error(f"{label} attestation is not an object")
    if (
        set(wheel) != {"path", "sha256"}
        or set(build)
        != {"command_argv", "environment_ledger_path", "environment_ledger_sha256"}
        or set(installed)
        != {"manifest_path", "manifest_file_sha256", "ledger_sha256"}
        or set(container) != {"image", "digest", "sbom_path", "sbom_sha256"}
        or not isinstance(build["command_argv"], list)
        or not build["command_argv"]
        or any(not isinstance(item, str) or not item for item in build["command_argv"])
        or container["image"] != rt["container_image"]
        or container["digest"] != rt["container_digest"]
        or not isinstance(container["digest"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", container["digest"])
        or not isinstance(installed["ledger_sha256"], str)
        or not SHA256_RE.fullmatch(installed["ledger_sha256"])
    ):
        raise M0Error("vLLM build/container attestation contract mismatch")
    for section, path_key, hash_key in (
        (wheel, "path", "sha256"),
        (build, "environment_ledger_path", "environment_ledger_sha256"),
        (installed, "manifest_path", "manifest_file_sha256"),
        (container, "sbom_path", "sbom_sha256"),
    ):
        path_value = section[path_key]
        expected_hash = section[hash_key]
        if (
            not isinstance(path_value, str)
            or not Path(path_value).is_absolute()
            or not isinstance(expected_hash, str)
            or not SHA256_RE.fullmatch(expected_hash)
        ):
            raise M0Error(f"invalid attested file binding: {path_key}")
        if verify_files:
            path = Path(path_value)
            if (
                not path.is_file()
                or path.is_symlink()
                or file_sha256(path) != expected_hash
            ):
                raise M0Error(f"attested file/hash mismatch: {path_key}")
    if (
        not isinstance(value["provenance"], dict)
        or set(value["provenance"])
        != {"builder_identity", "build_timestamp_utc", "attestation_method"}
        or any(
            not isinstance(item, str) or not item
            for item in value["provenance"].values()
        )
    ):
        raise M0Error("vLLM build provenance is empty")
    if verify_files:
        sbom = load_json(Path(container["sbom_path"]))
        if not (
            isinstance(sbom.get("spdxVersion"), str)
            or isinstance(sbom.get("bomFormat"), str)
        ):
            raise M0Error("container SBOM is neither SPDX nor CycloneDX JSON")
        validate_sbom_vllm_component(sbom, rt["version"])
        manifest = load_json(Path(installed["manifest_path"]))
        validate_distribution_manifest(manifest)
        if (
            manifest["distribution_version"] != rt["version"]
            or manifest["ledger_sha256"] != installed["ledger_sha256"]
        ):
            raise M0Error("installed vLLM distribution binding mismatch")
        if verify_installed and manifest != build_installed_distribution_manifest("vllm"):
            raise M0Error("installed vLLM files differ from frozen distribution ledger")
        return manifest
    return {}


def validate_runtime_attestation(
    runtime: Mapping[str, Any], *, verify_installed: bool = True
) -> dict[str, Any]:
    binding = runtime.get("runtime_attestation")
    if not isinstance(binding, dict) or set(binding) != {
        "build_attestation_path",
        "build_attestation_file_sha256",
    }:
        raise M0Error("runtime build-attestation file binding is missing")
    path = Path(binding["build_attestation_path"])
    claimed_hash = binding["build_attestation_file_sha256"]
    if (
        not path.is_absolute()
        or not path.is_file()
        or path.is_symlink()
        or not isinstance(claimed_hash, str)
        or not SHA256_RE.fullmatch(claimed_hash)
        or file_sha256(path) != claimed_hash
    ):
        raise M0Error("runtime build-attestation file/hash mismatch")
    value = load_json(path)
    validate_build_attestation(
        value, runtime=runtime, verify_files=True, verify_installed=verify_installed
    )
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise M0Error("installed-distribution manifest output already exists")
    value = build_installed_distribution_manifest()
    write_new_json(args.output, value)
    print(value["ledger_sha256"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M0Error as exc:
        raise SystemExit(f"HARD-STOP: {exc}") from exc
