#!/usr/bin/env python3
"""Fail-closed arrival-driver wrapper for TRACK_GPU target_5.

The historical SERV-P0-25 raw was produced by ``serving_burst_runner.py``.
That runner already owns the vLLM ``AsyncLLMEngine`` server and its Poisson
open-loop scheduler, so this driver deliberately does not reimplement either.
It freezes the target_5 argv, starts exactly one fresh runner attempt, validates
the raw request/arrival stream from index zero, and emits the compact input
accepted by ``measurement.parsers.serving_tail_parser``.

``gpu_campaign_runner.py`` is retained as a provenance boundary.  It is not an
open-loop server (it calls synchronous ``LLM.generate``), and its current hash
differs from the copy archived with the historical evidence.  Both hashes are
recorded explicitly rather than pretending source-level identity.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Protocol, Sequence

try:
    from measurement import model_identity_manifest as model_identity_contract
except ImportError:  # direct script execution outside an installed package
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement import model_identity_manifest as model_identity_contract


SCHEMA = "serv-p0-25-arrival-driver-attempt-v1"
SUMMARY_SCHEMA = "serving-tail-ci-result-v1"
MODEL_ID = "mistralai/Mixtral-8x7B-Instruct-v0.1"
MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"

# P-021 freezes the current source without rewriting it.  The older hash is a
# comparability boundary, not the executable selected for this new attempt.
EXPECTED_SERVING_RUNNER_SHA256 = (
    "f948a2920fa17eb7448ddaf3395e53f325db7527c1ca18b5d60bff379d1c144a"
)
EXPECTED_CURRENT_GPU_CAMPAIGN_RUNNER_SHA256 = (
    "30d3384a61b5afc3cf1dee96f8267c6411ca937d1f35a5b7509fa17336d6419c"
)
ARCHIVED_GPU_CAMPAIGN_RUNNER_SHA256 = (
    "304a7e2ce72f4d8c24390f292e4bc5e426c2ecac39f8a7824e575355dc8adae8"
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_ID_RE = re.compile(r"^SERV-P0-25-TAIL-[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


class ArrivalDriverError(RuntimeError):
    """An invariant violation that invalidates the target_5 attempt."""


@dataclasses.dataclass(frozen=True)
class Target5Contract:
    requests: int = 10_000
    arrival_rate_rps: float = 1.0472460793856333
    arrival_seed: int = 20_260_812
    concurrency: int = 8
    input_tokens: int = 128
    output_tokens: int = 32
    sampling_mode: str = "NATURAL_EOS_CAPPED"
    max_num_seqs: int = 8
    max_num_batched_tokens: int = 1024
    gpu_memory_utilization: float = 0.97
    warmup_bursts: int = 1

    def validate(self) -> None:
        if self.requests < 1:
            raise ArrivalDriverError("requests must be positive")
        if self.arrival_rate_rps <= 0:
            raise ArrivalDriverError("arrival_rate_rps must be positive")
        if self.concurrency != 8 or self.max_num_seqs != self.concurrency:
            raise ArrivalDriverError("target_5 requires concurrency=max_num_seqs=8")
        if (self.input_tokens, self.output_tokens) != (128, 32):
            raise ArrivalDriverError("target_5 requires the frozen 128-in/32-out shape")
        if self.sampling_mode != "NATURAL_EOS_CAPPED":
            raise ArrivalDriverError("target_5 requires NATURAL_EOS_CAPPED")
        if self.warmup_bursts != 1:
            raise ArrivalDriverError("target_5 preserves one excluded warmup burst")


TARGET5 = Target5Contract()


@dataclasses.dataclass(frozen=True)
class DriverConfig:
    attempt_id: str
    run_root: Path
    model_path: Path
    model_identity_json: Path
    serving_runner: Path
    gpu_campaign_runner: Path
    archived_gpu_campaign_runner: Path | None
    python_executable: Path


class Clock(Protocol):
    def utc_now(self) -> str: ...

    def monotonic_ns(self) -> int: ...


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> int: ...


class ServerContract(Protocol):
    def verify(self, execution_runner: Path) -> dict[str, Any]: ...


class SystemClock:
    def utc_now(self) -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> int:
        # Direct file descriptors keep multi-hour vLLM output durable while the
        # child is still running.  An interrupt or host-side exception cannot
        # strand all prior output in an in-memory capture buffer.
        with stdout_path.open("a", encoding="utf-8", buffering=1) as stdout_handle:
            with stderr_path.open("a", encoding="utf-8", buffering=1) as stderr_handle:
                completed = subprocess.run(
                    list(argv),
                    cwd=cwd,
                    check=False,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                )
        return completed.returncode


class EmbeddedAsyncServerContract:
    """Read-only capability check for the unchanged serving runner.

    Importing vLLM during a dry run would allocate runtime state and defeats the
    CPU-only preparation path.  The source hash is authoritative; these markers
    make a mismatched/incorrect path fail with a useful classification.
    """

    REQUIRED_MARKERS = (
        "AsyncLLMEngine",
        "--arrival-rate-rps",
        "--total-requests",
        "--arrival-seed",
        "POISSON_OPEN_LOOP",
        "NATURAL_EOS_CAPPED",
    )

    def verify(self, execution_runner: Path) -> dict[str, Any]:
        try:
            text = execution_runner.read_text(encoding="utf-8")
        except OSError as exc:
            raise ArrivalDriverError(
                f"cannot read execution runner {execution_runner}: {exc}"
            ) from exc
        missing = [marker for marker in self.REQUIRED_MARKERS if marker not in text]
        if missing:
            raise ArrivalDriverError(
                f"execution runner lacks frozen server/arrival capabilities: {missing}"
            )
        return {
            "server_ownership": "embedded_async_llm_engine_in_execution_runner",
            "transport": "in_process_vllm_async_engine",
            "capability_markers": list(self.REQUIRED_MARKERS),
            "external_server_reused": False,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ArrivalDriverError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Match serving_burst_runner.sha256_json byte-for-byte."""

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def command_identity(argv: list[str], timeout_seconds: float = 20.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except Exception as exc:
        return {
            "argv": argv,
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def runtime_identity(
    config: DriverConfig,
    *,
    captured_at_utc: str,
    server_identity: dict[str, Any],
    lineage: dict[str, Any],
) -> dict[str, Any]:
    selected_environment = {
        key: os.environ.get(key)
        for key in (
            "CUDA_HOME",
            "CUDA_PATH",
            "CUDACXX",
            "CUDA_VISIBLE_DEVICES",
            "CPATH",
            "LIBRARY_PATH",
            "LD_LIBRARY_PATH",
            "PATH",
            "VLLM_USE_SIMPLE_KV_OFFLOAD",
        )
    }
    cuda_home = selected_environment["CUDA_HOME"]
    cudacxx = selected_environment["CUDACXX"]
    if cudacxx:
        nvcc = str(cudacxx)
    elif cuda_home:
        nvcc = str(Path(cuda_home) / "bin" / "nvcc")
    else:
        nvcc = shutil.which("nvcc") or "nvcc"
    return {
        "schema_version": "serv-p0-25-runtime-identity-v1",
        "captured_at_utc": captured_at_utc,
        "python_executable": str(config.python_executable),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "uname": list(platform.uname()),
        "selected_environment": selected_environment,
        "packages_from_metadata": {
            name: package_version(name)
            for name in ("torch", "vllm", "transformers", "numpy", "safetensors")
        },
        "torch_runtime": command_identity(
            [
                str(config.python_executable),
                "-c",
                (
                    "import json,torch; print(json.dumps({"
                    "'torch':torch.__version__,'torch_cuda':torch.version.cuda,"
                    "'cuda_available':torch.cuda.is_available()}))"
                ),
            ]
        ),
        "vllm_runtime": command_identity(
            [
                str(config.python_executable),
                "-c",
                "import vllm; print(vllm.__version__)",
            ]
        ),
        "nvcc": command_identity([nvcc, "--version"]),
        "path_resolved_nvcc": shutil.which("nvcc"),
        "nvidia_smi_identity": command_identity(
            [
                "nvidia-smi",
                "--query-gpu=name,uuid,memory.total,driver_version,compute_cap",
                "--format=csv,noheader",
            ]
        ),
        "nvidia_compute_apps_pre_dispatch": command_identity(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader",
            ]
        ),
        "server": server_identity,
        "code_lineage": lineage,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ArrivalDriverError(f"JSON object contains duplicate key {key!r}")
        value[key] = item
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except ArrivalDriverError:
        raise
    except Exception as exc:
        raise ArrivalDriverError(f"cannot parse JSON {path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArrivalDriverError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
                if not isinstance(value, dict):
                    raise TypeError("row is not an object")
                rows.append(value)
    except Exception as exc:
        raise ArrivalDriverError(
            f"cannot parse JSONL {path}: {type(exc).__name__}: {exc}"
        ) from exc
    return rows


def validate_model_identity(
    path: Path,
    model_path: Path | None = None,
    *,
    contract: model_identity_contract.ModelContract = (
        model_identity_contract.CANONICAL_CONTRACT
    ),
) -> dict[str, Any]:
    """Re-attest both the embedded manifest and every on-disk model file."""

    identity = read_json(path)
    if model_path is None:
        embedded_path = identity.get("model_path")
        if not isinstance(embedded_path, str) or not Path(embedded_path).is_absolute():
            raise ArrivalDriverError(
                "model identity model_path must be absolute when no expected path is supplied"
            )
        model_path = Path(embedded_path)
    required = {
        "schema_version": model_identity_contract.SCHEMA_VERSION,
        "model_id": contract.model_id,
        "revision": contract.revision,
        "verification_status": "PASS",
        "model_path": str(model_path.resolve()),
        "checksum_manifest_canonicalization": (
            model_identity_contract.CANONICALIZATION
        ),
    }
    for key, expected in required.items():
        if identity.get(key) != expected:
            raise ArrivalDriverError(
                f"model identity {key}: expected {expected!r}, got {identity.get(key)!r}"
            )
    checksum = identity.get("checksum_manifest_sha256")
    if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
        raise ArrivalDriverError(
            "model identity requires a lowercase checksum_manifest_sha256"
        )
    embedded = identity.get("checksum_manifest")
    if not isinstance(embedded, dict):
        raise ArrivalDriverError("model identity checksum_manifest must be an object")
    recomputed = hashlib.sha256(
        model_identity_contract.canonical_json_bytes(embedded)
    ).hexdigest()
    if recomputed != checksum:
        raise ArrivalDriverError(
            "model identity embedded checksum_manifest digest mismatch: "
            f"declared {checksum}, recomputed {recomputed}"
        )
    try:
        freshly_verified = model_identity_contract.build_manifest(
            model_path,
            model_id=contract.model_id,
            revision=contract.revision,
            contract=contract,
        )
    except Exception as exc:
        raise ArrivalDriverError(
            "on-disk model identity verification failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if identity != freshly_verified:
        differing_keys = sorted(
            key
            for key in set(identity) | set(freshly_verified)
            if identity.get(key) != freshly_verified.get(key)
        )
        raise ArrivalDriverError(
            "model identity does not equal a fresh canonical on-disk verification; "
            f"differing_keys={differing_keys}"
        )
    return freshly_verified


def require_hash(path: Path, expected: str, label: str) -> dict[str, Any]:
    observed = sha256_file(path)
    if observed != expected:
        raise ArrivalDriverError(
            f"{label} SHA-256 mismatch: expected {expected}, got {observed} ({path})"
        )
    return {"path": str(path), "sha256": observed, "status": "PASS"}


def code_lineage(config: DriverConfig) -> dict[str, Any]:
    serving = require_hash(
        config.serving_runner,
        EXPECTED_SERVING_RUNNER_SHA256,
        "serving execution runner",
    )
    current = require_hash(
        config.gpu_campaign_runner,
        EXPECTED_CURRENT_GPU_CAMPAIGN_RUNNER_SHA256,
        "current gpu_campaign_runner",
    )
    archived: dict[str, Any] = {
        "expected_sha256": ARCHIVED_GPU_CAMPAIGN_RUNNER_SHA256,
        "status": "HASH_RECORDED_NOT_MATERIALIZED",
    }
    if config.archived_gpu_campaign_runner is not None:
        archived = require_hash(
            config.archived_gpu_campaign_runner,
            ARCHIVED_GPU_CAMPAIGN_RUNNER_SHA256,
            "archived gpu_campaign_runner",
        )
    return {
        "execution_runner": serving,
        "current_gpu_campaign_runner": current,
        "archived_gpu_campaign_runner": archived,
        "gpu_campaign_runner_source_identity_equal": False,
        "gpu_campaign_runner_hash_transition": {
            "archived": ARCHIVED_GPU_CAMPAIGN_RUNNER_SHA256,
            "current": EXPECTED_CURRENT_GPU_CAMPAIGN_RUNNER_SHA256,
        },
        "historical_serving_runner_source_identity": (
            "UNVERIFIABLE_ARCHIVED_SERV_P0_25_RAW_HAS_NO_SERVING_RUNNER_SHA256"
        ),
        "comparability_boundary": (
            "The open-loop execution runner is serving_burst_runner.py. The current "
            "gpu_campaign_runner.py is synchronous and differs from its archived "
            "304a7e2c... copy. Historical SERV-P0-25 raw does not attest a "
            "serving_burst_runner.py hash, so no source-identical-runner claim is made."
        ),
    }


def _build_runner_argv(
    config: DriverConfig,
    runner_run_root: Path,
    contract: Target5Contract,
) -> list[str]:
    contract.validate()
    return [
        str(config.python_executable),
        "-u",
        str(config.serving_runner),
        "--model-path",
        str(config.model_path),
        "--run-root",
        str(runner_run_root),
        "--experiment-id",
        config.attempt_id,
        "--input-tokens",
        str(contract.input_tokens),
        "--output-tokens",
        str(contract.output_tokens),
        "--sampling-mode",
        contract.sampling_mode,
        "--concurrency",
        str(contract.concurrency),
        "--bursts",
        "1",
        "--intra-burst-gap-ms",
        "0",
        "--inter-burst-gap-ms",
        "0",
        "--arrival-rate-rps",
        repr(contract.arrival_rate_rps),
        "--total-requests",
        str(contract.requests),
        "--arrival-seed",
        str(contract.arrival_seed),
        "--warmup-burst",
        "--max-num-seqs",
        str(contract.max_num_seqs),
        "--max-num-batched-tokens",
        str(contract.max_num_batched_tokens),
        "--gpu-memory-utilization",
        repr(contract.gpu_memory_utilization),
    ]


def build_runner_argv(config: DriverConfig, runner_run_root: Path) -> list[str]:
    """Return the immutable production argv (the CPU tests use a private seam)."""

    return _build_runner_argv(config, runner_run_root, TARGET5)


def linear_percentile(values: Sequence[int], fraction: float) -> float:
    if not values:
        raise ArrivalDriverError("cannot compute percentile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _expect_equal(observed: Any, expected: Any, label: str) -> None:
    if observed != expected:
        raise ArrivalDriverError(f"{label}: expected {expected!r}, got {observed!r}")


def _validate_manifest(
    manifest: dict[str, Any], config: DriverConfig, contract: Target5Contract
) -> None:
    _expect_equal(manifest.get("schema_version"), "phase7-serving-manifest-v2", "raw manifest schema")
    _expect_equal(manifest.get("experiment_id"), config.attempt_id, "raw experiment_id")
    _expect_equal(manifest.get("runtime_class"), "SERVING_VARIANT", "runtime_class")
    _expect_equal(manifest.get("sampling_mode"), contract.sampling_mode, "sampling_mode")
    _expect_equal(manifest.get("arrival_mode"), "POISSON_OPEN_LOOP", "arrival_mode")
    _expect_equal(manifest.get("model_revision"), MODEL_REVISION, "model_revision")
    _expect_equal(manifest.get("model_path"), str(config.model_path), "raw manifest model_path")
    arrival = manifest.get("arrival_contract")
    if not isinstance(arrival, dict):
        raise ArrivalDriverError("raw manifest arrival_contract is not an object")
    _expect_equal(arrival.get("rate_rps"), contract.arrival_rate_rps, "arrival rate")
    _expect_equal(arrival.get("seed"), contract.arrival_seed, "arrival seed")
    _expect_equal(arrival.get("total_requests"), contract.requests, "arrival request count")
    variables = manifest.get("variables")
    if not isinstance(variables, dict):
        raise ArrivalDriverError("raw manifest variables is not an object")
    frozen = {
        "model_path": str(config.model_path),
        "input_tokens": contract.input_tokens,
        "output_tokens": contract.output_tokens,
        "concurrency": contract.concurrency,
        "arrival_rate_rps": contract.arrival_rate_rps,
        "arrival_seed": contract.arrival_seed,
        "total_requests": contract.requests,
        "max_num_seqs": contract.max_num_seqs,
        "max_num_batched_tokens": contract.max_num_batched_tokens,
        "gpu_memory_utilization": contract.gpu_memory_utilization,
        "sampling_mode": contract.sampling_mode,
        "warmup_burst": True,
        "warmup_bursts": contract.warmup_bursts,
    }
    for key, expected in frozen.items():
        _expect_equal(variables.get(key), expected, f"raw manifest variables.{key}")


def _validate_input_fixture(
    fixture: dict[str, Any], contract: Target5Contract
) -> list[dict[str, Any]]:
    _expect_equal(
        fixture.get("fixture_id"),
        "phase7_mixtral_serving_repeated_anchor_v1",
        "input fixture ID",
    )
    slots = fixture.get("request_plan")
    if not isinstance(slots, list) or any(not isinstance(slot, dict) for slot in slots):
        raise ArrivalDriverError("input fixture request_plan is not a list of objects")
    _expect_equal(len(slots), contract.concurrency, "input fixture slot count")
    for index, slot in enumerate(slots):
        _expect_equal(slot.get("slot"), index, f"input fixture slot[{index}].slot")
        _expect_equal(slot.get("class"), "homogeneous", f"input fixture slot[{index}].class")
        _expect_equal(
            slot.get("input_tokens"),
            contract.input_tokens,
            f"input fixture slot[{index}].input_tokens",
        )
        _expect_equal(
            slot.get("output_tokens"),
            contract.output_tokens,
            f"input fixture slot[{index}].output_tokens",
        )
        token_ids = slot.get("token_ids")
        if not isinstance(token_ids, list) or any(
            not isinstance(token, int) or isinstance(token, bool) for token in token_ids
        ):
            raise ArrivalDriverError(
                f"input fixture slot[{index}].token_ids is not a list of integers"
            )
        _expect_equal(
            len(token_ids),
            contract.input_tokens,
            f"input fixture slot[{index}].token_ids length",
        )
        _expect_equal(
            slot.get("token_count"),
            len(token_ids),
            f"input fixture slot[{index}].token_count",
        )
        _expect_equal(
            slot.get("token_ids_sha256"),
            sha256_json(token_ids),
            f"input fixture slot[{index}].token_ids_sha256",
        )
    return slots


def _validate_arrival_trace(
    rows: list[dict[str, Any]],
    contract: Target5Contract,
    fixture_slots: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    _expect_equal(len(rows), contract.requests, "arrival trace row count")
    rng = random.Random(contract.arrival_seed)
    expected_offset_ns = 0
    previous_scheduled_ns: int | None = None
    schedule_start_ns: int | None = None
    by_index: dict[int, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if index:
            expected_offset_ns += int(
                rng.expovariate(contract.arrival_rate_rps) * 1_000_000_000
            )
        expected_id = f"open-{index:06d}"
        _expect_equal(row.get("schema_version"), "phase7-serving-arrival-v1", f"arrival[{index}].schema")
        _expect_equal(row.get("arrival_mode"), "POISSON_OPEN_LOOP", f"arrival[{index}].mode")
        _expect_equal(row.get("arrival_index"), index, f"arrival[{index}].index")
        _expect_equal(row.get("request_id"), expected_id, f"arrival[{index}].request_id")
        _expect_equal(row.get("arrival_seed"), contract.arrival_seed, f"arrival[{index}].seed")
        _expect_equal(row.get("arrival_rate_rps"), contract.arrival_rate_rps, f"arrival[{index}].rate")
        _expect_equal(row.get("input_tokens"), contract.input_tokens, f"arrival[{index}].input_tokens")
        _expect_equal(row.get("output_tokens"), contract.output_tokens, f"arrival[{index}].output_tokens")
        slot_index = index % contract.concurrency
        slot = fixture_slots[slot_index]
        _expect_equal(row.get("slot"), slot_index, f"arrival[{index}].slot")
        _expect_equal(row.get("class"), slot["class"], f"arrival[{index}].class")
        _expect_equal(row.get("scheduled_offset_ns"), expected_offset_ns, f"arrival[{index}].offset")
        scheduled_ns = row.get("scheduled_monotonic_ns")
        if not isinstance(scheduled_ns, int) or isinstance(scheduled_ns, bool):
            raise ArrivalDriverError(f"arrival[{index}].scheduled_monotonic_ns is not an integer")
        if schedule_start_ns is None:
            schedule_start_ns = scheduled_ns
        _expect_equal(
            scheduled_ns,
            schedule_start_ns + expected_offset_ns,
            f"arrival[{index}].scheduled_monotonic_ns",
        )
        if previous_scheduled_ns is not None and scheduled_ns < previous_scheduled_ns:
            raise ArrivalDriverError(f"arrival[{index}] has a decreasing scheduled timestamp")
        previous_scheduled_ns = scheduled_ns
        by_index[index] = row
    return by_index


def _validate_request_records(
    rows: list[dict[str, Any]],
    contract: Target5Contract,
    *,
    source: str,
    arrival_by_index: dict[int, dict[str, Any]],
    fixture_slots: list[dict[str, Any]],
) -> tuple[list[int], dict[int, dict[str, Any]]]:
    _expect_equal(len(rows), contract.requests, f"{source} row count")
    by_index: dict[int, dict[str, Any]] = {}
    for row in rows:
        index = row.get("arrival_index")
        if not isinstance(index, int) or isinstance(index, bool) or index in by_index:
            raise ArrivalDriverError(f"{source} has invalid/duplicate arrival_index {index!r}")
        by_index[index] = row
    _expect_equal(sorted(by_index), list(range(contract.requests)), f"{source} indices")

    latencies: list[int] = []
    for index in range(contract.requests):
        row = by_index[index]
        trace = arrival_by_index[index]
        fixture = fixture_slots[index % contract.concurrency]
        _expect_equal(row.get("schema_version"), "phase7-serving-request-v1", f"{source}[{index}].schema")
        _expect_equal(row.get("request_id"), f"open-{index:06d}", f"{source}[{index}].request_id")
        _expect_equal(row.get("sampling_mode"), contract.sampling_mode, f"{source}[{index}].sampling_mode")
        _expect_equal(row.get("input_tokens"), contract.input_tokens, f"{source}[{index}].input_tokens")
        _expect_equal(
            row.get("requested_output_tokens"),
            contract.output_tokens,
            f"{source}[{index}].requested_output_tokens",
        )
        if row.get("error") is not None:
            raise ArrivalDriverError(f"{source}[{index}] request error: {row.get('error')}")
        output_tokens = row.get("output_tokens")
        if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or not (1 <= output_tokens <= contract.output_tokens):
            raise ArrivalDriverError(f"{source}[{index}] output_tokens is outside [1,{contract.output_tokens}]")
        if row.get("finish_reason") not in {"stop", "length"}:
            raise ArrivalDriverError(f"{source}[{index}] has invalid finish_reason {row.get('finish_reason')!r}")
        _expect_equal(
            row.get("input_ids_sha256"),
            fixture["token_ids_sha256"],
            f"{source}[{index}].input_ids_sha256",
        )
        output_token_ids = row.get("output_token_ids")
        if not isinstance(output_token_ids, list) or any(
            not isinstance(token, int) or isinstance(token, bool)
            for token in output_token_ids
        ):
            raise ArrivalDriverError(
                f"{source}[{index}].output_token_ids is not a list of integers"
            )
        _expect_equal(
            len(output_token_ids),
            output_tokens,
            f"{source}[{index}].output_token_ids length",
        )
        _expect_equal(
            row.get("output_ids_sha256"),
            sha256_json(output_token_ids),
            f"{source}[{index}].output_ids_sha256",
        )
        if not isinstance(row.get("output_text"), str):
            raise ArrivalDriverError(f"{source}[{index}].output_text is not a string")
        scheduled = row.get("client_scheduled_arrival_monotonic_ns")
        observed = row.get("server_observed_arrival_monotonic_ns")
        submitted = row.get("submitted_monotonic_ns")
        first = row.get("first_yield_monotonic_ns")
        completed = row.get("completed_monotonic_ns")
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (scheduled, observed, submitted, first, completed)):
            raise ArrivalDriverError(f"{source}[{index}] has missing/non-integer monotonic timestamps")
        if observed != submitted:
            raise ArrivalDriverError(f"{source}[{index}] submitted != server-observed arrival")
        _expect_equal(
            scheduled,
            trace["scheduled_monotonic_ns"],
            f"{source}[{index}].client_scheduled_arrival_monotonic_ns",
        )
        if not scheduled <= observed <= first <= completed:
            raise ArrivalDriverError(f"{source}[{index}] violates scheduled<=observed<=first<=completed")
        completion_ns = completed - observed
        ttft_ns = first - observed
        _expect_equal(row.get("completion_latency_ns"), completion_ns, f"{source}[{index}].completion_latency_ns")
        _expect_equal(row.get("ttft_ns"), ttft_ns, f"{source}[{index}].ttft_ns")
        latencies.append(completion_ns)
    return latencies, by_index


def _validate_warmup_records(
    rows: list[dict[str, Any]],
    contract: Target5Contract,
    fixture_slots: list[dict[str, Any]],
) -> None:
    _expect_equal(len(rows), contract.concurrency, "excluded warmup request count")
    by_id = {row.get("request_id"): row for row in rows}
    expected_ids = {
        f"warmup-0000-{index:04d}" for index in range(contract.concurrency)
    }
    _expect_equal(set(by_id), expected_ids, "excluded warmup request IDs")
    for slot_index in range(contract.concurrency):
        request_id = f"warmup-0000-{slot_index:04d}"
        row = by_id[request_id]
        fixture = fixture_slots[slot_index]
        _expect_equal(row.get("arrival_index"), None, f"{request_id}.arrival_index")
        _expect_equal(row.get("error"), None, f"{request_id}.error")
        _expect_equal(row.get("input_tokens"), contract.input_tokens, f"{request_id}.input_tokens")
        _expect_equal(
            row.get("requested_output_tokens"),
            contract.output_tokens,
            f"{request_id}.requested_output_tokens",
        )
        _expect_equal(
            row.get("input_ids_sha256"),
            fixture["token_ids_sha256"],
            f"{request_id}.input_ids_sha256",
        )
        output_ids = row.get("output_token_ids")
        if not isinstance(output_ids, list) or any(
            not isinstance(token, int) or isinstance(token, bool) for token in output_ids
        ):
            raise ArrivalDriverError(f"{request_id}.output_token_ids is invalid")
        _expect_equal(row.get("output_tokens"), len(output_ids), f"{request_id}.output_tokens")
        _expect_equal(
            row.get("output_ids_sha256"),
            sha256_json(output_ids),
            f"{request_id}.output_ids_sha256",
        )
        submitted = row.get("submitted_monotonic_ns")
        scheduled = row.get("client_scheduled_arrival_monotonic_ns")
        observed = row.get("server_observed_arrival_monotonic_ns")
        first = row.get("first_yield_monotonic_ns")
        completed = row.get("completed_monotonic_ns")
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (submitted, scheduled, observed, first, completed)
        ):
            raise ArrivalDriverError(f"{request_id} has invalid timestamps")
        if not scheduled == submitted == observed <= first <= completed:
            raise ArrivalDriverError(f"{request_id} violates warmup timestamp ordering")
        _expect_equal(
            row.get("ttft_ns"), first - observed, f"{request_id}.ttft_ns"
        )
        _expect_equal(
            row.get("completion_latency_ns"),
            completed - observed,
            f"{request_id}.completion_latency_ns",
        )


def validate_raw_run(
    raw_run_dir: Path,
    config: DriverConfig,
    contract: Target5Contract = TARGET5,
) -> dict[str, Any]:
    required = (
        "manifest.json",
        "status.json",
        "result.json",
        "arrival_trace.jsonl",
        "requests.jsonl",
        "requested_engine_args.json",
        "input_fixture.json",
        "telemetry.jsonl",
        "warmup_requests.jsonl",
    )
    missing = [name for name in required if not (raw_run_dir / name).is_file()]
    if missing:
        raise ArrivalDriverError(f"raw runner output is incomplete; missing {missing}")
    manifest = read_json(raw_run_dir / "manifest.json")
    status = read_json(raw_run_dir / "status.json")
    result = read_json(raw_run_dir / "result.json")
    requested_engine = read_json(raw_run_dir / "requested_engine_args.json")
    input_fixture = read_json(raw_run_dir / "input_fixture.json")
    trace = read_jsonl(raw_run_dir / "arrival_trace.jsonl")
    request_rows = read_jsonl(raw_run_dir / "requests.jsonl")
    warmup_rows = read_jsonl(raw_run_dir / "warmup_requests.jsonl")
    telemetry_rows = read_jsonl(raw_run_dir / "telemetry.jsonl")
    _validate_manifest(manifest, config, contract)
    _expect_equal(status.get("status"), "PASS", "raw status")
    _expect_equal(result.get("schema_version"), "phase7-serving-result-v2", "raw result schema")
    _expect_equal(result.get("status"), "PASS", "raw result status")
    _expect_equal(result.get("arrival_mode"), "POISSON_OPEN_LOOP", "raw result arrival mode")
    _expect_equal(result.get("arrival_rate_rps"), contract.arrival_rate_rps, "raw result arrival rate")
    _expect_equal(result.get("arrival_seed"), contract.arrival_seed, "raw result arrival seed")
    _expect_equal(result.get("requested_request_count"), contract.requests, "raw requested count")
    _expect_equal(result.get("completed_request_count"), contract.requests, "raw completed count")
    result_rows = result.get("records")
    if not isinstance(result_rows, list) or any(not isinstance(row, dict) for row in result_rows):
        raise ArrivalDriverError("raw result.records is not a list of objects")
    fixture_slots = _validate_input_fixture(input_fixture, contract)
    _expect_equal(
        requested_engine.get("model_revision"),
        MODEL_REVISION,
        "requested engine model_revision",
    )
    _expect_equal(
        requested_engine.get("sampling_mode"),
        contract.sampling_mode,
        "requested engine sampling_mode",
    )
    expected_plan = [
        {
            "slot": index,
            "class": "homogeneous",
            "input_tokens": contract.input_tokens,
            "output_tokens": contract.output_tokens,
        }
        for index in range(contract.concurrency)
    ]
    _expect_equal(
        requested_engine.get("request_plan"),
        expected_plan,
        "requested engine request_plan",
    )
    sampling = requested_engine.get("sampling_params_by_slot")
    if not isinstance(sampling, list):
        raise ArrivalDriverError("requested engine sampling_params_by_slot is not a list")
    expected_sampling = {
        "n": 1,
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 0,
        "seed": 0,
        "stop": [],
        "ignore_eos": False,
        "detokenize": True,
        "skip_special_tokens": False,
        "max_tokens": contract.output_tokens,
        "min_tokens": 0,
    }
    _expect_equal(
        sampling,
        [expected_sampling for _ in range(contract.concurrency)],
        "requested engine sampling_params_by_slot",
    )
    engine_repr = requested_engine.get("engine_args")
    if not isinstance(engine_repr, str):
        raise ArrivalDriverError("requested engine_args is not a string")
    engine_markers = (
        f"model='{config.model_path}'",
        "dtype='bfloat16'",
        "max_model_len=32768",
        f"max_num_seqs={contract.max_num_seqs}",
        f"max_num_batched_tokens={contract.max_num_batched_tokens}",
        f"gpu_memory_utilization={contract.gpu_memory_utilization}",
        "enforce_eager=True",
        "enable_prefix_caching=False",
        "tensor_parallel_size=1",
        "pipeline_parallel_size=1",
    )
    missing_engine_markers = [marker for marker in engine_markers if marker not in engine_repr]
    if missing_engine_markers:
        raise ArrivalDriverError(
            f"requested engine_args lacks frozen markers: {missing_engine_markers}"
        )
    arrival_by_index = _validate_arrival_trace(trace, contract, fixture_slots)
    result_latencies, result_by_index = _validate_request_records(
        result_rows,
        contract,
        source="result.records",
        arrival_by_index=arrival_by_index,
        fixture_slots=fixture_slots,
    )
    request_latencies, requests_by_index = _validate_request_records(
        request_rows,
        contract,
        source="requests.jsonl",
        arrival_by_index=arrival_by_index,
        fixture_slots=fixture_slots,
    )
    _validate_warmup_records(warmup_rows, contract, fixture_slots)
    if len(telemetry_rows) < 2:
        raise ArrivalDriverError("raw telemetry must retain at least pre/post snapshots")
    if sorted(result_latencies) != sorted(request_latencies):
        raise ArrivalDriverError("result.records and requests.jsonl latency samples differ")
    result_record_hashes = [
        sha256_json(result_by_index[index]) for index in range(contract.requests)
    ]
    request_record_hashes = [
        sha256_json(requests_by_index[index]) for index in range(contract.requests)
    ]
    _expect_equal(
        request_record_hashes,
        result_record_hashes,
        "result.records vs requests.jsonl canonical row hashes",
    )
    for index in range(contract.requests):
        _expect_equal(
            requests_by_index[index],
            result_by_index[index],
            f"result.records vs requests.jsonl row[{index}]",
        )
    result_ids = {row["request_id"] for row in result_rows}
    trace_ids = {row["request_id"] for row in trace}
    if result_ids != trace_ids:
        raise ArrivalDriverError("arrival trace and request result ID sets differ")
    return {
        "records": result_rows,
        "completion_latency_ns": result_latencies,
        "arrival_trace_count": len(trace),
        "request_jsonl_count": len(request_rows),
        "warmup_request_count": len(warmup_rows),
        "telemetry_row_count": len(telemetry_rows),
        "canonical_request_records_sha256": sha256_json(result_record_hashes),
        "raw_file_sha256": {
            name: sha256_file(raw_run_dir / name) for name in required
        },
    }


def serving_tail_summary(
    config: DriverConfig,
    raw_run_dir: Path,
    validated: dict[str, Any],
    lineage: dict[str, Any],
    contract: Target5Contract = TARGET5,
) -> dict[str, Any]:
    latency_ns = validated["completion_latency_ns"]
    return {
        "schema_version": SUMMARY_SCHEMA,
        "attempt_id": config.attempt_id,
        "start_request_index": 0,
        "resumed_partial_progress": False,
        "planned_requests": contract.requests,
        "completed_requests": len(latency_ns),
        "completion_latency": {
            "p50_ms": linear_percentile(latency_ns, 0.50) / 1_000_000,
            "p95_ms": linear_percentile(latency_ns, 0.95) / 1_000_000,
            "p99_ms": linear_percentile(latency_ns, 0.99) / 1_000_000,
            "max_ms": max(latency_ns) / 1_000_000,
        },
        "arrival_contract": {
            "mode": "POISSON_OPEN_LOOP",
            "rate_rps": contract.arrival_rate_rps,
            "seed": contract.arrival_seed,
        },
        "request_shape": {
            "concurrency": contract.concurrency,
            "input_tokens": contract.input_tokens,
            "output_tokens_cap": contract.output_tokens,
            "sampling_mode": contract.sampling_mode,
        },
        "raw_run_dir": str(raw_run_dir),
        "raw_file_sha256": validated["raw_file_sha256"],
        "canonical_request_records_sha256": validated[
            "canonical_request_records_sha256"
        ],
        "code_lineage": lineage,
        "claim_boundary": (
            "Fresh 10K tail-CI supplement for the frozen offload-off serving domain. "
            "It extends but does not replace the historical 1K SERV-P0-25 sample; "
            "source-identical gpu_campaign_runner comparability is not claimed."
        ),
    }


def _write_checksums(attempt_dir: Path) -> None:
    output = attempt_dir / "ATTEMPT_SHA256SUMS"
    rows = []
    for path in sorted(attempt_dir.rglob("*")):
        if path.is_file() and path != output:
            rows.append(f"{sha256_file(path)}  {path.relative_to(attempt_dir)}")
    output.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _discover_raw_run(runner_root: Path) -> Path:
    candidates = sorted(path for path in runner_root.iterdir() if path.is_dir())
    if len(candidates) != 1:
        raise ArrivalDriverError(
            f"expected exactly one fresh raw runner directory, found {len(candidates)}"
        )
    return candidates[0]


def _execute_with_contract(
    config: DriverConfig,
    *,
    contract: Target5Contract,
    model_contract: model_identity_contract.ModelContract = (
        model_identity_contract.CANONICAL_CONTRACT
    ),
    clock: Clock | None = None,
    process_runner: ProcessRunner | None = None,
    server_contract: ServerContract | None = None,
) -> Path:
    clock = clock or SystemClock()
    process_runner = process_runner or SubprocessRunner()
    server_contract = server_contract or EmbeddedAsyncServerContract()
    contract.validate()
    if not ATTEMPT_ID_RE.fullmatch(config.attempt_id):
        raise ArrivalDriverError(
            "attempt_id must be a fresh SERV-P0-25-TAIL-* identifier without path separators"
        )
    if not config.model_path.is_dir():
        raise ArrivalDriverError(f"model path is not a directory: {config.model_path}")
    attempt_dir = config.run_root.resolve() / config.attempt_id
    if attempt_dir.exists():
        raise ArrivalDriverError(
            f"attempt directory already exists (resume is forbidden): {attempt_dir}"
        )
    attempt_dir.mkdir(parents=True)
    for relative in ("artifacts", "environment", "logs"):
        (attempt_dir / relative).mkdir()
    runner_root = attempt_dir / "artifacts" / "runner_runs"
    runner_root.mkdir()
    started_utc = clock.utc_now()
    started_ns = clock.monotonic_ns()
    argv = _build_runner_argv(config, runner_root, contract)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA,
        "attempt_id": config.attempt_id,
        "target": "target_5_SERV-P0-25_tail_CI",
        "status": "RUNNING",
        "started_at_utc": started_utc,
        "start_request_index": 0,
        "resumed_partial_progress": False,
        "exact_argv": argv,
        "cwd": str(Path.cwd()),
        "contract": dataclasses.asdict(contract),
        "failure_classification": None,
    }
    write_json(attempt_dir / "manifest.json", manifest)
    write_json(
        attempt_dir / "resolved_config.yaml",
        {
            "schema_version": "serv-p0-25-arrival-driver-config-v1",
            "attempt_id": config.attempt_id,
            "model_path": str(config.model_path),
            "start_request_index": 0,
            "resumed_partial_progress": False,
            "contract": dataclasses.asdict(contract),
            "exact_runner_argv": argv,
        },
    )
    (attempt_dir / "logs" / "command.log").write_text(
        json.dumps(argv, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    stdout_path = attempt_dir / "logs" / "stdout.log"
    stderr_path = attempt_dir / "logs" / "stderr.log"
    stdout_path.touch()
    stderr_path.touch()
    child_returncode: int | None = None
    try:
        model_identity = validate_model_identity(
            config.model_identity_json,
            config.model_path,
            contract=model_contract,
        )
        lineage = code_lineage(config)
        lineage["driver"] = {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
        }
        server_identity = server_contract.verify(config.serving_runner)
        write_json(attempt_dir / "environment" / "model_identity.json", model_identity)
        write_json(
            attempt_dir / "environment" / "runtime_identity.json",
            runtime_identity(
                config,
                captured_at_utc=clock.utc_now(),
                server_identity=server_identity,
                lineage=lineage,
            ),
        )
        child_returncode = process_runner.run(
            argv,
            cwd=Path.cwd(),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        if child_returncode != 0:
            raise ArrivalDriverError(
                f"serving runner returned {child_returncode}; raw attempt retained"
            )
        raw_run_dir = _discover_raw_run(runner_root)
        validated = validate_raw_run(raw_run_dir, config, contract)
        summary = serving_tail_summary(config, raw_run_dir, validated, lineage, contract)
        # Import only the pure-stdlib parser validation path; a PASS wrapper may
        # never emit a summary that its downstream gate rejects.
        try:
            from measurement.parsers.serving_tail_parser import validate as validate_summary
        except ImportError:  # direct script execution outside an installed package
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from measurement.parsers.serving_tail_parser import validate as validate_summary

        validate_summary(summary)
        write_json(attempt_dir / "serving_tail.json", summary)
        finished_ns = clock.monotonic_ns()
        manifest.update(
            {
                "status": "PASS",
                "finished_at_utc": clock.utc_now(),
                "duration_ns": finished_ns - started_ns,
                "runner_returncode": child_returncode,
                "raw_run_dir": str(raw_run_dir),
                "code_lineage": lineage,
            }
        )
        write_json(attempt_dir / "manifest.json", manifest)
        write_json(
            attempt_dir / "metrics.json",
            {
                "status": "PASS",
                "planned_requests": contract.requests,
                "completed_requests": contract.requests,
                "duration_ns": manifest["duration_ns"],
                "runner_returncode": child_returncode,
                "completion_latency": summary["completion_latency"],
            },
        )
        _write_checksums(attempt_dir)
        return attempt_dir
    except BaseException as exc:
        finished_ns = clock.monotonic_ns()
        classification = (
            "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "ARRIVAL_DRIVER_FAIL_CLOSED"
        )
        manifest.update(
            {
                "status": "FAIL",
                "finished_at_utc": clock.utc_now(),
                "duration_ns": finished_ns - started_ns,
                "runner_returncode": child_returncode,
                "failure_classification": classification,
                "failure": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        )
        write_json(attempt_dir / "manifest.json", manifest)
        write_json(
            attempt_dir / "metrics.json",
            {
                "status": "FAIL",
                "duration_ns": manifest["duration_ns"],
                "runner_returncode": child_returncode,
                "failure_classification": classification,
            },
        )
        write_json(
            attempt_dir / "failure.json",
            {
                "status": "FAIL",
                "classification": classification,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        stdout_path.touch(exist_ok=True)
        stderr_path.touch(exist_ok=True)
        _write_checksums(attempt_dir)
        raise


def execute(
    config: DriverConfig,
    *,
    clock: Clock | None = None,
    process_runner: ProcessRunner | None = None,
    server_contract: ServerContract | None = None,
) -> Path:
    """Execute exactly the owner-frozen target_5 contract."""

    return _execute_with_contract(
        config,
        contract=TARGET5,
        clock=clock,
        process_runner=process_runner,
        server_contract=server_contract,
    )


def default_paths() -> dict[str, Path]:
    root = Path(__file__).resolve().parents[2]
    real_run = root / "explorations" / "moe_cycle_simulator" / "phase7" / "real_run"
    return {
        "serving_runner": real_run / "serving_burst_runner.py",
        "gpu_campaign_runner": real_run / "gpu_campaign_runner.py",
        "archived_gpu_campaign_runner": (
            root
            / "evidence"
            / "measurement_backups"
            / "20260811T161000Z__phase7_remote_natural_matrix_backup"
            / "remote_code"
            / "gpu_campaign_runner.py"
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument(
        "--model-identity-json",
        type=Path,
        required=True,
        help=(
            "track-gpu-model-identity-v1 JSON with exact revision, PASS verification, "
            "and checksum_manifest_sha256"
        ),
    )
    parser.add_argument("--serving-runner", type=Path, default=defaults["serving_runner"])
    parser.add_argument(
        "--gpu-campaign-runner", type=Path, default=defaults["gpu_campaign_runner"]
    )
    parser.add_argument(
        "--archived-gpu-campaign-runner",
        type=Path,
        default=defaults["archived_gpu_campaign_runner"],
        help="set to an empty string only on a deployment that omits immutable evidence",
    )
    parser.add_argument("--python-executable", type=Path, default=Path(sys.executable))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    archived = args.archived_gpu_campaign_runner
    if archived is not None and not archived.is_file():
        archived = None
    config = DriverConfig(
        attempt_id=args.attempt_id,
        run_root=args.run_root,
        model_path=args.model_path,
        model_identity_json=args.model_identity_json,
        serving_runner=args.serving_runner,
        gpu_campaign_runner=args.gpu_campaign_runner,
        archived_gpu_campaign_runner=archived,
        python_executable=args.python_executable,
    )
    try:
        attempt_dir = execute(config)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            print(json.dumps({"status": "FAIL", "classification": "INTERRUPTED"}), file=sys.stderr)
            return 130
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "classification": "ARRIVAL_DRIVER_FAIL_CLOSED",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "attempt_dir": str(attempt_dir),
                "serving_tail": str(attempt_dir / "serving_tail.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
