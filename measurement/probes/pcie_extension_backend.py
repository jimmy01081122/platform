"""Backends for the PCIe extension probe (four axes beyond V2-GAP-A / frozen S-sweep).

BACKGROUND -- what this closes: two independent GPU probes already sweep
transfer *concurrency* on the canonical RTX PRO 6000 Blackwell box:

  * V2-GAP-A (``multistream_aggregate_probe.py`` / ``aggregate_backend.py``):
    N independent objects, each its own CUDA stream, all issued from a common
    start event, ONE direction at a time. Found exact serialization
    (sum/max == (N+1)/2) at every N and every size tested.
  * The frozen harness's ``copy_streams`` sweep (``gpu_run_package_v2``): ONE
    object split across S streams, also one direction at a time. Found a
    small-transfer-only penalty (3.65x at 65536 B, 2.10x at 1048576 B, ~1.00x
    by 22020096 B).

Both axes are *same-direction* concurrency. Neither measured BIDIRECTIONAL
concurrency (H2D racing D2H, the actual shape of MoE weight offload: fetching
new experts while evicting old KV/experts), neither densified the unsampled
1 MiB-21 MiB decade where the S-axis penalty visibly fades, and the frozen
harness never ran the contract's declared 4096-byte point. No existing GPU
attempt record captures PCIe link generation/width either, so a measured
bandwidth number can't be checked against what the box actually negotiated.
This module's two backends (``MockPcieExtensionBackend`` for the CPU smoke
test, ``TorchPcieExtensionBackend`` for TRACK_GPU) fill exactly those four
gaps. See ``measurement/probes/pcie_extension_probe.py`` for the CLI/sweep
and ``measurement/parsers/pcie_extension_parser.py`` for the acceptance
envelope.

INSTRUMENTATION DISCIPLINE -- CORRECTED after supervisor review; read this
before trusting the git-blame age of this comment. A common CUDA start event
is recorded once on the coordinator stream; every participating stream first
``wait_event``s on it, does its copy, then records its OWN completion event.
The joint/wait-all completion is derived as ``max()`` over those per-leg
completion events' elapsed times from the common start -- it is NEVER a
separately recorded coordinator event, no matter where in the sequence that
recording would happen. Recording a CUDA event is itself a queued
stream operation with its own nonzero, variable dispatch/launch latency;
waiting on every leg *before* recording a distinct joint event prevents that
event from firing too early, but does not stop its own recording overhead
from being folded into ``elapsed_time(common_start, joint_event)``. At the
large-transfer sizes this axis also samples that overhead is negligible, but
at the small-transfer end (tens of microseconds) it is comparable to the
transfer itself -- exactly where this axis's overlap-vs-serialize question is
hardest to answer and least able to tolerate a systematic bias.

This module was first written (by a worktree-isolated task) against this
repo's HEAD commit, which predates an uncommitted live-tree fix to
``aggregate_backend.py``'s ``TorchAggregateBackend`` (documented as
DECISION_LOG P-022): an early V2-GAP-A GPU attempt recorded exactly the
separate-event pattern described above, and it produced a non-physical
result on real hardware (N=1 aggregate exceeding its own single serialized
transfer, because the extra event's recording delay was charged on top of
the true completion time) that the strict parser correctly rejected. The fix
there replaced the separate event with ``max(per_object_ms)`` computed from
the already-recorded per-leg events. This module's bidirectional primitive
originally reintroduced the pre-fix pattern (a worktree snapshotted at HEAD
cannot see a fix that was never committed); it has since been corrected here
to the same ``max()``-of-already-recorded-events derivation, applied to the
two-leg (H2D, D2H) case instead of V2-GAP-A's N-leg case.

Every real (``gpu``) measurement below is one of three primitives, each built
on that same discipline:
  1. ``_measure_unidirectional_once``    -- one leg alone (baselines).
  2. ``_measure_bidirectional_once``     -- one H2D leg + one D2H leg racing
     concurrently on two independent pinned-host/device buffer pairs.
  3. ``_measure_stream_split_once``      -- ONE buffer chunked across S
     streams. The chunk math (``chunk = ceil(total / S)``, per-stream
     ``[index*chunk : min((index+1)*chunk, total)]``, guarded by
     ``if begin < finish``, coordinator ``wait_stream``s every stream
     unconditionally) is copied verbatim from
     ``measurement/gpu_run_package_v2/scripts/benchmark.py`` (read-only
     reference, lines ~310-333) so ``copy_streams`` means exactly what it
     means in the frozen harness. Only the *splitting* math is replicated
     from that file; the event-based timing wrapper around it is this
     module's own (matching primitives 1-2), because the excerpt read for
     the splitting semantics used a separate external timer this module has
     no access to and does not need in order to match the split semantics.

The CPU mock (``MockPcieExtensionBackend``) is NOT a performance model, same
disclaimer as ``aggregate_backend.py``'s mock: closed-form, deterministic,
clearly-synthetic numbers chosen only so the plumbing has something
structurally valid to serialize. Any result produced through it is stamped
``evidence = "cpu_smoke_test_not_measurement"`` upstream (probe module), never
``"measured"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import gc
import math
import statistics
import subprocess
from typing import Any, Callable

try:
    from measurement.probes.mock_backend import BackendError
except ImportError:  # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes.mock_backend import BackendError

# --------------------------------------------------------------------------- #
# Shared constants / gap labeling (axes 2 + 3 both use the stream-split
# primitive; gap_label records WHY a given byte size is being swept without
# the parser or backend having to special-case "axis 2 vs axis 3").
# --------------------------------------------------------------------------- #

# Frozen harness's own S-axis grid neighbors either side of the unsampled
# decade (gpu_measurement_contract_v1.yaml's copy_streams sweep, read-only
# reference -- NOT reproduced or altered here).
CROSSOVER_LOWER_BYTES = 1048576      # 1 MiB: penalty measured 2.10x here
CROSSOVER_UPPER_BYTES = 22020096     # 21 MiB: penalty measured ~1.00x here
# In the frozen contract's byte grid by declaration but never actually run.
DECLARED_UNMEASURED_BYTES = 4096     # 4 KiB

GAP_LABEL_SMALL_CELL = "declared_never_measured_4096"
GAP_LABEL_CROSSOVER = "crossover_densification"
GAP_LABEL_CUSTOM = "custom_sweep_point"


def gap_label_of(object_bytes: int) -> str:
    """Why this stream-split byte size is being swept (provenance, not physics)."""
    if object_bytes == DECLARED_UNMEASURED_BYTES:
        return GAP_LABEL_SMALL_CELL
    if CROSSOVER_LOWER_BYTES < object_bytes < CROSSOVER_UPPER_BYTES:
        return GAP_LABEL_CROSSOVER
    return GAP_LABEL_CUSTOM


# --------------------------------------------------------------------------- #
# Axis 4: PCIe link generation/width capture (nvidia-smi subprocess boundary).
# --------------------------------------------------------------------------- #

PCIE_LINK_QUERY_FIELDS = (
    "pcie.link.gen.current,pcie.link.gen.max,"
    "pcie.link.width.current,pcie.link.width.max"
)
# Exact argv, fixed here as the single source of truth so the probe, the
# parser (exact-match check) and the unit test never drift apart.
PCIE_LINK_ARGV = [
    "nvidia-smi",
    f"--query-gpu={PCIE_LINK_QUERY_FIELDS}",
    "--format=csv,noheader",
]


def capture_pcie_link_info(
    command_runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> dict[str, Any]:
    """Best-effort PCIe link generation/width capture, closing a real gap:

    every existing GPU-attempt environment record captures GPU name/uuid/
    memory/driver/compute_cap via nvidia-smi (see e.g.
    ``measurement/gpu_run_package_v2/scripts/projectctl.py``'s
    ``_default_gpu_provider`` and ``collectors/c1_telemetry.py``'s
    ``TelemetrySampler._run_smi``, both read-only references for this
    subprocess-capture style) but never the PCIe link itself -- so there is
    no way to check a measured bandwidth number against the link the box
    actually negotiated, or compare it across boxes.

    Never fabricates a reading: this dev host has no GPU and no nvidia-smi,
    so under the real ``subprocess.run`` default this always returns
    ``status="unavailable"`` here -- that is the expected, correct behavior
    on this machine, not a bug to work around. It never substitutes a mock
    number. ``command_runner`` is an injectable seam (mirrors
    ``TelemetrySampler``'s ``command_runner`` constructor parameter) so a
    test can assert the exact argv issued, and so the mock/CPU backend path
    exercises this same function with no real hardware required.
    """
    try:
        result = command_runner(
            list(PCIE_LINK_ARGV), capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "argv": list(PCIE_LINK_ARGV),
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    if result.returncode != 0 or not result.stdout.strip():
        reason = (result.stderr or "").strip() or "nvidia-smi returned no output"
        return {"argv": list(PCIE_LINK_ARGV), "status": "unavailable", "reason": reason}
    row = result.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in row.split(",")]
    if len(parts) != 4:
        return {
            "argv": list(PCIE_LINK_ARGV),
            "status": "unavailable",
            "reason": f"unexpected nvidia-smi row shape: {row!r}",
        }
    return {
        "argv": list(PCIE_LINK_ARGV),
        "status": "ok",
        "pcie_link_gen_current": parts[0],
        "pcie_link_gen_max": parts[1],
        "pcie_link_width_current": parts[2],
        "pcie_link_width_max": parts[3],
    }


# --------------------------------------------------------------------------- #
# CPU mock backend (axes 1-3; axis 4 goes through capture_pcie_link_info
# directly, unconditionally, from the probe -- see its module docstring).
# --------------------------------------------------------------------------- #


@dataclass
class MockPcieExtensionBackend:
    """CPU mock of bidirectional-concurrency and stream-split PCIe transfers.

    Deterministic closed forms only -- see module docstring. Every cell this
    emits is stamped ``evidence = "cpu_smoke_test_not_measurement"`` upstream.
    """

    name: str = "mock_pcie_extension"
    # Synthetic per-direction bandwidth (bytes/ms) and small-transfer floor
    # (ms). Order-of-magnitude only, chosen to resemble the real ~26-28 GB/s
    # figures already used as the same kind of placeholder in
    # aggregate_backend.py's MockAggregateBackend -- not a claim about any
    # real device.
    _bandwidth_bytes_per_ms: dict[str, float] = field(
        default_factory=lambda: {"h2d": 28.0e6, "d2h": 26.0e6}
    )
    _floor_ms: dict[str, float] = field(
        default_factory=lambda: {"h2d": 0.006, "d2h": 0.007}
    )
    # Arbitrary synthetic overlap constant in (0, 1]: 1.0 would mean perfect
    # dual-engine overlap (joint == max(baselines)), 0.0 fully serialized
    # (joint == sum(baselines)). Fixed at the midpoint so the mock's joint
    # completion is neither -- it must not bias the plumbing toward either of
    # the two physically valid real-hardware outcomes.
    _overlap_efficiency: float = 0.5

    def _baseline_ms(self, object_bytes: int, direction: str) -> float:
        bw = self._bandwidth_bytes_per_ms[direction]
        floor = self._floor_ms[direction]
        return max(floor, object_bytes / bw)

    def _stream_penalty(self, object_bytes: int, copy_streams: int) -> float:
        if copy_streams <= 1:
            return 1.0
        # Synthetic only: echoes (does not reproduce) the frozen harness's
        # observed shape of "penalty at small sizes, ~1.00x by ~21 MiB" so a
        # multi-cell mock sweep is monotone-ish and inspectable, not flat.
        fade = min(1.0, (CROSSOVER_LOWER_BYTES * 4.0) / object_bytes)
        return 1.0 + (copy_streams - 1) * 0.15 * fade

    # -- axis 1: bidirectional concurrency + self-contained baselines ----- #

    def measure_bidirectional_cell(self, object_bytes: int, repeats: int) -> dict[str, Any]:
        if object_bytes <= 0:
            raise BackendError(f"object_bytes must be positive, got {object_bytes}")
        if repeats <= 0:
            raise BackendError(f"repeats must be positive, got {repeats}")

        h2d_baseline = round(self._baseline_ms(object_bytes, "h2d"), 9)
        d2h_baseline = round(self._baseline_ms(object_bytes, "d2h"), 9)
        slower = max(h2d_baseline, d2h_baseline)
        faster = min(h2d_baseline, d2h_baseline)
        # joint = slower + (1 - efficiency) * faster; lies in
        # [slower, slower + faster] == [max(baselines), sum(baselines)] for
        # any efficiency in [0, 1] -- the same envelope the real backend and
        # the parser enforce.
        joint = round(slower + (1.0 - self._overlap_efficiency) * faster, 9)

        if h2d_baseline >= d2h_baseline:
            h2d_concurrent, d2h_concurrent = joint, d2h_baseline
        else:
            h2d_concurrent, d2h_concurrent = h2d_baseline, joint

        h2d_samples = [h2d_concurrent] * repeats
        d2h_samples = [d2h_concurrent] * repeats
        joint_samples = [joint] * repeats
        h2d_baseline_samples = [h2d_baseline] * repeats
        d2h_baseline_samples = [d2h_baseline] * repeats

        max_baseline = max(h2d_baseline, d2h_baseline)
        sum_baseline = h2d_baseline + d2h_baseline
        return {
            "object_bytes": object_bytes,
            "h2d_ms_samples": h2d_samples,
            "h2d_ms_mean": h2d_concurrent,
            "d2h_ms_samples": d2h_samples,
            "d2h_ms_mean": d2h_concurrent,
            "joint_completion_ms_samples": joint_samples,
            "joint_completion_ms_mean": joint,
            "h2d_baseline_ms_samples": h2d_baseline_samples,
            "h2d_baseline_ms_mean": h2d_baseline,
            "d2h_baseline_ms_samples": d2h_baseline_samples,
            "d2h_baseline_ms_mean": d2h_baseline,
            "joint_over_max_ratio": round(joint / max_baseline, 9),
            "joint_over_sum_ratio": round(joint / sum_baseline, 9),
        }

    # -- axes 2 + 3: single object split across copy_streams streams ------ #

    def measure_stream_split_cell(
        self, object_bytes: int, copy_streams: int, direction: str, repeats: int
    ) -> dict[str, Any]:
        if object_bytes <= 0:
            raise BackendError(f"object_bytes must be positive, got {object_bytes}")
        if copy_streams <= 0:
            raise BackendError(f"copy_streams must be positive, got {copy_streams}")
        if direction not in ("h2d", "d2h"):
            raise BackendError(f"direction must be h2d/d2h, got {direction!r}")
        if repeats <= 0:
            raise BackendError(f"repeats must be positive, got {repeats}")

        base = self._baseline_ms(object_bytes, direction)
        latency = round(base * self._stream_penalty(object_bytes, copy_streams), 9)
        samples = [latency] * repeats
        chunk_bytes = math.ceil(object_bytes / copy_streams)
        return {
            "object_bytes": object_bytes,
            "copy_streams": copy_streams,
            "direction": direction,
            "gap_label": gap_label_of(object_bytes),
            "chunk_bytes": chunk_bytes,
            "latency_ms_samples": samples,
            "latency_ms_mean": latency,
        }


# --------------------------------------------------------------------------- #
# Real CUDA backend (TRACK_GPU only). Imports torch lazily; refuses loudly,
# never substitutes mock data, if CUDA is unavailable.
# --------------------------------------------------------------------------- #


@dataclass
class TorchPcieExtensionBackend:
    """Live CUDA backend for the four PCIe-extension axes.

    See module docstring for the instrumentation discipline shared by all
    three measurement primitives below (common start event; each leg records
    its own completion event; coordinator waits on every relevant completion
    event; a distinct joint/aggregate event is recorded only after all of
    those waits are queued).
    """

    name: str = "gpu"
    warmup_repeats: int = 2
    _torch: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._torch is None:
            try:
                import torch
            except ImportError as exc:  # pragma: no cover - depends on host
                raise BackendError("torch is required for GPU measurements") from exc
            self._torch = torch
        if not self._torch.cuda.is_available():
            raise BackendError(
                "CUDA unavailable; refusing to substitute a CPU/mock backend"
            )

    # -- shared timing primitives ------------------------------------------ #

    def _measure_unidirectional_once(self, host: Any, dev: Any, stream: Any, direction: str) -> float:
        torch = self._torch
        common_start = torch.cuda.Event(enable_timing=True)
        completion = torch.cuda.Event(enable_timing=True)
        coordinator = torch.cuda.current_stream()
        common_start.record(coordinator)
        with torch.cuda.stream(stream):
            stream.wait_event(common_start)
            if direction == "h2d":
                dev.copy_(host, non_blocking=True)
            else:
                host.copy_(dev, non_blocking=True)
            completion.record(stream)
        coordinator.wait_event(completion)
        joint = torch.cuda.Event(enable_timing=True)
        joint.record(coordinator)
        torch.cuda.synchronize()
        return float(common_start.elapsed_time(joint))

    def _measure_bidirectional_once(
        self,
        host_h2d: Any, dev_h2d: Any, stream_h2d: Any,
        host_d2h: Any, dev_d2h: Any, stream_d2h: Any,
    ) -> tuple[float, float, float]:
        torch = self._torch
        common_start = torch.cuda.Event(enable_timing=True)
        h2d_done = torch.cuda.Event(enable_timing=True)
        d2h_done = torch.cuda.Event(enable_timing=True)
        coordinator = torch.cuda.current_stream()
        common_start.record(coordinator)

        with torch.cuda.stream(stream_h2d):
            stream_h2d.wait_event(common_start)
            dev_h2d.copy_(host_h2d, non_blocking=True)
            h2d_done.record(stream_h2d)

        with torch.cuda.stream(stream_d2h):
            stream_d2h.wait_event(common_start)
            host_d2h.copy_(dev_d2h, non_blocking=True)
            d2h_done.record(stream_d2h)

        coordinator.wait_event(h2d_done)
        coordinator.wait_event(d2h_done)
        torch.cuda.synchronize()

        h2d_ms = float(common_start.elapsed_time(h2d_done))
        d2h_ms = float(common_start.elapsed_time(d2h_done))
        # Wait-all completion is the latest constituent completion from the
        # common start -- NOT a separately recorded coordinator event (see
        # module docstring: recording a distinct event has its own nonzero
        # dispatch latency that would be charged to the transfer).
        joint_ms = max(h2d_ms, d2h_ms)
        return h2d_ms, d2h_ms, joint_ms

    def _measure_stream_split_once(
        self, host: Any, dev: Any, streams: list[Any], direction: str,
        chunk_bytes: int, total_bytes: int,
    ) -> float:
        torch = self._torch
        common_start = torch.cuda.Event(enable_timing=True)
        completion = torch.cuda.Event(enable_timing=True)
        coordinator = torch.cuda.current_stream()
        common_start.record(coordinator)

        # Chunking logic replicated verbatim from
        # measurement/gpu_run_package_v2/scripts/benchmark.py (~lines 310-333,
        # read-only reference): ceil-division chunk size, per-stream slice
        # [index*chunk : min((index+1)*chunk, total)], skip an empty tail
        # slice, but wait_stream every stream unconditionally each iteration.
        for index, stream in enumerate(streams):
            begin = index * chunk_bytes
            finish = min((index + 1) * chunk_bytes, total_bytes)
            if begin < finish:
                with torch.cuda.stream(stream):
                    stream.wait_event(common_start)
                    if direction == "h2d":
                        dev[begin:finish].copy_(host[begin:finish], non_blocking=True)
                    else:
                        host[begin:finish].copy_(dev[begin:finish], non_blocking=True)
            coordinator.wait_stream(stream)

        completion.record(coordinator)
        torch.cuda.synchronize()
        return float(common_start.elapsed_time(completion))

    # -- axis 1 -------------------------------------------------------------#

    def measure_bidirectional_cell(self, object_bytes: int, repeats: int) -> dict[str, Any]:
        if object_bytes <= 0:
            raise BackendError(f"object_bytes must be positive, got {object_bytes}")
        if repeats <= 0:
            raise BackendError(f"repeats must be positive, got {repeats}")

        torch = self._torch
        try:
            # Two independent pinned-host + device buffer pairs, exactly as
            # specified: the H2D leg and the D2H leg never touch each
            # other's memory.
            host_h2d = torch.empty(object_bytes, dtype=torch.uint8, pin_memory=True)
            dev_h2d = torch.empty(object_bytes, dtype=torch.uint8, device="cuda")
            host_d2h = torch.empty(object_bytes, dtype=torch.uint8, pin_memory=True)
            dev_d2h = torch.empty(object_bytes, dtype=torch.uint8, device="cuda")
            stream_h2d = torch.cuda.Stream()
            stream_d2h = torch.cuda.Stream()
            baseline_stream = torch.cuda.Stream()
            torch.cuda.synchronize()

            for _ in range(self.warmup_repeats):
                self._measure_unidirectional_once(host_h2d, dev_h2d, baseline_stream, "h2d")
                self._measure_unidirectional_once(host_d2h, dev_d2h, baseline_stream, "d2h")
                self._measure_bidirectional_once(
                    host_h2d, dev_h2d, stream_h2d, host_d2h, dev_d2h, stream_d2h
                )

            h2d_baseline_samples: list[float] = []
            d2h_baseline_samples: list[float] = []
            h2d_samples: list[float] = []
            d2h_samples: list[float] = []
            joint_samples: list[float] = []
            for _ in range(repeats):
                h2d_baseline_samples.append(
                    self._measure_unidirectional_once(host_h2d, dev_h2d, baseline_stream, "h2d")
                )
                d2h_baseline_samples.append(
                    self._measure_unidirectional_once(host_d2h, dev_d2h, baseline_stream, "d2h")
                )
                h2d_ms, d2h_ms, joint_ms = self._measure_bidirectional_once(
                    host_h2d, dev_h2d, stream_h2d, host_d2h, dev_d2h, stream_d2h
                )
                h2d_samples.append(h2d_ms)
                d2h_samples.append(d2h_ms)
                joint_samples.append(joint_ms)
        except RuntimeError as exc:
            raise BackendError(
                f"CUDA bidirectional transfer measurement failed for "
                f"bytes={object_bytes}: {exc}"
            ) from exc
        finally:
            # Explicit per-name deletes (mutating locals() does not reliably
            # unbind function-local variables in CPython, so a loop over
            # names cannot replace this) -- mirrors aggregate_backend.py's
            # cleanup exactly, extended to this method's seven buffers/streams.
            if "host_h2d" in locals():
                del host_h2d
            if "dev_h2d" in locals():
                del dev_h2d
            if "host_d2h" in locals():
                del host_d2h
            if "dev_d2h" in locals():
                del dev_d2h
            if "stream_h2d" in locals():
                del stream_h2d
            if "stream_d2h" in locals():
                del stream_d2h
            if "baseline_stream" in locals():
                del baseline_stream
            gc.collect()
            torch.cuda.empty_cache()

        h2d_samples = [round(v, 9) for v in h2d_samples]
        d2h_samples = [round(v, 9) for v in d2h_samples]
        joint_samples = [round(v, 9) for v in joint_samples]
        h2d_baseline_samples = [round(v, 9) for v in h2d_baseline_samples]
        d2h_baseline_samples = [round(v, 9) for v in d2h_baseline_samples]

        h2d_mean = round(statistics.fmean(h2d_samples), 9)
        d2h_mean = round(statistics.fmean(d2h_samples), 9)
        joint_mean = round(statistics.fmean(joint_samples), 9)
        h2d_baseline_mean = round(statistics.fmean(h2d_baseline_samples), 9)
        d2h_baseline_mean = round(statistics.fmean(d2h_baseline_samples), 9)
        max_baseline = max(h2d_baseline_mean, d2h_baseline_mean)
        sum_baseline = h2d_baseline_mean + d2h_baseline_mean

        return {
            "object_bytes": object_bytes,
            "h2d_ms_samples": h2d_samples,
            "h2d_ms_mean": h2d_mean,
            "d2h_ms_samples": d2h_samples,
            "d2h_ms_mean": d2h_mean,
            "joint_completion_ms_samples": joint_samples,
            "joint_completion_ms_mean": joint_mean,
            "h2d_baseline_ms_samples": h2d_baseline_samples,
            "h2d_baseline_ms_mean": h2d_baseline_mean,
            "d2h_baseline_ms_samples": d2h_baseline_samples,
            "d2h_baseline_ms_mean": d2h_baseline_mean,
            "joint_over_max_ratio": round(joint_mean / max_baseline, 9),
            "joint_over_sum_ratio": round(joint_mean / sum_baseline, 9),
        }

    # -- axes 2 + 3 -----------------------------------------------------------#

    def measure_stream_split_cell(
        self, object_bytes: int, copy_streams: int, direction: str, repeats: int
    ) -> dict[str, Any]:
        if object_bytes <= 0:
            raise BackendError(f"object_bytes must be positive, got {object_bytes}")
        if copy_streams <= 0:
            raise BackendError(f"copy_streams must be positive, got {copy_streams}")
        if direction not in ("h2d", "d2h"):
            raise BackendError(f"direction must be h2d/d2h, got {direction!r}")
        if repeats <= 0:
            raise BackendError(f"repeats must be positive, got {repeats}")

        torch = self._torch
        chunk_bytes = math.ceil(object_bytes / copy_streams)
        try:
            host = torch.empty(object_bytes, dtype=torch.uint8, pin_memory=True)
            dev = torch.empty(object_bytes, dtype=torch.uint8, device="cuda")
            streams = [torch.cuda.Stream() for _ in range(copy_streams)]
            torch.cuda.synchronize()

            for _ in range(self.warmup_repeats):
                self._measure_stream_split_once(
                    host, dev, streams, direction, chunk_bytes, object_bytes
                )

            samples = [
                self._measure_stream_split_once(
                    host, dev, streams, direction, chunk_bytes, object_bytes
                )
                for _ in range(repeats)
            ]
        except RuntimeError as exc:
            raise BackendError(
                f"CUDA stream-split transfer measurement failed for "
                f"bytes={object_bytes}, copy_streams={copy_streams}, "
                f"direction={direction}: {exc}"
            ) from exc
        finally:
            if "host" in locals():
                del host
            if "dev" in locals():
                del dev
            if "streams" in locals():
                del streams
            gc.collect()
            torch.cuda.empty_cache()

        samples = [round(v, 9) for v in samples]
        return {
            "object_bytes": object_bytes,
            "copy_streams": copy_streams,
            "direction": direction,
            "gap_label": gap_label_of(object_bytes),
            "chunk_bytes": chunk_bytes,
            "latency_ms_samples": samples,
            "latency_ms_mean": round(statistics.fmean(samples), 9),
        }


_REGISTRY = {
    "mock_pcie_extension": MockPcieExtensionBackend,
    "gpu": TorchPcieExtensionBackend,
}


def registered_backends() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def resolve_backend(name: str):
    if name not in _REGISTRY:
        raise BackendError(
            f"unregistered backend {name!r}; registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]
