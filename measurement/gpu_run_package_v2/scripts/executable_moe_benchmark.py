#!/usr/bin/env python3
"""Run the pinned tiny random Qwen2MoE as an M0 correctness pipeline smoke."""
from __future__ import annotations

import argparse
import collections
import gc
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from benchmark_quality import evaluate_choice, evaluate_gsm8k

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/executable_moe/m0_tiny_qwen2moe_v1.yaml"
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class JsonlWriter:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.handle = path.open("w", encoding="utf-8")
        self.counts: collections.Counter[str] = collections.Counter()

    def write(self, event: dict) -> None:
        self.handle.write(canonical_json(event) + "\n")
        self.handle.flush()
        self.counts[event["event"]] += 1

    def close(self) -> None:
        self.handle.close()


def model_snapshot_inventory(snapshot: Path) -> dict:
    files = []
    for path in sorted(snapshot.rglob("*")):
        if not path.is_file() or ".cache" in path.parts:
            continue
        relative = str(path.relative_to(snapshot))
        files.append({
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    aggregate = sha256_bytes(canonical_json([
        [item["path"], item["sha256"]] for item in files
    ]).encode("utf-8"))
    return {
        "schema_version": "model-snapshot-inventory-v1",
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "aggregate_sha256": aggregate,
        "files": files,
    }


def select_smoke_samples(manifest: Path, config: dict) -> dict[str, list[dict]]:
    rows = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    selected = {}
    for benchmark, spec in config["dataset"]["benchmarks"].items():
        candidates = [
            row for row in rows
            if row["split"] == config["dataset"]["split"]
            and row["task_id"] == spec["task_id"]
            and row["metadata"]["benchmark"] == benchmark
        ]
        candidates.sort(key=lambda row: row["sample_id"])
        expected = int(spec["measured_samples"])
        if len(candidates) != expected:
            raise ValueError(
                f"{benchmark} expected {expected} smoke samples, got {len(candidates)}"
            )
        registry_key = config["model"]["registry_key"]
        ineligible = [
            row["sample_id"] for row in candidates
            if registry_key not in row["enabled_models"]
        ]
        if ineligible:
            raise ValueError(
                f"{registry_key} is not suite-eligible for smoke samples: "
                f"{ineligible}"
            )
        selected[benchmark] = candidates
    return selected


def nvidia_smi_native() -> str:
    command = [
        "nvidia-smi", "--query-gpu=index,name,uuid,memory.total,memory.used,"
        "memory.free,driver_version,pstate,temperature.gpu,power.draw",
        "--format=csv",
    ]
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {type(exc).__name__}: {exc}\n"


def hardware_provenance(torch_mod, device: str) -> dict:
    result = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "device": device,
        "nvidia_smi": nvidia_smi_native(),
    }
    if device == "cuda":
        props = torch_mod.cuda.get_device_properties(0)
        free, total = torch_mod.cuda.mem_get_info(0)
        result["cuda"] = {
            "name": props.name,
            "capability": list(torch_mod.cuda.get_device_capability(0)),
            "total_memory_bytes": props.total_memory,
            "free_memory_bytes_at_provenance": free,
            "mem_get_info_total_bytes": total,
            "torch_cuda_runtime": torch_mod.version.cuda,
        }
    return result


def runtime_provenance(torch_mod, transformers_mod, datasets_mod) -> dict:
    return {
        "python": platform.python_version(),
        "torch": torch_mod.__version__,
        "transformers": transformers_mod.__version__,
        "datasets": datasets_mod.__version__,
        "cuda_runtime": torch_mod.version.cuda,
        "command": [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
        "environment": {
            "HF_HOME": os.environ.get("HF_HOME"),
            "HF_HUB_CACHE": os.environ.get("HF_HUB_CACHE"),
            "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE"),
            "HF_DATASETS_OFFLINE": os.environ.get("HF_DATASETS_OFFLINE"),
            "PYTHONPATH": os.environ.get("PYTHONPATH"),
        },
    }


def cross_pass_identity(config: dict, snapshot_hash: str) -> dict:
    generation = config["generation"]
    return {
        "model_id": config["model"]["repo_id"],
        "model_revision": config["model"]["revision"],
        "weights_revision": config["model"]["revision"],
        "tokenizer_revision": config["model"]["revision"],
        "snapshot_hash": snapshot_hash,
        "effective_generation_config_hash": sha256_bytes(
            canonical_json(generation).encode("utf-8")
        ),
    }


def seed_everything(torch_mod, seed: int, deterministic: bool) -> None:
    import random

    random.seed(seed)
    torch_mod.manual_seed(seed)
    if torch_mod.cuda.is_available():
        torch_mod.cuda.manual_seed_all(seed)
    if deterministic:
        torch_mod.use_deterministic_algorithms(True)
        if hasattr(torch_mod.backends, "cudnn"):
            torch_mod.backends.cudnn.benchmark = False


def load_model(config: dict, device: str):
    import torch
    from safetensors import safe_open
    from transformers import AutoModelForCausalLM, AutoTokenizer

    seed_everything(
        torch, int(config["runtime"]["seed"]),
        bool(config["runtime"]["deterministic_algorithms"]),
    )
    snapshot = ROOT / config["model"]["snapshot_path"]
    tokenizer = AutoTokenizer.from_pretrained(
        snapshot, local_files_only=True, trust_remote_code=False
    )
    model, loading_info = AutoModelForCausalLM.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=torch.float32,
        output_loading_info=True,
    )
    compatibility = config["model"]["fused_expert_compatibility"]
    remapped = []
    weights_path = snapshot / "model.safetensors"
    with safe_open(weights_path, framework="pt", device="cpu") as tensors:
        for layer_index, layer in enumerate(model.model.layers):
            prefix = f"model.layers.{layer_index}.mlp.experts"
            gate_up = tensors.get_tensor(f"{prefix}.gate_up_proj")
            down = tensors.get_tensor(f"{prefix}.down_proj")
            gate, up = gate_up.chunk(2, dim=1)
            if len(layer.mlp.experts) != gate.shape[0]:
                raise RuntimeError("fused expert count does not match runtime model")
            for expert_index, expert in enumerate(layer.mlp.experts):
                mappings = (
                    ("gate_proj", expert.gate_proj.weight, gate[expert_index]),
                    ("up_proj", expert.up_proj.weight, up[expert_index]),
                    ("down_proj", expert.down_proj.weight, down[expert_index]),
                )
                for projection, destination, source in mappings:
                    destination.data.copy_(source.to(destination.dtype))
                    exact = torch.equal(destination.detach().cpu(), source)
                    if compatibility["verify_exact_tensor_copy"] and not exact:
                        raise RuntimeError(
                            f"compatibility remap failed: layer={layer_index} "
                            f"expert={expert_index} projection={projection}"
                        )
                    remapped.append({
                        "layer": layer_index,
                        "expert": expert_index,
                        "projection": projection,
                        "shape": list(source.shape),
                        "exact_copy_verified": exact,
                    })
    loading_info["compatibility_remap"] = {
        "adapter_revision": compatibility["adapter_revision"],
        "source_file": str(weights_path.relative_to(ROOT)),
        "source_file_sha256": sha256_file(weights_path),
        "gate_up_order": compatibility["gate_up_order"],
        "tensor_count": len(remapped),
        "all_exact": all(item["exact_copy_verified"] for item in remapped),
        "tensors": remapped,
    }
    model.eval().to(device)
    return tokenizer, model, loading_info


def loading_summary(loading_info: dict) -> dict:
    keys = ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")
    result = {key: loading_info.get(key, []) for key in keys}
    remap = loading_info.get("compatibility_remap")
    result["raw_loader_fully_consumed"] = not any(result[key] for key in keys)
    result["compatibility_remap"] = remap
    expected_missing = 24
    expected_unexpected = 4
    remap_resolved = bool(
        remap
        and remap["all_exact"]
        and remap["tensor_count"] == expected_missing
        and len(result["missing_keys"]) == expected_missing
        and len(result["unexpected_keys"]) == expected_unexpected
        and not result["mismatched_keys"]
        and not result["error_msgs"]
    )
    result["checkpoint_fully_consumed_after_compatibility_remap"] = (
        result["raw_loader_fully_consumed"] or remap_resolved
    )
    result["interpretation"] = (
        "Transformers 4.53.2 reported fused/per-expert key differences; "
        "qwen2moe-fused-to-modulelist-v1 copied and exactly verified all 24 "
        "expert projection tensors. Evidence remains M0 pipeline/correctness only."
        if remap_resolved else
        "checkpoint compatibility remains unresolved; do not treat as complete load"
    )
    return result


class RouterCapture:
    def __init__(self, writer: JsonlWriter, config: dict):
        self.writer = writer
        self.top_k = int(config["model"]["expected_top_k"])
        self.num_experts = int(config["model"]["expected_num_experts"])
        self.handles = []
        self.context: dict[str, Any] = {}
        self.calls: collections.Counter[str] = collections.Counter()
        self.nan_count = 0
        self.inf_count = 0

    def set_request(self, request_id: str, input_tokens: int) -> None:
        self.context = {
            "request_id": request_id,
            "input_tokens": input_tokens,
        }
        self.calls.clear()

    def install(self, model) -> int:
        for name, module in model.named_modules():
            if name.endswith(".mlp.gate"):
                self.handles.append(module.register_forward_hook(
                    self._make_hook(name)
                ))
        return len(self.handles)

    def _make_hook(self, module_name: str):
        layer = int(module_name.split(".layers.")[1].split(".")[0])

        def hook(_module, inputs, output):
            import torch

            logits = output.detach().float().cpu()
            hidden = inputs[0]
            call_index = self.calls[module_name]
            self.calls[module_name] += 1
            phase = "prefill" if call_index == 0 else "decode"
            if phase == "prefill":
                positions = list(range(logits.shape[0]))
            else:
                positions = [
                    self.context["input_tokens"] + call_index - 1 + offset
                    for offset in range(logits.shape[0])
                ]
            scores = torch.softmax(logits, dim=-1)
            top_scores, top_experts = torch.topk(scores, self.top_k, dim=-1)
            reconstructed = collections.Counter(
                int(value) for value in top_experts.flatten().tolist()
            )
            nan_count = int(torch.isnan(logits).sum().item())
            inf_count = int(torch.isinf(logits).sum().item())
            self.nan_count += nan_count
            self.inf_count += inf_count
            shape_ok = (
                logits.ndim == 2
                and logits.shape[1] == self.num_experts
                and hidden.shape[-1] > 0
                and len(positions) == logits.shape[0]
            )
            self.writer.write({
                "event": "routing",
                "pass": "P2",
                "request_id": self.context["request_id"],
                "layer": layer,
                "module": module_name,
                "phase": phase,
                "call_index": call_index,
                "token_positions": positions,
                "input_shape": list(hidden.shape),
                "router_shape": list(logits.shape),
                "router_logits": logits.tolist(),
                "router_scores": scores.tolist(),
                "top_k_experts": top_experts.tolist(),
                "top_k_scores": top_scores.tolist(),
                "routing_semantics": "reconstructed_topk_from_gate_logits",
                "reconstructed_topk_from_gate_logits": {
                    "counts": {
                        str(expert): reconstructed.get(expert, 0)
                        for expert in range(self.num_experts)
                    },
                    "top_k": self.top_k,
                },
                "actual_dispatch_verified": False,
                "drop_overflow_unavailable": True,
                "reconstructed_topk_counts": {
                    str(expert): reconstructed.get(expert, 0)
                    for expert in range(self.num_experts)
                },
                "nan_count": nan_count,
                "inf_count": inf_count,
                "shape_sanity": {
                    "valid": shape_ok,
                    "expected_experts": self.num_experts,
                    "expected_top_k": self.top_k,
                },
            })
        return hook

    def remove(self) -> None:
        for handle in self.handles:
            handle.remove()
        self.handles.clear()


def quality_for(sample: dict, output_text: str) -> dict:
    benchmark = sample["metadata"]["benchmark"]
    if benchmark == "gsm8k":
        return evaluate_gsm8k(output_text, sample["reference"])
    if benchmark == "mmlu":
        return evaluate_choice(output_text, sample["reference"])
    raise ValueError(f"unsupported benchmark: {benchmark}")


def stop_reason(output_ids: list[int], eos_token_id: int | list[int] | None,
                max_new_tokens: int) -> str:
    eos_ids = (
        set(eos_token_id) if isinstance(eos_token_id, list)
        else ({eos_token_id} if eos_token_id is not None else set())
    )
    if output_ids and output_ids[-1] in eos_ids:
        return "eos_token"
    if len(output_ids) >= max_new_tokens:
        return "max_new_tokens"
    return "generation_ended_other"


def cuda_memory_state(torch_mod) -> dict:
    free, total = torch_mod.cuda.mem_get_info(0)
    return {
        "allocated_bytes": torch_mod.cuda.memory_allocated(0),
        "reserved_bytes": torch_mod.cuda.memory_reserved(0),
        "max_allocated_bytes": torch_mod.cuda.max_memory_allocated(0),
        "max_reserved_bytes": torch_mod.cuda.max_memory_reserved(0),
        "driver_free_bytes": free,
        "driver_total_bytes": total,
    }


def run_one_request(
    torch_mod, tokenizer, model, sample: dict, benchmark: str, run_kind: str,
    pass_id: str, generation: dict, writer: JsonlWriter,
    router: RouterCapture | None, identity: dict,
) -> dict:
    request_id = f"{benchmark}:{run_kind}:{sample['sample_id']}"
    seed_everything(torch_mod, int(generation["seed"]), True)
    encoded = tokenizer(sample["prompt"], return_tensors="pt")
    input_ids = encoded["input_ids"].to(model.device)
    attention_mask = encoded.get("attention_mask")
    if attention_mask is not None:
        attention_mask = attention_mask.to(model.device)
    if router:
        router.set_request(request_id, int(input_ids.shape[1]))
    before_memory = (
        cuda_memory_state(torch_mod) if pass_id == "P3" and model.device.type == "cuda"
        else None
    )
    if model.device.type == "cuda":
        torch_mod.cuda.synchronize()
    started_ns = time.perf_counter_ns()
    with torch_mod.inference_mode():
        generated = model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            do_sample=bool(generation["do_sample"]),
            max_new_tokens=int(generation["max_new_tokens"]),
            use_cache=bool(generation["use_cache"]),
            return_dict_in_generate=True,
            output_scores=True,
            pad_token_id=tokenizer.eos_token_id,
        )
    if model.device.type == "cuda":
        torch_mod.cuda.synchronize()
    latency_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
    full_ids = [int(value) for value in generated.sequences[0].detach().cpu().tolist()]
    input_tokens = int(input_ids.shape[1])
    output_ids = full_ids[input_tokens:]
    output_text = tokenizer.decode(output_ids, skip_special_tokens=True)
    score_nan = sum(
        int(torch_mod.isnan(score).sum().item()) for score in generated.scores
    )
    score_inf = sum(
        int(torch_mod.isinf(score).sum().item()) for score in generated.scores
    )
    after_memory = (
        cuda_memory_state(torch_mod) if pass_id == "P3" and model.device.type == "cuda"
        else None
    )
    result = {
        "event": "sample",
        "pass": pass_id,
        "request_id": request_id,
        "benchmark": benchmark,
        "run_kind": run_kind,
        "repetition": 0,
        "sample_id": sample["sample_id"],
        "prompt_hash": sample["prompt_hash"],
        "raw_sample_hash": sample["raw_sample_hash"],
        "model_id": identity["model_id"],
        "model_revision": identity["model_revision"],
        "weights_revision": identity["weights_revision"],
        "tokenizer_revision": identity["tokenizer_revision"],
        "snapshot_hash": identity["snapshot_hash"],
        "effective_generation_config_hash": identity[
            "effective_generation_config_hash"
        ],
        "input_token_ids": [int(value) for value in input_ids[0].detach().cpu().tolist()],
        "input_token_count": input_tokens,
        "output_token_ids": output_ids,
        "output_token_count": len(output_ids),
        "output_text": output_text,
        "output_hash": sha256_bytes(canonical_json(output_ids).encode("utf-8")),
        "stop_reason": stop_reason(
            output_ids, tokenizer.eos_token_id, int(generation["max_new_tokens"])
        ),
        "latency_ms": latency_ms,
        "latency_scope": (
            "P0 clean single-run vertical smoke; insufficient for statistics"
            if pass_id == "P0"
            else f"{pass_id} instrumented pass; never substitute for P0 latency"
        ),
        "quality": quality_for(sample, output_text),
        "nan_inf": {
            "generation_score_nan": score_nan,
            "generation_score_inf": score_inf,
            "finite": score_nan == 0 and score_inf == 0,
        },
    }
    if pass_id == "P3":
        result["memory_before"] = before_memory
        result["memory_after"] = after_memory
    writer.write(result)
    return result


def save_allocator_snapshot(torch_mod, path: Path) -> dict:
    snapshot = torch_mod.cuda.memory_snapshot()
    write_json(path, snapshot)
    return {
        "path": str(path.relative_to(ROOT)),
        "sha256": sha256_file(path),
        "segment_count": len(snapshot),
    }


def run_pass(
    pass_id: str, config: dict, samples: dict[str, list[dict]],
    output_root: Path, device: str, common_provenance: dict,
) -> tuple[list[dict], dict]:
    import torch

    pass_dir = output_root / pass_id.lower()
    pass_dir.mkdir(parents=True, exist_ok=False)
    native_path = pass_dir / "native.jsonl"
    writer = JsonlWriter(native_path)
    smi_before = nvidia_smi_native()
    (pass_dir / "nvidia-smi-before.txt").write_text(smi_before, encoding="utf-8")
    tokenizer, model, loading_info = load_model(config, device)
    loading = loading_summary(loading_info)
    hook_count_before = sum(len(module._forward_hooks) for module in model.modules())
    router = None
    if pass_id == "P2":
        router = RouterCapture(writer, config)
        installed = router.install(model)
        if installed != int(config["model"]["expected_num_layers"]):
            raise RuntimeError(
                f"expected {config['model']['expected_num_layers']} router hooks, "
                f"installed {installed}"
            )
    elif hook_count_before != 0:
        raise RuntimeError(f"{pass_id} must start without hooks")
    writer.write({
        "event": "pass_start",
        "pass": pass_id,
        "evidence_scope": config["evidence_scope"],
        "generation": config["generation"],
        "device": device,
        "hook_count_before": hook_count_before,
        "hook_count_installed": len(router.handles) if router else 0,
        "loading": loading,
        "provenance": common_provenance,
    })
    allocator_before = None
    if pass_id == "P3" and device == "cuda":
        torch.cuda.reset_peak_memory_stats(0)
        allocator_before = save_allocator_snapshot(
            torch, pass_dir / "allocator-before.json"
        )
    results = []
    for benchmark in sorted(samples):
        benchmark_samples = samples[benchmark]
        results.append(run_one_request(
            torch, tokenizer, model, benchmark_samples[0], benchmark, "warmup",
            pass_id, config["generation"], writer, router,
            common_provenance["cross_pass_identity"],
        ))
        for sample in benchmark_samples:
            results.append(run_one_request(
                torch, tokenizer, model, sample, benchmark, "measured",
                pass_id, config["generation"], writer, router,
                common_provenance["cross_pass_identity"],
            ))
    allocator_after = None
    memory_peak = None
    if pass_id == "P3" and device == "cuda":
        allocator_after = save_allocator_snapshot(
            torch, pass_dir / "allocator-after.json"
        )
        memory_peak = cuda_memory_state(torch)
    if router:
        router.remove()
    writer.write({
        "event": "pass_end",
        "pass": pass_id,
        "sample_event_count": len(results),
        "measured_sample_count": sum(
            result["run_kind"] == "measured" for result in results
        ),
        "routing_event_count": writer.counts["routing"],
        "routing_nan_count": router.nan_count if router else 0,
        "routing_inf_count": router.inf_count if router else 0,
        "allocator_before": allocator_before,
        "allocator_after": allocator_after,
        "memory_peak": memory_peak,
        "statistical_status": config["statistics"]["status"],
    })
    writer.close()
    del model
    del tokenizer
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
    smi_after = nvidia_smi_native()
    (pass_dir / "nvidia-smi-after.txt").write_text(smi_after, encoding="utf-8")
    summary = {
        "pass": pass_id,
        "native_jsonl": str(native_path.relative_to(ROOT)),
        "native_sha256": sha256_file(native_path),
        "event_counts": dict(writer.counts),
        "loading": loading,
    }
    return results, summary


def verify_cross_pass(results_by_pass: dict[str, list[dict]]) -> dict:
    identity_fields = [
        "input_token_ids",
        "prompt_hash",
        "sample_id",
        "model_id",
        "model_revision",
        "weights_revision",
        "tokenizer_revision",
        "snapshot_hash",
        "effective_generation_config_hash",
        "output_token_ids",
        "output_hash",
    ]
    baseline = {
        result["request_id"]: result
        for result in results_by_pass["P0"]
    }
    mismatches = []
    compared_passes = sorted(
        pass_id for pass_id in results_by_pass if pass_id != "P0"
    )
    for pass_id in compared_passes:
        compared = {
            result["request_id"]: result
            for result in results_by_pass[pass_id]
        }
        if set(compared) != set(baseline):
            mismatches.append({
                "pass": pass_id,
                "reason": "request_set_mismatch",
            })
            continue
        for request_id, expected in baseline.items():
            actual = compared[request_id]
            for field in identity_fields:
                if field not in expected or field not in actual:
                    mismatches.append({
                        "pass": pass_id,
                        "request_id": request_id,
                        "field": field,
                        "reason": "identity_field_missing",
                    })
                elif actual[field] != expected[field]:
                    mismatches.append({
                        "pass": pass_id,
                        "request_id": request_id,
                        "field": field,
                        "reason": "identity_mismatch",
                    })
    return {
        "status": "pass" if not mismatches else "fail",
        "compared_requests": len(baseline),
        "passes": ["P0", *compared_passes],
        "requirements": [f"identical_{field}" for field in identity_fields],
        "mismatches": mismatches,
    }


def native_sample_results(path: Path) -> list[dict]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    start = next(row for row in rows if row.get("event") == "pass_start")
    provenance = start["provenance"]
    model = provenance["model"]
    legacy_identity = {
        "model_id": model["repo_id"],
        "model_revision": model["revision"],
        "weights_revision": model["revision"],
        "tokenizer_revision": model["revision"],
        "snapshot_hash": model["snapshot_aggregate_sha256"],
        "effective_generation_config_hash": sha256_bytes(
            canonical_json(start["generation"]).encode("utf-8")
        ),
    }
    samples = [row for row in rows if row.get("event") == "sample"]
    for sample in samples:
        for field, value in legacy_identity.items():
            sample.setdefault(field, value)
    return samples


def release_model(torch_mod, model, tokenizer, device: str) -> None:
    del model
    del tokenizer
    gc.collect()
    if device == "cuda":
        torch_mod.cuda.empty_cache()
        torch_mod.cuda.synchronize()


def profiler_event_summary(events: list[Any]) -> dict:
    names = [str(event.name) for event in events]
    device_types = [str(event.device_type).lower() for event in events]
    return {
        "total_events": len(events),
        "cpu_events": sum("cpu" in value for value in device_types),
        "cuda_events": sum("cuda" in value for value in device_types),
        "cuda_api_events": sum(name.lower().startswith("cuda") for name in names),
        "kernel_events": sum(
            "cuda" in device and not name.lower().startswith("cuda")
            for name, device in zip(names, device_types)
        ),
        "stream_events": sum("stream" in name.lower() for name in names),
        "sync_events": sum(
            any(token in name.lower() for token in ("synchron", "event"))
            for name in names
        ),
    }


def run_p1_profiler(
    config: dict, samples: dict[str, list[dict]], output_root: Path,
    device: str, common_provenance: dict,
) -> tuple[list[dict], dict]:
    import torch

    pass_dir = output_root / "p1"
    pass_dir.mkdir(parents=True, exist_ok=False)
    writer = JsonlWriter(pass_dir / "native.jsonl")
    tokenizer, model, loading_info = load_model(config, device)
    loading = loading_summary(loading_info)
    hook_count = sum(len(module._forward_hooks) for module in model.modules())
    if hook_count:
        raise RuntimeError("P1 must run without model hooks")
    writer.write({
        "event": "pass_start",
        "pass": "P1",
        "device": device,
        "generation": config["generation"],
        "loading": loading,
        "provenance": common_provenance,
        "profiler": {
            "implementation": "torch.profiler",
            "torch_version": torch.__version__,
            "activities": ["CPU", "CUDA"],
            "record_shapes": True,
            "profile_memory": True,
            "with_stack": False,
            "overhead_label": "instrumented; never use as P0 latency",
        },
        "range_contract": "record_function_and_cuda_nvtx_per_request",
    })
    results = []
    traces = {}
    total_summary: collections.Counter[str] = collections.Counter()
    activities = [torch.profiler.ProfilerActivity.CPU]
    if device == "cuda":
        activities.append(torch.profiler.ProfilerActivity.CUDA)
    for benchmark in sorted(samples):
        trace_path = pass_dir / f"{benchmark}-chrome-trace.json"
        with torch.profiler.profile(
            activities=activities,
            record_shapes=True,
            profile_memory=True,
            with_stack=False,
        ) as profiler:
            requests = [
                ("warmup", samples[benchmark][0]),
                *(("measured", sample) for sample in samples[benchmark]),
            ]
            for run_kind, sample in requests:
                label = f"P1/{benchmark}/{run_kind}/{sample['sample_id']}"
                with torch.profiler.record_function(label):
                    if device == "cuda":
                        torch.cuda.nvtx.range_push(label)
                    try:
                        results.append(run_one_request(
                            torch, tokenizer, model, sample, benchmark, run_kind,
                            "P1", config["generation"], writer, None,
                            common_provenance["cross_pass_identity"],
                        ))
                    finally:
                        if device == "cuda":
                            torch.cuda.nvtx.range_pop()
                profiler.step()
        profiler.export_chrome_trace(str(trace_path))
        event_summary = profiler_event_summary(list(profiler.events()))
        total_summary.update(event_summary)
        traces[benchmark] = {
            "path": str(trace_path.relative_to(ROOT)),
            "sha256": sha256_file(trace_path),
            "bytes": trace_path.stat().st_size,
            "events": event_summary,
        }
    writer.write({
        "event": "pass_end",
        "pass": "P1",
        "sample_event_count": len(results),
        "measured_sample_count": sum(
            result["run_kind"] == "measured" for result in results
        ),
        "trace_files": traces,
        "events": dict(total_summary),
        "overhead_label": "instrumented; P1 latency is not P0 latency",
    })
    writer.close()
    release_model(torch, model, tokenizer, device)
    return results, {
        "pass": "P1",
        "status": "measured_instrumented",
        "native_jsonl": str((pass_dir / "native.jsonl").relative_to(ROOT)),
        "native_sha256": sha256_file(pass_dir / "native.jsonl"),
        "event_counts": dict(writer.counts),
        "profiler_events": dict(total_summary),
        "traces": traces,
        "overhead_label": "instrumented; never use as P0 latency",
    }


class TelemetrySampler:
    GPU_FIELDS = [
        "timestamp",
        "name",
        "uuid",
        "clocks.current.graphics",
        "clocks.current.memory",
        "power.draw",
        "temperature.gpu",
        "utilization.gpu",
        "utilization.memory",
        "clocks_throttle_reasons.active",
        "pcie.link.gen.current",
        "pcie.link.width.current",
        "memory.used",
        "memory.free",
    ]

    def __init__(self, gpu_csv: Path, system_jsonl: Path, interval_seconds: float):
        self.gpu_csv = gpu_csv
        self.system_jsonl = system_jsonl
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.sample_count = 0
        self.errors: list[str] = []
        self.started_ns: int | None = None
        self.finished_ns: int | None = None
        self.supported_fields: list[str] = []
        self.unsupported_fields: list[str] = []

    @staticmethod
    def _query(fields: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "nvidia-smi", f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )

    def probe(self) -> None:
        for field in self.GPU_FIELDS:
            result = self._query([field])
            if result.returncode == 0:
                self.supported_fields.append(field)
            else:
                self.unsupported_fields.append(field)

    def start(self) -> None:
        self.probe()
        self.gpu_csv.parent.mkdir(parents=True, exist_ok=True)
        self.gpu_csv.write_text(
            ",".join(self.supported_fields) + "\n", encoding="utf-8"
        )
        self.system_jsonl.write_text("", encoding="utf-8")
        self.started_ns = time.monotonic_ns()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            tick_ns = time.monotonic_ns()
            if self.supported_fields:
                result = self._query(self.supported_fields)
                if result.returncode == 0:
                    with self.gpu_csv.open("a", encoding="utf-8") as handle:
                        handle.write(result.stdout)
                else:
                    self.errors.append(result.stderr.strip())
            try:
                system = {
                    "monotonic_ns": tick_ns,
                    "wall_time_ns": time.time_ns(),
                    "proc_stat_raw": Path("/proc/stat").read_text(encoding="utf-8"),
                    "proc_meminfo_raw": Path("/proc/meminfo").read_text(
                        encoding="utf-8"
                    ),
                    "proc_self_stat_raw": Path("/proc/self/stat").read_text(
                        encoding="utf-8"
                    ),
                }
                with self.system_jsonl.open("a", encoding="utf-8") as handle:
                    handle.write(canonical_json(system) + "\n")
                self.sample_count += 1
            except OSError as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            elapsed = (time.monotonic_ns() - tick_ns) / 1_000_000_000.0
            self.stop_event.wait(max(0.0, self.interval_seconds - elapsed))
        self.finished_ns = time.monotonic_ns()

    def stop(self) -> dict:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        duration_seconds = (
            ((self.finished_ns or time.monotonic_ns()) - (self.started_ns or 0))
            / 1_000_000_000.0
        )
        sufficient = self.sample_count >= 20 and duration_seconds >= 5.0
        return {
            "interval_seconds": self.interval_seconds,
            "sample_count": self.sample_count,
            "duration_seconds": duration_seconds,
            "supported_fields": self.supported_fields,
            "unsupported_fields": self.unsupported_fields,
            "errors": self.errors,
            "sampling_sufficiency": (
                "sufficient" if sufficient
                else "insufficient_short_vertical_smoke"
            ),
            "minimum_contract": {"samples": 20, "duration_seconds": 5.0},
        }


def run_p5_telemetry(
    config: dict, samples: dict[str, list[dict]], output_root: Path,
    device: str, common_provenance: dict,
) -> tuple[list[dict], dict]:
    import torch

    pass_dir = output_root / "p5"
    pass_dir.mkdir(parents=True, exist_ok=False)
    writer = JsonlWriter(pass_dir / "native.jsonl")
    tokenizer, model, loading_info = load_model(config, device)
    loading = loading_summary(loading_info)
    if sum(len(module._forward_hooks) for module in model.modules()):
        raise RuntimeError("P5 must run without model hooks")
    sampler = TelemetrySampler(
        pass_dir / "nvidia-smi-telemetry.csv",
        pass_dir / "system-proc-telemetry.jsonl",
        interval_seconds=0.05,
    )
    writer.write({
        "event": "pass_start",
        "pass": "P5",
        "device": device,
        "generation": config["generation"],
        "loading": loading,
        "provenance": common_provenance,
        "telemetry_interval_seconds": sampler.interval_seconds,
        "overhead_label": "concurrent telemetry; never use as P0 latency",
    })
    results = []
    sampler.start()
    try:
        for benchmark in sorted(samples):
            benchmark_samples = samples[benchmark]
            results.append(run_one_request(
                torch, tokenizer, model, benchmark_samples[0], benchmark, "warmup",
                "P5", config["generation"], writer, None,
                common_provenance["cross_pass_identity"],
            ))
            for sample in benchmark_samples:
                results.append(run_one_request(
                    torch, tokenizer, model, sample, benchmark, "measured",
                    "P5", config["generation"], writer, None,
                    common_provenance["cross_pass_identity"],
                ))
    finally:
        telemetry = sampler.stop()
    writer.write({
        "event": "pass_end",
        "pass": "P5",
        "sample_event_count": len(results),
        "measured_sample_count": sum(
            result["run_kind"] == "measured" for result in results
        ),
        "telemetry": telemetry,
        "gpu_csv_sha256": sha256_file(sampler.gpu_csv),
        "system_jsonl_sha256": sha256_file(sampler.system_jsonl),
        "overhead_label": "concurrent telemetry; P5 latency is not P0 latency",
    })
    writer.close()
    release_model(torch, model, tokenizer, device)
    return results, {
        "pass": "P5",
        "status": "measured_instrumented",
        "native_jsonl": str((pass_dir / "native.jsonl").relative_to(ROOT)),
        "native_sha256": sha256_file(pass_dir / "native.jsonl"),
        "event_counts": dict(writer.counts),
        "telemetry": telemetry,
        "gpu_csv": {
            "path": str(sampler.gpu_csv.relative_to(ROOT)),
            "sha256": sha256_file(sampler.gpu_csv),
        },
        "system_jsonl": {
            "path": str(sampler.system_jsonl.relative_to(ROOT)),
            "sha256": sha256_file(sampler.system_jsonl),
        },
        "overhead_label": "concurrent telemetry; never use as P0 latency",
    }


def supplement_standard_vertical_slice(
    config: dict, output_root: Path, device: str, overwrite: bool,
) -> dict:
    import datasets
    import torch
    import transformers

    protected = {
        pass_id: output_root / pass_id.lower() / "native.jsonl"
        for pass_id in ("P0", "P2", "P3")
    }
    for pass_id, path in protected.items():
        if not path.is_file():
            raise RuntimeError(f"required existing {pass_id} raw missing: {path}")
    protected_hashes = {
        pass_id: sha256_file(path) for pass_id, path in protected.items()
    }
    supplement_paths = [
        output_root / name
        for name in ("p1", "p4", "p5", "p6")
    ]
    supplement_files = [
        output_root / "standard_vertical_slice_manifest.json",
    ]
    existing = [path for path in [*supplement_paths, *supplement_files] if path.exists()]
    if existing and not overwrite:
        raise RuntimeError(f"supplement artifacts already exist: {existing}")
    if overwrite:
        for path in supplement_paths:
            if path.is_dir():
                shutil.rmtree(path)
        for path in supplement_files:
            if path.is_file():
                path.unlink()
    manifest = ROOT / config["dataset"]["manifest"]
    if sha256_file(manifest) != config["dataset"]["manifest_sha256"]:
        raise RuntimeError("frozen sample manifest SHA-256 mismatch")
    selected = select_smoke_samples(manifest, config)
    snapshot_hash = model_snapshot_inventory(
        ROOT / config["model"]["snapshot_path"]
    )["aggregate_sha256"]
    mapping_path = output_root / "suite_class_mapping_v1.2.0.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    native_id_by_raw = {
        item["raw_sample_hash"]: item["native_sample_id"]
        for item in mapping["samples"]
    }
    for rows in selected.values():
        for row in rows:
            row["sample_id"] = native_id_by_raw[row["raw_sample_hash"]]
    common_provenance = {
        "schema_version": "m0-standard-supplement-provenance-v1",
        "model": config["model"],
        "dataset": {
            "manifest": config["dataset"]["manifest"],
            "manifest_sha256": config["dataset"]["manifest_sha256"],
            "suite_revision": config["dataset"]["suite_revision"],
            "suite_class_mapping": str(mapping_path.relative_to(ROOT)),
        },
        "runtime": runtime_provenance(torch, transformers, datasets),
        "hardware": hardware_provenance(torch, device),
        "cross_pass_identity": cross_pass_identity(config, snapshot_hash),
    }
    p1_results, p1_summary = run_p1_profiler(
        config, selected, output_root, device, common_provenance
    )
    p5_results, p5_summary = run_p5_telemetry(
        config, selected, output_root, device, common_provenance
    )
    ncu_path = shutil.which("ncu")
    p4_manifest = {
        "schema_version": "m0-pass-capability-v1",
        "pass": "P4",
        "status": "unsupported" if ncu_path is None else "not_run",
        "executed": False,
        "capability": {
            "nvidia_nsight_compute_path": ncu_path,
            "hardware_counters_collected": False,
        },
        "reason": (
            "Nsight Compute executable not available in isolated runtime"
            if ncu_path is None else
            "counter collection explicitly excluded from this supplement"
        ),
        "prohibit_fabricated_counters": True,
    }
    p6_manifest = {
        "schema_version": "m0-pass-capability-v1",
        "pass": "P6",
        "status": "optional_not_run",
        "executed": False,
        "reason": "no cycle/detailed simulator requested for M0 standard slice",
    }
    write_json(output_root / "p4/manifest.json", p4_manifest)
    write_json(output_root / "p6/manifest.json", p6_manifest)
    results_by_pass = {
        "P0": native_sample_results(protected["P0"]),
        "P1": p1_results,
        "P2": native_sample_results(protected["P2"]),
        "P3": native_sample_results(protected["P3"]),
        "P5": p5_results,
    }
    consistency = verify_cross_pass(results_by_pass)
    write_json(output_root / "cross_pass_consistency.json", consistency)
    final_protected_hashes = {
        pass_id: sha256_file(path) for pass_id, path in protected.items()
    }
    if final_protected_hashes != protected_hashes:
        raise RuntimeError("protected P0/P2/P3 raw artifacts changed")
    standard_manifest = {
        "schema_version": "m0-standard-vertical-slice-v1",
        "status": "completed" if consistency["status"] == "pass" else "failed",
        "device": device,
        "passes": {
            "P0": {"status": "existing_clean", "raw_sha256": protected_hashes["P0"]},
            "P1": p1_summary,
            "P2": {"status": "existing_routing", "raw_sha256": protected_hashes["P2"]},
            "P3": {"status": "existing_memory", "raw_sha256": protected_hashes["P3"]},
            "P4": p4_manifest,
            "P5": p5_summary,
            "P6": p6_manifest,
        },
        "cross_pass_consistency": consistency,
        "protected_raw_unchanged": True,
        "limitations": [
            "P1 profiler overhead invalidates P0 latency comparison",
            "P5 telemetry overhead invalidates P0 latency comparison",
            "short P5 duration is insufficient for formal telemetry statistics",
            "P4 hardware counters were not collected",
            "P6 optional detailed simulation was not run",
        ],
    }
    write_json(output_root / "standard_vertical_slice_manifest.json", standard_manifest)
    write_json(output_root / "checksums.json", artifact_checksums(output_root))
    if consistency["status"] != "pass":
        raise RuntimeError("P1/P5 cross-pass consistency failed")
    return standard_manifest


def artifact_checksums(output_root: Path) -> dict:
    files = []
    for path in sorted(output_root.rglob("*")):
        if path.is_file() and path.name != "checksums.json":
            files.append({
                "path": str(path.relative_to(output_root)),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    return {
        "schema_version": "m0-artifact-checksums-v1",
        "algorithm": "sha256",
        "files": files,
    }


def capacity_boundary_failure(
    output_root: Path, config: dict, device: str, run_mode: str,
) -> dict:
    preserved = artifact_checksums(output_root)["files"]
    manifest = {
        "schema_version": "capacity-boundary-failure-v1",
        "status": "capacity_boundary",
        "failure": "torch.cuda.OutOfMemoryError",
        "device": device,
        "run_mode": run_mode,
        "output_preserved": True,
        "preserved_partial_artifacts": preserved,
        "cpu_fallback_performed": False,
        "release_eligible": False,
        "model_id": config["model"]["repo_id"],
        "weights_revision": config["model"]["revision"],
        "suite_revision": config["dataset"]["suite_revision"],
    }
    write_json(output_root / "capacity_boundary_failure_manifest.json", manifest)
    return manifest


def execute(config: dict, output_root: Path, device: str) -> dict:
    import datasets
    import torch
    import transformers

    manifest = ROOT / config["dataset"]["manifest"]
    if sha256_file(manifest) != config["dataset"]["manifest_sha256"]:
        raise RuntimeError("frozen sample manifest SHA-256 mismatch")
    snapshot = ROOT / config["model"]["snapshot_path"]
    snapshot_info = model_snapshot_inventory(snapshot)
    write_json(output_root / "model_snapshot_inventory.json", {
        **snapshot_info,
        "repo_id": config["model"]["repo_id"],
        "revision": config["model"]["revision"],
        "snapshot_path": config["model"]["snapshot_path"],
    })
    selected = select_smoke_samples(manifest, config)
    common_provenance = {
        "schema_version": "m0-executable-provenance-v1",
        "model": {
            **config["model"],
            "snapshot_aggregate_sha256": snapshot_info["aggregate_sha256"],
        },
        "dataset": {
            "manifest": config["dataset"]["manifest"],
            "manifest_sha256": config["dataset"]["manifest_sha256"],
            "selected": {
                benchmark: [
                    {
                        "sample_id": row["sample_id"],
                        "raw_sample_hash": row["raw_sample_hash"],
                        "dataset_revision": row["source"]["dataset_revision"],
                    }
                    for row in rows
                ]
                for benchmark, rows in selected.items()
            },
        },
        "runtime": runtime_provenance(torch, transformers, datasets),
        "hardware": hardware_provenance(torch, device),
        "cross_pass_identity": cross_pass_identity(
            config, snapshot_info["aggregate_sha256"]
        ),
    }
    results_by_pass = {}
    summaries = {}
    for pass_id in ("P0", "P2", "P3"):
        results_by_pass[pass_id], summaries[pass_id] = run_pass(
            pass_id, config, selected, output_root, device, common_provenance
        )
    consistency = verify_cross_pass(results_by_pass)
    write_json(output_root / "cross_pass_consistency.json", consistency)
    if consistency["status"] != "pass":
        raise RuntimeError("cross-pass output consistency failed")
    p0_measured = [
        result for result in results_by_pass["P0"]
        if result["run_kind"] == "measured"
    ]
    invalid = [
        result["request_id"] for result in p0_measured
        if result["quality"]["validity"] is not True
    ]
    if invalid:
        failure = {
            "schema_version": "m0-executable-benchmark-run-v2",
            "status": "failed_invalid_output",
            "validity_gate": "hard_fail",
            "invalid_requests": invalid,
            "release_eligible": False,
            "evidence_scope": config["evidence_scope"],
        }
        write_json(output_root / "run_manifest.json", failure)
        raise RuntimeError(f"invalid benchmark outputs: {invalid}")
    benchmark_summary = {}
    for benchmark in sorted(selected):
        values = [
            result for result in p0_measured if result["benchmark"] == benchmark
        ]
        benchmark_summary[benchmark] = {
            "measured_samples": len(values),
            "latency_ms_vertical_smoke": [result["latency_ms"] for result in values],
            "quality_validity": sum(
                result["quality"]["validity"] for result in values
            ),
            "quality_correctness": sum(
                result["quality"]["correctness"] for result in values
            ),
            "quality_score": (
                sum(result["quality"]["correctness"] is True for result in values)
                / len(values)
            ),
            "nan_inf_free": all(result["nan_inf"]["finite"] for result in values),
            "dataset_revision": selected[benchmark][0]["source"]["dataset_revision"],
        }
    run_manifest = {
        "schema_version": "m0-executable-benchmark-run-v2",
        "status": "completed",
        "validity_gate": "pass",
        "quality_zero_allowed_for_pipeline_smoke": True,
        "release_eligible": False,
        "evidence_scope": config["evidence_scope"],
        "config_revision": config["config_revision"],
        "device": device,
        "sample_policy": {
            "warmups_per_benchmark": 1,
            "measured_per_benchmark": 4,
            "repetitions": 1,
            "statistical_status": config["statistics"]["status"],
        },
        "benchmark_summary": benchmark_summary,
        "pass_summaries": summaries,
        "cross_pass_consistency": consistency,
        "model_snapshot_aggregate_sha256": snapshot_info["aggregate_sha256"],
    }
    write_json(output_root / "run_manifest.json", run_manifest)
    write_json(output_root / "checksums.json", artifact_checksums(output_root))
    return run_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument(
        "--run-mode", choices=["local", "gpu", "paid"], default="gpu"
    )
    parser.add_argument("--local-cpu-fallback", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--supplement-standard", action="store_true")
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    output_root = (args.output or ROOT / config["output"]["root"]).resolve()
    import torch

    if args.local_cpu_fallback and args.run_mode != "local":
        raise SystemExit("--local-cpu-fallback is valid only with --run-mode local")
    device = args.device
    if device == "auto":
        if torch.cuda.is_available():
            device = "cuda"
        elif args.run_mode == "local" and args.local_cpu_fallback:
            device = "cpu"
        else:
            raise SystemExit(
                f"{args.run_mode} mode requires CUDA; CPU requires explicit "
                "--run-mode local --local-cpu-fallback"
            )
    if device == "cpu" and not (
        args.run_mode == "local" and args.local_cpu_fallback
    ):
        raise SystemExit(
            "CPU execution requires --run-mode local --local-cpu-fallback"
        )
    if args.supplement_standard:
        if not output_root.is_dir():
            raise SystemExit(f"existing M0 artifact root required: {output_root}")
        manifest = supplement_standard_vertical_slice(
            config, output_root, device, args.overwrite
        )
        print(json.dumps({
            "status": manifest["status"],
            "device": manifest["device"],
            "output": str(output_root),
            "cross_pass": manifest["cross_pass_consistency"]["status"],
            "mode": "supplement_standard",
        }, sort_keys=True))
        return 0
    if output_root.exists():
        if not args.overwrite:
            raise SystemExit(f"output exists; refusing overwrite: {output_root}")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    try:
        manifest = execute(config, output_root, device)
    except torch.cuda.OutOfMemoryError:
        failure = capacity_boundary_failure(
            output_root, config, device, args.run_mode
        )
        if not (
            device == "cuda"
            and args.run_mode == "local"
            and args.local_cpu_fallback
        ):
            print(json.dumps({
                "status": failure["status"],
                "device": device,
                "output": str(output_root),
                "failure_manifest": str(
                    output_root / "capacity_boundary_failure_manifest.json"
                ),
            }, sort_keys=True))
            return 2
        gc.collect()
        torch.cuda.empty_cache()
        cpu_output = output_root / "local_cpu_fallback"
        cpu_output.mkdir()
        cpu_manifest = execute(config, cpu_output, "cpu")
        cpu_manifest["status"] = "local_cpu_fallback_completed"
        cpu_manifest["release_eligible"] = False
        cpu_manifest["fallback"] = {
            "from": "cuda",
            "to": "cpu",
            "reason": "torch.cuda.OutOfMemoryError",
            "explicit_local_flag": True,
        }
        write_json(cpu_output / "run_manifest.json", cpu_manifest)
        failure["cpu_fallback_performed"] = True
        write_json(output_root / "capacity_boundary_failure_manifest.json", failure)
        manifest = {
            "schema_version": "m0-local-fallback-run-v1",
            "status": "capacity_boundary_with_local_cpu_fallback",
            "device": "cpu",
            "release_eligible": False,
            "gpu_failure_manifest": "capacity_boundary_failure_manifest.json",
            "cpu_fallback_output": "local_cpu_fallback",
            "cross_pass_consistency": cpu_manifest["cross_pass_consistency"],
        }
        write_json(output_root / "run_manifest.json", manifest)
    if device == "cpu" and manifest["status"] == "completed":
        manifest["status"] = "local_cpu_explicit_completed"
        manifest["release_eligible"] = False
        write_json(output_root / "run_manifest.json", manifest)
    print(json.dumps({
        "status": manifest["status"],
        "device": manifest["device"],
        "output": str(output_root),
        "cross_pass": manifest["cross_pass_consistency"]["status"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
