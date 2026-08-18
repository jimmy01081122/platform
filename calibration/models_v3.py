"""STAGE_A1 cal_model_form_repair_v2 — model forms (v3 outputs).

Implements the three preregistered candidate model forms
(experiments/specs/cal_model_form_repair_v2.yaml + its OWNER_RESOLUTION
addendum). Pure model classes with no fallback softening: physical-constraint
violations hard-fail, and out-of-domain queries return explicit UNSUPPORTED /
EXTRAPOLATED states rather than silent constants.

All results produced with these models are FIT-side / diagnostic (decision
P-005). Nothing here is a held-out judgment or a calibrated PASS.

Kept dependency-free (no numpy/sklearn) so it runs in the platform venv and
under `make test`.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any


class ModelError(ValueError):
    """Raised on a non-physical fit or a contract violation."""


UNSUPPORTED = "UNSUPPORTED"
INTERPOLATED = "INTERPOLATED"
EXTRAPOLATED = "EXTRAPOLATED"


def mape(measured: list[float], predicted: list[float]) -> float:
    vals = [abs(p - m) / m * 100.0 for m, p in zip(measured, predicted) if m > 0]
    if not vals:
        raise ModelError("MAPE over empty/degenerate set")
    return statistics.fmean(vals)


def max_ape(measured: list[float], predicted: list[float]) -> float:
    return max(abs(p - m) / m * 100.0 for m, p in zip(measured, predicted) if m > 0)


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least squares. Returns (slope, intercept). Raises if x has no
    spread (slope unidentifiable)."""
    n = len(xs)
    if n < 2:
        raise ModelError("need >= 2 points for a line fit")
    xbar = statistics.fmean(xs)
    ybar = statistics.fmean(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom == 0:
        raise ModelError("degenerate fit: x has no variation")
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
    return slope, ybar - slope * xbar


# ---------------------------------------------------------------------------
# Candidate A — PCIe two-regime
#   T_d(B, S) = max( O_d(S), A_d + B / BW_d ),  O_d(S) = alpha_d + beta_d*(S-1)
# OWNER_RESOLUTION 2026-08-18: no clamp. A<0 / BW<=0 / alpha<0 / beta<0 all
# hard-fail. S = intra-object stream partition count. Production single-object
# uses S=1; production S>1 is UNSUPPORTED (multi-object concurrency is a
# separate simulator resource-contention quantity, not this S).
# ---------------------------------------------------------------------------

BULK_MIN_BYTES = 22_020_096   # 21 MiB
OVERHEAD_MAX_BYTES = 1_048_576  # 1 MiB


@dataclass
class PcieTwoRegime:
    bulk: dict[str, dict[str, float]]
    overhead: dict[str, dict[str, float]]

    @classmethod
    def fit(cls, points: list[dict[str, Any]]) -> "PcieTwoRegime":
        """points: list of {direction, bytes, streams, latency_ms}."""
        bulk: dict[str, dict[str, float]] = {}
        overhead: dict[str, dict[str, float]] = {}
        for d in ("h2d", "d2h"):
            dp = [p for p in points if p["direction"] == d]
            # bulk: S=1, bytes >= BULK_MIN_BYTES
            base = [p for p in dp if p["streams"] == 1 and p["bytes"] >= BULK_MIN_BYTES]
            if len(base) < 2:
                raise ModelError(f"PCIe {d}: insufficient bulk points")
            slope, intercept = _ols(
                [float(p["bytes"]) for p in base], [float(p["latency_ms"]) for p in base]
            )
            bw = 1.0 / slope if slope != 0 else float("inf")
            # OWNER_RESOLUTION hard-fail (no clamp):
            if bw <= 0 or slope <= 0:
                raise ModelError(f"PCIe {d}: non-physical bulk bandwidth (BW<=0)")
            if intercept < 0:
                raise ModelError(f"PCIe {d}: non-physical bulk intercept (A<0)")
            bulk[d] = {"intercept_ms": intercept, "bandwidth_bytes_per_ms": bw}
            # overhead: bytes <= OVERHEAD_MAX_BYTES, median per stream, linear in (S-1)
            small = [p for p in dp if p["bytes"] <= OVERHEAD_MAX_BYTES]
            by_stream: dict[int, float] = {}
            for s in sorted({p["streams"] for p in small}):
                by_stream[s] = statistics.median(
                    [p["latency_ms"] for p in small if p["streams"] == s]
                )
            if len(by_stream) < 2:
                raise ModelError(f"PCIe {d}: insufficient overhead stream points")
            beta, alpha = _ols(
                [float(s - 1) for s in by_stream], [by_stream[s] for s in by_stream]
            )
            if alpha < 0:
                raise ModelError(f"PCIe {d}: non-physical overhead intercept (alpha<0)")
            if beta < 0:
                raise ModelError(f"PCIe {d}: non-physical overhead slope (beta<0)")
            overhead[d] = {
                "alpha_ms": alpha,
                "beta_ms_per_extra_stream": beta,
                "empirical_floor_by_stream": {str(s): by_stream[s] for s in by_stream},
            }
        return cls(bulk=bulk, overhead=overhead)

    def predict(self, direction: str, bytes_: int, streams: int, *, mode: str = "evaluation") -> Any:
        """mode='evaluation' reproduces the microbenchmark at its measured S.
        mode='production' enforces the OWNER_RESOLUTION rule: single-object
        transfers use S=1; S>1 returns UNSUPPORTED (never silently applies the
        microbenchmark overhead term to a production transfer)."""
        if direction not in self.bulk or bytes_ <= 0 or streams < 1:
            raise ModelError("invalid PCIe query")
        if mode == "production" and streams > 1:
            return UNSUPPORTED
        b = self.bulk[direction]
        o = self.overhead[direction]
        bulk = b["intercept_ms"] + bytes_ / b["bandwidth_bytes_per_ms"]
        floor = o["alpha_ms"] + o["beta_ms_per_extra_stream"] * (streams - 1)
        return max(bulk, floor)


class A1StoredPcie:
    """v1 committed PCIe parameters (calibration/fits/v2/parameters.json),
    for the required three-way comparison."""

    PARAMS = {
        "h2d": {"intercept_ms": 0.015341920668000775, "bandwidth_bytes_per_ms": 28480108.698140044, "floor_ms": 0.028482377142857142},
        "d2h": {"intercept_ms": 0.014215807554351478, "bandwidth_bytes_per_ms": 28408685.50988128, "floor_ms": 0.027774121212121212},
    }

    def predict(self, direction: str, bytes_: int, streams: int) -> float:
        p = self.PARAMS[direction]
        return max(p["floor_ms"], p["intercept_ms"] + bytes_ / p["bandwidth_bytes_per_ms"])


class OldStreamFactorPcie:
    """The original q0 model: per-transfer latency multiplied by a
    stream_latency_factor (the model form the whole A1 effort is replacing).
    Reconstructed from rtx-q0-fitted-parameters.json for the three-way
    comparison; predicts the SAME bytes scaled by the stream factor."""

    STREAM_FACTORS = {1: 1.0, 2: 1.7384584273483465, 4: 2.762992620065082}
    PARAMS = A1StoredPcie.PARAMS

    def predict(self, direction: str, bytes_: int, streams: int) -> float:
        p = self.PARAMS[direction]
        base = p["intercept_ms"] + bytes_ / p["bandwidth_bytes_per_ms"]
        return base * self.STREAM_FACTORS[streams]


# ---------------------------------------------------------------------------
# Candidate B — component local predictor
# ---------------------------------------------------------------------------

@dataclass
class ComponentPoint:
    operation: str
    tokens: int
    phase: str
    concurrency: int
    latency_ms: float
    workload: str


class GlobalAffine:
    """v1 form: per-operation affine in expert_tokens."""

    def __init__(self) -> None:
        self.params: dict[str, tuple[float, float]] = {}

    def fit(self, points: list[ComponentPoint]) -> "GlobalAffine":
        for op in sorted({p.operation for p in points}):
            ps = [p for p in points if p.operation == op]
            xs = [float(p.tokens) for p in ps]
            ys = [p.latency_ms for p in ps]
            try:
                slope, intercept = _ols(xs, ys)
            except ModelError:
                slope, intercept = 0.0, statistics.fmean(ys)
            self.params[op] = (intercept, slope)
        return self

    def predict_point(self, p: ComponentPoint) -> float:
        intercept, slope = self.params[p.operation]
        return max(0.0, intercept + slope * p.tokens)


class NearestProfile:
    """k=1 nearest profile in log-token space (comparator)."""

    def __init__(self) -> None:
        self.points: list[ComponentPoint] = []

    def fit(self, points: list[ComponentPoint]) -> "NearestProfile":
        self.points = list(points)
        return self

    def predict_point(self, p: ComponentPoint) -> float:
        cand = [q for q in self.points if q.operation == p.operation and q.phase == p.phase]
        if not cand:
            cand = [q for q in self.points if q.operation == p.operation]
        q = min(cand, key=lambda q: abs(math.log2(max(1, q.tokens)) - math.log2(max(1, p.tokens))))
        return q.latency_ms


@dataclass
class KnnPrediction:
    value: float
    state: str            # INTERPOLATED | EXTRAPOLATED | UNSUPPORTED
    nearest_distance: float
    n_neighbors: int


class ProfileKNN:
    """Preregistered component predictor (cal_model_form_repair_v2 candidate B).

    k=3, distance = |log2(expert_tokens) difference|, phase and operation are
    hard partitions, inverse-distance weighting, minimum_neighbors=2. Emits an
    explicit INTERPOLATED / EXTRAPOLATED / UNSUPPORTED state and never silently
    falls back across phase or operation.
    """

    def __init__(self, k: int = 3, power: float = 1.0, minimum_neighbors: int = 2,
                 use_log: bool = True) -> None:
        self.k = k
        self.power = power
        self.minimum_neighbors = minimum_neighbors
        self.use_log = use_log
        self.points: list[ComponentPoint] = []
        self.envelope: dict[tuple[str, str], tuple[int, int]] = {}

    def _axis(self, tokens: int) -> float:
        return math.log2(max(1, tokens)) if self.use_log else float(tokens)

    def fit(self, points: list[ComponentPoint]) -> "ProfileKNN":
        self.points = list(points)
        self.envelope = {}
        for p in points:
            key = (p.operation, p.phase)
            lo, hi = self.envelope.get(key, (p.tokens, p.tokens))
            self.envelope[key] = (min(lo, p.tokens), max(hi, p.tokens))
        return self

    def predict_point(self, p: ComponentPoint) -> KnnPrediction:
        cand = [q for q in self.points if q.operation == p.operation and q.phase == p.phase]
        if len(cand) < self.minimum_neighbors:
            return KnnPrediction(float("nan"), UNSUPPORTED, float("inf"), len(cand))
        # Preregistered tie handling: by distance, then expert_tokens ascending,
        # then a stable deterministic key (workload, operation, latency). Sort by
        # an explicit key so tied ComponentPoints are never compared directly.
        scored = sorted(
            ((abs(self._axis(q.tokens) - self._axis(p.tokens)), q) for q in cand),
            key=lambda t: (t[0], t[1].tokens, t[1].workload, t[1].operation, t[1].latency_ms),
        )[: self.k]
        nearest = scored[0][0]
        lo, hi = self.envelope[(p.operation, p.phase)]
        state = INTERPOLATED if lo <= p.tokens <= hi else EXTRAPOLATED
        if nearest < 1e-12:
            return KnnPrediction(scored[0][1].latency_ms, state, 0.0, len(scored))
        weights = [1.0 / ((d + 1e-9) ** self.power) for d, _ in scored]
        val = sum(w * q.latency_ms for w, (_, q) in zip(weights, scored)) / sum(weights)
        return KnnPrediction(val, state, nearest, len(scored))


# ---------------------------------------------------------------------------
# Candidate C — MoE replay explicit operator graph
# ---------------------------------------------------------------------------

# The actually-timed graph of window_replay() (benchmark.py:534-542), per
# window over route length n:
REPLAY_OPERATOR_GRAPH = [
    {"op": "argsort_route", "kind": "sort", "measured": False},
    {"op": "gather", "kind": "index_select", "measured": True, "folded_into": "gather_scatter"},
    {"op": "gemm_gate", "kind": "gemm", "measured": True, "folded_into": "grouped_gemm"},
    {"op": "gemm_up", "kind": "gemm", "measured": True, "folded_into": "grouped_gemm"},
    {"op": "act_silu_mul", "kind": "elementwise", "measured": True, "folded_into": "grouped_gemm"},
    {"op": "gemm_down", "kind": "gemm", "measured": True, "folded_into": "grouped_gemm"},
    {"op": "argsort_inverse", "kind": "sort", "measured": False},
    {"op": "scatter", "kind": "index_select", "measured": True, "folded_into": "gather_scatter"},
]

# Declared metadata (benchmark.py:579-582) records only these:
REPLAY_DECLARED_METADATA_OPS = ["grouped_gemm", "gather_scatter"]


@dataclass
class ReplayPoint:
    workload: str
    concurrency: int
    tokens: int
    routes: int
    measured_tpot_ms: float
    a1_pred_tpot_ms: float


def replay_missing_terms() -> list[str]:
    """Operators in the timed graph that no existing calibration probe can
    identify -> BLOCKED_ON_MEASUREMENT."""
    return [o["op"] for o in REPLAY_OPERATOR_GRAPH if not o["measured"]]


@dataclass
class FixedTauRouteDiagnostic:
    """DIAGNOSTIC_ONLY. Not a calibrated model: a single additive constant that
    stands in for the missing sort/permute term. Preregistered as a comparator
    only; must never be promoted."""

    tau_route_ms: float

    def predict_tpot(self, p: ReplayPoint) -> float:
        return p.a1_pred_tpot_ms + self.tau_route_ms * p.routes / p.tokens

    @staticmethod
    def throughput_from_tpot(tpot_ms: float) -> float:
        # Throughput is DERIVED, never fit independently.
        return 1000.0 / tpot_ms
