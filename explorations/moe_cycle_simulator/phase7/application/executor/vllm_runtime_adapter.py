#!/usr/bin/env python3
"""Version-qualified vLLM constructor binding for the Phase 7 M0 probe."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Mapping

from .common import M0Error, file_sha256, load_json


ADAPTER_SCHEMA = "moe-simulator-phase7-vllm-runtime-adapter-v1"
ADAPTER_ID = "vllm-exact-version-kv-cache-bf16-v1"


def validate_adapter_contract(
    contract: Mapping[str, Any],
    *,
    runtime: Mapping[str, Any],
    implementation_path: Path | None = None,
) -> None:
    expected_keys = {
        "schema_version",
        "status",
        "adapter_id",
        "implementation_path",
        "implementation_sha256",
        "qualified_runtime",
        "llm_constructor_binding",
        "resolved_kv_cache_evidence",
        "loaded_module_binding",
    }
    if set(contract) != expected_keys:
        raise M0Error("vLLM runtime-adapter contract key closure mismatch")
    if (
        contract["schema_version"] != ADAPTER_SCHEMA
        or contract["status"] != "FROZEN"
        or contract["adapter_id"] != ADAPTER_ID
    ):
        raise M0Error("vLLM runtime-adapter contract is not frozen or supported")
    qualified = contract["qualified_runtime"]
    if (
        not isinstance(qualified, dict)
        or set(qualified) != {"version", "git_commit"}
        or qualified["version"] != runtime["runtime"]["version"]
        or qualified["git_commit"] != runtime["runtime"]["git_commit"]
    ):
        raise M0Error("vLLM runtime adapter is not qualified for this version/commit")
    binding = contract["llm_constructor_binding"]
    if binding != {
        "callable": "vllm.LLM",
        "parameter": "kv_cache_dtype",
        "bf16_value": "bfloat16",
        "signature_requirement": "EXPLICIT_OR_VAR_KEYWORD",
        "probe_evidence_field": "engine.constructor_arguments.kv_cache_dtype",
    }:
        raise M0Error("vLLM KV-cache constructor binding changed")
    resolved = contract["resolved_kv_cache_evidence"]
    if (
        not isinstance(resolved, dict)
        or set(resolved) != {
            "method",
            "attribute_path",
            "expected_normalized_value",
        }
        or resolved["method"] != "FROZEN_VERSION_ATTRIBUTE_PATH"
        or resolved["expected_normalized_value"] != "bfloat16"
        or not isinstance(resolved["attribute_path"], list)
        or not resolved["attribute_path"]
        or any(
            not isinstance(item, str)
            or not item
            or item.startswith("[BLOCKING:")
            for item in resolved["attribute_path"]
        )
    ):
        raise M0Error("resolved KV-cache evidence path is unresolved or invalid")
    if contract["loaded_module_binding"] != {
        "distribution_name": "vllm",
        "module_prefix": "vllm",
        "isolated_python": True,
        "bind_all_loaded_modules": True,
        "bind_all_loaded_binary_modules": True,
    }:
        raise M0Error("vLLM loaded-module binding contract changed")
    declared_path = Path(contract["implementation_path"])
    actual_path = (implementation_path or Path(__file__)).resolve(strict=True)
    if (
        not declared_path.is_absolute()
        or declared_path.resolve(strict=True) != actual_path
        or declared_path.is_symlink()
        or file_sha256(actual_path) != contract["implementation_sha256"]
    ):
        raise M0Error("vLLM runtime-adapter implementation hash mismatch")


def load_adapter_contract(runtime: Mapping[str, Any]) -> dict[str, Any]:
    binding = runtime.get("runtime_adapter_contract")
    if not isinstance(binding, dict) or set(binding) != {"path", "file_sha256"}:
        raise M0Error("runtime adapter file binding is missing")
    path = Path(binding["path"])
    if (
        not path.is_absolute()
        or not path.is_file()
        or path.is_symlink()
        or file_sha256(path) != binding["file_sha256"]
    ):
        raise M0Error("runtime adapter contract file/hash mismatch")
    contract = load_json(path)
    validate_adapter_contract(contract, runtime=runtime)
    return contract


def bind_llm_constructor(
    llm_class: type[Any],
    arguments: Mapping[str, Any],
    adapter_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Return constructor args with the exact BF16 KV-cache request injected."""
    binding = adapter_contract["llm_constructor_binding"]
    parameter = binding["parameter"]
    signature = inspect.signature(llm_class.__init__)
    accepts_parameter = parameter in signature.parameters
    accepts_var_keyword = any(
        item.kind is inspect.Parameter.VAR_KEYWORD
        for item in signature.parameters.values()
    )
    if not accepts_parameter and not accepts_var_keyword:
        raise M0Error(
            "qualified vLLM adapter cannot pass the required kv_cache_dtype parameter"
        )
    if parameter in arguments:
        raise M0Error("caller must not pre-populate the adapter-owned KV-cache field")
    result = dict(arguments)
    result[parameter] = binding["bf16_value"]
    return result


def resolve_kv_cache_dtype(
    llm: Any, adapter_contract: Mapping[str, Any]
) -> dict[str, Any]:
    evidence = adapter_contract["resolved_kv_cache_evidence"]
    current: Any = llm
    for component in evidence["attribute_path"]:
        if isinstance(current, Mapping):
            if component not in current:
                raise M0Error(
                    f"resolved KV-cache evidence mapping lacks {component}"
                )
            current = current[component]
        else:
            try:
                current = getattr(current, component)
            except AttributeError as exc:
                raise M0Error(
                    f"resolved KV-cache evidence attribute lacks {component}"
                ) from exc
    raw_value = str(current)
    normalized = raw_value.casefold().removeprefix("torch.")
    if normalized == "bf16":
        normalized = "bfloat16"
    if normalized != evidence["expected_normalized_value"]:
        raise M0Error(
            f"resolved vLLM KV-cache dtype is not BF16: {raw_value}"
        )
    return {
        "method": evidence["method"],
        "attribute_path": evidence["attribute_path"],
        "raw_value": raw_value,
        "normalized_value": normalized,
    }
