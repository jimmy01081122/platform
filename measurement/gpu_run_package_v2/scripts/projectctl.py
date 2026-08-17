#!/usr/bin/env python3
"""C1 model, trace, scheduler, package, and verification control plane."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from scheduler import (  # noqa: E402
    LOGICAL_PASSES,
    ExecutionLease,
    ExecutionLockBusy,
    SchedulerEngine,
    SchedulerStore,
    State,
    TimeBudget,
    expand_work_units,
    execution_lock,
    read_owner,
)
from scheduler.store import atomic_json, fsync_directory  # noqa: E402
from scheduler.validators import sha256_file, verify_complete  # noqa: E402
from scheduler.gpu_entrypoint_policy import (  # noqa: E402
    assert_qualification_route,
    hard_disabled_reason,
)
from collectors.trace_contract import build_execution_alignment_key  # noqa: E402
from scripts.c1_canonicalize import canonicalize  # noqa: E402
from scripts.c1_cleanroom_verify import verify as cleanroom_verify  # noqa: E402
from scripts.c1_quality import (  # noqa: E402
    build_contract_binding,
    compare_cross_pass_evidence,
    validate_contract_binding,
    validate_quality_artifact,
)
from scripts.c1_system_ir import build_system_ir  # noqa: E402

OK = 0
BLOCKED = 20
LOCAL_PROFILE = "local_rtx3050_c1a"
EXPECTED_GPU_NAME = "NVIDIA GeForce RTX 3050"
EXPECTED_SUITE_ID = "granite_c1_smoke_v1"
EXPECTED_SUITE_REVISION = "granite-c1-v1.1.0"
EXPECTED_SOURCE_MANIFEST_SHA256 = (
    "4de9eda6a8eabb5e49c897563033e2fa9d9a8b62db7b81790bb9a4c871f5621e"
)
EXPECTED_SELECTION_MANIFEST_SHA256 = (
    "f32aa23823c2b88f7f2fab2a11cbbafe39c10adc54d5570ca7474b27996cd2bd"
)
EXPECTED_SNAPSHOT_INVENTORY_SHA256 = (
    "3c7da0e1f7951acb0657e606bde10bf48e8ad6b8515c1960bf9defc37aff6fc4"
)
MIN_FREE_VRAM_BYTES = 5_000_000_000
MIN_DISK_BYTES = 8 * 1024**3
RUN_ROOT = Path(os.environ.get(
    "PROJECTCTL_RUN_ROOT", str(PACKAGE_ROOT / "scheduler_runs")
))
DIAGNOSTIC_ROOT = Path(os.environ.get(
    "PROJECTCTL_DIAGNOSTIC_ROOT", str(PACKAGE_ROOT / "diagnostic_runs")
))
QUALIFICATION_ROOT = Path(os.environ.get(
    "PROJECTCTL_QUALIFICATION_ROOT", str(PACKAGE_ROOT / "qualification_runs")
))
DIAGNOSTIC_SUITE_ID = "granite_c1_token_drift_diag_v1"
DIAGNOSTIC_EVIDENCE_CLASS = "diagnostic_non_c1"
DIAGNOSTIC_SUITE_PATH = (
    PACKAGE_ROOT / "configs/test_suites/granite_c1_token_drift_diag_v1.json"
)
DIAGNOSTIC_MODE = "token_drift_v1"
QUALITY_CONTRACT_PATH = (
    "configs/test_suites/granite_c1/c1_quality_contract_v2.json"
)
PARENT_DIAGNOSTIC_SESSION_ID = "granite-c1a-canary-v3-20260718"
PARENT_SUITE_SNAPSHOT_SHA256 = (
    "ca07a79d4212c783e588a505f5fb5a24d0af6561f3bd463020256eaa160a4b43"
)
PARENT_SESSION_RECORD_SHA256 = (
    "50994494930b8fb5282557d6c86d00f6000483ba39c72e25f84a15260294eb40"
)
GPU_PROVIDER = None
G25_GPU_PROVIDER = None


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def blocked(message: str, **details: Any) -> int:
    emit({"status": "blocked", "reason": message, **details})
    return BLOCKED


def snapshot_inventory(path: str | Path) -> dict[str, Any]:
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("--model-snapshot must be a real local directory")
    files = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"model snapshot symlink is forbidden: {item}")
        if not item.is_file():
            continue
        files.append({
            "path": item.relative_to(root).as_posix(),
            "bytes": item.stat().st_size,
            "sha256": sha256_file(item),
        })
    if not files:
        raise ValueError("model snapshot contains no files")
    canonical = json.dumps(
        files, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "absolute_path": str(root),
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "inventory_sha256": hashlib.sha256(canonical).hexdigest(),
        "files": files,
    }


def verify_snapshot(expected: dict[str, Any]) -> None:
    actual = snapshot_inventory(expected["absolute_path"])
    if actual != expected:
        raise ValueError("model snapshot identity changed since session start")


def _default_gpu_provider(root: Path) -> dict[str, Any]:
    fields = "index,name,uuid,pci.bus_id,memory.total,memory.free"
    result = subprocess.run(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        text=True, capture_output=True, check=False,
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
            "index": parts[0],
            "name": parts[1],
            "uuid": parts[2],
            "pci_bus_id": parts[3],
            "total_vram_bytes": int(parts[4]) * 1024**2,
            "free_vram_bytes": int(parts[5]) * 1024**2,
        })
    processes = subprocess.run(
        [
            "nvidia-smi", "--query-compute-apps=pid",
            "--format=csv,noheader,nounits",
        ],
        text=True, capture_output=True, check=False,
    )
    if processes.returncode != 0:
        raise RuntimeError(
            processes.stderr.strip() or "nvidia-smi process query failed"
        )
    pids = [
        line.strip() for line in processes.stdout.splitlines()
        if line.strip() and line.strip().isdigit()
    ]
    return {
        "gpus": gpus,
        "compute_processes": pids,
        "disk_free_bytes": shutil.disk_usage(root).free,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }


def hardware_preflight(root: Path) -> dict[str, Any]:
    provider = GPU_PROVIDER or _default_gpu_provider
    environment = provider(root)
    gpus = environment.get("gpus")
    if not isinstance(gpus, list) or len(gpus) != 1:
        raise ValueError("C1-A requires exactly one visible CUDA device")
    gpu = gpus[0]
    required = (
        "name", "uuid", "pci_bus_id", "total_vram_bytes", "free_vram_bytes"
    )
    if any(gpu.get(field) in (None, "") for field in required):
        raise ValueError("GPU identity/VRAM preflight fields are incomplete")
    if gpu["name"] != EXPECTED_GPU_NAME:
        raise ValueError(
            f"GPU model must be {EXPECTED_GPU_NAME}, got {gpu['name']}"
        )
    if environment.get("compute_processes"):
        raise ValueError("existing GPU compute process forbids execution")
    if int(gpu["free_vram_bytes"]) < MIN_FREE_VRAM_BYTES:
        raise ValueError(
            f"free VRAM must be >= {MIN_FREE_VRAM_BYTES} bytes"
        )
    if int(environment.get("disk_free_bytes", 0)) < MIN_DISK_BYTES:
        raise ValueError(f"free disk must be >= {MIN_DISK_BYTES} bytes")
    return {
        "schema_version": "c1-a-environment-v1",
        "gpu": gpu,
        "compute_processes": [],
        "disk_free_bytes": int(environment["disk_free_bytes"]),
        "cuda_visible_devices": environment.get("cuda_visible_devices"),
    }


def require_c1_a(suite: dict[str, Any], units: list, profile: str) -> None:
    if profile != LOCAL_PROFILE:
        raise ValueError(f"execution profile must be {LOCAL_PROFILE}")
    if suite.get("stage") != "C1-A":
        raise ValueError("local execution is restricted to C1-A")
    if suite.get("suite_id") != EXPECTED_SUITE_ID:
        raise ValueError(f"C1-A suite_id must be {EXPECTED_SUITE_ID}")
    if suite.get("suite_revision") != EXPECTED_SUITE_REVISION:
        raise ValueError(
            f"C1-A suite_revision must be {EXPECTED_SUITE_REVISION}"
        )
    if suite.get("repetitions") != 1:
        raise ValueError("C1-A requires exactly one repetition")
    if set(suite.get("logical_passes", suite.get("passes", []))) != {"P0", "P2"}:
        raise ValueError("C1-A requires exactly P0 and P2")
    if len(units) != 16:
        raise ValueError(f"C1-A requires exactly 16 work units, got {len(units)}")
    if len({unit.work_unit_id for unit in units}) != 16:
        raise ValueError("C1-A requires 16 unique work unit IDs")
    if len(suite.get("models", [])) != 1 or len(suite.get("samples", [])) != 8:
        raise ValueError("C1-A requires exactly one model and eight samples")


def verify_granite_suite_sources(suite: dict[str, Any]) -> None:
    declarations = (
        (
            suite.get("source_manifest_path"),
            suite.get("source_manifest_sha256"),
            "configs/test_suites/frozen/v1.4.0/sample_manifest.jsonl",
            EXPECTED_SOURCE_MANIFEST_SHA256,
        ),
        (
            suite.get("selection_manifest_path"),
            suite.get("selection_manifest_sha256"),
            "configs/test_suites/granite_c1/sample_manifest.jsonl",
            EXPECTED_SELECTION_MANIFEST_SHA256,
        ),
        (
            suite.get("snapshot_inventory_path"),
            suite.get("snapshot_inventory_sha256"),
            "configs/test_suites/granite_c1/snapshot_inventory.json",
            EXPECTED_SNAPSHOT_INVENTORY_SHA256,
        ),
    )
    for declared_path, declared_hash, expected_path, expected_hash in declarations:
        if declared_path != expected_path or declared_hash != expected_hash:
            raise ValueError(f"Granite suite declaration drift: {expected_path}")
        path = (PACKAGE_ROOT / expected_path).resolve()
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ValueError(f"Granite suite source hash mismatch: {expected_path}")


def suite_snapshot_sha256(path: Path) -> str:
    return sha256_file(path)


def suite_candidates(name: str) -> list[Path]:
    supplied = Path(name)
    base = PACKAGE_ROOT / "configs/test_suites"
    return [
        supplied,
        base / name,
        base / f"{name}.json",
        base / f"{name}.yaml",
        base / name / "suite.json",
        base / name / "suite.yaml",
    ]


def load_structured(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        value = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required to read YAML suite files") from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("suite top level must be an object")
    return value


def _verified_package_source(relative: Any, label: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
    ):
        raise ValueError(f"{label} path is invalid")
    root = PACKAGE_ROOT.resolve()
    unresolved = PACKAGE_ROOT / relative
    current = unresolved
    while current != PACKAGE_ROOT:
        if current.is_symlink():
            raise ValueError(f"{label} source symlink is forbidden")
        current = current.parent
    path = unresolved.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} source escapes package root") from exc
    if not path.is_file():
        raise ValueError(f"{label} source is not a file")
    return path


def bind_quality_contract(suite: dict[str, Any]) -> dict[str, Any]:
    """Attach verified prospective v2 contract provenance to a suite snapshot."""
    if suite.get("evidence_class") == DIAGNOSTIC_EVIDENCE_CLASS:
        return suite
    contract_path = _verified_package_source(
        QUALITY_CONTRACT_PATH, "quality contract"
    )
    contract = load_structured(contract_path)
    evaluator_path = _verified_package_source(
        contract.get("evaluator_source_path"), "quality evaluator"
    )
    engine_path = _verified_package_source(
        contract.get("quality_engine_path"), "quality engine"
    )
    return {
        **suite,
        "quality_contract": build_contract_binding(
            contract,
            path=QUALITY_CONTRACT_PATH,
            source_sha256=sha256_file(contract_path),
            evaluator_source_sha256=sha256_file(evaluator_path),
            quality_engine_sha256=sha256_file(engine_path),
            samples=suite.get("samples", []),
        ),
    }


def verify_quality_contract_source(
    suite: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = suite.get("quality_contract")
    if not isinstance(binding, dict):
        raise ValueError("formal suite lacks C1 Quality Contract v2 binding")
    relative = binding.get("path")
    if relative != QUALITY_CONTRACT_PATH:
        raise ValueError("quality contract source path drift")
    path = _verified_package_source(relative, "quality contract")
    contract = load_structured(path)
    evaluator_relative = contract.get("evaluator_source_path")
    engine_relative = contract.get("quality_engine_path")
    if binding.get("evaluator_source_path") != evaluator_relative:
        raise ValueError("quality evaluator source path drift")
    if binding.get("quality_engine_path") != engine_relative:
        raise ValueError("quality engine source path drift")
    evaluator_path = _verified_package_source(
        evaluator_relative, "quality evaluator"
    )
    engine_path = _verified_package_source(engine_relative, "quality engine")
    validate_contract_binding(
        binding,
        contract,
        path=relative,
        source_sha256=sha256_file(path),
        evaluator_source_sha256=sha256_file(evaluator_path),
        quality_engine_sha256=sha256_file(engine_path),
        samples=suite.get("samples", []),
    )
    return contract, dict(binding)


def load_suite(name: str) -> tuple[dict[str, Any], Path]:
    for candidate in suite_candidates(name):
        path = candidate if candidate.is_absolute() else candidate.resolve()
        if path.is_file():
            value = load_structured(path)
            if name in value.get("suites", {}):
                return bind_quality_contract(
                    select_named_suite(value, path, name)
                ), path
            return bind_quality_contract(value), path
    base = PACKAGE_ROOT / "configs/test_suites"
    for path in sorted(base.glob("**/suite.yaml")):
        value = load_structured(path)
        if name in value.get("suites", {}):
            return bind_quality_contract(select_named_suite(value, path, name)), path
    raise FileNotFoundError(f"suite not found: {name}")


def select_named_suite(
    registry: dict[str, Any], path: Path, name: str
) -> dict[str, Any]:
    selected = registry["suites"][name]
    if not isinstance(selected, dict):
        raise ValueError(f"suite definition must be an object: {name}")
    manifest_path = path.parent / "sample_manifest.jsonl"
    rows = []
    if manifest_path.is_file():
        rows = [
            json.loads(line)
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rows = [row for row in rows if row.get("suite_id") == name]
    model = registry.get("model")
    model_id = model.get("id") if isinstance(model, dict) else None
    source_rows: dict[str, dict[str, Any]] = {}
    source = registry.get("source_suite")
    if isinstance(source, dict) and isinstance(source.get("manifest"), str):
        source_path = (PACKAGE_ROOT / source["manifest"]).resolve()
        if source_path.is_file():
            source_rows = {
                row["sample_id"]: row
                for row in (
                    json.loads(line)
                    for line in source_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            }

    def embed_source_sample(row: dict[str, Any]) -> dict[str, Any] | None:
        source_sample = source_rows.get(row.get("sample_id"))
        if source_sample is None:
            return None
        if source_sample.get("raw_sample_hash") != row.get("raw_sample_hash"):
            raise ValueError("selection/source raw sample identity mismatch")
        embedded = dict(source_sample)
        override = row.get("prompt_override")
        if override is not None:
            if (
                row.get("task_id") != "T0"
                or not isinstance(override, str)
                or not override
                or row.get("prompt_template_revision")
                != "granite-c1-t0-override-v1"
                or hashlib.sha256(override.encode("utf-8")).hexdigest()
                != row.get("prompt_hash")
            ):
                raise ValueError("invalid Granite C1 T0 prompt override")
            embedded["source_prompt_hash"] = embedded.get("prompt_hash")
            embedded["prompt"] = override
            embedded["prompt_hash"] = row["prompt_hash"]
            embedded["prompt_template_revision"] = row[
                "prompt_template_revision"
            ]
        elif (
            hashlib.sha256(str(embedded.get("prompt", "")).encode("utf-8")).hexdigest()
            != row.get("prompt_hash")
        ):
            raise ValueError("selection/source prompt hash mismatch")
        return embedded

    generation_path = path.parent / "generation_config.yaml"
    generation = load_structured(generation_path) if generation_path.is_file() else {}
    profile_name = selected.get("generation_profile")
    common = generation.get("common") if isinstance(generation.get("common"), dict) else {}
    profiles = generation.get("profiles") if isinstance(generation.get("profiles"), dict) else {}
    profile = profiles.get(profile_name, {}) if isinstance(profile_name, str) else {}
    return {
        **selected,
        "suite_id": name,
        "suite_revision": registry.get("suite_revision"),
        "source_manifest_path": (
            source.get("manifest") if isinstance(source, dict) else None
        ),
        "source_manifest_sha256": (
            source.get("manifest_sha256") if isinstance(source, dict) else None
        ),
        "selection_manifest_path": registry.get("selection_manifest"),
        "selection_manifest_sha256": registry.get("selection_manifest_sha256"),
        "snapshot_inventory_path": registry.get("snapshot_inventory"),
        "snapshot_inventory_sha256": registry.get("snapshot_inventory_sha256"),
        "models": [model_id] if model_id else [],
        # instance_id preserves explicitly repeated evaluation instances.
        "samples": [
            {
                **row,
                "sample_id": row.get("instance_id", row.get("sample_id")),
                "source_sample_id": row.get("sample_id"),
                "source_sample": embed_source_sample(row),
            }
            for row in rows
        ],
        "logical_passes": selected.get("passes"),
        "generation_config": {**common, **profile, "seed": generation.get(
            "determinism", {}
        ).get("seed", registry.get("seed", 0))},
        "model": model,
    }


def _string_ids(value: Any, *keys: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("suite requires a non-empty list")
    result = []
    for item in value:
        if isinstance(item, str) and item:
            result.append(item)
            continue
        if isinstance(item, dict):
            selected = next(
                (item[key] for key in keys if isinstance(item.get(key), str)),
                None,
            )
            if selected:
                result.append(selected)
                continue
        raise ValueError(f"suite item lacks one of {keys}: {item!r}")
    return result


def suite_units(suite: dict[str, Any]) -> list:
    models = _string_ids(suite.get("models"), "model_id", "id")
    samples_value = suite.get("samples")
    if samples_value is None and isinstance(suite.get("benchmarks"), list):
        samples_value = [
            sample
            for benchmark in suite["benchmarks"]
            if isinstance(benchmark, dict)
            for sample in benchmark.get("samples", [])
        ]
    samples = _string_ids(samples_value, "sample_id", "id")
    repetitions = suite.get("repetitions", 3)
    passes = suite.get("logical_passes", suite.get("passes", list(LOGICAL_PASSES)))
    if not isinstance(passes, list):
        raise ValueError("logical_passes must be a list")
    return expand_work_units(models, samples, repetitions, passes)


def collector_resolver(suite: dict[str, Any]):
    configured = suite.get("collector_commands", {})

    def resolve(unit):
        command = configured.get(unit.logical_pass) if isinstance(configured, dict) else None
        if command is None and unit.model_id in {
            "ibm-granite/granite-3.1-1b-a400m-instruct",
            "granite-3.1-1b-a400m",
        }:
            command = [sys.executable, "scripts/c1_worker.py"]
        if not isinstance(command, (str, list)) or not command:
            return None
        parts = shlex.split(command) if isinstance(command, str) else list(command)
        if not parts or any(not isinstance(part, str) for part in parts):
            return None
        executable = Path(parts[0])
        if "/" in parts[0] and not executable.is_absolute():
            parts[0] = str((PACKAGE_ROOT / executable).resolve())
        if (
            ("/" in parts[0] and not Path(parts[0]).is_file())
            or ("/" not in parts[0] and shutil.which(parts[0]) is None)
        ):
            return None
        for index, part in enumerate(parts[1:], 1):
            if part.endswith(".py") and not Path(part).is_absolute():
                parts[index] = str((PACKAGE_ROOT / part).resolve())
        return parts

    return resolve


def session_root(identifier: str | None) -> Path:
    if identifier:
        candidate = Path(identifier)
        return candidate.resolve() if candidate.is_absolute() else (RUN_ROOT / identifier).resolve()
    pointer = RUN_ROOT / "current_session.json"
    if not pointer.is_file():
        raise FileNotFoundError("no current scheduler session; pass --session")
    value = json.loads(pointer.read_text(encoding="utf-8"))
    return (RUN_ROOT / value["session_id"]).resolve()


def set_current(root: Path) -> None:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    atomic_json(RUN_ROOT / "current_session.json", {"session_id": root.name})


def load_session_metadata(
    root: Path, *, verify_model_snapshot: bool = True
) -> dict[str, Any]:
    value = json.loads((root / "session.json").read_text(encoding="utf-8"))
    if value.get("schema_version") != "projectctl-session-v2":
        raise ValueError("unsupported or missing immutable session metadata")
    snapshot = root / "suite_snapshot.json"
    if suite_snapshot_sha256(snapshot) != value.get("suite_snapshot_sha256"):
        raise ValueError("suite snapshot hash differs from immutable session")
    suite = json.loads(snapshot.read_text(encoding="utf-8"))
    if value.get("session_class") != DIAGNOSTIC_EVIDENCE_CLASS:
        verify_quality_contract_source(suite)
        if value.get("quality_contract") != suite.get("quality_contract"):
            raise ValueError("session quality contract binding differs from suite")
    if verify_model_snapshot:
        verify_snapshot(value["model_snapshot"])
    return value


def make_engine(
    root: Path, suite: dict[str, Any], session: dict[str, Any] | None = None,
    lease: ExecutionLease | None = None,
    extra_collector_env: dict[str, str] | None = None,
) -> SchedulerEngine:
    session = session or load_session_metadata(root)
    budget_value = suite.get("time_budget") or {}
    budget = TimeBudget(
        session_minutes=budget_value.get("session_minutes", 120),
        stop_dispatch_before_end_minutes=budget_value.get(
            "stop_dispatch_before_end_minutes", 15
        ),
        packaging_reserve_minutes=budget_value.get(
            "packaging_reserve_minutes", 15
        ),
    )
    return SchedulerEngine(
        SchedulerStore(root),
        collector_resolver(suite),
        budget=budget,
        max_attempts=int(suite.get("max_attempts", 3)),
        execution_deadline_epoch=float(session["execution_deadline_epoch"]),
        session_deadline_epoch=float(session["session_deadline_epoch"]),
        collector_timeout_seconds=min(
            float(suite.get("unit_timeout_seconds", 8 * 60)),
            8 * 60,
        ),
        execution_lease_fd=lease.fileno() if lease is not None else None,
        collector_env={
            "PROJECTCTL_SUITE_SNAPSHOT": str(root / "suite_snapshot.json"),
            "PROJECTCTL_SESSION_ID": root.name,
            "C1_MODEL_SNAPSHOT": session["model_snapshot"]["absolute_path"],
            "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
            "CUDA_LAUNCH_BLOCKING": "1",
            "PYTHONHASHSEED": "0",
            **(extra_collector_env or {}),
        },
    )


def model_command(args: argparse.Namespace) -> int:
    if args.model_action == "smoke":
        return blocked(hard_disabled_reason("c1_model_smoke"))
    if args.model_action != "smoke":
        return _model_command_impl(args)
    raise AssertionError(args.model_action)


def _model_command_impl(args: argparse.Namespace) -> int:
    aliases = {
        "granite-3.1-1b-a400m",
        "ibm-granite/granite-3.1-1b-a400m-instruct",
    }
    if args.model not in aliases:
        return blocked("model adapter unavailable", model=args.model)
    try:
        from adapters.models.granite_moe.adapter import GraniteMoeAdapter
        adapter = GraniteMoeAdapter(args.snapshot)
        report = adapter.preflight(snapshot_path=args.snapshot)
    except Exception as exc:
        return blocked("model preflight could not execute", error=str(exc))
    value = asdict(report)
    value["status"] = "eligible" if report.eligible else "blocked"
    if args.model_action == "preflight" or not report.eligible:
        emit(value)
        return OK if report.eligible else BLOCKED
    try:
        adapter.load_model(local_files_only=True)
        batch = adapter.tokenize(args.prompt)
        from adapters.models.contract import GenerationRequest
        result = adapter.generate(
            batch, GenerationRequest(max_new_tokens=args.max_new_tokens)
        )
        adapter.cleanup()
    except Exception as exc:
        return blocked("model smoke could not execute", error=str(exc))
    emit({
        "status": "complete" if result.return_code == 0 else "failed",
        "model_id": adapter.identity.model_id,
        "input_token_count": result.input_token_count,
        "output_token_count": result.output_token_count,
        "output_hash": result.output_hash,
        "stop_reason": result.stop_reason,
        "return_code": result.return_code,
    })
    return OK if result.return_code == 0 else BLOCKED


def start_run(args: argparse.Namespace) -> int:
    try:
        with execution_lock(RUN_ROOT) as lease:
            return _start_run_locked(args, lease)
    except ExecutionLockBusy as exc:
        return blocked(str(exc), owner=read_owner(RUN_ROOT))


def _start_run_locked(args: argparse.Namespace, lease: ExecutionLease) -> int:
    try:
        suite, suite_path = load_suite(args.suite)
        if suite.get("enabled") is False or suite.get("eligible") is False:
            reasons = suite.get("eligibility_blockers") or [
                "suite is disabled or ineligible"
            ]
            return blocked(
                "suite is not eligible for execution",
                suite=args.suite,
                blockers=reasons,
            )
        units = suite_units(suite)
        require_c1_a(suite, units, args.profile)
        verify_granite_suite_sources(suite)
        model_snapshot = snapshot_inventory(args.model_snapshot)
        environment = hardware_preflight(RUN_ROOT)
    except (OSError, ValueError, RuntimeError) as exc:
        return blocked(str(exc), suite=args.suite)
    identifier = args.session or (
        f"{Path(args.suite).stem}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    )
    root = session_root(identifier)
    if root.exists():
        return blocked("new session path already exists; use run resume", session=root.name)
    root.mkdir(parents=True, exist_ok=True)
    snapshot = root / "suite_snapshot.json"
    atomic_json(snapshot, suite)
    started_epoch = time.time()
    session = {
        "schema_version": "projectctl-session-v2",
        "session_id": root.name,
        "profile": LOCAL_PROFILE,
        "suite_snapshot_sha256": suite_snapshot_sha256(snapshot),
        "started_epoch": started_epoch,
        "execution_deadline_epoch": started_epoch + 105 * 60,
        "session_deadline_epoch": started_epoch + 120 * 60,
        "model_snapshot": model_snapshot,
        "environment": environment,
        "quality_contract": suite["quality_contract"],
    }
    atomic_json(root / "session.json", session)
    snapshot.chmod(0o444)
    (root / "session.json").chmod(0o444)
    set_current(root)
    engine = make_engine(root, suite, session, lease)
    result = run_c1_a(
        engine,
        units,
        suite=suite,
        canary_only=bool(getattr(args, "canary_only", False)),
    )
    statuses = summarize(engine.store)
    emit({
        "status": "blocked" if statuses.get(State.UNAVAILABLE.value) else "scheduled",
        "session_id": root.name,
        "suite_path": str(suite_path),
        "execution": result,
        "states": statuses,
    })
    return BLOCKED if result.get("fail_fast") else OK


def load_session(
    args: argparse.Namespace, lease: ExecutionLease | None = None
) -> tuple[Path, dict[str, Any], SchedulerEngine]:
    root = session_root(args.session)
    suite = json.loads((root / "suite_snapshot.json").read_text(encoding="utf-8"))
    session = load_session_metadata(root)
    units = suite_units(suite)
    require_c1_a(suite, units, session.get("profile"))
    verify_granite_suite_sources(suite)
    return root, suite, make_engine(root, suite, session, lease)


def summarize(store: SchedulerStore) -> dict[str, int]:
    result: dict[str, int] = {}
    for record in store.records():
        result[record["state"]] = result.get(record["state"], 0) + 1
    return result


def revalidate_hardware(root: Path, session: dict[str, Any]) -> dict[str, Any]:
    current = hardware_preflight(root)
    expected_gpu = session.get("environment", {}).get("gpu", {})
    current_gpu = current["gpu"]
    for field in ("name", "uuid", "pci_bus_id"):
        if current_gpu.get(field) != expected_gpu.get(field):
            raise ValueError(f"session GPU {field} mismatch")
    return current


FORMAL_GENERATION_FIELDS = (
    "input_token_ids",
    "output_token_ids",
    "stop_reason",
    "output_hash",
    "execution_alignment_key",
)


def _formal_generation_evidence(row: Any) -> dict[str, Any]:
    """Validate and select exact generation evidence used by formal gates."""
    if not isinstance(row, dict):
        raise ValueError("generation row must be an object")
    for name in ("input_token_ids", "output_token_ids"):
        values = row.get(name)
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                for value in values
            )
        ):
            raise ValueError(
                f"{name} must contain only non-negative integer token IDs"
            )
        count_name = name.removesuffix("_ids") + "_count"
        count = row.get(count_name)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count != len(values)
        ):
            raise ValueError(f"{count_name} differs from {name}")
    for name in ("stop_reason", "execution_alignment_key"):
        if not isinstance(row.get(name), str) or not row[name].strip():
            raise ValueError(f"{name} must be a non-empty string")
    if not re.fullmatch(r"[0-9a-f]{64}", row["execution_alignment_key"]):
        raise ValueError("execution_alignment_key is not a SHA-256 digest")
    try:
        expected_alignment_key = build_execution_alignment_key(row)
    except ValueError as exc:
        raise ValueError(f"execution alignment fields invalid: {exc}") from exc
    if row["execution_alignment_key"] != expected_alignment_key:
        raise ValueError(
            "execution_alignment_key does not match canonical execution fields"
        )
    expected_hash = hashlib.sha256(json.dumps(
        row["output_token_ids"], separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    if row.get("output_hash") != expected_hash:
        raise ValueError(
            "output_hash does not match canonical output_token_ids"
        )
    return {name: row[name] for name in FORMAL_GENERATION_FIELDS}


def _formal_quality_evidence(row: Any) -> dict[str, str]:
    if not isinstance(row, dict) or row.get("schema_version") != "c1-quality-v2":
        raise ValueError("formal quality row must be c1-quality-v2")
    if row.get("parser_outcome") != "parseable":
        raise ValueError("formal complete unit must be parseable")
    if row.get("task_outcome") not in {"correct", "incorrect"}:
        raise ValueError("formal complete unit task outcome is unresolved")
    if row.get("blocking_status") != "pass":
        raise ValueError("formal complete unit has blocking quality status")
    evaluator = row.get("evaluator")
    if not isinstance(evaluator, str) or not evaluator:
        raise ValueError("formal quality evaluator is missing")
    binding = row.get("quality_binding_sha256")
    if not isinstance(binding, str) or not re.fullmatch(r"[0-9a-f]{64}", binding):
        raise ValueError("formal quality binding hash is invalid")
    return {
        "evaluator": evaluator,
        "parser_outcome": row["parser_outcome"],
        "task_outcome": row["task_outcome"],
        "quality_binding_sha256": binding,
}


def _validate_artifact_schema(schema_name: str, row: Any) -> None:
    """Apply the package JSON schema before semantic formal-evidence checks."""
    if not isinstance(row, dict):
        raise ValueError(f"{schema_name} artifact must be an object")
    try:
        import jsonschema
    except ImportError as exc:
        raise RuntimeError("jsonschema runtime is not installed") from exc
    schema = json.loads(
        (PACKAGE_ROOT / "schemas" / schema_name).read_text(encoding="utf-8")
    )
    try:
        jsonschema.validate(row, schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"{schema_name} validation failed: {exc.message}") from exc


def compare_formal_passes(
    store: SchedulerStore,
    units: list,
    *,
    expected_passes: list[str] | tuple[str, ...],
    suite: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    quality_contract = contract_binding = None
    selections: dict[str, dict[str, Any]] = {}
    if suite is not None:
        quality_contract, contract_binding = verify_quality_contract_source(suite)
        selections = {
            row.get("sample_id"): row
            for row in suite.get("samples", [])
            if isinstance(row, dict)
        }
    evidence: dict[str, dict[str, Any]] = {}
    for unit in units:
        record = store.load(unit)
        if record["state"] != State.COMPLETE.value:
            return False, f"{unit.logical_pass} is {record['state']}"
        root = store.complete_dir / unit.work_unit_id
        try:
            generation_rows = [
                json.loads(line)
                for line in (root / "generation_results.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"{unit.logical_pass} generation evidence invalid: {exc}"
        if len(generation_rows) != 1:
            return False, f"{unit.logical_pass} requires one generation row"
        try:
            if suite is not None:
                _validate_artifact_schema(
                    "c1_benchmark.schema.json", generation_rows[0]
                )
            formal_generation = _formal_generation_evidence(generation_rows[0])
        except ValueError as exc:
            return False, f"{unit.logical_pass} generation evidence invalid: {exc}"
        quality_path = root / "quality_results.jsonl"
        if not quality_path.is_file():
            return False, f"{unit.logical_pass} quality evidence is missing"
        try:
            quality_rows = [
                json.loads(line)
                for line in quality_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"{unit.logical_pass} quality evidence invalid: {exc}"
        if len(quality_rows) != 1:
            return False, f"{unit.logical_pass} requires one quality row"
        try:
            if suite is not None:
                selection = selections.get(unit.sample_id)
                if not isinstance(selection, dict) or not isinstance(
                    selection.get("source_sample"), dict
                ):
                    raise ValueError("suite sample lacks frozen source sample")
                generation_row = generation_rows[0]
                quality_row = quality_rows[0]
                _validate_artifact_schema("c1_quality.schema.json", quality_row)
                if (
                    generation_row.get("sample_id")
                    != selection.get("source_sample_id")
                    or generation_row.get("repetition_id") != unit.repetition
                    or quality_row.get("pass_id") != unit.logical_pass
                ):
                    raise ValueError("formal artifact identity differs from work unit")
                validate_quality_artifact(
                    quality_row,
                    contract=quality_contract,
                    contract_binding=contract_binding,
                    sample=selection["source_sample"],
                    generation=generation_row,
                )
            formal_quality = _formal_quality_evidence(quality_rows[0])
        except ValueError as exc:
            return False, f"{unit.logical_pass} quality evidence invalid: {exc}"
        evidence[unit.logical_pass] = {
            "generation": formal_generation,
            "quality": formal_quality,
        }
    findings = compare_cross_pass_evidence(
        evidence, expected_passes=expected_passes
    )
    if findings:
        finding = findings[0]
        pass_id = finding.get("pass_id", "suite")
        field = finding.get("field", finding["kind"])
        return False, f"P0/{pass_id} {field} drift"
    return True, None


def _pair_evidence(
    store: SchedulerStore, pair: list, suite: dict[str, Any] | None = None
) -> tuple[bool, str | None]:
    return compare_formal_passes(
        store, pair, expected_passes=("P0", "P2"), suite=suite
    )


def run_c1_a(
    engine: SchedulerEngine,
    units: list,
    *,
    suite: dict[str, Any] | None = None,
    canary_only: bool = False,
) -> dict[str, int | bool | str]:
    engine.register(units)
    by_sample: dict[tuple[str, str, int], dict[str, Any]] = {}
    for unit in units:
        key = (unit.model_id, unit.sample_id, unit.repetition)
        by_sample.setdefault(key, {})[unit.logical_pass] = unit
    ordered_keys = sorted(by_sample)
    dispatched = 0
    for index, key in enumerate(ordered_keys):
        passes = by_sample[key]
        if set(passes) != {"P0", "P2"}:
            return {
                "dispatched": dispatched, "budget_exhausted": False,
                "fail_fast": True, "reason": "C1-A pair shape invalid",
            }
        pair = [passes["P0"], passes["P2"]]
        for unit in pair:
            if State(engine.store.load(unit)["state"]) is State.COMPLETE:
                continue
            if not engine._can_dispatch():
                return {
                    "dispatched": dispatched, "budget_exhausted": True,
                    "fail_fast": True, "reason": "execution deadline reached",
                }
            state = engine.run_unit(unit)
            dispatched += 1
            if state is not State.COMPLETE:
                return {
                    "dispatched": dispatched, "budget_exhausted": False,
                    "fail_fast": True, "failed_work_unit_id": unit.work_unit_id,
                    "failed_state": state.value,
                }
        aligned, reason = _pair_evidence(engine.store, pair, suite)
        if not aligned:
            return {
                "dispatched": dispatched, "budget_exhausted": False,
                "fail_fast": True, "reason": reason or "P0/P2 pair invalid",
                "canary": index == 0,
            }
        if canary_only:
            return {
                "dispatched": dispatched,
                "budget_exhausted": False,
                "fail_fast": False,
                "canary": True,
                "canary_complete": True,
            }
    return {
        "dispatched": dispatched, "budget_exhausted": False,
        "fail_fast": False,
    }


def diagnostic_session_root(identifier: str | None) -> Path:
    if identifier:
        candidate = Path(identifier)
        if (
            candidate.is_absolute()
            or not identifier.strip()
            or candidate.name != identifier
            or identifier in {".", ".."}
        ):
            raise ValueError(
                "diagnostic session must be a single relative identifier"
            )
        base = DIAGNOSTIC_ROOT.resolve()
        resolved = (base / identifier).resolve()
        if resolved.parent != base:
            raise ValueError("diagnostic session path escapes diagnostic_runs")
        return resolved
    pointer = DIAGNOSTIC_ROOT / "current_session.json"
    if not pointer.is_file():
        raise FileNotFoundError("no current diagnostic session; pass --session")
    value = json.loads(pointer.read_text(encoding="utf-8"))
    return diagnostic_session_root(value["session_id"])


def validate_diagnostic_suite(suite: dict[str, Any]) -> list:
    if (
        suite.get("schema_version") != "granite-c1-diagnostic-suite-v1"
        or suite.get("suite_id") != DIAGNOSTIC_SUITE_ID
        or suite.get("suite_revision") != EXPECTED_SUITE_REVISION
        or suite.get("evidence_class") != "diagnostic_non_c1"
        or suite.get("parent_session") != "granite-c1a-canary-v3-20260718"
        or suite.get("repetitions") != 2
        or suite.get("logical_passes") != ["P0", "P2"]
        or suite.get("max_attempts") != 1
    ):
        raise ValueError("diagnostic suite fixed contract drift")
    generation = suite.get("generation_config")
    if (
        not isinstance(generation, dict)
        or generation.get("return_dict_in_generate") is not True
        or generation.get("output_scores") is not True
        or hashlib.sha256(json.dumps(
            generation, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        != suite.get("generation_config_sha256")
    ):
        raise ValueError("diagnostic generation config hash drift")
    samples = suite.get("samples")
    model = suite.get("model")
    if not isinstance(samples, list) or len(samples) != 1:
        raise ValueError("diagnostic suite requires exactly one sample")
    sample = samples[0]
    fixed = {
        "sample_id": "c1a-t0-01",
        "prompt_hash":
            "3fd469a534c95ffb5a243ef6627b05cb1515012b4f482940218b8367c6b511c3",
        "raw_sample_hash":
            "7fdb4d724ff4d56bbaea05e73460d78fbebbdc9d20f9ddd04d94e496fc0a6aa5",
    }
    if any(sample.get(name) != value for name, value in fixed.items()):
        raise ValueError("diagnostic sample identity/hash drift")
    source = sample.get("source_sample") or {}
    if hashlib.sha256(str(source.get("prompt", "")).encode()).hexdigest() != fixed[
        "prompt_hash"
    ]:
        raise ValueError("diagnostic prompt bytes differ from pinned hash")
    source_metadata = source.get("metadata") or {}
    if (
        source.get("reference") != [7]
        or source_metadata.get("token_contract")
        != "artificial_fixture_ids_not_model_tokenizer_ids"
        or source_metadata.get("expected_semantics") != "identity"
        or source_metadata.get("input_token_ids") != [7]
    ):
        raise ValueError("diagnostic T0 semantic contract drift")
    if (
        not isinstance(model, dict)
        or model.get("revision")
        != "0da7a48b0276d500ce5922fd2b33944091fc6c09"
        or model.get("weights_sha256")
        != "ac02591061f1344027a7e7b11dbb4143f75f166c47dc09b742f5de3ab1dde1d1"
        or model.get("chat_template_sha256")
        != "08962c2f15d56767854b46dfc4070b37f4c443551833bba65b417191735f3187"
    ):
        raise ValueError("diagnostic model/chat identity drift")
    units = suite_units(suite)
    if len(units) != 4 or len({unit.work_unit_id for unit in units}) != 4:
        raise ValueError("diagnostic suite must expand to four unique work units")
    return units


def _verify_parent_evidence(path_value: str, expected_hash: str) -> dict[str, str]:
    unresolved_path = Path(path_value).expanduser()
    if unresolved_path.is_symlink():
        raise ValueError("parent evidence must be a real file")
    path = unresolved_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("parent evidence must be a real file")
    if len(expected_hash) != 64 or sha256_file(path) != expected_hash:
        raise ValueError("parent evidence SHA-256 mismatch")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"parent evidence is not valid JSON: {exc}") from exc
    if (
        document.get("schema_version") != "c1-token-drift-parent-evidence-v1"
        or document.get("evidence_class") != "diagnostic_parent_only"
        or document.get("formal_gate_pass") is not False
        or document.get("parent_session") != PARENT_DIAGNOSTIC_SESSION_ID
        or document.get("suite_snapshot_sha256")
        != PARENT_SUITE_SNAPSHOT_SHA256
        or document.get("parent_session_record_sha256")
        != PARENT_SESSION_RECORD_SHA256
    ):
        raise ValueError("parent evidence session identity drift")
    observations = document.get("observations")
    if not isinstance(observations, list) or len(observations) != 2:
        raise ValueError("parent evidence requires exactly P0 and P2 observations")
    by_pass = {
        observation.get("pass"): observation
        for observation in observations
        if isinstance(observation, dict)
    }
    expected_observations = {
        "P0": {
            "state": "COMPLETE",
            "work_unit_id":
                "e1a161aa5465674e1b165333c89e70ff51fe8e3eaa6bbde94b4e59df903912dc",
            "output_token_ids": [41, 0],
            "output_hash":
                "4d7b656061287b5fe72bd824eae96717ffda0397a1dbd79f8118f0316a78c522",
        },
        "P2": {
            "state": "FAILED_RETRYABLE",
            "work_unit_id":
                "d868116d5a2a999ed89d64c42b2806675125544f5c6ff000036af1fd1e6da7e5",
            "output_token_ids": [20, 41, 20, 0],
            "output_hash":
                "d1b182b9d9e7bdd8f0dcbc7b536966d9d5d3640cc8431d09e6f21469772c1ddc",
        },
    }
    for pass_id, expected in expected_observations.items():
        observation = by_pass.get(pass_id)
        if (
            not isinstance(observation, dict)
            or observation.get("sample_id") != "c1a-t0-01"
            or observation.get("execution_alignment_key")
            != "cfe453fa353f372ca2dd87e48c82135a668005c5ef966ce95efe4ef7c9af6b77"
            or any(observation.get(key) != value for key, value in expected.items())
        ):
            raise ValueError(f"parent evidence {pass_id} observation drift")
    unresolved_parent_root = RUN_ROOT / PARENT_DIAGNOSTIC_SESSION_ID
    if unresolved_parent_root.is_symlink():
        raise ValueError("parent session source path is not trusted")
    parent_root = unresolved_parent_root.resolve(strict=True)
    if (
        parent_root.parent != RUN_ROOT.resolve()
        or not parent_root.is_dir()
        or parent_root.is_symlink()
    ):
        raise ValueError("parent session source path is not trusted")
    source_paths = {
        "session": parent_root / "session.json",
        "suite": parent_root / "suite_snapshot.json",
        "P0_generation": (
            parent_root / "complete"
            / expected_observations["P0"]["work_unit_id"]
            / "generation_results.jsonl"
        ),
        "P2_generation": (
            parent_root / ".tmp"
            / expected_observations["P2"]["work_unit_id"]
            / "failure_generation_results.jsonl"
        ),
    }

    def trusted_parent_source(source: Path) -> bool:
        try:
            source.relative_to(parent_root)
            resolved = source.resolve(strict=True)
            resolved.relative_to(parent_root)
        except (OSError, RuntimeError, ValueError):
            return False
        current = source
        while current != parent_root:
            if current.is_symlink():
                return False
            current = current.parent
        return resolved.is_file()

    if any(not trusted_parent_source(source) for source in source_paths.values()):
        raise ValueError("parent session source artifact is missing or untrusted")
    if (
        sha256_file(source_paths["session"])
        != document["parent_session_record_sha256"]
        or sha256_file(source_paths["suite"])
        != document["suite_snapshot_sha256"]
    ):
        raise ValueError("parent session source identity mismatch")
    parent_session = json.loads(source_paths["session"].read_text(encoding="utf-8"))
    if (
        parent_session.get("session_id") != document["parent_session"]
        or parent_session.get("suite_snapshot_sha256")
        != document["suite_snapshot_sha256"]
    ):
        raise ValueError("parent session record does not bind the frozen suite")
    for pass_id in ("P0", "P2"):
        observation = by_pass[pass_id]
        generation_path = source_paths[f"{pass_id}_generation"]
        if sha256_file(generation_path) != observation.get(
            "generation_artifact_sha256"
        ):
            raise ValueError(
                f"parent {pass_id} generation source SHA-256 mismatch"
            )
        rows = [
            json.loads(line)
            for line in generation_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(rows) != 1:
            raise ValueError(f"parent {pass_id} generation source is invalid")
        row = rows[0]
        if (
            row.get("output_token_ids") != observation["output_token_ids"]
            or row.get("output_hash") != observation["output_hash"]
            or row.get("execution_alignment_key")
            != observation["execution_alignment_key"]
        ):
            raise ValueError(f"parent {pass_id} generation content drift")
        state_path = parent_root / "state" / (
            observation["work_unit_id"] + ".json"
        )
        if not trusted_parent_source(state_path):
            raise ValueError(f"parent {pass_id} state source is missing")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            state.get("state") != observation["state"]
            or (state.get("work_unit") or {}).get("work_unit_id")
            != observation["work_unit_id"]
        ):
            raise ValueError(f"parent {pass_id} scheduler state drift")
    return {
        "session_id": PARENT_DIAGNOSTIC_SESSION_ID,
        "path": str(path),
        "sha256": expected_hash,
        "usage": "provenance_only",
    }


def load_diagnostic_session(
    root: Path, *, verify_model_snapshot: bool = True
) -> tuple[dict[str, Any], dict[str, Any], list]:
    session = load_session_metadata(
        root, verify_model_snapshot=verify_model_snapshot
    )
    if (
        session.get("session_class") != "diagnostic_non_c1"
        or session.get("diagnostic_mode") != DIAGNOSTIC_MODE
    ):
        raise ValueError("session is not an isolated token-drift diagnostic")
    suite = json.loads((root / "suite_snapshot.json").read_text(encoding="utf-8"))
    units = validate_diagnostic_suite(suite)
    parent = session.get("parent_provenance") or {}
    _verify_parent_evidence(parent.get("path", ""), parent.get("sha256", ""))
    return session, suite, units


def _generation_evidence(store: SchedulerStore, unit: Any) -> dict[str, Any]:
    path = store.complete_dir / unit.work_unit_id / "generation_results.jsonl"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) != 1:
        raise ValueError(f"{unit.logical_pass}/rep{unit.repetition} needs one row")
    row = rows[0]
    ids = row.get("output_token_ids")
    if (
        not isinstance(ids, list)
        or any(not isinstance(item, int) or isinstance(item, bool) for item in ids)
        or row.get("output_token_count") != len(ids)
    ):
        raise ValueError("diagnostic output token IDs/count are invalid")
    required = (
        "output_hash", "stop_reason", "prompt_hash", "execution_alignment_key"
    )
    if any(not isinstance(row.get(name), str) or not row[name] for name in required):
        raise ValueError("diagnostic generation identity is incomplete")
    if not re.fullmatch(r"[0-9a-f]{64}", row["execution_alignment_key"]):
        raise ValueError("diagnostic execution alignment key is invalid")
    input_document = {
        "prompt_hash": row["prompt_hash"],
        "input_token_count": row.get("input_token_count"),
        "tokenization_metadata": row.get("tokenization_metadata"),
    }
    return {
        "output_token_ids": ids,
        "output_hash": row["output_hash"],
        "stop_reason": row["stop_reason"],
        "output_token_count": row["output_token_count"],
        "execution_alignment_key": row["execution_alignment_key"],
        "input_hash": hashlib.sha256(json.dumps(
            input_document, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest(),
        "tokenization_metadata": row.get("tokenization_metadata"),
    }


def _routing_fingerprint(store: SchedulerStore, unit: Any) -> str:
    path = store.complete_dir / unit.work_unit_id / "routing_dispatch.jsonl"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("diagnostic P2 routing evidence is empty")
    identity_fields = {
        "event_key", "execution_alignment_key", "request_id",
        "capture_overhead_total_ns",
    }
    rendered = sorted(
        json.dumps(
            {
                name: value for name, value in row.items()
                if name not in identity_fields
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        for row in rows
    )
    return hashlib.sha256(("\n".join(rendered) + "\n").encode()).hexdigest()


def _same_generation(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left[name] == right[name] for name in (
        "output_token_ids", "output_hash", "stop_reason",
        "output_token_count", "input_hash",
    ))


def _diagnostic_score_evidence(
    store: SchedulerStore, unit: Any, generation: dict[str, Any]
) -> dict[str, Any]:
    path = store.complete_dir / unit.work_unit_id / "diagnostic_scores.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != "c1-token-drift-diagnostic-v1"
        or payload.get("mode") != DIAGNOSTIC_MODE
        or payload.get("evidence_class") != "diagnostic_non_c1"
        or payload.get("semantic_equality_used_for_alignment") is not False
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(payload.get("execution_alignment_key", ""))
        )
    ):
        raise ValueError("diagnostic score envelope is invalid")
    if (
        payload["execution_alignment_key"]
        != generation["execution_alignment_key"]
    ):
        raise ValueError(
            "diagnostic score alignment differs from generation evidence"
        )
    tokenization = payload.get("tokenization_diagnostics")
    generation_tokenization = generation.get("tokenization_metadata")
    if (
        not isinstance(tokenization, dict)
        or not isinstance(generation_tokenization, dict)
        or any(
            tokenization.get(name) != value
            for name, value in generation_tokenization.items()
        )
    ):
        raise ValueError("diagnostic tokenization evidence is invalid")
    runtime = payload.get("runtime_diagnostics")
    deterministic_flags = (
        runtime.get("deterministic_flags") if isinstance(runtime, dict) else None
    )
    required_boolean_flags = {
        "torch_deterministic_algorithms_enabled",
        "cuda_matmul_allow_tf32",
        "cudnn_enabled",
        "cudnn_deterministic",
        "cudnn_benchmark",
        "cudnn_allow_tf32",
        "cuda_matmul_allow_bf16_reduced_precision_reduction",
        "cuda_matmul_allow_fp16_reduced_precision_reduction",
    }
    required_environment_flags = {
        "CUBLAS_WORKSPACE_CONFIG",
        "CUDA_LAUNCH_BLOCKING",
    }
    expected_deterministic_flags = {
        "torch_deterministic_algorithms_enabled": True,
        "cuda_matmul_allow_tf32": False,
        "cudnn_enabled": True,
        "cudnn_deterministic": True,
        "cudnn_benchmark": False,
        "cudnn_allow_tf32": False,
        "cuda_matmul_allow_bf16_reduced_precision_reduction": False,
        "cuda_matmul_allow_fp16_reduced_precision_reduction": False,
        "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
        "CUDA_LAUNCH_BLOCKING": "1",
    }
    if (
        not isinstance(runtime, dict)
        or runtime.get("schema_version")
        != "token-drift-runtime-diagnostics-v2"
        or not isinstance(deterministic_flags, dict)
        or set(deterministic_flags)
        != required_boolean_flags | required_environment_flags
        or any(
            not isinstance(deterministic_flags[name], bool)
            for name in required_boolean_flags
        )
        or any(
            deterministic_flags[name] is not None
            and not isinstance(deterministic_flags[name], str)
            for name in required_environment_flags
        )
        or deterministic_flags != expected_deterministic_flags
    ):
        raise ValueError("diagnostic runtime flags are incomplete")
    scores = payload.get("score_diagnostics")
    if (
        not isinstance(scores, dict)
        or scores.get("schema_version") != "token-drift-score-diagnostics-v1"
        or scores.get("capture_phase") != "post_generate"
        or not isinstance(scores.get("steps"), list)
        or scores.get("step_count") != len(scores["steps"])
        or scores["step_count"] != generation["output_token_count"]
    ):
        raise ValueError("diagnostic score sequence is invalid")
    tensor_hashes: list[str] = []
    margins: list[float] = []
    top2_ids: list[list[int]] = []
    score_dtypes: list[str] = []
    for index, (step, generated_id) in enumerate(zip(
        scores["steps"], generation["output_token_ids"]
    )):
        if not isinstance(step, dict):
            raise ValueError(f"diagnostic score step {index} is invalid")
        ids = step.get("top2_token_ids")
        logits = step.get("top2_logits")
        shape = step.get("score_shape")
        tensor_hash = step.get("full_score_tensor_sha256")
        score_dtype = step.get("score_dtype")
        canonical_dtype = (
            score_dtype.removeprefix("torch.")
            if isinstance(score_dtype, str)
            else None
        )
        dtype_bytes = {
            "float16": 2,
            "bfloat16": 2,
            "float32": 4,
            "float64": 8,
        }
        if (
            step.get("generation_step") != index
            or step.get("generated_token_id") != generated_id
            or not isinstance(ids, list)
            or len(ids) != 2
            or any(not isinstance(value, int) or isinstance(value, bool) for value in ids)
            or ids[0] != generated_id
            or ids[0] == ids[1]
            or not isinstance(logits, list)
            or len(logits) != 2
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in logits
            )
            or float(logits[0]) < float(logits[1])
            or not isinstance(step.get("margin"), (int, float))
            or isinstance(step.get("margin"), bool)
            or not math.isfinite(float(step["margin"]))
            or not math.isclose(
                float(step["margin"]),
                float(logits[0]) - float(logits[1]),
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
            or not isinstance(shape, list)
            or len(shape) != 2
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in shape
            )
            or shape[0] != 1
            or shape[1] < 2
            or not isinstance(step.get("score_tensor_bytes"), int)
            or step["score_tensor_bytes"] <= 0
            or canonical_dtype not in dtype_bytes
            or step["score_tensor_bytes"]
            != math.prod(shape) * dtype_bytes[canonical_dtype]
            or not re.fullmatch(r"[0-9a-f]{64}", str(tensor_hash or ""))
        ):
            raise ValueError(f"diagnostic score step {index} is invalid")
        tensor_hashes.append(tensor_hash)
        margins.append(float(step["margin"]))
        top2_ids.append(ids)
        score_dtypes.append(canonical_dtype)
    return {
        "artifact_sha256": sha256_file(path),
        "tokenization_sha256": hashlib.sha256(json.dumps(
            tokenization,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest(),
        "step_count": scores["step_count"],
        "top2_token_ids": top2_ids,
        "margins": margins,
        "score_dtypes": score_dtypes,
        "score_tensor_sha256": tensor_hashes,
        "runtime_flags_sha256": hashlib.sha256(json.dumps(
            deterministic_flags,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
    }


def _same_score_evidence(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    return all(left[name] == right[name] for name in (
        "tokenization_sha256",
        "step_count",
        "top2_token_ids",
        "margins",
        "score_dtypes",
        "score_tensor_sha256",
        "runtime_flags_sha256",
    ))


def _same_execution_invariants(
    generations: list[dict[str, Any]], scores: list[dict[str, Any]]
) -> bool:
    return (
        bool(generations)
        and len(generations) == len(scores)
        and len({row["input_hash"] for row in generations}) == 1
        and len({row["tokenization_sha256"] for row in scores}) == 1
        and len({row["runtime_flags_sha256"] for row in scores}) == 1
    )


def compare_diagnostic(root: Path) -> tuple[int, dict[str, Any]]:
    session, suite, units = load_diagnostic_session(root)
    store = SchedulerStore(root)
    indexed = {
        (unit.logical_pass, unit.repetition): unit for unit in units
    }
    report: dict[str, Any] = {
        "schema_version": "c1-token-drift-diagnostic-report-v1",
        "session_id": root.name,
        "suite_id": suite["suite_id"],
        "evidence_class": "diagnostic_non_c1",
        "parent_provenance": session["parent_provenance"],
        "status": "diagnostic_incomplete",
        "classification": None,
        "formal_gate_pass": False,
        "semantic_equivalence_accepted_as_pass": False,
    }
    try:
        p0 = [_generation_evidence(store, indexed[("P0", rep)]) for rep in (0, 1)]
        p0_scores = [
            _diagnostic_score_evidence(
                store, indexed[("P0", rep)], p0[rep]
            )
            for rep in (0, 1)
        ]
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        report["reason"] = f"P0 evidence incomplete: {exc}"
        atomic_json(root / "diagnostic_compare.json", report)
        return BLOCKED, report
    if not _same_execution_invariants(p0, p0_scores):
        report.update({
            "reason": "P0 input/tokenization/runtime invariant drift",
            "P0": p0,
            "P0_score_evidence": p0_scores,
        })
        atomic_json(root / "diagnostic_compare.json", report)
        return BLOCKED, report
    if (
        not _same_generation(p0[0], p0[1])
        or not _same_score_evidence(p0_scores[0], p0_scores[1])
    ):
        report.update({
            "status": "diagnostic_complete",
            "classification": "baseline_condition_instability",
            "classification_scope": "observational_only",
            "causal_claim_authorized": False,
            "P0": p0,
            "P0_score_evidence": p0_scores,
            "score_evidence_validated": True,
        })
        atomic_json(root / "diagnostic_compare.json", report)
        return OK, report
    p2_states = [
        State(store.load(indexed[("P2", rep)])["state"]) for rep in (0, 1)
    ]
    if p2_states == [State.PENDING, State.PENDING]:
        report.update({
            "status": "diagnostic_p0_stable",
            "P0": p0,
            "P0_score_evidence": p0_scores,
            "score_evidence_validated": True,
        })
        atomic_json(root / "diagnostic_compare.json", report)
        return OK, report
    try:
        p2 = [_generation_evidence(store, indexed[("P2", rep)]) for rep in (0, 1)]
        p2_scores = [
            _diagnostic_score_evidence(
                store, indexed[("P2", rep)], p2[rep]
            )
            for rep in (0, 1)
        ]
        routing = [
            _routing_fingerprint(store, indexed[("P2", rep)]) for rep in (0, 1)
        ]
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report["reason"] = f"P2 evidence incomplete: {exc}"
        report["P0"] = p0
        report["P0_score_evidence"] = p0_scores
        atomic_json(root / "diagnostic_compare.json", report)
        return BLOCKED, report
    if (
        not _same_execution_invariants(p0 + p2, p0_scores + p2_scores)
        or any(
            p0[rep]["execution_alignment_key"]
            != p2[rep]["execution_alignment_key"]
            for rep in (0, 1)
        )
    ):
        report.update({
            "reason": "four-unit input/tokenization/runtime invariant drift",
            "P0": p0,
            "P2": p2,
            "P0_score_evidence": p0_scores,
            "P2_score_evidence": p2_scores,
        })
        atomic_json(root / "diagnostic_compare.json", report)
        return BLOCKED, report
    if (
        not _same_generation(p2[0], p2[1])
        or not _same_score_evidence(p2_scores[0], p2_scores[1])
        or routing[0] != routing[1]
    ):
        classification = "instrumented_condition_instability"
    elif (
        not _same_generation(p0[0], p2[0])
        or not _same_score_evidence(p0_scores[0], p2_scores[0])
    ):
        classification = "instrumentation_association"
    else:
        classification = "no_observed_drift_under_tested_configuration"
    report.update({
        "status": "diagnostic_complete",
        "classification": classification,
        "classification_scope": "observational_only",
        "causal_claim_authorized": False,
        "P0": p0,
        "P2": p2,
        "P0_score_evidence": p0_scores,
        "P2_score_evidence": p2_scores,
        "score_evidence_validated": True,
        "P2_canonical_routing_fingerprints": routing,
    })
    atomic_json(root / "diagnostic_compare.json", report)
    return OK, report


def run_diagnostic_staged(
    engine: SchedulerEngine, units: list
) -> dict[str, Any]:
    engine.register(units)
    indexed = {
        (unit.logical_pass, unit.repetition): unit for unit in units
    }
    dispatched = 0
    for pass_id in ("P0", "P2"):
        for repetition in (0, 1):
            unit = indexed[(pass_id, repetition)]
            if State(engine.store.load(unit)["state"]) is State.COMPLETE:
                continue
            state = engine.run_unit(unit)
            dispatched += 1
            if state is not State.COMPLETE:
                return {
                    "dispatched": dispatched, "fail_fast": True,
                    "failed_work_unit_id": unit.work_unit_id,
                    "failed_state": state.value,
                }
        code, report = compare_diagnostic(engine.store.root)
        if code != OK:
            return {
                "dispatched": dispatched,
                "fail_fast": True,
                "reason": report.get("reason", "diagnostic evidence invalid"),
                "P2_dispatched": pass_id == "P2",
            }
        if pass_id == "P0" and report.get("classification") == (
            "baseline_condition_instability"
        ):
            return {
                "dispatched": dispatched, "fail_fast": True,
                "classification": "baseline_condition_instability",
                "P2_dispatched": False,
            }
    code, report = compare_diagnostic(engine.store.root)
    return {
        "dispatched": dispatched,
        "fail_fast": code != OK,
        "classification": report.get("classification"),
        "formal_gate_pass": False,
    }


def diagnostic_run(args: argparse.Namespace) -> int:
    try:
        with execution_lock(RUN_ROOT) as lease:
            return _diagnostic_run_locked(args, lease)
    except ExecutionLockBusy as exc:
        return blocked(str(exc), owner=read_owner(RUN_ROOT))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return blocked(f"diagnostic preflight failed: {exc}")


def _diagnostic_run_locked(
    args: argparse.Namespace, lease: ExecutionLease
) -> int:
    if args.profile != LOCAL_PROFILE:
        raise ValueError(f"diagnostic profile must be {LOCAL_PROFILE}")
    suite = load_structured(DIAGNOSTIC_SUITE_PATH)
    units = validate_diagnostic_suite(suite)
    supplied_parent = _verify_parent_evidence(
        args.parent_evidence, args.parent_evidence_sha256
    )
    identifier = args.session or (
        f"{DIAGNOSTIC_SUITE_ID}-"
        f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    )
    root = diagnostic_session_root(identifier)
    if root.exists():
        session, existing_suite, units = load_diagnostic_session(root)
        if existing_suite != suite:
            raise ValueError("diagnostic frozen suite differs from session")
        if snapshot_inventory(args.model_snapshot) != session["model_snapshot"]:
            raise ValueError("diagnostic model snapshot differs from session")
        if supplied_parent != session["parent_provenance"]:
            raise ValueError("diagnostic parent provenance differs from session")
        revalidate_hardware(root, session)
        SchedulerStore(root).reconcile()
    else:
        model_snapshot = snapshot_inventory(args.model_snapshot)
        environment = hardware_preflight(RUN_ROOT)
        root.mkdir(parents=True)
        snapshot_path = root / "suite_snapshot.json"
        atomic_json(snapshot_path, suite)
        started = time.time()
        session = {
            "schema_version": "projectctl-session-v2",
            "session_class": "diagnostic_non_c1",
            "diagnostic_mode": DIAGNOSTIC_MODE,
            "session_id": root.name,
            "profile": LOCAL_PROFILE,
            "suite_snapshot_sha256": suite_snapshot_sha256(snapshot_path),
            "started_epoch": started,
            "execution_deadline_epoch": started + 105 * 60,
            "session_deadline_epoch": started + 120 * 60,
            "model_snapshot": model_snapshot,
            "environment": environment,
            "parent_provenance": supplied_parent,
        }
        atomic_json(root / "session.json", session)
        snapshot_path.chmod(0o444)
        (root / "session.json").chmod(0o444)
        DIAGNOSTIC_ROOT.mkdir(parents=True, exist_ok=True)
        atomic_json(
            DIAGNOSTIC_ROOT / "current_session.json",
            {"session_id": root.name},
        )
    if time.time() >= float(session["session_deadline_epoch"]):
        return blocked("diagnostic hard session deadline has expired")
    engine = make_engine(
        root, suite, session, lease,
        extra_collector_env={"C1_DIAGNOSTIC_MODE": DIAGNOSTIC_MODE},
    )
    result = run_diagnostic_staged(engine, units)
    emit({
        "status": "diagnostic_stopped" if result["fail_fast"]
        else "diagnostic_complete",
        "session_id": root.name,
        "execution": result,
        "states": summarize(engine.store),
        "formal_gate_pass": False,
    })
    return BLOCKED if result["fail_fast"] else OK


def diagnostic_command(args: argparse.Namespace) -> int:
    if args.diagnostic_action == "run":
        return blocked(hard_disabled_reason("c1_diagnostic_run"))
    try:
        root = diagnostic_session_root(args.session)
        if args.diagnostic_action == "status":
            session, suite, units = load_diagnostic_session(root)
            emit({
                "status": "diagnostic_only",
                "session_id": root.name,
                "session_class": session["session_class"],
                "suite_id": suite["suite_id"],
                "expected_units": len(units),
                "states": summarize(SchedulerStore(root)),
                "formal_gate_pass": False,
            })
            return OK
        code, report = compare_diagnostic(root)
        emit(report)
        return code
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        return blocked(f"diagnostic {args.diagnostic_action} failed: {exc}")


def run_command(args: argparse.Namespace) -> int:
    if args.run_action == "start":
        return blocked(hard_disabled_reason("c1_run_start"))
    if args.run_action == "resume":
        return blocked(hard_disabled_reason("c1_run_resume"))
    if args.run_action == "retry-failed":
        return blocked(hard_disabled_reason("c1_run_retry_failed"))
    try:
        root, suite, engine = load_session(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return blocked(str(exc))
    units = suite_units(suite)
    if args.run_action == "status":
        emit({
            "status": "ok",
            "session_id": root.name,
            "states": summarize(engine.store),
            "reconciliation": "not_performed_without_execution_lock",
        })
        return OK
    if args.run_action == "skip-completed":
        remaining = engine.skip_completed(units)
        emit({"status": "ok", "remaining": len(remaining),
              "skipped_complete": len(units) - len(remaining)})
        return OK
    if args.run_action == "package":
        return package_session(root, args.output)
    raise AssertionError(args.run_action)


def _write_trace_audit_provenance(
    root: Path,
    *,
    session: dict[str, Any],
    expected_unit_count: int,
    complete_unit_count: int,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    artifact_path = root / "TRACE_AUDIT.json"
    sidecar_path = root / "TRACE_AUDIT.json.sha256"
    artifact = {
        "schema_version": "c1-trace-audit-provenance-v1",
        "session_id": root.name,
        "status": "complete" if not findings else "incomplete",
        "suite_snapshot_sha256": session["suite_snapshot_sha256"],
        "package_manifest_sha256": sha256_file(
            PACKAGE_ROOT / "package_manifest.json"
        ),
        "checksums_sha256": sha256_file(PACKAGE_ROOT / "checksums.txt"),
        "audit_tool_sha256": sha256_file(
            PACKAGE_ROOT / "scripts/c1_trace_audit.py"
        ),
        "projectctl_sha256": sha256_file(Path(__file__)),
        "expected_unit_count": expected_unit_count,
        "complete_unit_count": complete_unit_count,
        "finding_count": len(findings),
        "findings": findings,
    }
    atomic_json(artifact_path, artifact)
    digest = sha256_file(artifact_path)
    _atomic_text(sidecar_path, f"{digest}  {artifact_path.name}\n")
    return {
        "path": artifact_path.name,
        "sha256": digest,
        "sidecar": sidecar_path.name,
    }


def audit_session(
    root: Path, *, lease: ExecutionLease | None = None
) -> tuple[int, dict[str, Any]]:
    store = SchedulerStore(root)
    reconciliation: dict[str, int] | str = (
        store.reconcile()
        if lease is not None and lease.active
        else "not_performed_without_execution_lock"
    )
    session = load_session_metadata(root, verify_model_snapshot=False)
    suite = json.loads((root / "suite_snapshot.json").read_text(encoding="utf-8"))
    units = suite_units(suite)
    require_c1_a(suite, units, session.get("profile"))
    verify_granite_suite_sources(suite)
    quality_contract, contract_binding = verify_quality_contract_source(suite)
    selections = {
        sample.get("sample_id"): sample
        for sample in suite.get("samples", [])
        if isinstance(sample, dict)
    }
    expected_units = {unit.work_unit_id: unit for unit in units}
    findings: list[dict[str, Any]] = []
    records = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(store.state_dir.glob("*.json"))
    }
    complete_ids = {
        path.name for path in store.complete_dir.iterdir()
        if path.is_dir() and not path.is_symlink()
    }
    expected_ids = set(expected_units)
    for unit_id in sorted(expected_ids - set(records)):
        findings.append({"work_unit_id": unit_id, "error": "missing state"})
    for unit_id in sorted(set(records) - expected_ids):
        findings.append({"work_unit_id": unit_id, "error": "unexpected state"})
    for unit_id in sorted(complete_ids - expected_ids):
        findings.append({"work_unit_id": unit_id, "error": "unexpected complete artifact"})
    alignment: dict[
        tuple[str, str, int], dict[str, dict[str, Any]]
    ] = {}
    for unit_id, unit in expected_units.items():
        record = records.get(unit_id)
        if record is None:
            continue
        if record.get("work_unit") != unit.as_dict():
            findings.append({
                "work_unit_id": unit_id,
                "error": "state identity differs from suite snapshot",
            })
            continue
        if record["state"] != State.COMPLETE.value:
            findings.append({
                "work_unit_id": unit_id,
                "state": record["state"],
                "remediation": f"projectctl run resume --session {root.name}",
            })
            continue
        if unit_id not in complete_ids:
            findings.append({
                "work_unit_id": unit_id,
                "error": "COMPLETE state lacks complete artifact",
            })
            continue
        errors = verify_complete(store.complete_dir / unit_id, unit)
        if errors:
            findings.append({"work_unit_id": unit_id, "errors": errors})
            continue
        artifact_root = store.complete_dir / unit_id
        generation_path = artifact_root / "generation_results.jsonl"
        quality_path = artifact_root / "quality_results.jsonl"
        if generation_path.is_file() and quality_path.is_file():
            try:
                generation_rows = [
                    json.loads(line)
                    for line in generation_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                quality_rows = [
                    json.loads(line)
                    for line in quality_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if len(generation_rows) != 1 or len(quality_rows) != 1:
                    raise ValueError("requires exactly one generation/quality row")
                generation_row = generation_rows[0]
                quality_row = quality_rows[0]
                _validate_artifact_schema(
                    "c1_benchmark.schema.json", generation_row
                )
                _validate_artifact_schema("c1_quality.schema.json", quality_row)
                selection = selections.get(unit.sample_id)
                if not isinstance(selection, dict) or not isinstance(
                    selection.get("source_sample"), dict
                ):
                    raise ValueError("suite sample lacks frozen source sample")
                if (
                    generation_row.get("sample_id")
                    != selection.get("source_sample_id")
                    or generation_row.get("repetition_id") != unit.repetition
                ):
                    raise ValueError(
                        "generation identity differs from work unit"
                    )
                if quality_row.get("pass_id") != unit.logical_pass:
                    raise ValueError("quality pass_id differs from work unit")
                if quality_row.get("execution_alignment_key") != (
                    generation_row.get("execution_alignment_key")
                ):
                    raise ValueError(
                        "quality execution alignment differs from generation"
                    )
                formal_generation = _formal_generation_evidence(generation_row)
                validate_quality_artifact(
                    quality_row,
                    contract=quality_contract,
                    contract_binding=contract_binding,
                    sample=selection["source_sample"],
                    generation=generation_row,
                )
                evidence = {
                    "generation": formal_generation,
                    "quality": _formal_quality_evidence(quality_row),
                }
                key = (unit.model_id, unit.sample_id, unit.repetition)
                alignment.setdefault(key, {})[unit.logical_pass] = evidence
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                findings.append({
                    "work_unit_id": unit_id,
                    "errors": [f"invalid formal evidence: {exc}"],
                })
        else:
            findings.append({
                "work_unit_id": unit_id,
                "errors": ["generation/quality evidence is missing"],
            })
    for key, passes in sorted(alignment.items()):
        comparison_findings = compare_cross_pass_evidence(
            passes, expected_passes=suite["logical_passes"]
        )
        errors = {
            "execution_alignment_key": "execution alignment drift",
            "input_token_ids": "input token IDs drift",
            "output_token_ids": "output token IDs drift",
            "stop_reason": "stop reason drift",
            "output_hash": "output drift",
            "evaluator": "evaluator classification drift",
            "parser_outcome": "parser outcome classification drift",
            "task_outcome": "task outcome classification drift",
            "quality_binding_sha256": "quality binding provenance drift",
        }
        for comparison in comparison_findings:
            pass_id = comparison.get("pass_id")
            field = comparison.get("field")
            if comparison["kind"] in {"generation_drift", "classification_drift"}:
                error = f"P0/{pass_id} {errors[field]}"
            elif comparison["kind"] == "missing_pass_evidence":
                error = f"{pass_id} formal evidence missing"
            elif comparison["kind"] == "missing_baseline":
                error = "P0 formal baseline missing"
            else:
                error = f"{pass_id or 'suite'} formal evidence invalid"
            findings.append({
                "identity": {
                    "model_id": key[0], "sample_id": key[1], "repetition": key[2]
                },
                "error": error,
                "comparison": comparison,
            })
    report = {
        "status": "complete" if not findings else "incomplete",
        "session_id": root.name,
        "reconciliation": reconciliation,
        "findings": findings,
    }
    report["provenance_artifact"] = _write_trace_audit_provenance(
        root,
        session=session,
        expected_unit_count=len(expected_units),
        complete_unit_count=sum(
            records.get(unit_id, {}).get("state") == State.COMPLETE.value
            for unit_id in expected_units
        ),
        findings=findings,
    )
    return (OK if not findings else BLOCKED), report


def trace_command(args: argparse.Namespace) -> int:
    if args.trace_action == "run":
        return blocked(hard_disabled_reason("c1_trace_run"))
    try:
        root = session_root(args.session)
        code, report = audit_session(root)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return blocked(str(exc))
    emit(report)
    return code


def package_session(root: Path, output: str | None) -> int:
    lock_root = root.parent.resolve()
    try:
        with execution_lock(lock_root) as lease:
            return _package_session_locked(root, output, lease)
    except ExecutionLockBusy as exc:
        return blocked(str(exc), owner=read_owner(lock_root))
    except (OSError, ValueError, RuntimeError) as exc:
        return blocked(f"package failed: {exc}")


def _package_session_locked(
    root: Path, output: str | None, lease: ExecutionLease
) -> int:
    session = load_session_metadata(root)
    if time.time() >= float(session["session_deadline_epoch"]):
        return blocked("hard session deadline has expired")
    code, report = audit_session(root, lease=lease)
    if code != OK:
        emit(report)
        return BLOCKED
    archive = (
        Path(output).resolve()
        if output else root.parent / f"{root.name}.scheduler.tar.gz"
    )
    if archive.exists():
        return blocked("refusing to overwrite archive", archive=str(archive))
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    if sidecar.exists():
        return blocked("refusing to overwrite sidecar", sidecar=str(sidecar))
    archive.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tempfile.TemporaryDirectory(prefix="projectctl-package-") as staging:
            staged_root = Path(staging) / root.name
            shutil.copytree(root, staged_root)
            _prepare_cleanroom_stage(staged_root)
            with tarfile.open(temporary, "w:gz") as tar:
                tar.add(staged_root, arcname=root.name, recursive=True)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, archive)
        fsync_directory(archive.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    checksum = sha256_file(archive)
    _atomic_text(sidecar, f"{checksum}  {archive.name}\n")
    verify_code = verify_archive(archive)
    if (
        verify_code != OK
        or time.time() >= float(session["session_deadline_epoch"])
    ):
        archive.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        fsync_directory(archive.parent)
        return blocked(
            "archive verification failed or hard session deadline expired",
        )
    emit({"status": "complete", "archive": str(archive),
          "sha256": checksum})
    return OK


def _prepare_cleanroom_stage(root: Path) -> None:
    # The immutable live session keeps the required absolute snapshot path.
    # The distributable copy retains every hash/size while redacting only the
    # host-local path rejected by the cleanroom privacy scan.
    session_path = root / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["model_snapshot"]["absolute_path"] = "<host-local-path-redacted>"
    session_path.chmod(0o644)
    atomic_json(session_path, session)
    routing_inputs = sorted(root.rglob("routing_dispatch.jsonl"))
    if not routing_inputs:
        raise ValueError("cleanroom package requires P2 routing artifacts")
    canonical_root = root / "canonical"
    canonical_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="projectctl-routing-") as temporary:
        merged = Path(temporary) / "routing.jsonl"
        with merged.open("wb") as stream:
            for path in routing_inputs:
                stream.write(path.read_bytes())
        canonical = canonicalize(merged, canonical_root / "routing.json")
    system = build_system_ir(
        canonical_root / "routing.json", canonical_root / "system.json"
    )
    atomic_json(root / "summary.json", {
        "schema_version": "c1-rebuilt-summary-v1",
        "routing_event_count": len(canonical["events"]),
        "system_event_count": len(system["events"]),
    })
    inventory_path = root / "PACKAGE_INVENTORY.json"
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != inventory_path
    ]
    atomic_json(inventory_path, {
        "schema_version": "c1-package-inventory-v1",
        "files": files,
    })


def _atomic_text(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def verify_archive(archive: Path) -> int:
    if not archive.is_file():
        return blocked("archive not found", archive=str(archive))
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    if not sidecar.is_file() or sidecar.is_symlink():
        return blocked("archive checksum sidecar is mandatory", archive=str(archive))
    parts = sidecar.read_text(encoding="utf-8").strip().split(maxsplit=1)
    if (
        len(parts) != 2
        or parts[1].lstrip("*") != archive.name
        or parts[0] != sha256_file(archive)
    ):
        return blocked("archive checksum mismatch", archive=str(archive))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        try:
            with tarfile.open(archive, "r:*") as tar:
                members = tar.getmembers()
                if any(
                    member.name.startswith("/")
                    or ".." in Path(member.name).parts
                    or member.issym() or member.islnk()
                    or not (member.isfile() or member.isdir())
                    for member in members
                ):
                    return blocked("archive contains unsafe member")
                tar.extractall(root)
        except (OSError, tarfile.TarError) as exc:
            return blocked("archive extraction failed", error=str(exc))
        children = list(root.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            return blocked("archive must contain exactly one session root")
        try:
            code, report = audit_session(children[0])
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return blocked("archive session audit failed", error=str(exc))
    if code != OK:
        emit({"status": report["status"], "archive": str(archive),
              "audit": report})
        return code
    with tempfile.TemporaryDirectory(prefix="projectctl-cleanroom-") as cleanroom:
        try:
            cleanroom_code, cleanroom_report = cleanroom_verify(
                archive, Path(cleanroom)
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return blocked("cleanroom rebuild failed", error=str(exc))
    emit({
        "status": "complete" if cleanroom_code == 0 else "blocked",
        "archive": str(archive),
        "audit": report,
        "cleanroom": cleanroom_report,
    })
    return OK if cleanroom_code == 0 else BLOCKED


def verify_command(args: argparse.Namespace) -> int:
    return verify_archive(Path(args.archive).resolve())


def qualification_command(args: argparse.Namespace) -> int:
    """Dispatch G2.5 governance; real start remains approval and review bound."""
    from scripts.g25_qualification import (
        audit_session,
        pilot_plan,
        pilot_static_preflight,
        pilot_status,
        replay_session,
        write_synthetic_session,
    )

    try:
        if args.qualification_action == "plan":
            emit(pilot_plan())
            return OK
        if args.qualification_action == "preflight":
            report = pilot_static_preflight(QUALIFICATION_ROOT)
            emit(report)
            return OK if report["status"] == "static_pass_dynamic_gpu_pending" else BLOCKED
        if args.qualification_action == "status":
            status = pilot_status(QUALIFICATION_ROOT, args.session)
            requested = args.session or status["session_id"]
            root = QUALIFICATION_ROOT / requested
            if root.is_dir():
                from scheduler.g25_session import (
                    audit_finalized_application,
                    audit_partial_session,
                    external_seal_anchor_path,
                )

                application_audit = (
                    audit_finalized_application(
                        root,
                        seal_anchor=external_seal_anchor_path(root),
                        expected_anchor_sha256=args.seal_anchor_sha256,
                    )
                    if (root / "terminal.json").is_file()
                    and (root / "final_seal.json").is_file()
                    else audit_partial_session(root)
                )
                status = {**status, "application_audit": application_audit}
            emit(status)
            return OK
        if args.qualification_action == "synthetic-run":
            root, verdict, audit = write_synthetic_session(
                QUALIFICATION_ROOT,
                args.session,
                args.scenario,
            )
            emit({
                "status": "complete",
                "session_root": str(root),
                "verdict": verdict,
                "audit": audit,
                "gpu_used": False,
                "formal_gate_pass": False,
            })
            return OK
        if args.qualification_action == "start":
            assert_qualification_route()
            from scheduler.g25_application import query_dynamic_preflight
            from scripts.g25_application_runner import (
                execute_application,
            )

            approval = Path(args.approval_record).resolve(strict=True)
            review = Path(args.review_record).resolve(strict=True)
            evaluation = Path(args.evaluation_record).resolve(strict=True)
            snapshot = Path(args.model_snapshot).resolve(strict=True)
            provider = G25_GPU_PROVIDER or query_dynamic_preflight
            try:
                with execution_lock(RUN_ROOT) as lease:
                    code, report = execute_application(
                        output_root=QUALIFICATION_ROOT,
                        run_root=RUN_ROOT,
                        approval_record=approval,
                        review_record=review,
                        evaluation_record=evaluation,
                        review_tag=args.review_tag,
                        model_snapshot=snapshot,
                        lease=lease,
                        provider=provider,
                    )
            except ExecutionLockBusy as exc:
                return blocked(str(exc), owner=read_owner(RUN_ROOT))
            emit(report)
            return code
        root = (QUALIFICATION_ROOT / args.session).resolve(strict=True)
        root.relative_to(QUALIFICATION_ROOT.resolve())
        if args.qualification_action == "replay":
            emit(replay_session(root))
            return OK
        if not (root / "session.json").is_file():
            from scheduler.g25_session import (
                audit_finalized_application,
                audit_partial_session,
                external_seal_anchor_path,
            )

            partial = (
                audit_finalized_application(
                    root,
                    seal_anchor=external_seal_anchor_path(root),
                    expected_anchor_sha256=getattr(args, "seal_anchor_sha256", None),
                )
                if (root / "terminal.json").is_file()
                and (root / "final_seal.json").is_file()
                else audit_partial_session(root)
            )
            emit(partial)
            return BLOCKED
        if root.name == "granite-c1a-g25-qualification-r1-20260719":
            from scheduler.g25_session import (
                audit_finalized_application,
                external_seal_anchor_path,
            )

            final = audit_finalized_application(
                root,
                seal_anchor=external_seal_anchor_path(root),
                expected_anchor_sha256=args.seal_anchor_sha256,
            )
            emit(final)
            return OK if final["qualification_pass"] else BLOCKED
        audit = audit_session(root)
        emit(audit)
        return OK if audit["status"] == "complete" else BLOCKED
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        return blocked(f"qualification {args.qualification_action} failed: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    top = parser.add_subparsers(dest="area", required=True)

    model = top.add_parser("model")
    model_sub = model.add_subparsers(dest="model_action", required=True)
    for action in ("preflight", "smoke"):
        command = model_sub.add_parser(action)
        command.add_argument("--model", required=True)
        command.add_argument("--snapshot")
        command.add_argument("--prompt", default="Explain sparse MoE routing briefly.")
        command.add_argument("--max-new-tokens", type=int, default=8)

    trace = top.add_parser("trace")
    trace_sub = trace.add_subparsers(dest="trace_action", required=True)
    trace_run = trace_sub.add_parser("run")
    trace_run.add_argument("--suite", required=True)
    trace_run.add_argument("--session")
    trace_run.add_argument("--model-snapshot", required=True)
    trace_run.add_argument("--profile", required=True, choices=(LOCAL_PROFILE,))
    trace_run.add_argument("--canary-only", action="store_true")
    trace_audit = trace_sub.add_parser("audit")
    trace_audit.add_argument("--session", required=True)

    diagnostic = top.add_parser("diagnostic")
    diagnostic_sub = diagnostic.add_subparsers(
        dest="diagnostic_action", required=True
    )
    diagnostic_run_parser = diagnostic_sub.add_parser("run")
    diagnostic_run_parser.add_argument("--session")
    diagnostic_run_parser.add_argument("--model-snapshot", required=True)
    diagnostic_run_parser.add_argument(
        "--profile", required=True, choices=(LOCAL_PROFILE,)
    )
    diagnostic_run_parser.add_argument("--parent-evidence", required=True)
    diagnostic_run_parser.add_argument(
        "--parent-evidence-sha256", required=True
    )
    for action in ("status", "compare"):
        command = diagnostic_sub.add_parser(action)
        command.add_argument("--session")

    qualification = top.add_parser("qualification")
    qualification_sub = qualification.add_subparsers(
        dest="qualification_action", required=True
    )
    qualification_sub.add_parser("plan")
    qualification_sub.add_parser("preflight")
    qualification_status = qualification_sub.add_parser("status")
    qualification_status.add_argument("--session")
    qualification_status.add_argument("--seal-anchor-sha256")
    synthetic = qualification_sub.add_parser("synthetic-run")
    synthetic.add_argument("--session", required=True)
    synthetic.add_argument(
        "--scenario",
        required=True,
        choices=(
            "all-256", "common-384", "common-512", "no-common",
            "timeout", "runtime-failure", "invalid-evidence",
        ),
    )
    qualification_start = qualification_sub.add_parser("start")
    qualification_start.add_argument("--approval-record", required=True)
    qualification_start.add_argument("--review-record", required=True)
    qualification_start.add_argument("--evaluation-record", required=True)
    qualification_start.add_argument("--review-tag", required=True)
    qualification_start.add_argument("--model-snapshot", required=True)
    for action in ("replay", "audit"):
        command = qualification_sub.add_parser(action)
        command.add_argument("--session", required=True)
        if action == "audit":
            command.add_argument("--seal-anchor-sha256")

    run = top.add_parser("run")
    run_sub = run.add_subparsers(dest="run_action", required=True)
    start = run_sub.add_parser("start")
    start.add_argument("--suite", required=True)
    start.add_argument("--session")
    start.add_argument("--model-snapshot", required=True)
    start.add_argument("--profile", required=True, choices=(LOCAL_PROFILE,))
    start.add_argument("--canary-only", action="store_true")
    for action in ("status", "resume", "retry-failed", "skip-completed"):
        command = run_sub.add_parser(action)
        command.add_argument("--session")
    package = run_sub.add_parser("package")
    package.add_argument("--session")
    package.add_argument("--output")
    verify = run_sub.add_parser("verify")
    verify.add_argument("--archive", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.area == "model":
        return model_command(args)
    if args.area == "trace":
        return trace_command(args)
    if args.area == "diagnostic":
        return diagnostic_command(args)
    if args.area == "qualification":
        return qualification_command(args)
    if args.run_action == "verify":
        return verify_command(args)
    return run_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
