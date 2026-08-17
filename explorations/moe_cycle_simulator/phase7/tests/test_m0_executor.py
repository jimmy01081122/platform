from __future__ import annotations

import copy
import importlib.machinery
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[4]
APPLICATION = REPO / "explorations/moe_cycle_simulator/phase7/application"
sys.path.insert(0, str(REPO))

from explorations.moe_cycle_simulator.phase7.application.executor.audit import (  # noqa: E402
    validate_summary,
)
from explorations.moe_cycle_simulator.phase7.application.executor.common import (  # noqa: E402
    M0Error,
    build_model_ledger,
    file_sha256,
    load_json,
    semantic_sha256,
    validate_contract,
    validate_fresh_target,
    validate_materialization_plan,
    validate_probe_record,
    verify_model_ledger,
    write_new_json,
)
from explorations.moe_cycle_simulator.phase7.application.executor.driver import (  # noqa: E402
    consume_approval,
    execution_environment,
    validate_m0_entry_parent,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_identity_bridge import (  # noqa: E402
    convert,
    runtime_variant_hash,
)
from explorations.moe_cycle_simulator.phase7.application.executor.package_ledger import (  # noqa: E402
    build as build_application_ledger,
)
from explorations.moe_cycle_simulator.phase7.application.executor.qualify import (  # noqa: E402
    child_environment,
    isolated_python_command,
    validate_backend_log_evidence,
)
from explorations.moe_cycle_simulator.phase7.application.executor.runtime_attestation import (  # noqa: E402
    attest_loaded_distribution_modules,
    validate_build_attestation,
    validate_distribution_manifest,
    validate_sbom_vllm_component,
)
from explorations.moe_cycle_simulator.phase7.application.executor.vllm_runtime_adapter import (  # noqa: E402
    bind_llm_constructor,
    resolve_kv_cache_dtype,
    validate_adapter_contract,
)
from explorations.moe_cycle_simulator.phase7.application.executor import (  # noqa: E402
    vllm_runtime_adapter as adapter_module,
)
from explorations.moe_cycle_simulator.phase7.application.executor.authority import (  # noqa: E402
    retain_authority,
    validate_retained_authority,
)
from explorations.moe_cycle_simulator.phase7.application.executor.build_prompt_fixture import (  # noqa: E402
    repeat_seed,
)


CONTRACT_PATH = APPLICATION / "m0_execution_contract.json"


def model_snapshot(root: Path) -> Path:
    snapshot = root / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text(
        json.dumps(
            {
                "model_type": "mixtral",
                "num_local_experts": 8,
                "num_experts_per_tok": 2,
                "max_position_embeddings": 32768,
                "torch_dtype": "bfloat16",
            }
        ),
        encoding="utf-8",
    )
    (snapshot / "tokenizer_config.json").write_text("{}\n", encoding="utf-8")
    (snapshot / "tokenizer.json").write_text("{}\n", encoding="utf-8")
    (snapshot / "model-00001-of-00001.safetensors").write_bytes(b"fake-safetensors")
    (snapshot / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "metadata": {"total_size": 16},
                "weight_map": {"model.layers.0.weight": "model-00001-of-00001.safetensors"},
            }
        ),
        encoding="utf-8",
    )
    return snapshot


def probe_record(
    contract: dict,
    *,
    index: int,
    contract_hash: str,
    runtime_hash: str,
    ledger_hash: str,
    session_id: str,
) -> dict:
    output = list(range(contract["probe"]["output_tokens"]))
    loaded_module = {
        "module_name": "vllm",
        "declared_path": "vllm/__init__.py",
        "resolved_path": "/srv/vllm/__init__.py",
        "size_bytes": 1,
        "sha256": "2" * 64,
        "binary": False,
    }
    import_evidence = {
        "schema_version": "moe-simulator-phase7-loaded-vllm-modules-v1",
        "distribution_name": "vllm",
        "distribution_version": "test",
        "distribution_ledger_sha256": "6" * 64,
        "module_prefix": "vllm",
        "loaded_module_count": 1,
        "loaded_modules": [loaded_module],
        "binary_module_count": 0,
        "binary_modules": [],
    }
    import_evidence["evidence_sha256"] = semantic_sha256(import_evidence)
    engine_arguments = {
        "model": "/srv/model",
        "tokenizer": "/srv/model",
        "tokenizer_mode": "auto",
        "skip_tokenizer_init": True,
        "trust_remote_code": False,
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "dtype": "bfloat16",
        "quantization": None,
        "seed": 0,
        "gpu_memory_utilization": "0.95",
        "cpu_offload_gb": 0,
        "swap_space": 0,
        "kv_cache_dtype": "bfloat16",
        "enforce_eager": True,
        "max_model_len": 32768,
        "max_num_batched_tokens": 32768,
        "max_num_seqs": 1,
        "generation_config": "vllm",
    }
    return {
        "schema_version": "moe-simulator-phase7-m0-probe-record-v1",
        "status": "COMPLETE",
        "session_id": session_id,
        "launch_index": index,
        "contract_sha256": contract_hash,
        "runtime_variant_sha256": runtime_hash,
        "model_ledger_sha256": ledger_hash,
        "process_identity": {
            "pid": 100 + index,
            "boot_id": "boot",
            "start_ticks": 1000 + index,
            "nonce": f"nonce-{index}",
        },
        "gpu": {
            "count": 1,
            "name": contract["target"]["exact_product_name"],
            "total_memory_bytes": contract["target"]["minimum_memory_bytes"],
            "uuid": "GPU-test",
            "driver_version": "test",
        },
        "memory": {
            name: {"used_memory_bytes": 1, "free_memory_bytes": 2}
            for name in ("before_load", "after_load", "after_generation")
        },
        "engine": {
            "constructor_arguments": engine_arguments,
            "constructor_arguments_sha256": semantic_sha256(engine_arguments),
            "resolved_kv_cache_dtype": {
                "method": "FROZEN_VERSION_ATTRIBUTE_PATH",
                "attribute_path": ["engine", "cache_config", "cache_dtype"],
                "raw_value": "torch.bfloat16",
                "normalized_value": "bfloat16",
            },
        },
        "software": {
            "vllm_version": "test",
            "vllm_init_sha256": "2" * 64,
            "vllm_import_evidence": import_evidence,
            "vllm_source_git_commit": "5" * 40,
            "installed_distribution_ledger_sha256": "6" * 64,
            "build_attestation_file_sha256": "7" * 64,
            "runtime_adapter_contract_sha256": "8" * 64,
            "torch_version": "test",
            "transformers_version": "test",
        },
        "runtime_qualified_version": "test",
        "runtime_qualified_git_commit": "5" * 40,
        "probe": {
            "input_token_count": contract["probe"]["input_tokens"],
            "prompt_token_ids_sha256": "3" * 64,
            "capacity_prompt_fixture_sha256": "4" * 64,
            "output_token_count": len(output),
            "output_token_ids": output,
            "output_token_ids_sha256": semantic_sha256(output),
            "finish_reason": "length",
            "stop_reason": None,
            "finished": True,
        },
    }


class FakeTokenizer:
    def encode(self, text: str, add_special_tokens: bool) -> list[int]:
        self.last = (text, add_special_tokens)
        return [7, 8, 9]


class M0ExecutorTests(unittest.TestCase):
    def test_m0_entry_requires_hash_bound_live_eligible_gate_m_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = Path(directory)
            parent = application / "gate_m_parent_evidence.template.json"
            parent_value = {
                "remote": {
                    "model_ledger_sha256": "1" * 64,
                    "capacity_prompt_fixture_sha256": "2" * 64,
                }
            }
            parent.write_text(json.dumps(parent_value) + "\n", encoding="utf-8")
            approval = {
                "gate_m_parent_evidence_file_sha256": file_sha256(parent)
            }
            runtime = {
                "model": {
                    "model_file_ledger_sha256": "1" * 64,
                    "capacity_prompt_fixture_sha256": "2" * 64,
                }
            }
            with patch(
                "explorations.moe_cycle_simulator.phase7.application.executor.gate_m_parent.validate_parent_file",
                return_value=parent_value,
            ) as verifier:
                validate_m0_entry_parent(application, approval, runtime)
                verifier.assert_called_once_with(
                    parent,
                    verify_live=True,
                    expected_file_sha256=approval[
                        "gate_m_parent_evidence_file_sha256"
                    ],
                )
                for field in (
                    "model_file_ledger_sha256",
                    "capacity_prompt_fixture_sha256",
                ):
                    changed = copy.deepcopy(runtime)
                    changed["model"][field] = "9" * 64
                    with self.assertRaisesRegex(M0Error, "Gate M/M0"):
                        validate_m0_entry_parent(application, approval, changed)
            approval["gate_m_parent_evidence_file_sha256"] = "0" * 64
            with self.assertRaisesRegex(M0Error, "does not bind"):
                validate_m0_entry_parent(application, approval, runtime)
            approval["gate_m_parent_evidence_file_sha256"] = file_sha256(parent)
            with self.assertRaises(M0Error):
                validate_m0_entry_parent(application, approval, runtime)

    def test_m0_entry_binds_one_captured_gate_m_parent_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            application = Path(directory)
            parent_path = application / "gate_m_parent_evidence.template.json"
            captured = {
                "remote": {
                    "model_ledger_sha256": "1" * 64,
                    "capacity_prompt_fixture_sha256": "2" * 64,
                }
            }
            substituted = {
                "remote": {
                    "model_ledger_sha256": "8" * 64,
                    "capacity_prompt_fixture_sha256": "9" * 64,
                }
            }
            captured_payload = (json.dumps(captured) + "\n").encode("utf-8")
            parent_path.write_bytes(captured_payload)
            parent_path.chmod(0o444)
            approval = {
                "gate_m_parent_evidence_file_sha256": file_sha256(parent_path)
            }

            def validate_then_replace(
                value: dict, *, verify_live: bool
            ) -> dict:
                self.assertEqual(value, captured)
                self.assertTrue(verify_live)
                parent_path.chmod(0o644)
                parent_path.write_text(
                    json.dumps(substituted) + "\n", encoding="utf-8"
                )
                parent_path.chmod(0o444)
                return {"status": "COMPLETE_M0_ELIGIBLE"}

            validator = (
                "explorations.moe_cycle_simulator.phase7.application.executor."
                "gate_m_parent.validate_gate_m_parent"
            )
            captured_runtime = {
                "model": {
                    "model_file_ledger_sha256": "1" * 64,
                    "capacity_prompt_fixture_sha256": "2" * 64,
                }
            }
            with patch(validator, side_effect=validate_then_replace):
                validate_m0_entry_parent(
                    application, approval, captured_runtime
                )
            self.assertEqual(load_json(parent_path), substituted)

            parent_path.chmod(0o644)
            parent_path.write_bytes(captured_payload)
            parent_path.chmod(0o444)
            substituted_runtime = {
                "model": {
                    "model_file_ledger_sha256": "8" * 64,
                    "capacity_prompt_fixture_sha256": "9" * 64,
                }
            }
            with (
                patch(validator, side_effect=validate_then_replace),
                self.assertRaisesRegex(M0Error, "Gate M/M0"),
            ):
                validate_m0_entry_parent(
                    application, approval, substituted_runtime
                )
            parent_path.chmod(0o644)

    def setUp(self) -> None:
        self.contract = load_json(CONTRACT_PATH)

    def test_contract_is_exact_and_float_free(self) -> None:
        validate_contract(self.contract)
        self.assertEqual(self.contract["probe"]["input_tokens"], 28672)
        self.assertEqual(self.contract["probe"]["output_tokens"], 4096)
        self.assertEqual(self.contract["probe"]["repetitions"], 3)

    def test_model_ledger_round_trip_and_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = model_snapshot(Path(directory))
            ledger = build_model_ledger(
                snapshot,
                model_id=self.contract["model"]["model_id"],
                repository_commit=self.contract["model"]["repository_commit"],
            )
            verify_model_ledger(snapshot, ledger, contract=self.contract)
            (snapshot / "config.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(M0Error):
                verify_model_ledger(snapshot, ledger, contract=self.contract)

    def test_model_ledger_rejects_self_consistent_wrong_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = model_snapshot(Path(directory))
            for model_id, revision in (
                ("mistralai/Other-Mixtral", self.contract["model"]["repository_commit"]),
                (self.contract["model"]["model_id"], "9" * 40),
            ):
                ledger = build_model_ledger(
                    snapshot,
                    model_id=model_id,
                    repository_commit=revision,
                )
                with self.assertRaisesRegex(M0Error, "exact M0 model/revision"):
                    verify_model_ledger(
                        snapshot, ledger, contract=self.contract
                    )

    def test_model_ledger_rejects_symlink_and_extra_weight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = model_snapshot(Path(directory))
            (snapshot / "bad").symlink_to(snapshot / "config.json")
            with self.assertRaises(M0Error):
                build_model_ledger(
                    snapshot,
                    model_id=self.contract["model"]["model_id"],
                    repository_commit=self.contract["model"]["repository_commit"],
                )

    def test_fresh_target_rejects_existing_and_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "fresh"
            self.assertEqual(validate_fresh_target(target, "target"), target)
            target.write_text("occupied", encoding="utf-8")
            with self.assertRaises(M0Error):
                validate_fresh_target(target, "target")
            real_parent = root / "real"
            real_parent.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaises(M0Error):
                validate_fresh_target(linked_parent / "new", "target")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = model_snapshot(Path(directory))
            (snapshot / "extra.safetensors").write_bytes(b"extra")
            with self.assertRaises(M0Error):
                build_model_ledger(
                    snapshot,
                    model_id=self.contract["model"]["model_id"],
                    repository_commit=self.contract["model"]["repository_commit"],
                )

    def test_prompt_builder_has_exact_count_and_no_implicit_bos(self) -> None:
        tokenizer = FakeTokenizer()
        values = repeat_seed(tokenizer, 28672)
        self.assertEqual(len(values), 28672)
        self.assertEqual(values[:6], [7, 8, 9, 7, 8, 9])
        self.assertEqual(tokenizer.last[1], False)

    def test_probe_record_fail_closed(self) -> None:
        record = probe_record(
            self.contract,
            index=1,
            contract_hash="a" * 64,
            runtime_hash="b" * 64,
            ledger_hash="c" * 64,
            session_id="phase7-m0-test",
        )
        validate_probe_record(
            record,
            contract_sha256="a" * 64,
            runtime_sha256="b" * 64,
            model_ledger_sha256="c" * 64,
            launch_index=1,
            session_id="phase7-m0-test",
            contract=self.contract,
        )
        for mutation in (
            ("output_token_count", 4095),
            ("finish_reason", "stop"),
            ("stop_reason", "eos"),
        ):
            changed = copy.deepcopy(record)
            changed["probe"][mutation[0]] = mutation[1]
            with self.assertRaises(M0Error):
                validate_probe_record(
                    changed,
                    contract_sha256="a" * 64,
                    runtime_sha256="b" * 64,
                    model_ledger_sha256="c" * 64,
                    launch_index=1,
                    session_id="phase7-m0-test",
                    contract=self.contract,
                )
        changed = copy.deepcopy(record)
        changed["engine"]["constructor_arguments"]["kv_cache_dtype"] = "auto"
        changed["engine"]["constructor_arguments_sha256"] = semantic_sha256(
            changed["engine"]["constructor_arguments"]
        )
        with self.assertRaises(M0Error):
            validate_probe_record(
                changed,
                contract_sha256="a" * 64,
                runtime_sha256="b" * 64,
                model_ledger_sha256="c" * 64,
                launch_index=1,
                session_id="phase7-m0-test",
                contract=self.contract,
            )

    def test_version_qualified_adapter_passes_exact_bf16_kv_cache(self) -> None:
        implementation = Path(adapter_module.__file__).resolve()
        runtime = {
            "runtime": {
                "version": "0.10.0",
                "git_commit": "a" * 40,
            }
        }
        adapter = {
            "schema_version": "moe-simulator-phase7-vllm-runtime-adapter-v1",
            "status": "FROZEN",
            "adapter_id": "vllm-exact-version-kv-cache-bf16-v1",
            "implementation_path": str(implementation),
            "implementation_sha256": file_sha256(implementation),
            "qualified_runtime": {
                "version": "0.10.0",
                "git_commit": "a" * 40,
            },
            "llm_constructor_binding": {
                "callable": "vllm.LLM",
                "parameter": "kv_cache_dtype",
                "bf16_value": "bfloat16",
                "signature_requirement": "EXPLICIT_OR_VAR_KEYWORD",
                "probe_evidence_field": "engine.constructor_arguments.kv_cache_dtype",
            },
            "resolved_kv_cache_evidence": {
                "method": "FROZEN_VERSION_ATTRIBUTE_PATH",
                "attribute_path": ["engine", "cache_config", "cache_dtype"],
                "expected_normalized_value": "bfloat16",
            },
            "loaded_module_binding": {
                "distribution_name": "vllm",
                "module_prefix": "vllm",
                "isolated_python": True,
                "bind_all_loaded_modules": True,
                "bind_all_loaded_binary_modules": True,
            },
        }

        class CompatibleLLM:
            def __init__(self, *, kv_cache_dtype: str, model: str) -> None:
                self.engine = type(
                    "Engine",
                    (),
                    {
                        "cache_config": type(
                            "CacheConfig",
                            (),
                            {"cache_dtype": "torch.bfloat16"},
                        )()
                    },
                )()

        validate_adapter_contract(adapter, runtime=runtime)
        bound = bind_llm_constructor(
            CompatibleLLM, {"model": "/srv/model"}, adapter
        )
        self.assertEqual(bound["kv_cache_dtype"], "bfloat16")
        resolved = resolve_kv_cache_dtype(
            CompatibleLLM(**bound), adapter
        )
        self.assertEqual(resolved["normalized_value"], "bfloat16")

        mismatched = copy.deepcopy(runtime)
        mismatched["runtime"]["version"] = "0.10.1"
        with self.assertRaises(M0Error):
            validate_adapter_contract(adapter, runtime=mismatched)

        class IncompatibleLLM:
            def __init__(self, *, model: str) -> None:
                pass

        with self.assertRaises(M0Error):
            bind_llm_constructor(
                IncompatibleLLM, {"model": "/srv/model"}, adapter
            )

        wrong_resolved = CompatibleLLM(
            kv_cache_dtype="bfloat16", model="/srv/model"
        )
        wrong_resolved.engine.cache_config.cache_dtype = "float16"
        with self.assertRaises(M0Error):
            resolve_kv_cache_dtype(wrong_resolved, adapter)

    def test_vllm_build_attestation_rejects_commit_and_version_mismatch(self) -> None:
        runtime = {
            "runtime": {
                "version": "0.10.0",
                "git_commit": "a" * 40,
                "container_image": "registry.invalid/vllm:exact",
                "container_digest": "sha256:" + "b" * 64,
            }
        }
        attestation = {
            "schema_version": "moe-simulator-phase7-vllm-build-attestation-v1",
            "status": "FROZEN",
            "package": {"name": "vllm", "version": "0.10.0"},
            "source": {
                "repository": "https://github.com/vllm-project/vllm",
                "git_commit": "a" * 40,
                "tree_sha256": "c" * 64,
            },
            "wheel": {"path": "/srv/vllm.whl", "sha256": "d" * 64},
            "build": {
                "command_argv": ["python", "-m", "build"],
                "environment_ledger_path": "/srv/build-environment.json",
                "environment_ledger_sha256": "e" * 64,
            },
            "installed_distribution": {
                "manifest_path": "/srv/vllm-installed.json",
                "manifest_file_sha256": "f" * 64,
                "ledger_sha256": "1" * 64,
            },
            "container": {
                "image": "registry.invalid/vllm:exact",
                "digest": "sha256:" + "b" * 64,
                "sbom_path": "/srv/container-sbom.json",
                "sbom_sha256": "2" * 64,
            },
            "provenance": {
                "builder_identity": "test-builder",
                "build_timestamp_utc": "2026-07-29T00:00:00Z",
                "attestation_method": "test-fixture",
            },
        }
        validate_build_attestation(
            attestation, runtime=runtime, verify_files=False
        )
        wrong_commit = copy.deepcopy(attestation)
        wrong_commit["source"]["git_commit"] = "9" * 40
        with self.assertRaises(M0Error):
            validate_build_attestation(
                wrong_commit, runtime=runtime, verify_files=False
            )
        wrong_version = copy.deepcopy(attestation)
        wrong_version["package"]["version"] = "0.10.1"
        with self.assertRaises(M0Error):
            validate_build_attestation(
                wrong_version, runtime=runtime, verify_files=False
            )
        wrong_container = copy.deepcopy(attestation)
        wrong_container["container"]["digest"] = "sha256:" + "9" * 64
        with self.assertRaises(M0Error):
            validate_build_attestation(
                wrong_container, runtime=runtime, verify_files=False
            )

    def test_installed_distribution_manifest_is_hash_closed(self) -> None:
        manifest = {
            "schema_version": "moe-simulator-phase7-installed-distribution-ledger-v1",
            "distribution_name": "vllm",
            "distribution_version": "0.10.0",
            "member_count": 1,
            "total_size_bytes": 10,
            "members": [
                {
                    "declared_path": "vllm-0.10.0.dist-info/RECORD",
                    "size_bytes": 10,
                    "sha256": "a" * 64,
                }
            ],
        }
        manifest["ledger_sha256"] = semantic_sha256(manifest)
        validate_distribution_manifest(manifest)
        tampered = copy.deepcopy(manifest)
        tampered["members"][0]["sha256"] = "b" * 64
        with self.assertRaises(M0Error):
            validate_distribution_manifest(tampered)

    def test_loaded_vllm_modules_are_bound_to_frozen_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_path = root / "vllm/__init__.py"
            init_path.parent.mkdir()
            init_path.write_bytes(b"frozen-vllm")
            extension_path = root / (
                "vllm/_C" + importlib.machinery.EXTENSION_SUFFIXES[0]
            )
            extension_path.write_bytes(b"frozen-extension")
            record_path = root / "vllm-0.10.0.dist-info/RECORD"
            record_path.parent.mkdir()
            record_path.write_bytes(b"record")
            paths = (init_path, extension_path, record_path)
            members = [
                {
                    "declared_path": path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
                for path in paths
            ]
            manifest = {
                "schema_version": "moe-simulator-phase7-installed-distribution-ledger-v1",
                "distribution_name": "vllm",
                "distribution_version": "0.10.0",
                "member_count": len(members),
                "total_size_bytes": sum(item["size_bytes"] for item in members),
                "members": sorted(members, key=lambda item: item["declared_path"]),
            }
            manifest["ledger_sha256"] = semantic_sha256(manifest)

            class FakeDistribution:
                version = "0.10.0"

                @staticmethod
                def locate_file(path: Path) -> Path:
                    return root / path

            modules = {
                "vllm": SimpleNamespace(
                    __file__=str(init_path),
                    __spec__=SimpleNamespace(origin=str(init_path)),
                ),
                "vllm._C": SimpleNamespace(
                    __file__=str(extension_path),
                    __spec__=SimpleNamespace(origin=str(extension_path)),
                ),
            }
            target = (
                "explorations.moe_cycle_simulator.phase7.application.executor."
                "runtime_attestation.importlib.metadata.distribution"
            )
            with patch(target, return_value=FakeDistribution()):
                evidence = attest_loaded_distribution_modules(
                    manifest, modules=modules
                )
                self.assertEqual(evidence["binary_modules"], ["vllm._C"])
                self.assertEqual(evidence["loaded_module_count"], 2)

                originless_modules = dict(modules)
                originless_modules["vllm.injected"] = ModuleType(
                    "vllm.injected"
                )
                with self.assertRaisesRegex(
                    M0Error, "no attested file origin"
                ):
                    attest_loaded_distribution_modules(
                        manifest, modules=originless_modules
                    )

                shadow = root / "shadow/vllm/__init__.py"
                shadow.parent.mkdir(parents=True)
                shadow.write_bytes(init_path.read_bytes())
                shadow_modules = dict(modules)
                shadow_modules["vllm"] = SimpleNamespace(
                    __file__=str(shadow),
                    __spec__=SimpleNamespace(origin=str(shadow)),
                )
                with self.assertRaisesRegex(M0Error, "outside the frozen"):
                    attest_loaded_distribution_modules(
                        manifest, modules=shadow_modules
                    )

                init_path.write_bytes(b"replacement-module")
                with self.assertRaisesRegex(M0Error, "drifted"):
                    attest_loaded_distribution_modules(
                        manifest, modules=modules
                    )

    def test_m0_python_environment_is_isolated_from_pythonpath(self) -> None:
        runtime = load_json(APPLICATION / "runtime_variant.template.json")
        with patch.dict(os.environ, {"PYTHONPATH": "/tmp/shadow"}, clear=True):
            self.assertNotIn("PYTHONPATH", child_environment(runtime))
            self.assertNotIn("PYTHONPATH", execution_environment(runtime))
        command = isolated_python_command(
            APPLICATION / "executor/single_launch.py", ["--sentinel"]
        )
        self.assertEqual(command[1], "-I")
        bad = copy.deepcopy(runtime)
        bad["command_environment"]["PYTHONPATH"] = "/tmp/shadow"
        with self.assertRaisesRegex(M0Error, "import-path"):
            child_environment(bad)
        with self.assertRaisesRegex(M0Error, "import-path"):
            execution_environment(bad)

    def test_container_sbom_must_name_exact_vllm_version(self) -> None:
        sbom = {
            "bomFormat": "CycloneDX",
            "components": [{"name": "vllm", "version": "0.10.0"}],
        }
        validate_sbom_vllm_component(sbom, "0.10.0")
        with self.assertRaises(M0Error):
            validate_sbom_vllm_component(sbom, "0.10.1")

    def test_three_launch_audit_requires_exact_identity_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launches = []
            for index in (1, 2, 3):
                launch = root / f"launch-{index}"
                launch.mkdir()
                record = probe_record(
                    self.contract,
                    index=index,
                    contract_hash="a" * 64,
                    runtime_hash="b" * 64,
                    ledger_hash="c" * 64,
                    session_id="phase7-m0-test",
                )
                write_new_json(launch / "probe.json", record)
                (launch / "stdout.log").write_bytes(b"")
                (launch / "stderr.log").write_bytes(b"")
                launches.append(
                    {
                        "launch_index": index,
                        "returncode": 0,
                        "timed_out": False,
                        "cleanup": {"status": "PASS", "residual_processes": []},
                        "process_tree_cleanup": {
                            "status": "CLEAN",
                            "surviving_pids": [],
                        },
                        "environment_sha256": "d" * 64,
                        "probe_path": f"launch-{index}/probe.json",
                        "stdout_sha256": file_sha256(launch / "stdout.log"),
                        "stderr_sha256": file_sha256(launch / "stderr.log"),
                    }
                )
            summary = {
                "schema_version": "moe-simulator-phase7-m0-qualification-summary-v1",
                "status": "COMPLETE",
                "session_id": "phase7-m0-test",
                "contract_sha256": "a" * 64,
                "runtime_variant_sha256": "b" * 64,
                "model_ledger_sha256": "c" * 64,
                "environment_sha256": "d" * 64,
                "launch_count": 3,
                "fresh_process_identity_count": 3,
                "retry_used": False,
                "resume_used": False,
                "launches": launches,
            }
            records = validate_summary(
                summary,
                qualification_root=root,
                contract=self.contract,
                contract_hash="a" * 64,
                runtime_hash="b" * 64,
                model_ledger={"ledger_sha256": "c" * 64},
                session_id="phase7-m0-test",
            )
            self.assertEqual(len(records), 3)
            summary["launches"][1]["timed_out"] = True
            with self.assertRaises(M0Error):
                validate_summary(
                    summary,
                    qualification_root=root,
                    contract=self.contract,
                    contract_hash="a" * 64,
                    runtime_hash="b" * 64,
                    model_ledger={"ledger_sha256": "c" * 64},
                    session_id="phase7-m0-test",
                )

    def test_materialization_plan_contract(self) -> None:
        plan = load_json(APPLICATION / "materialization_plan.template.json")
        plan["status"] = "FROZEN"
        plan["materializer"]["version"] = "1.0"
        root = "/vault/flow-mixtral-rtxpro6000-r12-test0001"
        plan["storage_contract"]["persistent_project_root"] = root
        plan["deployment"] = {
            "application_target": root
            + "/packages/materialization/repo/explorations/moe_cycle_simulator/phase7/application",
            "deployment_receipt": root
            + "/packages/materialization/deployment_receipt.json",
        }
        plan["paths"] = {
            "snapshot": root + "/model/snapshot",
            "model_ledger": root + "/model/ledger/model-ledger.json",
            "materialization_result": root + "/model/ledger/materialization-result.json",
            "capacity_prompt_fixture": root + "/fixtures/capacity-prompt.json",
        }
        plan["runtime_provenance"].update(
            {
                "output_root": root + "/evidence/runtime-provenance",
                "command_argv": ["python3", "runtime_provenance.py"],
            }
        )
        plan["command_argv"] = ["python3", "materialize.py"]
        validate_materialization_plan(plan, self.contract)

    def test_runtime_identity_bridge_is_deterministic(self) -> None:
        runtime = load_json(APPLICATION / "runtime_variant.template.json")
        rt = runtime["runtime"]
        for key, value in {
            "git_commit": "vllm-commit",
            "container_image": "image",
            "container_digest": "sha256:image",
            "cuda_runtime": "13.0",
            "driver": "999",
            "attention_backend": "FLASH_ATTN",
            "fused_moe_backend": "CUTLASS_MOE",
            "kernel_backend": "CUDA",
            "gpu_memory_utilization": "0.95",
        }.items():
            rt[key] = value
        runtime["collector"] = {
            "phase7_local_framework_hash": "1" * 64,
            "capacity_probe_hash": "2" * 64,
            "evidence_schema_hash": "3" * 64,
        }
        runtime["runtime_adapter_contract"] = {
            "path": "/srv/vllm-adapter.json",
            "file_sha256": "4" * 64,
        }
        runtime["runtime_attestation"] = {
            "build_attestation_path": "/srv/vllm-build.json",
            "build_attestation_file_sha256": "5" * 64,
        }
        value = convert(runtime)
        self.assertEqual(value["variant_id"], runtime_variant_hash(value))
        self.assertEqual(value, convert(runtime))
        self.assertFalse(value["offload"]["enabled"])
        self.assertEqual(value["runtime_adapter_contract_hash"], "4" * 64)
        self.assertEqual(value["runtime_build_attestation_hash"], "5" * 64)

    def test_one_shot_approval_registry_rejects_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "used.json"
            approval = {
                "used_once_registry_path": str(registry),
                "approval_id": "approval",
                "approval_token_sha256": "a" * 64,
                "approved_session_id": "phase7-m0-test",
                "_file_sha256": "b" * 64,
            }
            consume_approval(approval)
            with self.assertRaises(M0Error):
                consume_approval(approval)

    def test_authority_evidence_retains_exact_approval_consumption_and_package(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            package = build_application_ledger(APPLICATION)
            approval = load_json(APPLICATION / "approval.template.json")
            approval["application_ledger_sha256"] = package["ledger_sha256"]
            approval_path = root / "approval.json"
            write_new_json(approval_path, approval)
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": "moe-simulator-phase7-used-approval-v1",
                        "approval_id": approval["approval_id"],
                        "approval_token_sha256": approval["approval_token_sha256"],
                        "approved_session_id": approval["approved_session_id"],
                        "approval_file_sha256": file_sha256(approval_path),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            retained = retain_authority(
                application=APPLICATION,
                approval_path=approval_path,
                registry_path=registry,
                evidence_root=evidence,
                expected_application_ledger_sha256=package["ledger_sha256"],
            )
            self.assertEqual(
                (evidence / "authority/approval.json").read_bytes(),
                approval_path.read_bytes(),
            )
            self.assertEqual(
                retained["application_package_ledger_sha256"],
                package["ledger_sha256"],
            )
            self.assertEqual(
                validate_retained_authority(
                    evidence_root=evidence,
                    require_package_match=True,
                ),
                retained,
            )
            (evidence / "authority/approval.json").chmod(0o600)
            (evidence / "authority/approval.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with self.assertRaises(M0Error):
                validate_retained_authority(
                    evidence_root=evidence,
                    require_package_match=True,
                )

    def test_consumed_authority_bytes_survive_package_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            package = build_application_ledger(APPLICATION)
            approval = load_json(APPLICATION / "approval.template.json")
            approval["application_ledger_sha256"] = "0" * 64
            approval_path = root / "approval.json"
            write_new_json(approval_path, approval)
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": "moe-simulator-phase7-used-approval-v1",
                        "approval_id": approval["approval_id"],
                        "approval_token_sha256": approval["approval_token_sha256"],
                        "approved_session_id": approval["approved_session_id"],
                        "approval_file_sha256": file_sha256(approval_path),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                M0Error, "live recursive application package differs"
            ):
                retain_authority(
                    application=APPLICATION,
                    approval_path=approval_path,
                    registry_path=registry,
                    evidence_root=evidence,
                    expected_application_ledger_sha256="0" * 64,
                )
            self.assertEqual(
                (evidence / "authority/approval.json").read_bytes(),
                approval_path.read_bytes(),
            )
            record = validate_retained_authority(
                evidence_root=evidence,
                require_package_match=False,
            )
            self.assertEqual(record["package_verification"], "FAIL")
            self.assertEqual(
                record["application_package_ledger_sha256"],
                package["ledger_sha256"],
            )

    def test_recursive_application_ledger_binds_executables(self) -> None:
        ledger = build_application_ledger(APPLICATION)
        self.assertGreater(ledger["member_count"], 20)
        self.assertRegex(ledger["ledger_sha256"], r"^[0-9a-f]{64}$")
        paths = {item["path"] for item in ledger["members"]}
        self.assertIn("executor/single_launch.py", paths)
        self.assertIn("schemas/probe_record.schema.json", paths)
        self.assertNotIn("approval.template.json", paths)

    def test_m0_evidence_schemas_close_top_level_objects(self) -> None:
        schemas = APPLICATION / "schemas"
        names = {
            "authority_evidence.schema.json",
            "capacity_prompt.schema.json",
            "m0_result.schema.json",
            "model_ledger.schema.json",
            "materialization_evidence_ledger.schema.json",
            "preflight_evidence.schema.json",
            "probe_record.schema.json",
            "qualification_summary.schema.json",
            "session_ledger.schema.json",
            "installed_distribution.schema.json",
            "vllm_build_attestation.schema.json",
            "vllm_runtime_adapter.schema.json",
            "d0_evidence_ledger.schema.json",
            "d0_probe_result.schema.json",
            "d0_result.schema.json",
            "d0_failure.schema.json",
        }
        self.assertEqual(names, {path.name for path in schemas.glob("*.json")})
        for name in names:
            value = load_json(schemas / name)
            self.assertEqual(value["type"], "object")
            self.assertIs(value["additionalProperties"], False)
            self.assertTrue(value["required"])

    def test_backend_evidence_requires_all_frozen_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            stdout.write_text("attention=FLASH\nkernel=CUDA\n", encoding="utf-8")
            stderr.write_text("moe=CUTLASS\n", encoding="utf-8")
            runtime = {
                "backend_evidence_contract": {
                    "source": "VLLM_STARTUP_LOG_OR_FROZEN_VERSION_ADAPTER",
                    "required_utf8_markers": {
                        "attention_backend": "attention=FLASH",
                        "fused_moe_backend": "moe=CUTLASS",
                        "kernel_backend": "kernel=CUDA",
                    },
                }
            }
            evidence = validate_backend_log_evidence(stdout, stderr, runtime)
            self.assertIs(evidence["all_markers_observed"], True)
            stderr.write_text("", encoding="utf-8")
            with self.assertRaises(M0Error):
                validate_backend_log_evidence(stdout, stderr, runtime)

    def test_application_modes_and_shells_fail_before_hardware(self) -> None:
        draft = subprocess.run(
            [
                sys.executable,
                str(APPLICATION / "validate_application.py"),
                "--mode",
                "draft",
                "--application-dir",
                str(APPLICATION),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(draft.returncode, 0, draft.stderr)
        for mode in ("materialization-ready", "execution-ready"):
            blocked = subprocess.run(
                [
                    sys.executable,
                    str(APPLICATION / "validate_application.py"),
                    "--mode",
                    mode,
                    "--application-dir",
                    str(APPLICATION),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "MOE_PHASE7_EXECUTION_UNLOCK",
                "MOE_PHASE7_MATERIALIZATION_UNLOCK",
            }
        }
        for script, argv in (
            ("run_m0.template.sh", [str(APPLICATION), str(APPLICATION)]),
            ("preflight_m0.template.sh", [str(APPLICATION), "/tmp/forbidden.json"]),
            ("materialize_m0.template.sh", [str(APPLICATION), "/tmp/forbidden"]),
        ):
            result = subprocess.run(
                ["bash", str(APPLICATION / script), *argv],
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 64, result.stderr)

    def test_materialization_driver_only_accepts_bounded_work_reduction(self) -> None:
        driver = APPLICATION / "executor/materialization_driver.py"
        for seconds in (0, 4801):
            with self.subTest(seconds=seconds):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(driver),
                        "--application-dir",
                        "/definitely/not/reached",
                        "--evidence-root",
                        "/definitely/not/reached",
                        "--work-seconds",
                        str(seconds),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must not exceed the frozen allowance", result.stderr)


if __name__ == "__main__":
    unittest.main()
