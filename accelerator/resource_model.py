"""Parametric candidate-processor resource model (root spec §6.1).

The candidate support processor is expressed as nine swept parameters. C1's DSE
scans them to answer "which function, what spec, is it worth it". Nothing here is
a benefit claim; this is only the parameter surface and its default sweep ranges.

The nine parameters (root spec §6.1):

    pipeline_latency        issue_width          local_sram_capacity
    memory_bandwidth        queue_depth          operations_per_cycle
    clock_domain            area_proxy           power_proxy

Time discipline (root spec §4): the platform's canonical time is a femtosecond
integer and clocks are rational. ``clock_domain`` is therefore stored as an exact
rational frequency (num/den Hz) and the derived cycle period is an integer number
of femtoseconds computed with the same ``floor`` convention the C++ engine uses
for ``edge_time`` — no float clock drift.

Every parameter is scannable: ``ResourceSweep`` reads a list of allowed values (or
a min/max/step range) per parameter from config and yields the Cartesian product
of ``ResourceModel`` points, with a hard guardrail on the product size so an
accidental range cannot explode the DSE grid.

Fidelity: every ResourceModel is ANALYTICAL. The ``area_proxy`` / ``power_proxy``
defaults are anchored to the pre-layout STA DSE in hardware/ (root spec §11.2) and
carry a claim limit forbidding any physical-area / power / feasibility claim.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import product
from typing import Any, Iterator

from accelerator.fidelity import Fidelity, Provenance

FS_PER_SECOND = 10**15

# Ordered list of the nine parameter names (root spec §6.1). Used to validate
# config and to keep sweep axis order deterministic.
PARAM_NAMES: tuple[str, ...] = (
    "pipeline_latency_cycles",
    "issue_width",
    "local_sram_capacity_bytes",
    "memory_bandwidth_bytes_per_s",
    "queue_depth",
    "operations_per_cycle",
    "clock_frequency_hz",
    "area_proxy_um2",
    "power_proxy_mw",
)

# Guardrail: refuse to materialize a sweep larger than this many points unless the
# caller explicitly raises the cap. Keeps an accidental range from exploding C1.
DEFAULT_MAX_SWEEP_POINTS = 100_000


@dataclass(frozen=True)
class ResourceModel:
    """One point in the nine-dimensional candidate-processor spec space.

    All fields are ANALYTICAL. clock_frequency_hz is an exact Fraction so the
    derived cycle period is drift-free (root spec §4).
    """

    pipeline_latency_cycles: int          # issue-to-first-result latency, in cycles
    issue_width: int                      # work units issued per cycle
    local_sram_capacity_bytes: int        # on-chip staging/metadata capacity
    memory_bandwidth_bytes_per_s: int     # processor<->memory sustained bandwidth
    queue_depth: int                      # inbound transaction queue slots (backpressure)
    operations_per_cycle: int             # datapath ops/cycle (throughput proxy)
    clock_frequency_hz: Fraction          # "clock domain": exact rational frequency
    area_proxy_um2: float                 # ANALYTICAL area proxy (NOT physical area)
    power_proxy_mw: float                 # ANALYTICAL power proxy (NOT physical power)

    def __post_init__(self) -> None:
        for name in (
            "pipeline_latency_cycles",
            "issue_width",
            "local_sram_capacity_bytes",
            "queue_depth",
            "operations_per_cycle",
        ):
            v = getattr(self, name)
            if not isinstance(v, int) or v < 0:
                raise ValueError(f"{name} must be a non-negative int, got {v!r}")
        if self.issue_width < 1:
            raise ValueError("issue_width must be >= 1")
        if self.queue_depth < 1:
            raise ValueError("queue_depth must be >= 1")
        if self.operations_per_cycle < 1:
            raise ValueError("operations_per_cycle must be >= 1")
        if self.memory_bandwidth_bytes_per_s <= 0:
            raise ValueError("memory_bandwidth_bytes_per_s must be > 0")
        freq = self.clock_frequency_hz
        if not isinstance(freq, Fraction) or freq <= 0:
            raise ValueError("clock_frequency_hz must be a positive Fraction")
        if self.area_proxy_um2 < 0 or self.power_proxy_mw < 0:
            raise ValueError("area/power proxies must be non-negative")

    @property
    def cycle_period_fs(self) -> int:
        """Integer femtoseconds per cycle, floor convention (matches engine)."""
        # period = 1/freq seconds = den/num s = den*10^15/num fs, floored.
        return (self.clock_frequency_hz.denominator * FS_PER_SECOND) // (
            self.clock_frequency_hz.numerator
        )

    def cycles_to_fs(self, cycles: int) -> int:
        """Exact integer-fs duration of ``cycles`` cycles (no float drift)."""
        if cycles < 0:
            raise ValueError("cycles must be non-negative")
        return cycles * self.cycle_period_fs

    @property
    def fidelity(self) -> Fidelity:
        return Fidelity.ANALYTICAL

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for name in PARAM_NAMES:
            v = getattr(self, name)
            if isinstance(v, Fraction):
                d[name] = f"{v.numerator}/{v.denominator}"
            else:
                d[name] = v
        d["cycle_period_fs"] = self.cycle_period_fs
        d["fidelity"] = self.fidelity.value
        return d

    def with_(self, **changes: Any) -> "ResourceModel":
        return replace(self, **changes)


def _as_fraction(value: Any) -> Fraction:
    """Parse a clock value as an exact Fraction (accepts 'num/den', int, or float-str)."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value, 1)
    text = str(value).strip()
    if "/" in text:
        num, den = text.split("/", 1)
        return Fraction(int(num), int(den))
    # Accept a plain integer-hz string; reject bare floats to avoid drift.
    return Fraction(int(text), 1)


# Default ANALYTICAL area/power-proxy provenance, anchored to hardware/ STA DSE
# (root spec §11.2). These are pre-layout, wire-load-model, ideal-clock relative
# architecture numbers -- explicitly NOT physical area/power/feasibility.
AREA_POWER_PROXY_PROVENANCE = Provenance(
    fidelity=Fidelity.ANALYTICAL,
    evidence_refs=(
        "hardware/ STA DSE (root spec §11.2): seqbuf residency engine, banked LRU "
        "victim, argmin, expert decompressor",
    ),
    claim_limit=(
        "area_proxy/power_proxy are relative ANALYTICAL proxies for DSE ranking "
        "only; they are pre-layout wire-load-model ideal-clock numbers and MUST "
        "NOT be read as physical area, power, or product feasibility (root spec §11.2, §14.6)."
    ),
)


@dataclass(frozen=True)
class ResourceSweep:
    """A scannable set of allowed values per parameter -> ResourceModel product.

    ``axes`` maps each of the nine PARAM_NAMES to a list of allowed values. The
    sweep yields the Cartesian product in PARAM_NAMES order (deterministic).
    """

    axes: dict[str, list[Any]]
    max_points: int = DEFAULT_MAX_SWEEP_POINTS

    def __post_init__(self) -> None:
        missing = [n for n in PARAM_NAMES if n not in self.axes]
        if missing:
            raise ValueError(f"sweep is missing axes for: {missing}")
        extra = [n for n in self.axes if n not in PARAM_NAMES]
        if extra:
            raise ValueError(f"sweep has unknown axes: {extra}")
        for name, values in self.axes.items():
            if not isinstance(values, list) or not values:
                raise ValueError(f"axis {name} must be a non-empty list")

    def size(self) -> int:
        n = 1
        for name in PARAM_NAMES:
            n *= len(self.axes[name])
        return n

    def __iter__(self) -> Iterator[ResourceModel]:
        total = self.size()
        if total > self.max_points:
            raise ValueError(
                f"sweep would materialize {total} points > max_points={self.max_points}; "
                "narrow the ranges or raise max_points explicitly."
            )
        axis_values = [self.axes[name] for name in PARAM_NAMES]
        for combo in product(*axis_values):
            kwargs: dict[str, Any] = {}
            for name, value in zip(PARAM_NAMES, combo):
                if name == "clock_frequency_hz":
                    kwargs[name] = _as_fraction(value)
                elif name in ("area_proxy_um2", "power_proxy_mw"):
                    kwargs[name] = float(value)
                elif name == "memory_bandwidth_bytes_per_s":
                    kwargs[name] = int(value)
                else:
                    kwargs[name] = int(value)
            yield ResourceModel(**kwargs)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ResourceSweep":
        """Build a sweep from a config mapping.

        Each parameter accepts either an explicit ``values: [...]`` list or a
        ``range: {min, max, step}`` (inclusive). Exactly one form per parameter.
        """
        params = config.get("parameters")
        if not isinstance(params, dict):
            raise ValueError("config must have a 'parameters' mapping")
        axes: dict[str, list[Any]] = {}
        for name in PARAM_NAMES:
            spec = params.get(name)
            if spec is None:
                raise ValueError(f"config is missing parameter {name!r}")
            axes[name] = _expand_axis(name, spec)
        max_points = int(config.get("max_points", DEFAULT_MAX_SWEEP_POINTS))
        return cls(axes=axes, max_points=max_points)


def _expand_axis(name: str, spec: Any) -> list[Any]:
    if isinstance(spec, dict) and "values" in spec:
        values = spec["values"]
        if not isinstance(values, list) or not values:
            raise ValueError(f"{name}.values must be a non-empty list")
        return list(values)
    if isinstance(spec, dict) and "range" in spec:
        r = spec["range"]
        lo, hi, step = r["min"], r["max"], r.get("step", 1)
        if name in ("area_proxy_um2", "power_proxy_mw"):
            out, v = [], float(lo)
            while v <= float(hi) + 1e-9:
                out.append(v)
                v += float(step)
            return out
        out_i, vi = [], int(lo)
        if int(step) <= 0:
            raise ValueError(f"{name}.range.step must be > 0")
        while vi <= int(hi):
            out_i.append(vi)
            vi += int(step)
        if not out_i:
            raise ValueError(f"{name}.range produced no values")
        return out_i
    if isinstance(spec, list):
        return list(spec)
    # A scalar means a fixed (single-value) axis.
    return [spec]
