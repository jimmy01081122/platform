#!/usr/bin/env python3
"""Execute one scheduler C1 work unit without network or implicit path discovery."""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import math
import os
import sys
import time
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.c1_common import as_mapping, generate  # noqa: E402
from collectors.c1_contract import CollectorRequest  # noqa: E402
from collectors.c1_p0 import collect as collect_p0  # noqa: E402
from collectors.c1_p1 import collect as collect_p1  # noqa: E402
from collectors.c1_p2 import collect as collect_p2  # noqa: E402
from collectors.c1_p3 import collect as collect_p3  # noqa: E402
from collectors.c1_p5_basic import collect as collect_p5  # noqa: E402
from collectors.c1_telemetry import (  # noqa: E402
    TelemetrySampler, TelemetryUnavailable,
)
from collectors.trace_contract import (  # noqa: E402
    build_execution_alignment_key, canonical_hash,
)
from scripts.c1_quality import (  # noqa: E402
    build_quality_artifact,
    validate_contract_binding,
    validate_quality_artifact,
)
from scheduler.validators import C1_PASS_ARTIFACT, COMMON_C1_ARTIFACTS  # noqa: E402


class Unavailable(RuntimeError):
    pass


DIAGNOSTIC_MODE = "token_drift_v1"
DIAGNOSTIC_EVIDENCE_CLASS = "diagnostic_non_c1"
TOKENIZATION_SCHEMA_FIELDS = {
    "mode", "chat_template_sha256", "tokenizer_config_sha256",
    "message_roles", "add_generation_prompt", "prompt_construction_revision",
    "system_message_sha256", "generation_config_file_sha256",
    "special_tokens_map_sha256", "eos_token_id", "pad_token_id",
    "rendered_chat_sha256",
}


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(
            value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        ) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(
            dict(row), sort_keys=True, ensure_ascii=False, allow_nan=False
        ) + "\n"
                for row in rows),
        encoding="utf-8",
    )


def load_factory(spec: str | None):
    if not spec:
        from adapters.models.granite_moe.adapter import GraniteMoeAdapter
        return GraniteMoeAdapter
    module_name, separator, attribute = spec.partition(":")
    if not separator:
        raise ValueError("C1_ADAPTER_FACTORY must be module:attribute")
    return getattr(importlib.import_module(module_name), attribute)


def construct_adapter(snapshot: str | None):
    factory = load_factory(os.environ.get("C1_ADAPTER_FACTORY"))
    try:
        return factory(snapshot_path=snapshot)
    except TypeError:
        return factory()


class CaptureRunner:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.last_generation: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    def generate(self, tokens: Any, request: Any) -> Any:
        self.last_generation = self.adapter.generate(tokens, request)
        return self.last_generation

    def collect_quality_result(self, _result: Any, sample: Mapping[str, Any]) -> Any:
        return self.adapter.collect_quality_result(self.last_generation, sample)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def load_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    snapshot_path = Path(require_env("PROJECTCTL_SUITE_SNAPSHOT"))
    suite = json.loads(snapshot_path.read_text(encoding="utf-8"))
    sample_id = require_env("PROJECTCTL_SAMPLE_ID")
    selected = [
        row for row in suite.get("samples", []) if row.get("sample_id") == sample_id
    ]
    if len(selected) != 1:
        raise ValueError(f"suite snapshot must contain exactly one sample {sample_id!r}")
    selection = selected[0]
    sample = selection.get("source_sample")
    if not isinstance(sample, dict):
        raise ValueError("suite snapshot does not embed the frozen source sample")
    if sample.get("sample_id") != selection.get("source_sample_id"):
        raise ValueError("selection/source sample identity mismatch")
    if sha256(str(sample.get("prompt", "")).encode("utf-8")).hexdigest() != selection.get(
        "prompt_hash"
    ):
        raise ValueError("frozen sample prompt hash mismatch")
    config = suite.get("generation_config")
    if not isinstance(config, dict):
        raise ValueError("suite snapshot lacks generation_config")
    return suite, selection, sample


def validate_schema(name: str, value: Mapping[str, Any]) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise Unavailable("jsonschema runtime is not installed") from exc
    schema = json.loads((PACKAGE_ROOT / "schemas" / name).read_text(encoding="utf-8"))
    jsonschema.validate(dict(value), schema)


def quality_contract_context(
    suite: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    binding = suite.get("quality_contract")
    if binding is None:
        return None
    if not isinstance(binding, Mapping):
        raise ValueError("suite quality_contract binding must be an object")
    relative = binding.get("path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("suite quality contract path is missing")
    def verified_source(path_value: Any, label: str) -> Path:
        if (
            not isinstance(path_value, str)
            or not path_value
            or Path(path_value).is_absolute()
        ):
            raise ValueError(f"{label} path is invalid")
        root = PACKAGE_ROOT.resolve()
        unresolved = PACKAGE_ROOT / path_value
        current = unresolved
        while current != PACKAGE_ROOT:
            if current.is_symlink():
                raise ValueError(f"{label} source symlink is forbidden")
            current = current.parent
        resolved = unresolved.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} source escapes package root") from exc
        if not resolved.is_file():
            raise ValueError(f"{label} source is not a file")
        return resolved

    path = verified_source(relative, "quality contract")
    source_sha256 = sha256(path.read_bytes()).hexdigest()
    contract = json.loads(path.read_text(encoding="utf-8"))
    evaluator_relative = contract.get("evaluator_source_path")
    engine_relative = contract.get("quality_engine_path")
    if binding.get("evaluator_source_path") != evaluator_relative:
        raise ValueError("quality evaluator source path drift")
    if binding.get("quality_engine_path") != engine_relative:
        raise ValueError("quality engine source path drift")
    evaluator_path = verified_source(evaluator_relative, "quality evaluator")
    engine_path = verified_source(engine_relative, "quality engine")
    validate_contract_binding(
        binding,
        contract,
        path=relative,
        source_sha256=source_sha256,
        evaluator_source_sha256=sha256(evaluator_path.read_bytes()).hexdigest(),
        quality_engine_sha256=sha256(engine_path.read_bytes()).hexdigest(),
        samples=suite.get("samples", []),
    )
    return contract, dict(binding)


def preflight(adapter: Any, snapshot: str | None) -> None:
    try:
        report = adapter.preflight(snapshot_path=snapshot)
    except TypeError:
        report = adapter.preflight()
    value = as_mapping(report)
    if value.get("eligible") is not True:
        blockers = value.get("blockers") or ["adapter preflight is not eligible"]
        raise Unavailable("; ".join(str(item) for item in blockers))


def execution_document(
    suite: Mapping[str, Any], selection: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    model = suite.get("model") or {}
    return {
        "suite_version": suite.get("suite_revision") or suite.get("suite_id"),
        "model_revision": model.get("revision"),
        "tokenizer_revision": model.get("tokenizer_revision"),
        "benchmark_id": selection.get("benchmark_id"),
        "sample_id": selection.get("source_sample_id"),
        "prompt_hash": selection.get("prompt_hash"),
        "generation_config_hash": canonical_hash(config),
        "seed": int(config.get("seed", 0)),
        "repetition_id": int(require_env("PROJECTCTL_REPETITION")),
        "hardware_session_id": os.environ.get(
            "PROJECTCTL_HARDWARE_SESSION_ID", require_env("PROJECTCTL_SESSION_ID")
        ),
    }


def validate_generation_result(generation: Mapping[str, Any]) -> None:
    """Reject malformed exact-token evidence before writing formal artifacts."""
    for name in ("input_token_ids", "output_token_ids"):
        values = generation.get(name)
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
            raise RuntimeError(f"{name} must contain only non-negative integers")
        count_name = name.removesuffix("_ids") + "_count"
        count = generation.get(count_name)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count != len(values)
        ):
            raise RuntimeError(f"{count_name} differs from {name}")
    stop_reason = generation.get("stop_reason")
    if not isinstance(stop_reason, str) or not stop_reason.strip():
        raise RuntimeError("stop_reason must be a non-empty string")
    if generation.get("output_hash") != canonical_hash(
        generation["output_token_ids"]
    ):
        raise RuntimeError(
            "output_hash does not match canonical output_token_ids"
        )


def quality_document(
    quality: Mapping[str, Any], generation: Mapping[str, Any],
    records: list[Mapping[str, Any]], alignment: str, pass_id: str,
) -> dict[str, Any]:
    output_ids = generation.get("output_token_ids")
    nonempty = isinstance(output_ids, list) and bool(output_ids)
    consistent = (
        isinstance(output_ids, list)
        and generation.get("output_token_count") == len(output_ids)
    )
    validity = quality.get("validity", quality.get("status") == "pass")
    correctness = quality.get("correctness")
    evaluator = quality.get("evaluator")

    def finite(value: Any) -> bool:
        if isinstance(value, bool) or value is None:
            return True
        if isinstance(value, (int, float)):
            return math.isfinite(value)
        if isinstance(value, Mapping):
            return all(finite(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return all(finite(item) for item in value)
        return True

    finite_values = finite(records)
    expert_lists = [
        row["selected_experts"]
        for row in records
        if "selected_experts" in row
    ]
    expert_ids_legal = (
        all(
            isinstance(experts, list)
            and len(experts) == 8
            and len(set(experts)) == 8
            and all(
                isinstance(expert, int)
                and not isinstance(expert, bool)
                and 0 <= expert < 32
                for expert in experts
            )
            for experts in expert_lists
        )
        if expert_lists else None
    )
    reasons = []
    if not nonempty:
        reasons.append("generation output is empty")
    if not consistent:
        reasons.append("output token count differs from token IDs")
    if validity is False:
        reasons.append("benchmark output is not valid")
    if correctness is False:
        if evaluator == "t0_integer_semantics_v1":
            reasons.append(
                "T0 parsed integer sequence does not satisfy the frozen semantic reference"
            )
        elif evaluator == "gsm8k_last_number":
            reasons.append(
                "T1 gsm8k_last_number evaluator reported correctness=false"
            )
        else:
            reasons.append(
                f"{evaluator or 'quality evaluator'} reported correctness=false"
            )
    if not finite_values:
        reasons.append("collector records contain NaN or infinity")
    if expert_ids_legal is False:
        reasons.append("collector records contain illegal expert IDs")
    return {
        "schema_version": "c1-quality-v1",
        "execution_alignment_key": alignment,
        "status": "pass" if not reasons else "fail",
        "output_nonempty": nonempty,
        "finite_values": finite_values,
        "token_count_consistent": consistent,
        "expert_ids_legal": expert_ids_legal,
        # Only the cross-pass P0/P2 audit can establish semantic interference.
        "instrumentation_semantic_interference": None,
        "benchmark_parseable": validity if isinstance(validity, bool) else None,
        "score": (
            float(quality["correctness"])
            if isinstance(quality.get("correctness"), bool) else None
        ),
        "reasons": reasons,
        "pass_id": pass_id,
    }


def collect_pass(
    pass_id: str, runner: CaptureRunner, request: CollectorRequest, output: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if pass_id == "P0":
        result = collect_p0(runner, request)
    elif pass_id == "P1":
        raw_root = output / "raw/profiler"

        def save_artifact(name: str, payload: bytes) -> Mapping[str, Any]:
            safe_name = Path(name).name
            if not safe_name or safe_name != name or not payload:
                raise ValueError(f"unsafe or empty profiler artifact: {name!r}")
            raw_root.mkdir(parents=True, exist_ok=True)
            path = raw_root / safe_name
            path.write_bytes(payload)
            relative = path.relative_to(output).as_posix()
            return {
                "path": relative,
                "bytes": len(payload),
                "sha256": sha256(payload).hexdigest(),
            }

        backend_spec = os.environ.get("C1_P1_BACKEND_FACTORY")
        backend = load_factory(backend_spec)() if backend_spec else None
        result = collect_p1(
            runner, request, backend=backend, save_artifact=save_artifact
        )
        if result.status != "complete":
            raise Unavailable("; ".join(result.unavailable.values()))
    elif pass_id == "P2":
        result = collect_p2(runner, request)
    elif pass_id == "P3":
        result = collect_p3(runner, request)
    elif pass_id == "P5_BASIC":
        raw_path = output / "raw/telemetry_samples.jsonl"
        factory_spec = os.environ.get("C1_TELEMETRY_FACTORY")
        factory = load_factory(factory_spec) if factory_spec else TelemetrySampler
        sampler = factory(raw_path, interval_ms=100)
        runner.load_model(local_files_only=True)
        tokens = runner.tokenize(request.prompt)
        try:
            sampler.start()
        except TelemetryUnavailable as exc:
            raise Unavailable(str(exc)) from exc
        try:
            generate(runner, tokens, request.generation_config)
        finally:
            try:
                telemetry = sampler.stop()
            except TelemetryUnavailable as exc:
                raise Unavailable(str(exc)) from exc
        descriptor = {
            "path": raw_path.relative_to(output).as_posix(),
            "bytes": raw_path.stat().st_size,
            "sha256": sha256(raw_path.read_bytes()).hexdigest(),
        }
        telemetry["raw_artifact"] = descriptor

        class SummaryBackend:
            def sample(self) -> Mapping[str, Any]:
                return telemetry

        result = collect_p5(
            SummaryBackend(), request,
            sampling_interval_ms=telemetry["sampling_interval_ms"],
        )
        result.artifacts.append(descriptor)
    else:
        raise ValueError(f"unsupported logical pass: {pass_id}")
    return result.records, result.artifacts


def artifact_payload(pass_id: str, records: list[dict[str, Any]]) -> tuple[str, Any]:
    name = C1_PASS_ARTIFACT[pass_id]
    return name, records if pass_id == "P2" else (records[0] if len(records) == 1 else records)


def write_failure_evidence(
    output: Path,
    *,
    pass_id: str,
    generation_row: Mapping[str, Any],
    runtime: Mapping[str, Any],
    quality: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> list[str]:
    """Persist successful generation evidence before a quality-gate failure."""
    paths = [
        "failure_generation_results.jsonl",
        "failure_runtime_metadata.json",
        "failure_quality_results.jsonl",
    ]
    write_jsonl(output / paths[0], [generation_row])
    write_json(output / paths[1], runtime)
    write_jsonl(output / paths[2], [quality])
    artifact_name, payload = artifact_payload(pass_id, records)
    partial_name = f"failure_partial_{artifact_name}"
    if pass_id == "P2":
        write_jsonl(output / partial_name, payload)
    else:
        write_json(output / partial_name, payload)
    paths.append(partial_name)
    return paths


def validate_diagnostic_scores(
    diagnostics: Mapping[str, Any], output_token_ids: Any
) -> None:
    steps = diagnostics.get("steps")
    if (
        diagnostics.get("schema_version")
        != "token-drift-score-diagnostics-v1"
        or diagnostics.get("capture_phase") != "post_generate"
        or not isinstance(output_token_ids, list)
        or not isinstance(steps, list)
        or diagnostics.get("step_count") != len(steps)
        or len(steps) != len(output_token_ids)
    ):
        raise RuntimeError("authorized token drift score sequence is invalid")
    for index, (step, generated_id) in enumerate(zip(steps, output_token_ids)):
        if not isinstance(step, Mapping):
            raise RuntimeError(
                f"authorized token drift score step {index} is invalid"
            )
        ids = step.get("top2_token_ids")
        logits = step.get("top2_logits")
        shape = step.get("score_shape")
        digest = step.get("full_score_tensor_sha256")
        margin = step.get("margin")
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
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in ids
            )
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
            or not isinstance(margin, (int, float))
            or isinstance(margin, bool)
            or not math.isfinite(float(margin))
            or not math.isclose(
                float(margin),
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
            or canonical_dtype not in dtype_bytes
            or not isinstance(step.get("score_tensor_bytes"), int)
            or step["score_tensor_bytes"] <= 0
            or step["score_tensor_bytes"]
            != math.prod(shape) * dtype_bytes[canonical_dtype]
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(value not in "0123456789abcdef" for value in digest)
        ):
            raise RuntimeError(
                f"authorized token drift score step {index} is invalid"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    stdout_buffer, stderr_buffer = io.StringIO(), io.StringIO()
    adapter = None
    failure_evidence_paths: list[str] = []
    try:
        suite, selection, sample = load_inputs()
        contract_context = quality_contract_context(suite)
        pass_id = require_env("PROJECTCTL_LOGICAL_PASS")
        work_unit_id = require_env("PROJECTCTL_WORK_UNIT_ID")
        config = dict(suite["generation_config"])
        execution = execution_document(suite, selection, config)
        alignment = build_execution_alignment_key(execution)
        request = CollectorRequest(
            execution=execution,
            prompt=sample["prompt"],
            generation_config=config,
            request_id=work_unit_id,
            sample=sample,
        )
        model_snapshot = os.environ.get("C1_MODEL_SNAPSHOT")
        adapter = construct_adapter(model_snapshot)
        runner = CaptureRunner(adapter)
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            preflight(adapter, model_snapshot)
            records, dynamic_artifacts = collect_pass(
                pass_id, runner, request, output
            )
            if runner.last_generation is None:
                raise RuntimeError("collector did not execute generation")
            generation = as_mapping(runner.last_generation)
            runtime = as_mapping(adapter.collect_runtime_metadata())
            runtime_artifact = {
                key: value for key, value in runtime.items()
                if key != "diagnostic_metadata"
            }
            quality_native = as_mapping(
                adapter.collect_quality_result(runner.last_generation, sample)
            )
        if generation.get("return_code") != 0:
            raise RuntimeError(generation.get("exception") or "generation failed")
        if contract_context is None:
            validate_generation_result(generation)
        generation_row = {
            "schema_version": "c1-benchmark-v2",
            **execution,
            "execution_alignment_key": alignment,
            "output_hash": generation.get("output_hash"),
            "input_token_ids": generation.get("input_token_ids"),
            "input_token_count": generation.get("input_token_count"),
            "output_token_count": generation.get("output_token_count"),
            "output_token_ids": generation.get("output_token_ids"),
            "text": generation.get("text"),
            "stop_reason": generation.get("stop_reason"),
            "tokenization_metadata": {
                key: value
                for key, value in (
                    generation.get("tokenization_metadata") or {}
                ).items()
                if key in TOKENIZATION_SCHEMA_FIELDS
            },
        }
        if contract_context is None:
            quality = quality_document(
                quality_native, generation, records, alignment, pass_id
            )
        else:
            quality_contract, contract_binding = contract_context
            quality = build_quality_artifact(
                contract=quality_contract,
                contract_binding=contract_binding,
                sample=sample,
                quality=quality_native,
                generation=generation,
                records=records,
                alignment=alignment,
                pass_id=pass_id,
            )
            validate_quality_artifact(
                quality,
                contract=quality_contract,
                contract_binding=contract_binding,
                sample=sample,
                generation=generation_row,
            )
        diagnostic_authorized = (
            os.environ.get("C1_DIAGNOSTIC_MODE") == DIAGNOSTIC_MODE
            and suite.get("evidence_class") == DIAGNOSTIC_EVIDENCE_CLASS
        )
        quality_details = quality_native.get("details") or {}
        diagnostic_reason_set = {
            "benchmark output is not valid",
            "T0 parsed integer sequence does not satisfy "
            "the frozen semantic reference",
        }
        diagnostic_observation = (
            contract_context is None
            and diagnostic_authorized
            and sample.get("task_id") == "T0"
            and quality_native.get("evaluator") == "t0_integer_semantics_v1"
            and quality_native.get("correctness") is False
            and quality_details.get("contract_error") is None
            and quality_details.get("reference_matches_semantics") is True
            and quality.get("output_nonempty") is True
            and quality.get("finite_values") is True
            and quality.get("token_count_consistent") is True
            and set(quality["reasons"]).issubset(diagnostic_reason_set)
            and (
                "T0 parsed integer sequence does not satisfy "
                "the frozen semantic reference"
            ) in quality["reasons"]
        )
        if diagnostic_observation:
            quality = {**quality, "status": "unknown"}
        blocking_status = (
            quality["blocking_status"]
            if contract_context is not None
            else quality["status"]
        )
        if blocking_status != "pass":
            if not diagnostic_observation:
                failure_evidence_paths = write_failure_evidence(
                    output,
                    pass_id=pass_id,
                    generation_row=generation_row,
                    runtime=runtime_artifact,
                    quality=quality,
                    records=records,
                )
                reasons = quality.get(
                    "blocking_reasons", quality.get("reasons", [])
                )
                raise RuntimeError("; ".join(reasons))
        diagnostic_payload = None
        if diagnostic_authorized:
            score_diagnostics = generation.get("score_diagnostics")
            tokenization_diagnostics = generation.get("tokenization_metadata")
            runtime_diagnostics = runtime.get("diagnostic_metadata")
            runtime_flags = (
                runtime_diagnostics.get("deterministic_flags")
                if isinstance(runtime_diagnostics, Mapping)
                else None
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
                sample.get("task_id") != "T0"
                or config.get("return_dict_in_generate") is not True
                or config.get("output_scores") is not True
                or not isinstance(score_diagnostics, Mapping)
                or not isinstance(tokenization_diagnostics, Mapping)
                or not isinstance(runtime_diagnostics, Mapping)
                or runtime_diagnostics.get("schema_version")
                != "token-drift-runtime-diagnostics-v2"
                or not isinstance(runtime_flags, Mapping)
                or set(runtime_flags)
                != required_boolean_flags | required_environment_flags
                or any(
                    not isinstance(runtime_flags[name], bool)
                    for name in required_boolean_flags
                )
                or any(
                    runtime_flags[name] is not None
                    and not isinstance(runtime_flags[name], str)
                    for name in required_environment_flags
                )
                or dict(runtime_flags) != expected_deterministic_flags
                or any(
                    key not in tokenization_diagnostics
                    for key in (
                        "rendered_chat_sha256", "input_ids",
                        "attention_mask_sha256",
                    )
                )
            ):
                raise RuntimeError(
                    "authorized token drift diagnostic metadata is incomplete"
                )
            validate_diagnostic_scores(
                score_diagnostics, generation.get("output_token_ids")
            )
            diagnostic_payload = {
                "schema_version": "c1-token-drift-diagnostic-v1",
                "mode": DIAGNOSTIC_MODE,
                "evidence_class": DIAGNOSTIC_EVIDENCE_CLASS,
                "observation": (
                    "t0_semantic_mismatch"
                    if diagnostic_observation else "t0_semantic_match"
                ),
                "semantic_equality_used_for_alignment": False,
                "execution_alignment_key": alignment,
                "score_diagnostics": score_diagnostics,
                "tokenization_diagnostics": tokenization_diagnostics,
                "runtime_diagnostics": runtime_diagnostics,
                "quality": quality,
            }
        model = suite.get("model") or {}
        session = {
            "schema_version": "c1-session-v1",
            "session_id": require_env("PROJECTCTL_SESSION_ID"),
            "suite": {
                "suite_id": suite.get("suite_id"),
                "suite_version": suite.get("suite_revision"),
                "samples": [{
                    "benchmark_id": selection.get("benchmark_id"),
                    "sample_id": selection.get("source_sample_id"),
                }],
                "repetitions": suite.get("repetitions"),
            },
            "model": {
                "model_id": model.get("id", require_env("PROJECTCTL_MODEL_ID")),
                "model_revision": model.get("revision"),
                "tokenizer_revision": model.get("tokenizer_revision"),
            },
            "hardware_session_id": execution["hardware_session_id"],
            "pass_plan": suite.get("logical_passes"),
            "raw_immutable": True,
            "work_unit_id": work_unit_id,
        }
        if contract_context is not None:
            session["quality_contract"] = dict(contract_binding)
        validate_schema("c1_session.schema.json", session)
        validate_schema("c1_benchmark.schema.json", generation_row)
        validate_schema(
            "c1_quality.schema.json",
            quality if contract_context is not None else {
                key: value for key, value in quality.items() if key != "pass_id"
            },
        )
        if pass_id == "P2":
            for row in records:
                validate_schema("c1_routing.schema.json", row)

        write_json(output / "session_manifest.json", session)
        write_jsonl(output / "generation_results.jsonl", [generation_row])
        write_json(output / "runtime_metadata.json", runtime_artifact)
        write_jsonl(output / "quality_results.jsonl", [quality])
        diagnostic_paths: set[str] = set()
        if diagnostic_payload is not None:
            write_json(output / "diagnostic_scores.json", diagnostic_payload)
            diagnostic_paths.add("diagnostic_scores.json")
        name, payload = artifact_payload(pass_id, records)
        if pass_id == "P2":
            write_jsonl(output / name, payload)
        else:
            write_json(output / name, payload)
        (output / "stdout.log").write_text(
            stdout_buffer.getvalue() or "(no collector stdout)\n", encoding="utf-8"
        )
        (output / "stderr.log").write_text(
            stderr_buffer.getvalue() or "(no collector stderr)\n", encoding="utf-8"
        )
        dynamic_paths = {
            artifact["path"] for artifact in dynamic_artifacts
            if isinstance(artifact.get("path"), str)
        } | diagnostic_paths
        raw_without_manifest = sorted(
            COMMON_C1_ARTIFACTS - {"pass_manifest.json"} | {name} | dynamic_paths
        )
        pass_manifest = {
            "schema_version": "c1-pass-v1",
            "work_unit_id": work_unit_id,
            "pass_id": pass_id,
            "status": "COMPLETE",
            "execution_alignment_key": alignment,
            "raw_artifacts": [
                {
                    "path": relative,
                    "sha256": sha256((output / relative).read_bytes()).hexdigest(),
                    "bytes": (output / relative).stat().st_size,
                }
                for relative in raw_without_manifest
            ],
        }
        validate_schema("c1_pass.schema.json", pass_manifest)
        write_json(output / "pass_manifest.json", pass_manifest)
        raw_files = sorted(COMMON_C1_ARTIFACTS | {name} | dynamic_paths)
        write_json(output / "COLLECTOR_RESULT.json", {
            "contract_version": "c1-worker-v1",
            "status": "success",
            "schema_valid": True,
            "raw_files": raw_files,
            "work_unit_id": work_unit_id,
        })
        return 0
    except Exception as exc:
        unavailable = isinstance(exc, Unavailable)
        reason = f"{type(exc).__name__}: {exc}"
        write_json(output / "unavailable_reason.json", {
            "status": "unavailable" if unavailable else "failed",
            "reason": reason,
        })
        (output / "stdout.log").write_text(
            stdout_buffer.getvalue() or "(no collector stdout)\n", encoding="utf-8"
        )
        (output / "stderr.log").write_text(
            (stderr_buffer.getvalue() + reason + "\n"), encoding="utf-8"
        )
        write_json(output / "COLLECTOR_RESULT.json", {
            "contract_version": "c1-worker-v1",
            "status": "unavailable" if unavailable else "failed",
            "schema_valid": False,
            "raw_files": sorted({
                "unavailable_reason.json", "stdout.log", "stderr.log",
                *failure_evidence_paths,
            }),
            "work_unit_id": os.environ.get("PROJECTCTL_WORK_UNIT_ID"),
            "unavailable_reason": reason if unavailable else None,
        })
        return 20 if unavailable else 1
    finally:
        if adapter is not None:
            try:
                adapter.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
