from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from adapters.models.contract import (  # noqa: E402
    GenerationRequest,
    GenerationResult,
    TokenizedBatch,
)
from adapters.models.granite_moe.adapter import (  # noqa: E402
    ADAPTER_VERSION,
    DETERMINISTIC_ENVIRONMENT,
    EXPECTED_CHAT_TEMPLATE_SHA256,
    EXPECTED_SNAPSHOT_FILES,
    MODEL_REVISION,
    TOKENIZER_REVISION,
    TRANSFORMERS_VERSION,
    GraniteMoeAdapter,
    _configure_deterministic_environment,
    _configure_deterministic_torch,
    _cpu_tensor_snapshot,
    _parameter_content_sha256,
)
from adapters.models.granite_moe.routing import (  # noqa: E402
    GraniteRoutingCapture,
    reconstruct_actual_dispatch,
)
from adapters.models.granite_moe.snapshot import (  # noqa: E402
    SnapshotValidationError,
    validate_exact_snapshot,
)


class FakeHandle:
    def __init__(self, hooks: list, hook) -> None:
        self.hooks = hooks
        self.hook = hook
        self.removed = False

    def remove(self) -> None:
        if not self.removed:
            self.hooks.remove(self.hook)
            self.removed = True


class FakeModule:
    def __init__(self) -> None:
        self.hooks = []

    def register_forward_hook(self, hook):
        self.hooks.append(hook)
        return FakeHandle(self.hooks, hook)

    def emit(self, output) -> None:
        for hook in list(self.hooks):
            hook(self, (), output)


class FakeMoe(FakeModule):
    def __init__(self) -> None:
        super().__init__()
        self.router = FakeModule()


class FakeModel:
    def __init__(self) -> None:
        self.moes = [FakeMoe() for _ in range(24)]
        self.moe = self.moes[0]
        self.generate_error = RuntimeError("fixture generation failure")

    def named_modules(self):
        modules = []
        for layer, moe in enumerate(self.moes):
            name = f"model.layers.{layer}.block_sparse_moe"
            modules.extend([(name, moe), (f"{name}.router", moe.router)])
        return modules

    def generate(self, **_kwargs):
        raise self.generate_error


class FakeInferenceMode:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


class FakeCuda:
    @staticmethod
    def is_available():
        return False


class GateList(list):
    def __init__(self, values, dtype: str) -> None:
        super().__init__(values)
        self.dtype = dtype


def valid_router_tuple(
    tokens: int = 1,
    *,
    gate_dtype: str = "float32",
    token_weights: list[float] | None = None,
):
    assignment = [
        token * 8 + expert
        for expert in range(8)
        for token in range(tokens)
    ]
    batch_index = [index // 8 for index in assignment]
    weights = token_weights or [0.125] * 8
    gates = GateList(
        [
            weights[expert]
            for expert in range(8)
            for _token in range(tokens)
        ],
        gate_dtype,
    )
    expert_size = [tokens] * 8 + [0] * 24
    logits = [[0.0] * 32 for _ in range(tokens)]
    return assignment, batch_index, gates, expert_size, logits


class GraniteAdapterTests(unittest.TestCase):
    @staticmethod
    def generation_result(text: str) -> GenerationResult:
        return GenerationResult(
            text=text,
            input_token_ids=[11, 22],
            output_token_ids=[101, 202],
            input_token_count=2,
            output_token_count=2,
            stop_reason="max_new_tokens",
            output_hash="a" * 64,
            return_code=0,
            generation_seconds=0.01,
        )

    def test_snapshot_pin_contains_all_exact_files(self) -> None:
        self.assertEqual("granite-c1-adapter-v9", ADAPTER_VERSION)
        self.assertEqual(
            "08962c2f15d56767854b46dfc4070b37f4c443551833bba65b417191735f3187",
            EXPECTED_CHAT_TEMPLATE_SHA256,
        )
        self.assertEqual({
            "model.safetensors", "config.json", "generation_config.json",
            "tokenizer.json", "tokenizer_config.json", "vocab.json",
            "merges.txt", "special_tokens_map.json", "added_tokens.json",
        }, set(EXPECTED_SNAPSHOT_FILES))
        self.assertEqual(
            (
                2_669_283_096,
                "ac02591061f1344027a7e7b11dbb4143f75f166c47dc09b742f5de3ab1dde1d1",
            ),
            EXPECTED_SNAPSHOT_FILES["model.safetensors"],
        )

    def test_deterministic_runtime_configuration_is_fail_closed(self) -> None:
        matmul = types.SimpleNamespace(
            allow_tf32=True,
            allow_bf16_reduced_precision_reduction=True,
            allow_fp16_reduced_precision_reduction=True,
        )
        cudnn = types.SimpleNamespace(
            allow_tf32=True,
            benchmark=True,
            deterministic=False,
        )
        calls: list[bool] = []
        fake_torch = types.SimpleNamespace(
            use_deterministic_algorithms=calls.append,
            backends=types.SimpleNamespace(
                cuda=types.SimpleNamespace(matmul=matmul),
                cudnn=cudnn,
            ),
        )
        with patch.dict(os.environ, {}, clear=True):
            _configure_deterministic_environment()
            self.assertEqual(
                DETERMINISTIC_ENVIRONMENT,
                {
                    name: os.environ[name]
                    for name in DETERMINISTIC_ENVIRONMENT
                },
            )
            _configure_deterministic_torch(fake_torch)
        self.assertEqual([True], calls)
        self.assertFalse(matmul.allow_tf32)
        self.assertFalse(matmul.allow_bf16_reduced_precision_reduction)
        self.assertFalse(matmul.allow_fp16_reduced_precision_reduction)
        self.assertFalse(cudnn.allow_tf32)
        self.assertFalse(cudnn.benchmark)
        self.assertTrue(cudnn.deterministic)

    def test_deterministic_environment_rejects_conflicting_value(self) -> None:
        with patch.dict(
            os.environ, {"CUBLAS_WORKSPACE_CONFIG": ":16:8"}, clear=True
        ):
            with self.assertRaisesRegex(
                RuntimeError, "CUBLAS_WORKSPACE_CONFIG"
            ):
                _configure_deterministic_environment()

    def test_runtime_metadata_records_all_deterministic_controls(self) -> None:
        matmul = types.SimpleNamespace(
            allow_tf32=False,
            allow_bf16_reduced_precision_reduction=False,
            allow_fp16_reduced_precision_reduction=False,
        )
        fake_torch = types.SimpleNamespace(
            are_deterministic_algorithms_enabled=lambda: True,
            cuda=types.SimpleNamespace(is_available=lambda: False),
            backends=types.SimpleNamespace(
                cuda=types.SimpleNamespace(matmul=matmul),
                cudnn=types.SimpleNamespace(
                    enabled=True,
                    deterministic=True,
                    benchmark=False,
                    allow_tf32=False,
                ),
            ),
        )
        with (
            patch(
                "adapters.models.granite_moe.adapter._version",
                return_value="fixture-version",
            ),
            patch.dict(
                sys.modules, {"torch": fake_torch}
            ),
            patch.dict(
                os.environ,
                {
                    "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
                    "CUDA_LAUNCH_BLOCKING": "1",
                },
                clear=True,
            ),
        ):
            metadata = GraniteMoeAdapter().collect_runtime_metadata()
        self.assertEqual(
            "token-drift-runtime-diagnostics-v2",
            metadata.diagnostic_metadata["schema_version"],
        )
        self.assertEqual(
            {
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
            metadata.diagnostic_metadata["deterministic_flags"],
        )

    def test_runtime_metadata_rereads_actual_parameter_dtype_device_and_config(self) -> None:
        class FakeDevice:
            type = "cuda"

            def __str__(self):
                return "cuda:0"

        class FakeParameter:
            dtype = "torch.bfloat16"
            device = FakeDevice()
            shape = (512,)
            requires_grad = False
            _version = 0

            @staticmethod
            def numel():
                return 512

            @staticmethod
            def stride():
                return (1,)

            @staticmethod
            def element_size():
                return 2

            def untyped_storage(self):
                return types.SimpleNamespace(
                    data_ptr=lambda: id(self), nbytes=lambda: 1024
                )

        initial_parameters = [FakeParameter(), FakeParameter()]
        model_type = type(
            "GraniteMoeForCausalLM",
            (),
            {
                "__module__": "transformers.models.granitemoe.modeling_granitemoe",
                "named_parameters": lambda self: iter(self.parameter_items),
            },
        )
        model = model_type()
        model.parameter_items = [
            (f"model.parameter_{index}", parameter)
            for index, parameter in enumerate(initial_parameters)
        ]
        model.config = types.SimpleNamespace(
            model_type="granitemoe", architectures=["GraniteMoeForCausalLM"]
        )
        adapter = GraniteMoeAdapter()
        adapter.model = model
        with (
            patch("adapters.models.granite_moe.adapter._version", return_value=None),
            patch(
                "adapters.models.granite_moe.adapter._parameter_content_sha256",
                return_value="a" * 64,
            ),
        ):
            first = adapter.collect_runtime_metadata().parameter_evidence
            model.parameter_items = [("model.parameter_0", initial_parameters[0])]
            second = adapter.collect_runtime_metadata().parameter_evidence
        self.assertEqual(["bfloat16"], first["dtypes"])
        self.assertEqual(["cuda"], first["device_kinds"])
        self.assertEqual(["cuda:0"], first["device_locations"])
        self.assertEqual(1024, first["total_numel"])
        self.assertEqual(512, second["total_numel"])
        self.assertEqual(
            hashlib.sha256(json.dumps(
                first["parameters"], sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")).hexdigest(),
            first["parameter_manifest_sha256"],
        )

    def test_parameter_content_hash_detects_in_place_tensor_mutation(self) -> None:
        import torch

        parameter = torch.nn.Parameter(
            torch.tensor([1.0, 2.0], dtype=torch.bfloat16),
            requires_grad=False,
        )
        before = _parameter_content_sha256(parameter, chunk_bytes=2)
        parameter[0] = 3.0
        after = _parameter_content_sha256(parameter, chunk_bytes=2)
        self.assertNotEqual(before, after)

    def test_t0_integer_semantics_cover_all_frozen_operations(self) -> None:
        cases = (
            ("identity", [7], [7], " [ 7 ]. "),
            ("identity", [7], [7], '"7"'),
            ("identity", [7], [7], "'7'"),
            ("ordered_copy", [3, 5], [3, 5], "(3, 5)"),
            ("reverse", [2, 4, 6], [6, 4, 2], "6; 4; 2!"),
            ("integer_sum", [8, 13], [21], "{21}"),
        )
        adapter = GraniteMoeAdapter()
        for semantic, inputs, reference, text in cases:
            sample = {
                "task_id": "T0",
                "reference": reference,
                "metadata": {
                    "token_contract":
                        "artificial_fixture_ids_not_model_tokenizer_ids",
                    "expected_semantics": semantic,
                    "input_token_ids": inputs,
                },
            }
            with self.subTest(semantic=semantic):
                quality = adapter.collect_quality_result(
                    self.generation_result(text), sample
                )
                self.assertEqual("t0_integer_semantics_v1", quality.evaluator)
                self.assertTrue(quality.validity)
                self.assertTrue(quality.correctness)

    def test_t0_unbalanced_quotes_fail_closed(self) -> None:
        sample = {
            "task_id": "T0",
            "reference": [7],
            "metadata": {
                "token_contract":
                    "artificial_fixture_ids_not_model_tokenizer_ids",
                "expected_semantics": "identity",
                "input_token_ids": [7],
            },
        }
        for text in ('"7', "7'", '"7\''):
            with self.subTest(text=text):
                quality = GraniteMoeAdapter().collect_quality_result(
                    self.generation_result(text), sample
                )
                self.assertFalse(quality.validity)
                self.assertFalse(quality.correctness)

    def test_t0_extra_integer_is_parseable_but_semantically_rejected(self) -> None:
        sample = {
            "task_id": "T0",
            "reference": [3, 5],
            "metadata": {
                "token_contract":
                    "artificial_fixture_ids_not_model_tokenizer_ids",
                "expected_semantics": "ordered_copy",
                "input_token_ids": [3, 5],
            },
        }
        quality = GraniteMoeAdapter().collect_quality_result(
            self.generation_result("3, 5, 99"), sample
        )
        self.assertTrue(quality.validity)
        self.assertFalse(quality.correctness)
        self.assertEqual([3, 5, 99], quality.details["parsed_integers"])

    def test_t0_unknown_contract_or_semantic_fails_closed(self) -> None:
        for metadata in (
            {
                "token_contract": "model_tokenizer_ids",
                "expected_semantics": "identity",
                "input_token_ids": [7],
            },
            {
                "token_contract":
                    "artificial_fixture_ids_not_model_tokenizer_ids",
                "expected_semantics": "unknown",
                "input_token_ids": [7],
            },
        ):
            with self.subTest(metadata=metadata):
                quality = GraniteMoeAdapter().collect_quality_result(
                    self.generation_result("7"),
                    {"task_id": "T0", "reference": [7], "metadata": metadata},
                )
                self.assertFalse(quality.validity)
                self.assertFalse(quality.correctness)
                self.assertIsNotNone(quality.details["contract_error"])

    def test_reconstructs_actual_router_tuple_in_expert_execution_order(self) -> None:
        record = reconstruct_actual_dispatch(
            valid_router_tuple(tokens=2),
            layer="layer.0",
            call_index=3,
        )
        self.assertEqual(2, record.input_sequence_length)
        self.assertEqual("fp32", record.gate_dtype)
        self.assertEqual(16, len(record.token_indices))
        self.assertEqual([2] * 8 + [0] * 24, record.expert_size)
        self.assertTrue(record.actual_dispatch_verified)
        self.assertEqual("measured_actual_granitemoe_dispatch_tuple", record.evidence_class)

    def test_dual_hooks_capture_actual_dispatch_and_cleanup(self) -> None:
        model = FakeModel()
        capture = GraniteRoutingCapture(model)
        capture.enable()
        self.assertTrue(all(len(moe.router.hooks) == 1 for moe in model.moes))
        self.assertTrue(all(len(moe.hooks) == 1 for moe in model.moes))

        for moe in model.moes:
            moe.router.emit(valid_router_tuple())
            moe.emit(("hidden", "router_logits"))
        records = capture.disable()

        self.assertEqual(24, len(records))
        self.assertEqual(list(range(8)), records[0].expert_indices[:8])
        self.assertTrue(all(not moe.router.hooks for moe in model.moes))
        self.assertTrue(all(not moe.hooks for moe in model.moes))

    def test_generation_exception_still_removes_hook_handles_without_torch(self) -> None:
        fake_torch = types.SimpleNamespace(
            manual_seed=lambda _seed: None,
            cuda=FakeCuda(),
            inference_mode=lambda: FakeInferenceMode(),
        )
        adapter = GraniteMoeAdapter()
        adapter.model = FakeModel()
        adapter.tokenizer = types.SimpleNamespace(eos_token_id=0)
        adapter.device = "cpu"
        adapter.enable_routing_capture()
        with patch.dict(sys.modules, {"torch": fake_torch}):
            result = adapter.generate(
                TokenizedBatch(
                    tensors={"input_ids": [[1]]},
                    input_token_count=1,
                    prompt_hash="a" * 64,
                    input_token_ids=[1],
                ),
                GenerationRequest(max_new_tokens=1),
            )
        self.assertEqual(1, result.return_code)
        self.assertIn("fixture generation failure", result.exception)
        self.assertTrue(all(not moe.router.hooks for moe in adapter.model.moes))
        self.assertTrue(all(not moe.hooks for moe in adapter.model.moes))

    def test_tokenize_requires_and_applies_pinned_chat_template(self) -> None:
        template = "fixture-chat-template"
        template_hash = hashlib.sha256(template.encode()).hexdigest()

        class FakeTensorValue:
            shape = (1, 3)
            dtype = "int64"

            def to(self, device):
                self.device = device
                return self

            def detach(self):
                return self

            def cpu(self):
                return self

            def contiguous(self):
                return self

            def numpy(self):
                return types.SimpleNamespace(tobytes=lambda: b"\x01" * 24)

            def tolist(self):
                return [[1, 2, 3]]

        class FakeTokenizer:
            chat_template = template
            eos_token_id = 0
            pad_token_id = 0

            def __init__(self):
                self.calls = []

            def apply_chat_template(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                if kwargs.get("tokenize") is False:
                    return "rendered prompt"
                return {
                    "input_ids": FakeTensorValue(),
                    "attention_mask": FakeTensorValue(),
                }

        adapter = GraniteMoeAdapter()
        adapter.device = "cuda:0"
        adapter.tokenizer = FakeTokenizer()
        with patch(
            "adapters.models.granite_moe.adapter.EXPECTED_CHAT_TEMPLATE_SHA256",
            template_hash,
        ):
            batch = adapter.tokenize("prompt")
        self.assertEqual(2, len(adapter.tokenizer.calls))
        messages, kwargs = adapter.tokenizer.calls[-1]
        self.assertEqual("system", messages[0]["role"])
        self.assertIn("Today's Date: July 19, 2026.", messages[0]["content"])
        self.assertEqual({"role": "user", "content": "prompt"}, messages[1])
        self.assertTrue(kwargs["add_generation_prompt"])
        self.assertTrue(kwargs["tokenize"])
        self.assertEqual("pt", kwargs["return_tensors"])
        self.assertTrue(kwargs["return_dict"])
        self.assertEqual(3, batch.input_token_count)
        self.assertEqual("pinned_chat_template", batch.tokenization_metadata["mode"])
        self.assertEqual(
            template_hash, batch.tokenization_metadata["chat_template_sha256"]
        )
        self.assertEqual([1, 2, 3], batch.tokenization_metadata["input_ids"])
        self.assertEqual(0, batch.tokenization_metadata["eos_token_id"])
        self.assertEqual(0, batch.tokenization_metadata["pad_token_id"])
        self.assertEqual(
            "596d752bf46f5cace1f6826b52ed7d913347a4eea0ecce8ab2f869471ca40369",
            batch.tokenization_metadata["special_tokens_map_sha256"],
        )
        self.assertEqual(
            "granite-c1-fixed-system-date-v1",
            batch.tokenization_metadata["prompt_construction_revision"],
        )
        self.assertRegex(
            batch.tokenization_metadata["rendered_chat_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertRegex(
            batch.tokenization_metadata["attention_mask_sha256"],
            r"^[0-9a-f]{64}$",
        )

        adapter.tokenizer = types.SimpleNamespace(chat_template=None)
        with self.assertRaisesRegex(RuntimeError, "chat_template is required"):
            adapter.tokenize("prompt")

    def test_score_tensors_detach_to_cpu_only_after_generate_returns(self) -> None:
        events = []

        class FakeArray:
            def tobytes(self):
                return b"\x00\x00\x80?" * 3

        class FakeScore:
            dtype = "float32"
            shape = (1, 3)

            def detach(self):
                self.assert_after_generate = "generate_returned" in events
                events.append("score_detach")
                return self

            def cpu(self):
                events.append("score_cpu")
                return self

            def contiguous(self):
                return self

            def numpy(self):
                return FakeArray()

            def tolist(self):
                return [[1.0, 3.0, 2.0]]

        class FakeContinuation:
            def detach(self):
                return self

            def cpu(self):
                return self

            def tolist(self):
                return [9]

        class FakeSequence:
            def __getitem__(self, item):
                self.item = item
                return FakeContinuation()

        score = FakeScore()
        generated = types.SimpleNamespace(
            scores=[score],
            sequences=[FakeSequence()],
        )

        class FakeGeneratingModel:
            def generate(self, **_kwargs):
                events.append("generate_returned")
                return generated

        fake_torch = types.SimpleNamespace(
            manual_seed=lambda _seed: None,
            cuda=FakeCuda(),
            inference_mode=lambda: FakeInferenceMode(),
        )
        adapter = GraniteMoeAdapter()
        adapter.model = FakeGeneratingModel()
        adapter.tokenizer = types.SimpleNamespace(
            decode=lambda ids, skip_special_tokens: "9",
            eos_token_id=0,
        )
        with patch.dict(sys.modules, {"torch": fake_torch}):
            result = adapter.generate(
                TokenizedBatch(
                    tensors={"input_ids": [[1]]},
                    input_token_count=1,
                    prompt_hash="a" * 64,
                    input_token_ids=[1],
                ),
                GenerationRequest(
                    max_new_tokens=1,
                    extra={
                        "return_dict_in_generate": True,
                        "output_scores": True,
                    },
                ),
            )
        diagnostics = result.score_diagnostics
        self.assertTrue(score.assert_after_generate)
        self.assertLess(events.index("generate_returned"), events.index("score_detach"))
        self.assertLess(events.index("score_detach"), events.index("score_cpu"))
        step = diagnostics["steps"][0]
        self.assertEqual(9, step["generated_token_id"])
        self.assertEqual([1, 2], step["top2_token_ids"])
        self.assertEqual([3.0, 2.0], step["top2_logits"])
        self.assertEqual(1.0, step["margin"])
        self.assertRegex(step["full_score_tensor_sha256"], r"^[0-9a-f]{64}$")
        alternate = FakeScore()
        alternate.dtype = "float16"
        _, alternate_evidence = _cpu_tensor_snapshot(alternate)
        self.assertNotEqual(
            step["full_score_tensor_sha256"],
            alternate_evidence["sha256"],
        )

    def test_preflight_is_metadata_only_and_exactly_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary)
            payloads = {
                "model.safetensors": b"weight",
                "config.json": b"config",
                "generation_config.json": b"generation",
                "tokenizer.json": b"tokenizer",
                "tokenizer_config.json": json.dumps({
                    "chat_template": "fixture-chat-template",
                }).encode(),
                "vocab.json": b"vocab",
                "merges.txt": b"merges",
                "special_tokens_map.json": b"special",
                "added_tokens.json": b"added",
            }
            expected = {}
            for name, payload in payloads.items():
                (snapshot / name).write_bytes(payload)
                expected[name] = (len(payload), hashlib.sha256(payload).hexdigest())
            adapter = GraniteMoeAdapter(snapshot)
            chat_hash = hashlib.sha256(b"fixture-chat-template").hexdigest()
            with patch(
                "adapters.models.granite_moe.adapter._version",
                side_effect=lambda package: (
                    TRANSFORMERS_VERSION if package == "transformers" else "2.7.1"
                ),
            ), patch(
                "adapters.models.granite_moe.adapter.EXPECTED_SNAPSHOT_FILES",
                expected,
            ), patch(
                "adapters.models.granite_moe.adapter._cuda_available",
                return_value=True,
            ), patch(
                "adapters.models.granite_moe.adapter.EXPECTED_CHAT_TEMPLATE_SHA256",
                chat_hash,
            ):
                report = adapter.preflight()
        self.assertTrue(report.eligible)
        self.assertTrue(report.metadata_only)
        self.assertRegex(MODEL_REVISION, r"^[0-9a-f]{40}$")
        self.assertEqual(MODEL_REVISION, TOKENIZER_REVISION)
        self.assertEqual("4.47.0", TRANSFORMERS_VERSION)
        self.assertTrue(all(
            not fact["symlink_followed"]
            for fact in report.facts["snapshot_files"].values()
        ))
        self.assertTrue(report.facts["snapshot_inventory"]["exact_regular_file_set"])

    def test_preflight_rejects_snapshot_hash_and_size_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary)
            (snapshot / "config.json").write_bytes(b"bad")
            expected = {"config.json": (4, "0" * 64)}
            with patch(
                "adapters.models.granite_moe.adapter._version",
                side_effect=lambda package: (
                    TRANSFORMERS_VERSION if package == "transformers" else "2.7.1"
                ),
            ), patch(
                "adapters.models.granite_moe.adapter.EXPECTED_SNAPSHOT_FILES",
                expected,
            ), patch(
                "adapters.models.granite_moe.adapter._cuda_available",
                return_value=True,
            ):
                report = GraniteMoeAdapter(snapshot).preflight()
        self.assertFalse(report.eligible)
        self.assertIn("model snapshot identity mismatch: config.json", " ".join(report.blockers))

    def test_exact_snapshot_rejects_extra_missing_and_unsafe_entries(self) -> None:
        payload = b"payload"
        expected = {"payload.bin": (len(payload), hashlib.sha256(payload).hexdigest())}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payload.bin").write_bytes(payload)
            inventory = validate_exact_snapshot(root, expected_files=expected)
            self.assertTrue(inventory["exact_regular_file_set"])
            self.assertEqual(1, inventory["observed_file_count"])

            (root / "rogue.py").write_text("pass\n", encoding="utf-8")
            with self.assertRaisesRegex(SnapshotValidationError, "extra=.*rogue.py"):
                validate_exact_snapshot(root, expected_files=expected)
            (root / "rogue.py").unlink()

            (root / "payload.bin").unlink()
            with self.assertRaisesRegex(SnapshotValidationError, "missing=.*payload.bin"):
                validate_exact_snapshot(root, expected_files=expected)

            target = root / "target.bin"
            target.write_bytes(payload)
            (root / "payload.bin").symlink_to(target)
            expected_with_target = {
                **expected,
                "target.bin": (len(payload), hashlib.sha256(payload).hexdigest()),
            }
            with self.assertRaisesRegex(SnapshotValidationError, "non-regular"):
                validate_exact_snapshot(root, expected_files=expected_with_target)

    def test_exact_snapshot_rejects_directory_fifo_and_wrong_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "snapshot"
            root.mkdir()
            (root / "entry").mkdir()
            expected = {"entry": (0, hashlib.sha256(b"").hexdigest())}
            with self.assertRaisesRegex(SnapshotValidationError, "non-regular"):
                validate_exact_snapshot(root, expected_files=expected)
            (root / "entry").rmdir()
            os.mkfifo(root / "entry")
            with self.assertRaisesRegex(SnapshotValidationError, "non-regular"):
                validate_exact_snapshot(root, expected_files=expected)
            (root / "entry").unlink()
            (root / "entry").write_bytes(b"")
            different = parent / "different"
            different.mkdir()
            with self.assertRaisesRegex(SnapshotValidationError, "required root"):
                validate_exact_snapshot(
                    root,
                    expected_files=expected,
                    required_root=different,
                )

    def test_load_model_rejects_cpu_without_fallback(self) -> None:
        fake_torch = types.SimpleNamespace(cuda=FakeCuda())
        fake_transformers = types.SimpleNamespace(
            AutoConfig=object, AutoModelForCausalLM=object, AutoTokenizer=object
        )
        with patch(
            "adapters.models.granite_moe.adapter._version",
            return_value=TRANSFORMERS_VERSION,
        ), patch.dict(
            sys.modules,
            {"torch": fake_torch, "transformers": fake_transformers},
        ):
            with self.assertRaisesRegex(RuntimeError, "CPU fallback is forbidden"):
                GraniteMoeAdapter().load_model()

    def test_preflight_rejects_cpu_only_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "adapters.models.granite_moe.adapter._version",
            return_value=TRANSFORMERS_VERSION,
        ), patch(
            "adapters.models.granite_moe.adapter._cuda_available",
            return_value=False,
        ), patch(
            "adapters.models.granite_moe.adapter.EXPECTED_SNAPSHOT_FILES",
            {},
        ):
            report = GraniteMoeAdapter(temporary).preflight()
        self.assertFalse(report.eligible)
        self.assertIn(
            "CUDA runtime/device is required; CPU fallback is forbidden",
            report.blockers,
        )

    def test_adversarial_router_tuples_are_rejected(self) -> None:
        valid = list(valid_router_tuple())
        cases = []
        duplicate_expert = valid.copy()
        duplicate_expert[3] = [2] * 7 + [0] * 25
        cases.append(duplicate_expert)
        wrong_batch = valid.copy()
        wrong_batch[1] = [1] * 8
        cases.append(wrong_batch)
        bad_weight = valid.copy()
        bad_weight[2] = [0.1] * 8
        cases.append(bad_weight)
        bad_logits = valid.copy()
        bad_logits[4] = [[float("nan")] + [0.0] * 31]
        cases.append(bad_logits)
        for router_tuple in cases:
            with self.subTest(router_tuple=router_tuple), self.assertRaises(ValueError):
                reconstruct_actual_dispatch(
                    router_tuple, layer="model.layers.0.block_sparse_moe",
                    call_index=0,
                )

    def test_real_bf16_and_fp16_quantized_gate_sums_are_accepted(self) -> None:
        # Round-to-nearest outputs from BF16/FP16 quantization of the same
        # eight-way FP32 softmax vector.
        bf16_weights = [
            0.37890625, 0.224609375, 0.02978515625, 0.01129150390625,
            0.05126953125, 0.027099609375, 0.263671875, 0.01470947265625,
        ]
        fp16_weights = [
            0.378173828125, 0.22509765625, 0.02972412109375,
            0.01126861572265625, 0.051239013671875, 0.027069091796875,
            0.2626953125, 0.01470947265625,
        ]
        for dtype, weights, expected in (
            ("bfloat16", bf16_weights, "bf16"),
            ("float16", fp16_weights, "fp16"),
        ):
            with self.subTest(dtype=dtype):
                record = reconstruct_actual_dispatch(
                    valid_router_tuple(
                        gate_dtype=dtype,
                        token_weights=weights,
                    ),
                    layer="model.layers.0.block_sparse_moe",
                    call_index=0,
                )
                self.assertEqual(expected, record.gate_dtype)

    def test_dtype_specific_gate_sum_boundary_is_enforced(self) -> None:
        cases = (
            ("bfloat16", [0.125] * 7 + [0.131], True),
            ("float16", [0.125] * 7 + [0.1261], True),
            ("float32", [0.125] * 7 + [0.12502], True),
            ("unknown-dtype", [0.125] * 7 + [0.12502], True),
            ("bfloat16", [0.125] * 7 + [0.1299], False),
            ("float16", [0.125] * 7 + [0.1259], False),
        )
        for dtype, weights, rejected in cases:
            with self.subTest(dtype=dtype, weights=weights):
                operation = lambda: reconstruct_actual_dispatch(
                    valid_router_tuple(
                        gate_dtype=dtype,
                        token_weights=weights,
                    ),
                    layer="model.layers.0.block_sparse_moe",
                    call_index=0,
                )
                if rejected:
                    with self.assertRaises(ValueError):
                        operation()
                else:
                    operation()

    def test_prefill_and_two_decode_calls_are_proven(self) -> None:
        model = FakeModel()
        capture = GraniteRoutingCapture(model)
        capture.enable()
        for token_count in (3, 1, 1):
            for moe in model.moes:
                moe.router.emit(valid_router_tuple(token_count))
                moe.emit(None)
        records = capture.disable(
            input_token_count=3,
            output_token_count=3,
            require_generation_semantics=True,
        )
        first_layer = [row for row in records if ".layers.0." in row.layer]
        self.assertEqual([0, 1, 2], [row.call_index for row in first_layer])
        self.assertEqual([3, 1, 1], [
            row.input_sequence_length for row in first_layer
        ])

    def test_preflight_blocks_without_explicit_local_snapshot(self) -> None:
        with patch(
            "adapters.models.granite_moe.adapter._version",
            side_effect=lambda package: (
                TRANSFORMERS_VERSION if package == "transformers" else "2.7.1"
            ),
        ):
            report = GraniteMoeAdapter().preflight()
        self.assertFalse(report.eligible)
        self.assertIn("explicit pinned local snapshot is required", report.blockers)

    def test_generation_policy_rejects_beams_sampling_and_speculation(self) -> None:
        for request in (
            GenerationRequest(max_new_tokens=1, do_sample=True),
            GenerationRequest(max_new_tokens=1, num_beams=2),
            GenerationRequest(max_new_tokens=1, extra={"assistant_model": object()}),
            GenerationRequest(max_new_tokens=1, extra={"compile_config": object()}),
        ):
            with self.assertRaises(ValueError):
                GraniteMoeAdapter._validated_generation_kwargs(request)


if __name__ == "__main__":
    unittest.main()
