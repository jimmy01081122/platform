"""HF large-MoE routing trace -> canonical routing-v1 -> expanded expert-demand.

Source format (core12345/MoE_expert_selection_trace)
----------------------------------------------------
One JSON file == one query/request. Top-level is a list of generation *steps*:

    step[0]            -> prefill  (all prompt tokens)
    step[i], i>=1      -> decode   (1 token per step)

Each step is a dict keyed by layer id (string), whose value is a list of
per-token top-k expert selections::

    step = { "0": [[e0,e1,...,e_{k-1}], ...per token...], "1": [...], ... }

Expert ids are 0-based; num_experts is inferred as max_id+1 (a lower bound that
is corrected from the registered model config when available). This module emits
ONLY measured routing (which experts each token selected). No physical cost is
invented here; costs are attached downstream by workload expansion + a
registered platform cost model.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

ROUTING_SCHEMA_VERSION = "moe-routing-v1"
CONVERTER_VERSION = "moe-routing-converter/1.0.0"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class StepLayer:
    step_index: int
    phase: str
    layer_id: int
    selected_experts: list[list[int]]  # [num_tokens][top_k]

    @property
    def num_tokens(self) -> int:
        return len(self.selected_experts)

    @property
    def top_k(self) -> int:
        return len(self.selected_experts[0]) if self.selected_experts else 0


def parse_raw_trace(path: str | Path) -> Iterator[StepLayer]:
    """Yield StepLayer records from one raw query JSON, in (step, layer) order.

    Dense (non-MoE) layers appear as ``None`` in the source (e.g. DeepSeek-R1's
    first 3 layers, Llama-4's even layers). They carry no expert routing and are
    skipped: they produce no expert-demand and need no residency. Individual
    ``None`` token entries (rare) are also skipped.
    """
    with open(path) as f:
        steps = json.load(f)
    if not isinstance(steps, list):
        raise ValueError(f"unexpected top-level type in {path}: {type(steps).__name__}")
    for si, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"step {si} is not a dict in {path}")
        phase = "prefill" if si == 0 else "decode"
        for lk in sorted(step.keys(), key=lambda x: int(x)):
            sel = step[lk]
            if sel is None:            # dense layer: no routing
                continue
            norm = [[int(e) for e in tok] for tok in sel if tok is not None]
            if not norm:
                continue
            yield StepLayer(step_index=si, phase=phase, layer_id=int(lk), selected_experts=norm)


def infer_dims(path: str | Path) -> dict[str, int]:
    """Single pass to infer layer counts, top_k, num_experts, steps, phase counts."""
    moe_layers: set[int] = set()
    top_k = 0
    max_expert = -1
    n_steps = 0
    prefill_tokens = 0
    decode_step_ids: set[int] = set()
    for sl in parse_raw_trace(path):
        n_steps = max(n_steps, sl.step_index + 1)
        moe_layers.add(sl.layer_id)
        top_k = max(top_k, sl.top_k)
        for tok in sl.selected_experts:
            for e in tok:
                if e > max_expert:
                    max_expert = e
        if sl.step_index == 0:
            prefill_tokens = max(prefill_tokens, sl.num_tokens)
        else:
            decode_step_ids.add(sl.step_index)
    max_layer = max(moe_layers) if moe_layers else -1
    return {
        "num_layers": max_layer + 1,
        "num_moe_layers": len(moe_layers),
        "top_k": top_k,
        "num_experts_observed": max_expert + 1,
        "num_steps": n_steps,
        "prefill_tokens": prefill_tokens,
        "decode_steps": len(decode_step_ids),
    }


def to_canonical_records(
    path: str | Path,
    source_meta: dict[str, str],
    num_experts: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield moe-routing-v1 records for one query trace."""
    dims = infer_dims(path)
    ne = num_experts or dims["num_experts_observed"]
    request_id = source_meta["query_id"]
    for sl in parse_raw_trace(path):
        yield {
            "schema_version": ROUTING_SCHEMA_VERSION,
            "source": dict(source_meta),
            "request_id": request_id,
            "phase": sl.phase,
            "step_index": sl.step_index,
            "layer_id": sl.layer_id,
            "num_tokens": sl.num_tokens,
            "top_k": sl.top_k,
            "num_experts": ne,
            "selected_experts": sl.selected_experts,
            "router_scores": None,
            "provenance": "measured-routing-trace",
        }


# ---------------------------------------------------------------------------
# Workload expansion: routing-v1 -> expert_demand events (residency simulator)
# ---------------------------------------------------------------------------

def expand_to_expert_demand(
    records: list[dict[str, Any]],
    num_layers: int | None = None,
    scope: str = "per_layer_step",
) -> list[dict[str, Any]]:
    """Turn measured routing into per-(request, global layer_step) expert demand.

    assigned_tokens[e] = number of tokens (in this step,layer) that selected e.
    Global layer_step = step_index * num_layers + layer_id, giving a sequential
    inference order (step ascending, then layer ascending). Output is compatible
    with ``edgeflow.residency.demands_from_events``.
    """
    if num_layers is None:
        num_layers = 1 + max(r["layer_id"] for r in records)
    events: list[dict[str, Any]] = []
    for r in records:
        counts: Counter[int] = Counter()
        for tok in r["selected_experts"]:
            for e in tok:
                counts[e] += 1
        layer_step = r["step_index"] * num_layers + r["layer_id"]
        for e, tok in sorted(counts.items()):
            events.append({
                "request_id": r["request_id"],
                "name": "expert_demand",
                "attributes": {
                    "layer_step": layer_step,
                    "step_index": r["step_index"],
                    "layer_id": r["layer_id"],
                    "phase": r["phase"],
                    "expert_id": e,
                    "assigned_tokens": tok,
                },
            })
    return events


# ---------------------------------------------------------------------------
# Routing statistics (measured; assumption-free)
# ---------------------------------------------------------------------------

def routing_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute measured routing statistics over a set of routing-v1 records."""
    ne = records[0]["num_experts"] if records else 0
    freq: Counter[int] = Counter()
    total_selections = 0
    ws_prefill: list[int] = []   # unique experts per (step,layer) prefill
    ws_decode: list[int] = []
    per_layer_freq: dict[int, Counter] = defaultdict(Counter)
    tokens_prefill = 0
    tokens_decode = 0
    for r in records:
        uniq = set()
        for tok in r["selected_experts"]:
            for e in tok:
                freq[e] += 1
                per_layer_freq[r["layer_id"]][e] += 1
                total_selections += 1
                uniq.add(e)
        if r["phase"] == "prefill":
            ws_prefill.append(len(uniq))
            tokens_prefill += r["num_tokens"]
        else:
            ws_decode.append(len(uniq))
            tokens_decode += r["num_tokens"]

    def entropy(counter: Counter) -> float:
        tot = sum(counter.values())
        if tot == 0:
            return 0.0
        h = 0.0
        for v in counter.values():
            p = v / tot
            h -= p * math.log2(p)
        return h

    def mean(xs: list[int]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    active = len([e for e in range(ne) if freq[e] > 0])
    max_h = math.log2(ne) if ne > 1 else 1.0
    return {
        "num_experts": ne,
        "active_experts": active,
        "coverage_fraction": active / ne if ne else 0.0,
        "total_selections": total_selections,
        "expert_entropy_bits": entropy(freq),
        "expert_entropy_normalized": entropy(freq) / max_h if max_h else 0.0,
        "gini_load_imbalance": _gini([freq[e] for e in range(ne)]),
        "tokens_prefill": tokens_prefill,
        "tokens_decode": tokens_decode,
        "working_set_prefill_mean": mean(ws_prefill),
        "working_set_prefill_max": max(ws_prefill) if ws_prefill else 0,
        "working_set_decode_mean": mean(ws_decode),
        "working_set_decode_max": max(ws_decode) if ws_decode else 0,
        "top_experts": [e for e, _ in freq.most_common(16)],
        "expert_load": {str(e): freq[e] for e in range(ne)},
    }


def _gini(values: list[int]) -> float:
    xs = sorted(values)
    n = len(xs)
    s = sum(xs)
    if n == 0 or s == 0:
        return 0.0
    cum = 0
    for i, x in enumerate(xs, 1):
        cum += i * x
    return (2 * cum) / (n * s) - (n + 1) / n
