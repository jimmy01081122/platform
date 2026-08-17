"""S2 executable residency/prefetch model + simulator for MoE experts.

Consumes the canonical ``expert_demand`` stream (S1) and a platform cost model,
and evaluates three residency policies that share ONE algorithm interface
(SEMANTICS.md), so the same semantics can later be realized in software,
firmware, and RTL:

* ``on_demand``  : depth 0, load a missing expert only when demanded (baseline)
* ``lru``        : depth 0, same as on_demand for loads but keeps an LRU set
                   (identical to on_demand when eviction victim = LRU; kept as an
                   explicit baseline so eviction policy is a controlled variable)
* ``prefetch``   : depth d>0, router-guided lookahead prefetch of upcoming experts

Two fidelity layers are reported and kept separate on purpose:

1. Demand-driven accounting (ROBUST): compulsory misses, transfers, evictions,
   wasted prefetches. Depends only on the measured demand order, capacity C, and
   the policy. It does NOT depend on any bandwidth/latency assumption.
2. First-order timing (ASSUMPTION-DEPENDENT): stall time under a platform cost
   model (bandwidth, latency, copy engines) and a per-layer compute time. This
   is explicitly a swept/registered quantity, never a fabricated point value.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Demand extraction from canonical events
# ---------------------------------------------------------------------------

@dataclass
class LayerDemand:
    batch: str
    layer_step: int
    experts: list[int]                 # deterministic order (sorted)
    assigned_tokens: dict[int, int]    # expert_id -> tokens


def demands_from_events(events: list[dict[str, Any]]) -> list[LayerDemand]:
    """Group canonical events into ordered per-(batch, layer_step) demands.

    Global order: batch ascending (numeric), then layer_step ascending. This
    matches sequential inference over batches and layers.
    """
    groups: dict[tuple[str, int], dict[int, int]] = {}
    for ev in events:
        b = ev["request_id"]
        s = int(ev["attributes"]["layer_step"])
        e = int(ev["attributes"]["expert_id"])
        tok = int(ev["attributes"]["assigned_tokens"])
        groups.setdefault((b, s), {})[e] = groups.get((b, s), {}).get(e, 0) + tok
    out: list[LayerDemand] = []
    for (b, s) in sorted(groups, key=lambda k: (int(k[0]), k[1])):
        d = groups[(b, s)]
        out.append(LayerDemand(batch=b, layer_step=s, experts=sorted(d), assigned_tokens=d))
    return out


# ---------------------------------------------------------------------------
# Platform cost model (assumption-dependent, all values registered/swept)
# ---------------------------------------------------------------------------

@dataclass
class PlatformCost:
    profile_id: str
    expert_weight_bytes: int            # A-005 derived
    link_bandwidth_bytes_per_s: float   # A-006 (P-D) / A-007 (P-I), swept
    link_latency_s: float               # swept/registered
    copy_engines: int                   # concurrency for transfers
    # For P-I, transfers contend with compute for shared bandwidth. This factor
    # scales the effective prefetch bandwidth available during compute.
    prefetch_bw_fraction: float = 1.0

    def transfer_time_s(self) -> float:
        return self.link_latency_s + self.expert_weight_bytes / self.link_bandwidth_bytes_per_s


# ---------------------------------------------------------------------------
# Residency policies (shared interface)
# ---------------------------------------------------------------------------

@dataclass
class SimResult:
    policy: str
    capacity: int
    prefetch_depth: int
    total_demands: int
    demand_misses: int          # compulsory misses on the critical path
    prefetch_hits: int          # demands served by a prior prefetch
    transfers: int              # total expert loads (demand + prefetch)
    evictions: int
    wasted_prefetches: int      # prefetched then evicted before use
    # timing (assumption-dependent)
    critical_transfer_time_s: float | None = None
    total_compute_time_s: float | None = None
    total_stall_time_s: float | None = None
    total_time_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def miss_rate(self) -> float:
        return self.demand_misses / self.total_demands if self.total_demands else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["miss_rate"] = self.miss_rate
        return d


def _future_needed(demands: list[LayerDemand], i: int, depth: int) -> list[int]:
    """Experts needed in the next `depth` layer-steps, nearest first (dedup)."""
    seen: list[int] = []
    seenset: set[int] = set()
    for j in range(i + 1, min(i + 1 + depth, len(demands))):
        for e in demands[j].experts:
            if e not in seenset:
                seen.append(e)
                seenset.add(e)
    return seen


def simulate(
    demands: list[LayerDemand],
    capacity: int,
    prefetch_depth: int,
    policy: str,
    cost: PlatformCost | None = None,
    per_layer_compute_time_s: float | None = None,
    shared_link_bandwidth: bool = False,
    future_fn: "Callable[[list[LayerDemand], int, int], list[int]] | None" = None,
) -> SimResult:
    """Deterministic residency simulation. No RNG is used.

    Residency set R is an OrderedDict expert_id -> None, ordered by recency
    (LRU at the front). Prefetched-but-unused experts are tracked to count waste.

    `future_fn(demands, i, depth) -> list[int]` supplies the experts to prefetch
    after step i. Default `None` uses `_future_needed`, i.e. a PERFECT-lookahead
    ORACLE that peeks the actual next `depth` steps (this is what the headline W3
    prefetch numbers assume). Pass a past-only predictor to measure the realizable
    fraction of that oracle gain (scripts/w3_prefetch_predictability.py).
    """
    assert capacity >= 1
    if policy == "on_demand":
        prefetch_depth = 0
    R: "OrderedDict[int, bool]" = OrderedDict()  # value = was_prefetched_and_unused
    prefetched_pending: set[int] = set()

    total_demands = 0
    demand_misses = 0
    prefetch_hits = 0
    transfers = 0
    evictions = 0
    wasted = 0
    critical_transfer_units = 0     # number of experts loaded on critical path
    parallel_batches = 0            # ceil(n/engines) accumulation for timing

    engines = cost.copy_engines if cost else 1

    def touch(e: int) -> None:
        # mark most-recently-used; clears prefetch-pending flag on real use
        if e in R:
            R.move_to_end(e, last=True)
        R[e] = False
        prefetched_pending.discard(e)

    def evict_one(protect: set[int]) -> bool:
        nonlocal evictions, wasted
        for victim in list(R.keys()):
            if victim in protect:
                continue
            if victim in prefetched_pending:
                prefetched_pending.discard(victim)
                wasted += 1
            del R[victim]
            evictions += 1
            return True
        return False

    def insert(e: int, prefetched: bool, protect: set[int]) -> bool:
        nonlocal transfers
        while len(R) >= capacity and e not in R:
            if not evict_one(protect):  # nothing evictable
                return False
        transfers += 1
        R[e] = prefetched
        R.move_to_end(e, last=True)
        if prefetched:
            prefetched_pending.add(e)
        return True

    for i, dm in enumerate(demands):
        # Experts within a layer are processed sequentially (streamed): each
        # expert demand is a single access; a processed expert may be evicted to
        # make room for the next. Protect only the expert currently loading.
        step_misses = 0
        for e in dm.experts:
            if e in R:
                if e in prefetched_pending:
                    prefetch_hits += 1
                touch(e)
            else:
                demand_misses += 1
                step_misses += 1
                insert(e, prefetched=False, protect={e})
                touch(e)
            total_demands += 1
        critical_transfer_units += step_misses
        if engines > 0 and step_misses:
            parallel_batches += math.ceil(step_misses / engines)
        # prefetch phase (overlaps with this step's compute); protect soon-needed
        if prefetch_depth > 0:
            fut = (_future_needed(demands, i, prefetch_depth) if future_fn is None
                   else future_fn(demands, i, prefetch_depth))
            futset = set(fut)
            for e in fut:
                if e in R:
                    continue
                if len(R) >= capacity and all(k in futset for k in R):
                    break  # no evictable victim that isn't itself soon-needed
                insert(e, prefetched=True, protect=futset)

    # account experts still pending (prefetched, never used) at end as wasted
    wasted += len(prefetched_pending)

    res = SimResult(
        policy=policy, capacity=capacity, prefetch_depth=prefetch_depth,
        total_demands=total_demands, demand_misses=demand_misses,
        prefetch_hits=prefetch_hits, transfers=transfers, evictions=evictions,
        wasted_prefetches=wasted,
    )

    # ---- first-order timing (assumption-dependent) ----
    if cost is not None and per_layer_compute_time_s is not None:
        tt = cost.transfer_time_s()
        if shared_link_bandwidth:
            # SHARED-LINK model (physically correct for E copy engines over one link):
            # E concurrent transfers share the link bandwidth B, so the bandwidth term
            # bytes/B is SERIALIZED (independent of E), while the per-transfer LATENCY
            # is paid once per parallel batch (ceil(m/E) times). Copy engines therefore
            # hide LATENCY, not bandwidth. At E=1 this equals the optimistic model.
            bw_time = cost.expert_weight_bytes / cost.link_bandwidth_bytes_per_s
            crit_time = parallel_batches * cost.link_latency_s + critical_transfer_units * bw_time
        else:
            # OPTIMISTIC model (per-engine full bandwidth): each parallel transfer gets
            # the full link, so a batch of E finishes in one transfer_time. Over-credits
            # copy engines for bandwidth-bound transfers; kept as default for backward
            # compatibility with earlier W3 reports (see DECISION_LOG D-048).
            crit_time = parallel_batches * tt
        compute_time = per_layer_compute_time_s * len(demands)
        # prefetch loads overlap with compute; only the non-overlapped part stalls.
        # Critical-path demand loads are NOT overlapped -> full stall.
        res.critical_transfer_time_s = crit_time
        res.total_compute_time_s = compute_time
        res.total_stall_time_s = crit_time
        res.total_time_s = compute_time + crit_time
        res.extra["transfer_time_per_expert_s"] = tt
        res.extra["latency_batches"] = parallel_batches
        res.extra["critical_transfer_units"] = critical_transfer_units
        res.extra["shared_link_bandwidth"] = shared_link_bandwidth
    return res
