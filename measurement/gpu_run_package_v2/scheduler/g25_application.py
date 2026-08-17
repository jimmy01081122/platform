"""Fail-closed approval and hardware gates for the G2.5 GPU application.

This module deliberately contains no GPU discovery implementation.  Production
code must pass an explicit provider, while unit tests can provide immutable CPU
fixtures without importing torch or invoking ``nvidia-smi``.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import base64
import csv
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from scheduler.g25_runtime_closure import (
    build_attested_python_argv,
    verify_static_system_closure,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
APPROVAL_SCHEMA_PATH = PACKAGE_ROOT / "schemas/g25_gpu_pilot_approval.schema.json"
REVIEW_SCHEMA_PATH = PACKAGE_ROOT / "schemas/g25_same_source_review.schema.json"
EVALUATION_SCHEMA_PATH = PACKAGE_ROOT / "schemas/g25_5_6sol_evaluation.schema.json"
RUNTIME_INVENTORY_PATH = PACKAGE_ROOT / "configs/runtime/g25_local_runtime_v1.json"
SESSION_ID = "granite-c1a-g25-qualification-r1-20260719"
NVIDIA_SMI_TIMEOUT_SECONDS = 10
BF16_PROBE_TIMEOUT_SECONDS = 30
RUNTIME_PYTHON = Path("/usr/bin/python3")
NVIDIA_SMI = Path("/usr/lib/wsl/lib/nvidia-smi")


class ApprovalValidationError(ValueError):
    """The owner record does not authorize the exact requested application."""


class DynamicPreflightError(ValueError):
    """Runtime or hardware facts differ from the approved environment."""


class DecisionRecordValidationError(ValueError):
    """An independent review or 5.6sol record is absent or does not match."""


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_tree_fingerprint(root: Path) -> dict[str, Any]:
    """Hash every regular file and symlink path without following directory links."""
    root = root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise DynamicPreflightError(f"exact tree root is missing or unsafe: {root}")
    digest = hashlib.sha256()
    file_count = 0
    symlink_count = 0
    total_bytes = 0
    for directory, directories, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directories.sort()
        files.sort()
        retained_directories: list[str] = []
        for name in directories:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                descriptor = {
                    "kind": "symlink", "path": relative,
                    "target": os.readlink(candidate),
                }
                digest.update(json.dumps(
                    descriptor, sort_keys=True, separators=(",", ":")
                ).encode("utf-8") + b"\n")
                symlink_count += 1
            else:
                retained_directories.append(name)
        directories[:] = retained_directories
        for name in files:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                descriptor = {
                    "kind": "symlink", "path": relative,
                    "target": os.readlink(candidate),
                }
                symlink_count += 1
            elif stat.S_ISREG(mode):
                size = candidate.stat().st_size
                descriptor = {
                    "bytes": size, "kind": "file", "path": relative,
                    "sha256": _sha256_file(candidate),
                }
                file_count += 1
                total_bytes += size
            else:
                raise DynamicPreflightError(
                    f"exact tree contains unsupported filesystem entry: {relative}"
                )
            digest.update(json.dumps(
                descriptor, sort_keys=True, separators=(",", ":")
            ).encode("utf-8") + b"\n")
    return {
        "algorithm": "sorted-canonical-entry-jsonl-sha256-v1",
        "file_count": file_count,
        "symlink_count": symlink_count,
        "total_file_bytes": total_bytes,
        "tree_sha256": digest.hexdigest(),
    }


def _verify_exact_tree(root: Path, expected: Mapping[str, Any], label: str) -> None:
    if (
        not root.is_absolute()
        or root.is_symlink()
        or not root.is_dir()
        or root.resolve(strict=True) != root
    ):
        raise DynamicPreflightError(f"{label} root is not an exact real directory")
    if set(expected) != {
        "algorithm", "file_count", "symlink_count", "total_file_bytes",
        "tree_sha256",
    } or expected.get("algorithm") != "sorted-canonical-entry-jsonl-sha256-v1":
        raise DynamicPreflightError(f"{label} exact-tree descriptor differs")
    if _exact_tree_fingerprint(root) != dict(expected):
        raise DynamicPreflightError(f"{label} exact file set or content differs")


def _external_symlink_targets(root: Path) -> set[Path]:
    targets: set[Path] = set()
    for directory, directories, files in os.walk(
        root, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        entries = [directory_path / name for name in directories + files]
        directories[:] = [
            name for name in directories
            if not (directory_path / name).is_symlink()
        ]
        for entry in entries:
            if not entry.is_symlink():
                continue
            try:
                target = entry.resolve(strict=True)
                target.relative_to(root)
            except ValueError:
                targets.add(target)
            except OSError as exc:
                raise DynamicPreflightError(
                    f"exact tree contains a broken symlink: {entry}"
                ) from exc
    return targets


def load_runtime_inventory() -> dict[str, Any]:
    """Load the package-tracked inventory that defines the only approved runtime."""
    value = json.loads(RUNTIME_INVENTORY_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DynamicPreflightError("runtime inventory must be a JSON object")
    required = {
        "schema_version", "system_closure_relative", "system_closure_sha256",
        "runtime_root_relative", "requirements_lock_relative",
        "requirements_lock_sha256", "clean_environment", "python_no_user_site",
        "runtime_root_absolute", "stdlib_root", "driver_runtime_root",
        "isolated_python", "runtime_tree", "stdlib_tree", "driver_runtime_tree",
        "system_files", "interpreter", "tools", "distributions", "import_roots",
    }
    if set(value) != required or value.get("schema_version") != "g25-local-runtime-inventory-v2":
        raise DynamicPreflightError("runtime inventory shape differs from the frozen contract")
    return value


def verify_runtime_inventory(
    *, verify_record_files: bool = True, verify_exact_trees: bool = True,
    require_isolated: bool = False,
) -> dict[str, Any]:
    """Verify the interpreter, tools and hashed files of all pinned distributions."""
    inventory = load_runtime_inventory()
    static_closure = verify_static_system_closure(
        enforce_environment=require_isolated
    )
    configured_runtime_root = Path(inventory["runtime_root_absolute"])
    if (
        not configured_runtime_root.is_absolute()
        or configured_runtime_root.is_symlink()
        or configured_runtime_root.resolve(strict=True) != configured_runtime_root
    ):
        raise DynamicPreflightError("private runtime root is not an exact real directory")
    runtime_root = configured_runtime_root
    package_runtime = PACKAGE_ROOT / inventory["runtime_root_relative"]
    if package_runtime.exists() and package_runtime.resolve(strict=True) != runtime_root:
        raise DynamicPreflightError("package-relative and absolute runtime roots differ")
    if not runtime_root.is_dir() or runtime_root.is_symlink():
        raise DynamicPreflightError("package-local runtime root is missing or unsafe")
    environment_root = os.environ.get("G25_RUNTIME_ROOT")
    if environment_root is not None and Path(
        environment_root
    ).resolve(strict=True) != runtime_root:
        raise DynamicPreflightError("G25_RUNTIME_ROOT differs from frozen runtime")
    if inventory["isolated_python"] != {
        "flags": ["-I", "-S", "-B", "-X", "utf8"],
        "global_site_enabled": False,
        "pythonpath_enabled": False,
    }:
        raise DynamicPreflightError("isolated Python policy differs")
    if require_isolated:
        if not (
            sys.flags.isolated and sys.flags.no_site
            and sys.flags.dont_write_bytecode and sys.flags.utf8_mode == 1
        ):
            raise DynamicPreflightError(
                "application is not running under python -I -S -B -X utf8"
            )
        forbidden_paths = {
            "/usr/local/lib/python3.10/dist-packages",
            "/usr/lib/python3/dist-packages",
            "/usr/lib/python3.10/dist-packages",
        }
        if forbidden_paths.intersection(sys.path):
            raise DynamicPreflightError("global site path is active in isolated runtime")
    if verify_exact_trees:
        _verify_exact_tree(runtime_root, inventory["runtime_tree"], "private runtime")
        _verify_exact_tree(
            Path(inventory["stdlib_root"]), inventory["stdlib_tree"], "Python stdlib"
        )
        _verify_exact_tree(
            Path(inventory["driver_runtime_root"]),
            inventory["driver_runtime_tree"],
            "GPU driver runtime",
        )
    lock_path = (PACKAGE_ROOT / inventory["requirements_lock_relative"]).resolve(strict=True)
    if _sha256_file(lock_path) != inventory["requirements_lock_sha256"]:
        raise DynamicPreflightError("requirements.lock differs from runtime inventory")

    interpreter = inventory["interpreter"]
    argv_python = Path(interpreter["argv_path"])
    real_python = argv_python.resolve(strict=True)
    if (
        str(real_python) != interpreter["realpath"]
        or _sha256_file(real_python) != interpreter["sha256"]
    ):
        raise DynamicPreflightError("Python interpreter identity differs")
    for label, tool in inventory["tools"].items():
        tool_path = Path(tool["path"])
        if not tool_path.is_absolute() or tool_path.resolve(strict=True) != tool_path:
            raise DynamicPreflightError(f"{label} tool path is not an exact realpath")
        if _sha256_file(tool_path) != tool["sha256"]:
            raise DynamicPreflightError(f"{label} executable identity differs")
    system_files = inventory["system_files"]
    if not isinstance(system_files, list) or not system_files:
        raise DynamicPreflightError("system dependency inventory is empty")
    if len({item.get("path") for item in system_files if isinstance(item, dict)}) != len(
        system_files
    ):
        raise DynamicPreflightError("system dependency inventory contains duplicates")
    for item in system_files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise DynamicPreflightError("system dependency descriptor differs")
        dependency = Path(item["path"])
        if (
            not dependency.is_absolute()
            or dependency.is_symlink()
            or not dependency.is_file()
            or dependency.resolve(strict=True) != dependency
        ):
            raise DynamicPreflightError("system dependency is missing")
        if _sha256_file(dependency.resolve(strict=True)) != item["sha256"]:
            raise DynamicPreflightError(f"system dependency differs: {dependency}")
    allowed_external_targets = {
        real_python,
        *(Path(item["path"]) for item in system_files),
    }
    external_targets = set().union(*(
        _external_symlink_targets(root)
        for root in (
            runtime_root,
            Path(inventory["stdlib_root"]),
            Path(inventory["driver_runtime_root"]),
        )
    ))
    if external_targets - allowed_external_targets:
        raise DynamicPreflightError(
            "exact trees contain an external symlink without bound target evidence"
        )

    distributions = inventory["distributions"]
    if not isinstance(distributions, list) or len(distributions) != 71:
        raise DynamicPreflightError("runtime must bind exactly all 71 installed distributions")
    if len({item.get("dist_info") for item in distributions if isinstance(item, dict)}) != 71:
        raise DynamicPreflightError("runtime distribution inventory contains duplicates")
    observed_versions: dict[str, str] = {}
    verified_file_count = 0
    for item in distributions:
        if not isinstance(item, dict) or set(item) != {
            "name", "version", "dist_info", "record_sha256"
        }:
            raise DynamicPreflightError("runtime distribution descriptor differs")
        dist_info = runtime_root / item["dist_info"]
        record = dist_info / "RECORD"
        if not dist_info.is_dir() or dist_info.is_symlink() or not record.is_file():
            raise DynamicPreflightError(f"runtime distribution is missing: {item['name']}")
        if _sha256_file(record) != item["record_sha256"]:
            raise DynamicPreflightError(f"runtime RECORD differs: {item['name']}")
        distribution = importlib.metadata.PathDistribution(dist_info)
        if distribution.version != item["version"]:
            raise DynamicPreflightError(f"runtime version differs: {item['name']}")
        observed_versions[item["name"]] = distribution.version
        if verify_record_files:
            with record.open("r", encoding="utf-8", newline="") as stream:
                for row in csv.reader(stream):
                    if len(row) != 3:
                        raise DynamicPreflightError(f"malformed RECORD row: {item['name']}")
                    relative, encoded_hash, encoded_size = row
                    candidate_relative = Path(relative)
                    if (
                        not encoded_hash
                        or ".." in candidate_relative.parts
                        or "__pycache__" in candidate_relative.parts
                        or candidate_relative.suffix == ".pyc"
                    ):
                        continue
                    unresolved_candidate = runtime_root / candidate_relative
                    if unresolved_candidate.is_symlink():
                        raise DynamicPreflightError(
                            f"runtime RECORD file may not be a symlink: {relative}"
                        )
                    candidate = unresolved_candidate.resolve(strict=True)
                    try:
                        candidate.relative_to(runtime_root)
                    except ValueError as exc:
                        raise DynamicPreflightError(
                            f"runtime RECORD path escapes root: {item['name']}"
                        ) from exc
                    if not candidate.is_file() or candidate.is_symlink():
                        raise DynamicPreflightError(
                            f"runtime RECORD file is missing or unsafe: {relative}"
                        )
                    algorithm, separator, digest = encoded_hash.partition("=")
                    if algorithm != "sha256" or separator != "=":
                        raise DynamicPreflightError(
                            f"runtime RECORD uses an unsupported hash: {relative}"
                        )
                    expected_digest = base64.urlsafe_b64decode(
                        digest + "=" * (-len(digest) % 4)
                    ).hex()
                    if _sha256_file(candidate) != expected_digest:
                        raise DynamicPreflightError(f"runtime file hash differs: {relative}")
                    if encoded_size and candidate.stat().st_size != int(encoded_size):
                        raise DynamicPreflightError(f"runtime file size differs: {relative}")
                    verified_file_count += 1

    import_roots: dict[str, str] = {}
    for module, relative in inventory["import_roots"].items():
        unresolved_candidate = runtime_root / relative
        if unresolved_candidate.is_symlink():
            raise DynamicPreflightError(f"runtime import root is a symlink: {module}")
        candidate = unresolved_candidate.resolve(strict=True)
        candidate.relative_to(runtime_root)
        if not candidate.is_file() or candidate.is_symlink():
            raise DynamicPreflightError(f"runtime import root is missing: {module}")
        import_roots[module] = str(candidate)
    return {
        "runtime_inventory_sha256": _sha256_file(RUNTIME_INVENTORY_PATH),
        "requirements_lock_sha256": inventory["requirements_lock_sha256"],
        "runtime_root": str(runtime_root),
        "runtime_tree_sha256": inventory["runtime_tree"]["tree_sha256"],
        "stdlib_tree_sha256": inventory["stdlib_tree"]["tree_sha256"],
        "driver_runtime_tree_sha256": inventory["driver_runtime_tree"]["tree_sha256"],
        "system_files_sha256": canonical_hash(system_files),
        "system_closure_sha256": inventory["system_closure_sha256"],
        "static_dependency_edges_sha256": static_closure[
            "dependency_edges_sha256"
        ],
        "python_executable": str(argv_python),
        "python_realpath": str(real_python),
        "python_version": interpreter["version"],
        "distribution_versions": observed_versions,
        "import_roots": import_roots,
        "verified_record_file_count": verified_file_count if verify_record_files else None,
    }


@dataclass(frozen=True)
class ApprovalExpectations:
    """Values independently derived from the reviewed package and invocation."""

    argv: tuple[str, ...]
    annotated_tag: str
    tag_object: str
    commit: str
    tree: str
    package_tree: str
    bindings: Mapping[str, str]
    session_id: str = SESSION_ID


def _load_schema() -> dict[str, Any]:
    return json.loads(APPROVAL_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_regular_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise DecisionRecordValidationError(
                f"{label} path must be a regular non-symlink file"
            )
        value = json.loads(path.read_text(encoding="utf-8"))
    except DecisionRecordValidationError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionRecordValidationError(f"{label} could not be loaded: {exc}") from exc
    if not isinstance(value, dict):
        raise DecisionRecordValidationError(f"{label} must be a JSON object")
    return value


def _validate_decision_schema(
    value: Mapping[str, Any], schema_path: Path, *, label: str
) -> dict[str, Any]:
    import jsonschema

    record = dict(value)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator(schema).validate(record)
    except jsonschema.ValidationError as exc:
        raise DecisionRecordValidationError(
            f"{label} schema validation failed: {exc.message}"
        ) from exc
    return record


def load_and_validate_review_record(
    path: Path,
    *,
    target: Mapping[str, str],
    source_bindings: Mapping[str, str],
    package_identity: Mapping[str, Any],
    expected_argv: tuple[str, ...],
) -> dict[str, Any]:
    """Parse the independent three-role record instead of trusting owner assertions."""
    value = _validate_decision_schema(
        _load_regular_json(path, label="same-source review record"),
        REVIEW_SCHEMA_PATH,
        label="same-source review record",
    )
    if value["review_target"] != dict(target):
        raise DecisionRecordValidationError(
            "same-source review tag, object, commit, tree, or package tree differs"
        )
    if value["source_bindings"] != dict(source_bindings):
        raise DecisionRecordValidationError(
            "same-source review source bindings differ from the reviewed package"
        )
    if value["package_identity"] != dict(package_identity):
        raise DecisionRecordValidationError(
            "same-source review package inventory or ledger identity differs"
        )
    command = value["exact_command"]
    if command["argv_sha256"] != canonical_hash(command["argv"]):
        raise DecisionRecordValidationError(
            "same-source review argv hash is not canonical"
        )
    if command["argv"] != list(expected_argv):
        raise DecisionRecordValidationError(
            "same-source review did not assess the exact application argv"
        )
    reviewer_ids = [role["reviewer_id"] for role in value["roles"].values()]
    if len(set(reviewer_ids)) != 3:
        raise DecisionRecordValidationError(
            "same-source review requires three distinct reviewer identities"
        )
    return value


def load_and_validate_evaluation_record(
    path: Path,
    *,
    target: Mapping[str, str],
    source_bindings: Mapping[str, str],
    review_sha256: str,
    expected_argv: tuple[str, ...],
) -> dict[str, Any]:
    """Require a hash-bound gpt-5.6-sol GO over this review and exact argv."""
    value = _validate_decision_schema(
        _load_regular_json(path, label="5.6sol evaluation record"),
        EVALUATION_SCHEMA_PATH,
        label="5.6sol evaluation record",
    )
    if value["review_target"] != dict(target):
        raise DecisionRecordValidationError(
            "5.6sol evaluation target differs from the reviewed source"
        )
    if value["same_source_review_sha256"] != review_sha256:
        raise DecisionRecordValidationError(
            "5.6sol evaluation does not bind the supplied review record"
        )
    if value["source_bindings_sha256"] != canonical_hash(dict(source_bindings)):
        raise DecisionRecordValidationError(
            "5.6sol evaluation source binding set differs"
        )
    if value["package_checksum_ledger_sha256"] != source_bindings.get(
        "package_checksum_ledger_sha256"
    ):
        raise DecisionRecordValidationError(
            "5.6sol evaluation package ledger differs"
        )
    command = value["exact_command"]
    if command["argv_sha256"] != canonical_hash(command["argv"]):
        raise DecisionRecordValidationError(
            "5.6sol evaluation argv hash is not canonical"
        )
    if command["argv"] != list(expected_argv):
        raise DecisionRecordValidationError(
            "5.6sol evaluation did not assess the exact application argv"
        )
    return value


def validate_approval_record(
    record: Mapping[str, Any],
    expectations: ApprovalExpectations,
    *,
    now_epoch: float,
) -> dict[str, Any]:
    """Validate schema, expiry, command, review target, and every source hash."""
    import jsonschema

    value = dict(record)
    try:
        jsonschema.Draft202012Validator(_load_schema()).validate(value)
    except jsonschema.ValidationError as exc:
        raise ApprovalValidationError(
            f"owner approval schema validation failed: {exc.message}"
        ) from exc
    if not isinstance(now_epoch, (int, float)) or isinstance(now_epoch, bool):
        raise ApprovalValidationError("approval validation time must be numeric")
    now = float(now_epoch)
    if not math.isfinite(now) or now < 0:
        raise ApprovalValidationError("approval validation time must be finite")
    issued = float(value["issued_at_epoch"])
    expires = float(value["expires_at_epoch"])
    if issued >= expires:
        raise ApprovalValidationError("approval expiry must be after issue time")
    if now < issued or now >= expires:
        raise ApprovalValidationError("owner approval is not currently valid")

    expected_argv = list(expectations.argv)
    command = value["exact_command"]
    if command["argv_sha256"] != canonical_hash(command["argv"]):
        raise ApprovalValidationError("approval argv hash is not canonical")
    if command["argv"] != expected_argv:
        raise ApprovalValidationError("invoked command differs from owner-approved argv")
    if value["session_id"] != expectations.session_id:
        raise ApprovalValidationError("approval session differs from frozen session")
    target = value["review_target"]
    if target != {
        "annotated_tag": expectations.annotated_tag,
        "tag_object": expectations.tag_object,
        "commit": expectations.commit,
        "tree": expectations.tree,
        "package_tree": expectations.package_tree,
    }:
        raise ApprovalValidationError("review tag, commit, or tree differs")
    expected_bindings = dict(expectations.bindings)
    if value["bindings"] != expected_bindings:
        raise ApprovalValidationError("approval source bindings differ from reviewed package")
    if value["review"]["document_sha256"] != value["bindings"][
        "same_source_review_sha256"
    ]:
        raise ApprovalValidationError("review document hash is not bound consistently")
    if value["evaluation_gate"]["document_sha256"] != value["bindings"][
        "evaluation_record_sha256"
    ]:
        raise ApprovalValidationError(
            "5.6sol evaluation document hash is not bound consistently"
        )
    return value


def load_and_validate_approval(
    path: Path,
    expectations: ApprovalExpectations,
    *,
    now_epoch: float,
) -> dict[str, Any]:
    """Load one regular, non-symlink owner record and validate it fail closed."""
    try:
        if path.is_symlink() or not path.is_file():
            raise ApprovalValidationError("approval path must be a regular non-symlink file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ApprovalValidationError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalValidationError(f"owner approval could not be loaded: {exc}") from exc
    if not isinstance(value, dict):
        raise ApprovalValidationError("owner approval must be a JSON object")
    return validate_approval_record(value, expectations, now_epoch=now_epoch)


_FACT_KEYS = {
    "gpus", "compute_processes", "disk_free_bytes", "cuda_visible_devices",
    "runtime", "offline", "determinism",
}
_GPU_KEYS = {
    "index", "name", "uuid", "pci_bus_id", "total_vram_bytes",
    "free_vram_bytes", "bf16_supported",
}
_RUNTIME_KEYS = {
    "python", "torch", "transformers", "pyyaml", "jsonschema", "cuda",
    "python_executable", "python_realpath", "pythonpath",
    "python_no_user_site", "python_dont_write_bytecode",
    "python_isolated", "python_no_site", "python_ignore_environment",
    "runtime_inventory_sha256", "requirements_lock_sha256",
    "runtime_tree_sha256", "stdlib_tree_sha256", "driver_runtime_tree_sha256",
    "system_files_sha256", "system_closure_sha256",
    "static_dependency_edges_sha256",
    "module_files",
}
_OFFLINE_KEYS = {"hf_hub_offline", "transformers_offline"}
_DETERMINISM_KEYS = {
    "cublas_workspace_config", "cuda_launch_blocking", "pythonhashseed",
    "lc_all", "lang",
    "torch_deterministic_algorithms", "matmul_allow_tf32",
    "cudnn_allow_tf32", "cudnn_benchmark", "cudnn_deterministic",
    "bf16_reduced_precision_reduction", "fp16_reduced_precision_reduction",
}


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise DynamicPreflightError(f"{label} fields differ from the frozen fact contract")
    return value


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DynamicPreflightError(f"{label} must be a non-negative integer")
    return value


def validate_dynamic_preflight(
    facts: Mapping[str, Any], approval: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate supplied runtime facts without discovering or touching a GPU."""
    environment = _exact_keys(facts, _FACT_KEYS, "dynamic preflight")
    gpus = environment["gpus"]
    if not isinstance(gpus, list) or len(gpus) != 1:
        raise DynamicPreflightError("exactly one visible CUDA GPU is required")
    gpu = _exact_keys(gpus[0], _GPU_KEYS, "GPU")
    approved_hardware = approval["hardware"]
    for key in ("name", "uuid", "pci_bus_id", "total_vram_bytes"):
        if gpu[key] != approved_hardware[key]:
            raise DynamicPreflightError(f"GPU {key} differs from owner approval")
    _integer(gpu["total_vram_bytes"], "total VRAM")
    free_vram = _integer(gpu["free_vram_bytes"], "free VRAM")
    if free_vram < approved_hardware["minimum_free_vram_bytes"]:
        raise DynamicPreflightError("free VRAM is below the approved minimum")
    if gpu["bf16_supported"] is not True or approved_hardware["precision"] != "bf16":
        raise DynamicPreflightError("approved BF16 execution is not supported")
    processes = environment["compute_processes"]
    if not isinstance(processes, list) or processes:
        raise DynamicPreflightError("another GPU compute process forbids execution")
    disk = _integer(environment["disk_free_bytes"], "free disk")
    if disk < approved_hardware["minimum_free_disk_bytes"]:
        raise DynamicPreflightError("free disk is below the approved minimum")

    approved_env = approval["environment"]
    if environment["cuda_visible_devices"] != approved_env["cuda_visible_devices"]:
        raise DynamicPreflightError("CUDA_VISIBLE_DEVICES differs from owner approval")
    runtime = _exact_keys(environment["runtime"], _RUNTIME_KEYS, "runtime")
    if dict(runtime) != approval["runtime"]:
        raise DynamicPreflightError("runtime versions differ from owner approval")
    offline = _exact_keys(environment["offline"], _OFFLINE_KEYS, "offline")
    if dict(offline) != {
        "hf_hub_offline": approved_env["hf_hub_offline"],
        "transformers_offline": approved_env["transformers_offline"],
    }:
        raise DynamicPreflightError("offline environment is not frozen")
    deterministic = _exact_keys(
        environment["determinism"], _DETERMINISM_KEYS, "determinism"
    )
    expected_determinism = {
        "cublas_workspace_config": approved_env["cublas_workspace_config"],
        "cuda_launch_blocking": approved_env["cuda_launch_blocking"],
        "pythonhashseed": approved_env["pythonhashseed"],
        "lc_all": approved_env["lc_all"],
        "lang": approved_env["lang"],
        "torch_deterministic_algorithms": True,
        "matmul_allow_tf32": False,
        "cudnn_allow_tf32": False,
        "cudnn_benchmark": False,
        "cudnn_deterministic": True,
        "bf16_reduced_precision_reduction": False,
        "fp16_reduced_precision_reduction": False,
    }
    if dict(deterministic) != expected_determinism:
        raise DynamicPreflightError("deterministic runtime controls differ")
    return {
        "schema_version": "g25-dynamic-preflight-result-v1",
        "status": "pass",
        "session_id": approval["session_id"],
        "gpu": dict(gpu),
        "disk_free_bytes": disk,
        "runtime": dict(runtime),
        "offline": dict(offline),
        "determinism": dict(deterministic),
    }


def run_dynamic_preflight(
    provider: Callable[[Path], Mapping[str, Any]],
    root: Path,
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    """Call an explicit provider once and fail closed on provider or fact errors."""
    if not callable(provider):
        raise DynamicPreflightError("an explicit dynamic preflight provider is required")
    try:
        facts = provider(root)
    except Exception as exc:
        raise DynamicPreflightError(
            f"dynamic preflight provider failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(facts, Mapping):
        raise DynamicPreflightError("dynamic preflight provider must return an object")
    return validate_dynamic_preflight(facts, approval)


def _run_preflight_query(
    argv: list[str], *, label: str, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    """Run one bounded fact query and convert timeout into a closed failure."""
    try:
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{label} timed out after {timeout_seconds} seconds"
        ) from exc


def query_dynamic_preflight(root: Path) -> dict[str, Any]:
    """Collect the exact facts required by the approved local RTX 3050 start."""
    fields = "index,name,uuid,pci.bus_id,memory.total,memory.free"
    result = _run_preflight_query(
        [str(NVIDIA_SMI), f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        label="nvidia-smi GPU query",
        timeout_seconds=NVIDIA_SMI_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "nvidia-smi GPU query failed")
    gpus = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 6:
            raise RuntimeError(f"unexpected nvidia-smi row: {line}")
        gpus.append({
            "index": int(parts[0]), "name": parts[1], "uuid": parts[2],
            "pci_bus_id": parts[3],
            "total_vram_bytes": int(parts[4]) * 1024**2,
            "free_vram_bytes": int(parts[5]) * 1024**2,
            "bf16_supported": False,
        })
    bf16 = _run_preflight_query(
        build_attested_python_argv(
            "bf16-probe",
            package_root=PACKAGE_ROOT,
            python_executable=RUNTIME_PYTHON,
        ),
        label="BF16 capability query",
        timeout_seconds=BF16_PROBE_TIMEOUT_SECONDS,
    )
    if bf16.returncode != 0 or bf16.stdout.strip() not in {"0", "1"}:
        raise RuntimeError(bf16.stderr.strip() or "BF16 capability query failed")
    for gpu in gpus:
        gpu["bf16_supported"] = bf16.stdout.strip() == "1"
    # Query compute processes after the isolated BF16 probe has exited so the
    # long-lived parent never creates a CUDA context of its own.
    processes = _run_preflight_query(
        [str(NVIDIA_SMI), "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
        label="nvidia-smi process query",
        timeout_seconds=NVIDIA_SMI_TIMEOUT_SECONDS,
    )
    if processes.returncode != 0:
        raise RuntimeError(processes.stderr.strip() or "nvidia-smi process query failed")
    process_ids: list[int] = []
    for line in processes.stdout.splitlines():
        row = line.strip()
        if not row:
            continue
        if not row.isascii() or not row.isdecimal():
            raise RuntimeError(f"malformed nvidia-smi process row: {line}")
        pid = int(row)
        if pid <= 0 or pid in process_ids:
            raise RuntimeError(f"invalid nvidia-smi process pid: {line}")
        process_ids.append(pid)

    runtime_attestation = verify_runtime_inventory(
        verify_record_files=False, verify_exact_trees=True, require_isolated=True
    )
    import jsonschema
    import torch
    import transformers
    import yaml

    matmul = torch.backends.cuda.matmul
    return {
        "gpus": gpus,
        "compute_processes": process_ids,
        "disk_free_bytes": shutil.disk_usage(root).free,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "runtime": {
            "python": runtime_attestation["python_version"],
            "torch": importlib.metadata.version("torch"),
            "transformers": importlib.metadata.version("transformers"),
            "pyyaml": importlib.metadata.version("PyYAML"),
            "jsonschema": importlib.metadata.version("jsonschema"),
            "cuda": str(torch.version.cuda),
            "python_executable": str(RUNTIME_PYTHON),
            "python_realpath": str(RUNTIME_PYTHON.resolve(strict=True)),
            "pythonpath": os.environ.get("PYTHONPATH"),
            "python_no_user_site": os.environ.get("PYTHONNOUSERSITE"),
            "python_dont_write_bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
            "python_isolated": bool(sys.flags.isolated),
            "python_no_site": bool(sys.flags.no_site),
            "python_ignore_environment": bool(sys.flags.ignore_environment),
            "runtime_inventory_sha256": runtime_attestation["runtime_inventory_sha256"],
            "requirements_lock_sha256": runtime_attestation["requirements_lock_sha256"],
            "runtime_tree_sha256": runtime_attestation["runtime_tree_sha256"],
            "stdlib_tree_sha256": runtime_attestation["stdlib_tree_sha256"],
            "driver_runtime_tree_sha256": runtime_attestation[
                "driver_runtime_tree_sha256"
            ],
            "system_files_sha256": runtime_attestation["system_files_sha256"],
            "system_closure_sha256": runtime_attestation["system_closure_sha256"],
            "static_dependency_edges_sha256": runtime_attestation[
                "static_dependency_edges_sha256"
            ],
            "module_files": {
                "jsonschema": str(Path(jsonschema.__file__).resolve()),
                "torch": str(Path(torch.__file__).resolve()),
                "transformers": str(Path(transformers.__file__).resolve()),
                "yaml": str(Path(yaml.__file__).resolve()),
            },
        },
        "offline": {
            "hf_hub_offline": os.environ.get("HF_HUB_OFFLINE"),
            "transformers_offline": os.environ.get("TRANSFORMERS_OFFLINE"),
        },
        "determinism": {
            "cublas_workspace_config": os.environ.get("CUBLAS_WORKSPACE_CONFIG"),
            "cuda_launch_blocking": os.environ.get("CUDA_LAUNCH_BLOCKING"),
            "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
            "lc_all": os.environ.get("LC_ALL"),
            "lang": os.environ.get("LANG"),
            "torch_deterministic_algorithms": bool(
                torch.are_deterministic_algorithms_enabled()
            ),
            "matmul_allow_tf32": bool(matmul.allow_tf32),
            "cudnn_allow_tf32": bool(torch.backends.cudnn.allow_tf32),
            "cudnn_benchmark": bool(torch.backends.cudnn.benchmark),
            "cudnn_deterministic": bool(torch.backends.cudnn.deterministic),
            "bf16_reduced_precision_reduction": bool(
                getattr(matmul, "allow_bf16_reduced_precision_reduction", True)
            ),
            "fp16_reduced_precision_reduction": bool(
                getattr(matmul, "allow_fp16_reduced_precision_reduction", True)
            ),
        },
    }
