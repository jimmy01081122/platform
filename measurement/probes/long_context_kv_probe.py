#!/usr/bin/env python3
"""Long-context / offloaded-KV attention probe (TRACK_GPU_PREP priority 2).

This began as the largest preparation gap: the repository had no long-context
offload runner or config surface. P-020 subsequently source-audited vLLM 0.23's
native KV connector and froze 16-GiB canary / 140-GiB formal variants; the probe
still refuses live output unless worker-observed residency and transfer fields
are available.

What it measures (on a real GPU, later, via TRACK_GPU): across a sequence-length
sweep that MUST cross the point where the KV cache stops fitting in VRAM and is
forced to offload, it records TTFT, per-token decode latency, KV bytes moved and
when, and offload hit/miss.

Why the sweep must be wide (see contract target_2): every decode token of Mixtral
holds 128 KiB of KV (2,097,152 B block / 16 tokens, SWAP-K2). A 1M-token context
needs 128 GiB, above the 96 GB VRAM, so offload is *forced* somewhere in the
sweep. The exact knee depends on how much VRAM the weights leave free -- an
unmeasured quantity here -- so the sweep spans well below and well above every
plausible knee and the runner records the actual free budget so measurement,
not assumption, locates it.

CPU smoke test: ``--backend mock_longctx`` runs the full path with no GPU. Live
TRACK_GPU uses ``--backend vllm_longctx_offload_on`` plus explicit offload size,
backend, batching limit, and a worker-capable runtime adapter. The result is
stamped ``evidence = "cpu_smoke_test_not_measurement"``.

Hard rule (TRACK_GPU_PREP §8): on OOM the probe records the failure and stops;
it does NOT shorten the sequence or relax a threshold to "make it run" -- that is
an owner decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:  # allow running both as a module and as a script
    from measurement.probes import SCHEMA_LONGCTX_KV
    from measurement.probes.mock_backend import (
        BackendError,
        MockLongContextBackend,
        resolve_backend,
    )
    from measurement.probes.ir_evaluation_point import longctx_result_to_points
    from measurement.probes.vllm_backend import (
        LongContextRuntimeConfig,
        VllmLongContextBackend,
    )
except ImportError:  # pragma: no cover - script-relative fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes import SCHEMA_LONGCTX_KV
    from measurement.probes.mock_backend import (
        BackendError,
        MockLongContextBackend,
        resolve_backend,
    )
    from measurement.probes.ir_evaluation_point import longctx_result_to_points
    from measurement.probes.vllm_backend import (
        LongContextRuntimeConfig,
        VllmLongContextBackend,
    )

import hashlib


# Default sequence-length sweep: geometric, spanning from clearly-resident to
# clearly-offloaded regardless of the (unmeasured) weight-residency budget.
DEFAULT_SEQ_LENS = (4096, 16384, 65536, 131072, 262144, 524288, 1048576)

# Every value below is observed independently in each repeat.  The record-level
# value is the arithmetic mean of the corresponding ``*_repeats`` array; it is
# therefore the one and only primary value consumed by the IR adapter.
PRIMARY_MEAN_FIELDS = (
    "kv_resident_bytes",
    "kv_offloaded_bytes",
    "kv_offloaded_blocks",
    "ttft_ns",
    "decode_per_token_ns",
    "kv_move_ns",
    "kv_move_bytes",
)


def _arithmetic_mean(values: list[int]) -> float:
    """Return the unrounded arithmetic mean used by the output contract."""
    return sum(values) / len(values)


def _terminal_record(
    *,
    seq_len: int,
    repeat_index: int,
    repeats_expected: int,
    completed: list[dict[str, Any]],
    outcome: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build an auditable terminal record without discarding valid repeats."""
    terminal = dict(outcome or {})
    oom = bool(terminal.get("oom"))
    if outcome is None:
        text = error or "backend measurement failed"
        oom = "oom" in text.lower() or "out of memory" in text.lower()
    terminal.update({
        "seq_len": seq_len,
        "oom": oom,
        "measurement_failed": not oom,
        "error": terminal.get("error") or error or (
            "backend returned a terminal OOM result" if oom
            else "backend returned a terminal measurement failure"
        ),
        "failure_classification": terminal.get("failure_classification") or (
            "CUDA_OR_ENGINE_OOM" if oom else "BACKEND_MEASUREMENT_FAILURE"
        ),
        "stopped_sweep": True,
        "repeat_index": repeat_index,
        "repeats_expected": repeats_expected,
        "valid_repeats_completed": len(completed),
        # Partial successful repeats are still raw evidence and must not vanish
        # merely because a later repeat OOMed or lost instrumentation.
        "completed_repeat_measurements": [
            {**dict(row), "repeat_index": completed_index}
            for completed_index, row in enumerate(completed)
        ],
    })
    return terminal


def _aggregate_repeats(
    seq_len: int, repeats: list[dict[str, Any]]
) -> dict[str, Any]:
    """Keep every repeat and expose arithmetic means as primary values."""
    if not repeats:
        raise BackendError("cannot aggregate an empty long-context repeat set")
    for repeat_index, row in enumerate(repeats):
        if row.get("seq_len") != seq_len:
            raise BackendError(
                f"repeat {repeat_index} returned seq_len={row.get('seq_len')!r}; "
                f"expected {seq_len}"
            )
        if row.get("oom") or row.get("measurement_failed"):
            raise BackendError("terminal repeat reached successful aggregation")

    # Total bytes and total blocks are exact functions of seq_len, not noisy
    # measurements.  A disagreement across repeats is a backend contract error.
    for field in ("kv_total_bytes", "kv_blocks_total"):
        values = [row.get(field) for row in repeats]
        if any(value != values[0] for value in values[1:]):
            raise BackendError(f"{field} changed across repeats: {values!r}")

    record = dict(repeats[0])
    record.update({
        "seq_len": seq_len,
        "oom": False,
        "measurement_failed": False,
        "repeats": len(repeats),
        "primary_statistic": "arithmetic_mean",
        "primary_statistic_fields": list(PRIMARY_MEAN_FIELDS),
        "repeat_measurements": [
            {**dict(row), "repeat_index": repeat_index}
            for repeat_index, row in enumerate(repeats)
        ],
    })
    for field in PRIMARY_MEAN_FIELDS:
        values = [row[field] for row in repeats]
        record[f"{field}_repeats"] = values
        record[field] = _arithmetic_mean(values)

    engaged_repeats = [bool(row["offload_engaged"]) for row in repeats]
    record["offload_engaged_repeats"] = engaged_repeats
    record["offload_engaged"] = any(engaged_repeats)

    sources = [row.get("measurement_source") for row in repeats]
    if any(source is not None for source in sources):
        if not all(isinstance(source, dict) for source in sources):
            raise BackendError("measurement_source is missing from one or more repeats")
        source = dict(sources[0])
        source["worker_hook_observed_repeats"] = [
            item.get("worker_hook_observed") is True for item in sources
        ]
        source["worker_hook_observed"] = all(
            source["worker_hook_observed_repeats"]
        )
        source["primary_statistic"] = "arithmetic_mean"
        record["measurement_source"] = source
    return record


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", required=True,
                   help="backend name (mock_longctx for CPU smoke test)")
    p.add_argument("--seq-lens", default=",".join(str(s) for s in DEFAULT_SEQ_LENS),
                   help="comma-separated sequence lengths to sweep")
    p.add_argument("--kv-budget-bytes", type=int, default=None,
                   help="mock only: VRAM bytes available for KV before offload")
    p.add_argument("--out", required=True, help="output JSON path")
    p.add_argument("--repeats", type=int, default=3,
                   help="repeats per sequence length (frozen at 3)")
    p.add_argument("--model-path",
                   help="vllm_longctx_offload_on only: absolute pinned model path")
    p.add_argument("--runtime-adapter-module",
                   help="vllm_longctx_offload_on only: worker-capable adapter module")
    p.add_argument("--kv-offloading-size-gb", type=float,
                   help="vllm_longctx_offload_on only: P-020 value 16 or 140 GiB")
    p.add_argument("--kv-offloading-backend",
                   help="vllm_longctx_offload_on only: P-020 requires native")
    p.add_argument("--max-num-batched-tokens", type=int,
                   help="vllm_longctx_offload_on only: owner-recorded value > 0")
    p.add_argument(
        "--enable-prefix-caching",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=("vllm_longctx_offload_on only: explicit owner-recorded setting; "
              "use --enable-prefix-caching or --no-enable-prefix-caching"),
    )
    p.add_argument("--max-model-len", type=int, default=1048576,
                   help="vllm_longctx_offload_on engine maximum (must cover sweep)")
    p.add_argument("--pretty", action="store_true", help="indent output JSON")
    return p.parse_args(argv)


def _seq_lens(spec: str) -> list[int]:
    out: list[int] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        val = int(tok)
        if val <= 0:
            raise SystemExit(f"sequence length must be positive: {val}")
        out.append(val)
    if not out:
        raise SystemExit("no sequence lengths given")
    return out


def _build_backend(
    name: str,
    kv_budget_bytes: int | None,
    *,
    model_path: str | None = None,
    runtime_adapter_module: str | None = None,
    kv_offloading_size_gb: float | None = None,
    kv_offloading_backend: str | None = None,
    max_num_batched_tokens: int | None = None,
    enable_prefix_caching: bool | None = None,
    max_model_len: int = 1048576,
    requested_seq_lens: list[int] | None = None,
):
    cls = resolve_backend(name)
    if cls is MockLongContextBackend:
        if kv_budget_bytes is None:
            # default budget deliberately below the top of the sweep so the
            # smoke test exercises BOTH the resident and the offloaded branch.
            kv_budget_bytes = 8 * 1000**3
        return cls(kv_budget_bytes=kv_budget_bytes)
    if cls is VllmLongContextBackend:
        missing = [
            flag for flag, value in (
                ("--model-path", model_path),
                ("--runtime-adapter-module", runtime_adapter_module),
                ("--kv-offloading-size-gb", kv_offloading_size_gb),
                ("--kv-offloading-backend", kv_offloading_backend),
                ("--max-num-batched-tokens", max_num_batched_tokens),
                ("--enable-prefix-caching/--no-enable-prefix-caching",
                 enable_prefix_caching),
            ) if value is None
        ]
        if missing:
            raise BackendError(
                "vllm_longctx_offload_on requires explicit " + ", ".join(missing)
            )
        config = LongContextRuntimeConfig(
            model_path=str(model_path),
            kv_offloading_size_gb=float(kv_offloading_size_gb),
            kv_offloading_backend=str(kv_offloading_backend),
            max_num_batched_tokens=int(max_num_batched_tokens),
            enable_prefix_caching=bool(enable_prefix_caching),
            max_model_len=max_model_len,
        )
        return cls(
            config=config,
            runtime_adapter_module=str(runtime_adapter_module),
            requested_seq_lens=requested_seq_lens,
        )
    # Real GPU backend (or any other): not runnable in this pure-CPU track.
    raise BackendError(
        f"backend {name!r} is not runnable in TRACK_GPU_PREP; use mock_longctx "
        "for the CPU smoke test. Real long-context measurement belongs to TRACK_GPU."
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    seq_lens = _seq_lens(args.seq_lens)
    if args.repeats != 3:
        raise BackendError(
            "target_2 repeat count is frozen at 3; refusing to start runtime"
        )
    backend = _build_backend(
        args.backend,
        args.kv_budget_bytes,
        model_path=args.model_path,
        runtime_adapter_module=args.runtime_adapter_module,
        kv_offloading_size_gb=args.kv_offloading_size_gb,
        kv_offloading_backend=args.kv_offloading_backend,
        max_num_batched_tokens=args.max_num_batched_tokens,
        enable_prefix_caching=args.enable_prefix_caching,
        max_model_len=args.max_model_len,
        requested_seq_lens=seq_lens,
    )
    is_mock = isinstance(backend, MockLongContextBackend)

    records: list[dict[str, Any]] = []
    offload_knee_seq_len: int | None = None
    try:
        for seq_len in seq_lens:
            completed: list[dict[str, Any]] = []
            terminal: dict[str, Any] | None = None
            for repeat_index in range(args.repeats):
                try:
                    outcome = backend.measure(seq_len)
                except BackendError as exc:
                    terminal = _terminal_record(
                        seq_len=seq_len,
                        repeat_index=repeat_index,
                        repeats_expected=args.repeats,
                        completed=completed,
                        error=str(exc),
                    )
                    break
                if outcome.get("oom") or outcome.get("measurement_failed"):
                    terminal = _terminal_record(
                        seq_len=seq_len,
                        repeat_index=repeat_index,
                        repeats_expected=args.repeats,
                        completed=completed,
                        outcome=outcome,
                    )
                    break
                completed.append(outcome)

            if terminal is not None:
                records.append(terminal)
                break
            rec = _aggregate_repeats(seq_len, completed)
            records.append(rec)
            if offload_knee_seq_len is None and rec.get("offload_engaged"):
                offload_knee_seq_len = seq_len
    finally:
        closer = getattr(backend, "close", None)
        if callable(closer):
            closer()

    crossed_offload = any(
        r.get("offload_engaged") for r in records if not r.get("oom")
    )
    argv = _reconstruct_argv(args)
    runtime_variant_hash = hashlib.sha256(
        json.dumps({
            "backend": backend.name,
            "seq_lens": seq_lens,
            "repeats": args.repeats,
            "runtime_config": getattr(backend, "runtime_config", None),
        }, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = {
        "schema_version": SCHEMA_LONGCTX_KV,
        "target": "A6_long_context_offloaded_kv_attention",
        "backend": backend.name,
        "evidence": (
            "cpu_smoke_test_not_measurement" if is_mock else "measured"
        ),
        "argv": argv,
        "runtime_variant_hash": runtime_variant_hash,
        "runtime_variant": (
            "offload-on" if isinstance(backend, VllmLongContextBackend) else "mock"
        ),
        "runtime_identity": getattr(backend, "runtime_identity", None),
        "seq_lens_requested": seq_lens,
        "repeats": args.repeats,
        "primary_statistic": "arithmetic_mean",
        "primary_statistic_fields": list(PRIMARY_MEAN_FIELDS),
        "records": records,
        "sweep_crossed_offload_boundary": crossed_offload,
        "offload_knee_seq_len": offload_knee_seq_len,
    }
    # PREP-2: IR evaluation-point fields are now FIXED against STAGE_A2's
    # CalibrationIR schema. Each point is built from THIS result alone (no join
    # back to raw), directly carrying the operand-shape coordinate [seq_len].
    result["ir_evaluation_point_schema"] = "CalibrationIR"
    result["ir_evaluation_point_fields"] = "FILLED_PREP2"
    result["ir_evaluation_points"] = longctx_result_to_points(result)
    return result


def _reconstruct_argv(args: argparse.Namespace) -> list[str]:
    argv = ["--backend", args.backend, "--seq-lens", args.seq_lens,
            "--out", args.out, "--repeats", str(args.repeats)]
    if args.kv_budget_bytes is not None:
        argv += ["--kv-budget-bytes", str(args.kv_budget_bytes)]
    for flag, value in (
        ("--model-path", args.model_path),
        ("--runtime-adapter-module", args.runtime_adapter_module),
        ("--kv-offloading-size-gb", args.kv_offloading_size_gb),
        ("--kv-offloading-backend", args.kv_offloading_backend),
        ("--max-num-batched-tokens", args.max_num_batched_tokens),
    ):
        if value is not None:
            argv += [flag, str(value)]
    if args.enable_prefix_caching is not None:
        argv += [
            "--enable-prefix-caching" if args.enable_prefix_caching
            else "--no-enable-prefix-caching"
        ]
    if args.backend == "vllm_longctx_offload_on":
        argv += ["--max-model-len", str(args.max_model_len)]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, indent=2 if args.pretty else None, sort_keys=True)
        + "\n"
    )
    n = len([
        r for r in result["records"]
        if not r.get("oom") and not r.get("measurement_failed")
    ])
    print(
        f"long_context_kv_probe: backend={result['backend']} "
        f"evidence={result['evidence']} records={n} "
        f"crossed_offload={result['sweep_crossed_offload_boundary']} "
        f"knee={result['offload_knee_seq_len']} -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
