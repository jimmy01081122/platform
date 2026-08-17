from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from adapters.models.contract import (
    GenerationResult, ModelIdentity, PreflightReport, QualityResult,
    RoutingDispatch, RuntimeMetadata, TokenizedBatch,
)
from scripts.c1_evaluator import evaluate_frozen_sample


class FakeC1Adapter:
    identity = ModelIdentity("fixture-granite", "fixture-rev", "fixture-rev", "fake-v1")

    def __init__(self, **_kwargs):
        self.capture = False

    def preflight(self, **_kwargs):
        return PreflightReport(True, True, {"fake_runtime": True}, {})

    def load_model(self, **_kwargs):
        return None

    def tokenize(self, prompt):
        return TokenizedBatch(
            tensors={"input_ids": [1, 2]},
            input_token_count=2,
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            input_token_ids=[1, 2],
            tokenization_metadata={
                "mode": "pinned_chat_template",
                "chat_template_sha256": "a" * 64,
                "tokenizer_config_sha256": "b" * 64,
                "message_roles": ["user"],
                "add_generation_prompt": True,
                "rendered_chat_sha256": "c" * 64,
                "input_ids": [1, 2],
                "input_ids_tensor_sha256": "d" * 64,
                "attention_mask_sha256": "e" * 64,
                "attention_mask_dtype": "int64",
                "attention_mask_shape": [1, 2],
            },
        )

    def generate(self, batch, request):
        ids = [9, 10]
        if os.environ.get("C1_FAKE_P2_DRIFT") == "1" and os.environ.get(
            "PROJECTCTL_LOGICAL_PASS"
        ) == "P2":
            ids = [11]
        routing = []
        if self.capture:
            for layer in range(24):
                for call_index, sequence_length in enumerate(
                    [batch.input_token_count] + [1] * (len(ids) - 1)
                ):
                    routing.append(RoutingDispatch(
                        layer=f"model.layers.{layer}.block_sparse_moe",
                        call_index=call_index,
                        input_sequence_length=sequence_length,
                        token_indices=[
                            token
                            for _expert in range(8)
                            for token in range(sequence_length)
                        ],
                        expert_indices=[
                            expert
                            for expert in range(8)
                            for _token in range(sequence_length)
                        ],
                        gates=[0.125] * (sequence_length * 8),
                        gate_dtype="fp32",
                        expert_size=[sequence_length] * 8 + [0] * 24,
                        router_logits=[
                            [0.0] * 32 for _token in range(sequence_length)
                        ],
                    ))
        digest = hashlib.sha256(
            json.dumps(ids, separators=(",", ":")).encode()
        ).hexdigest()
        return GenerationResult(
            text="42", input_token_ids=list(batch.input_token_ids),
            output_token_ids=ids, input_token_count=batch.input_token_count,
            output_token_count=len(ids), stop_reason="eos_token",
            output_hash=digest, return_code=0, generation_seconds=0.001,
            tokenization_metadata=dict(batch.tokenization_metadata),
            routing=routing,
        )

    def enable_routing_capture(self):
        self.capture = True

    def disable_routing_capture(self):
        self.capture = False
        return []

    def collect_quality_result(self, result, sample):
        evaluated = evaluate_frozen_sample(result.text, sample)
        return QualityResult(
            evaluated["evaluator"],
            evaluated["validity"],
            evaluated["correctness"],
            evaluated["details"],
        )

    def collect_runtime_metadata(self):
        return RuntimeMetadata(
            self.identity, "fp32", "cpu", None, None, 0.0, None, 1024,
            self.capture,
        )

    def cleanup(self):
        self.capture = False


class FakeQualityFailureAdapter(FakeC1Adapter):
    def generate(self, batch, request):
        result = super().generate(batch, request)
        result.text = "99"
        return result

    def collect_quality_result(self, _result, _sample):
        return QualityResult(
            "t0_integer_semantics_v1",
            True,
            False,
            {
                "contract_error": None,
                "reference_matches_semantics": True,
            },
        )


class FakeT1QualityFailureAdapter(FakeC1Adapter):
    def generate(self, batch, request):
        result = super().generate(batch, request)
        result.text = "41"
        return result

    def collect_quality_result(self, _result, _sample):
        return QualityResult(
            "gsm8k_last_number",
            True,
            False,
            {"parsed_answer": 41, "expected_answer": 42},
        )


class FakeMaxNewTokensAdapter(FakeC1Adapter):
    def generate(self, batch, request):
        result = super().generate(batch, request)
        result.stop_reason = "max_new_tokens"
        return result


class FakeTokenDriftDiagnosticAdapter(FakeQualityFailureAdapter):
    def generate(self, batch, request):
        result = super().generate(batch, request)
        result.score_diagnostics = {
            "schema_version": "token-drift-score-diagnostics-v1",
            "capture_phase": "post_generate",
            "step_count": 2,
            "steps": [
                {
                    "generation_step": step,
                    "generated_token_id": token,
                    "top2_token_ids": [token, token + 1],
                    "top2_logits": [2.0, 1.0],
                    "margin": 1.0,
                    "score_dtype": "float32",
                    "score_shape": [1, 32],
                    "score_tensor_bytes": 128,
                    "full_score_tensor_sha256": str(step + 1) * 64,
                }
                for step, token in enumerate(result.output_token_ids)
            ],
        }
        return result

    def collect_runtime_metadata(self):
        value = super().collect_runtime_metadata()
        return RuntimeMetadata(
            value.model,
            value.precision,
            value.device,
            value.transformers_version,
            value.torch_version,
            value.load_seconds,
            value.peak_vram_bytes,
            value.peak_host_rss_bytes,
            value.routing_capture_enabled,
            {
                "schema_version": "token-drift-runtime-diagnostics-v2",
                "deterministic_flags": {
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
                },
            },
        )


class FakeInvalidTokenDriftDiagnosticAdapter(FakeTokenDriftDiagnosticAdapter):
    def collect_quality_result(self, _result, _sample):
        return QualityResult(
            "t0_integer_semantics_v1",
            False,
            False,
            {
                "contract_error": None,
                "reference_matches_semantics": True,
            },
        )


class FakeProfilerBackend:
    def available(self):
        return True, None

    def profile(self, operation):
        return operation(), [("torch_trace.json", b'{"traceEvents":[{"name":"fake"}]}\n')]


class FakeTelemetrySampler:
    def __init__(self, output_path: Path, *, interval_ms: int):
        self.output_path = output_path
        self.interval_ms = interval_ms

    def start(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            '{"sample_index":0,"gpu_utilization_percent":42.0}\n',
            encoding="utf-8",
        )

    def stop(self):
        unavailable = {
            "power_watts": "fixture nvidia-smi field unsupported",
            "throttle_reason": "fixture nvidia-smi field unsupported",
        }
        return {
            "gpu_utilization_percent": 42.0,
            "gpu_clock_mhz": 1200.0,
            "memory_clock_mhz": 5000.0,
            "temperature_celsius": 55.0,
            "vram_used_bytes": 1024,
            "cpu_utilization_percent": 12.5,
            "system_memory_used_bytes": 2048,
            "unavailable_reasons": unavailable,
            "sample_count": 1,
            "sampling_interval_ms": self.interval_ms,
            "telemetry_start_monotonic_ns": 10,
            "telemetry_end_monotonic_ns": 20,
            "monotonic_ns": 20,
            "failures": [],
        }
