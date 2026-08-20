"""Deterministic CPU mock backend for the V2-GAP-A multi-object aggregate probe.

Purpose: let the multi-object concurrent-transfer probe walk its *entire* code
path on a pure-CPU host. The mock returns deterministic, clearly-synthetic
aggregate-completion timings so the probe's serialization and the downstream
parser can be exercised end to end with no GPU, no torch, no PCIe.

It is NOT a performance model. The numbers come from a simple closed form
(per-object two-regime floor + shared-bandwidth aggregation) chosen only so the
plumbing has something *structurally valid and physically ordered* to serialize.
Any result produced through this backend is stamped
``evidence = "cpu_smoke_test_not_measurement"`` upstream.

AXIS DISCIPLINE (OWNER_RESOLUTION 2026-08-18, cal_model_form_repair_v2.yaml):
  * ``num_objects`` N is *multi-object copy concurrency* -- N independent
    residency-managed objects issued concurrently, measured by aggregate
    (wait-all) completion time. This is the quantity V2-GAP-A opens.
  * It is a DIFFERENT quantity from the two-regime ``copy_streams`` S
    (intra-object buffer partition, benchmark.py:316-334). This probe fixes
    ``copy_streams == 1`` for every object and MUST NOT borrow the S axis to
    express multi-object concurrency. The parser rejects any record that does.

The real GPU backend (a live PCIe multi-object transfer campaign) is
intentionally NOT implemented here: TRACK_GPU_PREP is pure CPU. The GPU backend
is a registered-but-unimplemented stub that raises if invoked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from measurement.probes.mock_backend import BackendError
except ImportError:  # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from measurement.probes.mock_backend import BackendError

# Frozen two-regime boundaries (cal_model_form_repair_v2.yaml candidate_a
# fit_protocol). Used only to label a synthetic object size's regime; NOT
# re-measured here.
BULK_MIN_BYTES = 22020096      # 21 MiB: bulk (bandwidth-bound) fit floor
OVERHEAD_MAX_BYTES = 1048576   # 1 MiB: small (launch/sync overhead) fit ceiling

# Real production residency objects (PLATFORM_FLOW_SPECIFICATION §2.2 frozen
# facts); the meaningful object sizes for the S>1 production question.
KV_BLOCK_BYTES = 2097152       # 2 MiB (SWAP-K2)
EXPERT_OBJECT_BYTES = 352321536  # 336 MiB (OFF-E-PR3)


def regime_of(object_bytes: int) -> str:
    if object_bytes >= BULK_MIN_BYTES:
        return "bulk"
    if object_bytes <= OVERHEAD_MAX_BYTES:
        return "small"
    return "transition"


@dataclass
class MockAggregateBackend:
    """CPU mock of a multi-object concurrent PCIe transfer campaign.

    For N independent objects of ``object_bytes`` each, moved concurrently in a
    single direction, it emits a deterministic per-object time and an aggregate
    (wait-all) completion time. By construction the aggregate is bounded by
    ``max(per_object) <= aggregate <= sum(per_object)`` -- the physical envelope
    a real measurement must also satisfy (the aggregate cannot finish before its
    slowest constituent, nor take longer than running them one after another).
    """

    # per-direction synthetic constants -- arbitrary, only to order the numbers
    bandwidth_bytes_per_ms: dict[str, float] = None  # populated in __post_init__
    overhead_floor_ms: dict[str, float] = None
    name: str = "mock_aggregate"

    def __post_init__(self) -> None:
        if self.bandwidth_bytes_per_ms is None:
            # ~28 GB/s h2d, ~26 GB/s d2h order-of-magnitude (synthetic)
            self.bandwidth_bytes_per_ms = {"h2d": 28.0e6, "d2h": 26.0e6}
        if self.overhead_floor_ms is None:
            self.overhead_floor_ms = {"h2d": 0.020, "d2h": 0.022}

    # contention factor c in (0,1]: how much each extra concurrent object adds,
    # as a fraction of a single object's time. c=1 => fully serialized (aggregate
    # = N*t1); c->0 => perfect overlap (aggregate = t1). Bulk transfers share
    # bandwidth so aggregate is near-serial; small transfers are launch-bound and
    # overlap more. Both keep max <= aggregate <= sum.
    _CONTENTION = {"bulk": 0.9, "transition": 0.5, "small": 0.25}

    def _single_object_ms(self, object_bytes: int, direction: str) -> float:
        bw = self.bandwidth_bytes_per_ms[direction]
        floor = self.overhead_floor_ms[direction]
        return max(floor, object_bytes / bw)

    def measure_cell(
        self, num_objects: int, object_bytes: int, direction: str, repeats: int
    ) -> dict[str, Any]:
        if num_objects <= 0:
            raise BackendError(f"num_objects must be positive, got {num_objects}")
        if object_bytes <= 0:
            raise BackendError(f"object_bytes must be positive, got {object_bytes}")
        if direction not in ("h2d", "d2h"):
            raise BackendError(f"direction must be h2d/d2h, got {direction!r}")
        if repeats <= 0:
            raise BackendError(f"repeats must be positive, got {repeats}")

        t1 = self._single_object_ms(object_bytes, direction)
        regime = regime_of(object_bytes)
        c = self._CONTENTION[regime]
        # aggregate = t1 * (1 + (N-1)*c); lies in [t1, N*t1] for c in [0,1].
        aggregate = t1 * (1.0 + (num_objects - 1) * c)
        # deterministic: repeats identical (a mock has no run-to-run noise); a
        # real campaign reports the Student-t CI across the repeats.
        samples = [round(aggregate, 9)] * repeats
        per_object = [round(t1, 9)] * num_objects
        return {
            "num_objects": num_objects,
            "object_bytes": object_bytes,
            "direction": direction,
            "copy_streams": 1,  # each object is a single S=1 transfer -- NOT the S axis
            "regime": regime,
            "aggregate_completion_ms_samples": samples,
            "aggregate_completion_ms_mean": round(sum(samples) / len(samples), 9),
            "per_object_ms": per_object,
            "max_per_object_ms": round(max(per_object), 9),
            "sum_per_object_ms": round(sum(per_object), 9),
        }


class GpuAggregateBackendStub:
    """Registered-but-unimplemented real GPU backend (anti-spoofing).

    Mirrors PLATFORM_FLOW_SPECIFICATION §6.3: an unavailable backend must refuse
    loudly, never silently degrade. In TRACK_GPU_PREP invoking it is a hard
    error -- this track is pure CPU.
    """

    name = "gpu"

    def __getattr__(self, _name: str):  # pragma: no cover - defensive
        raise BackendError(
            "GPU multi-object transfer backend is not runnable in TRACK_GPU_PREP "
            "(pure CPU). Real V2-GAP-A measurement belongs to TRACK_GPU."
        )


_REGISTRY = {
    "mock_aggregate": MockAggregateBackend,
    "gpu": GpuAggregateBackendStub,
}


def registered_backends() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def resolve_backend(name: str):
    if name not in _REGISTRY:
        raise BackendError(
            f"unregistered backend {name!r}; registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]
