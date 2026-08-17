"""S2 tests: residency/prefetch model sanity, conservation, ordering, contrast."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edgeflow import residency as RS  # noqa: E402


def _tiny_demands():
    # batch0: step0 encoder {0,1}; step1 decoder {0,2}
    return [
        RS.LayerDemand(batch="0", layer_step=0, experts=[0, 1], assigned_tokens={0: 3, 1: 1}),
        RS.LayerDemand(batch="0", layer_step=1, experts=[0, 2], assigned_tokens={0: 2, 2: 2}),
    ]


def test_contrast_capacity_extremes():
    d = _tiny_demands()
    # C large -> cold misses only: 0,1 at step0 (2) + 2 at step1 (1) = 3
    big = RS.simulate(d, capacity=8, prefetch_depth=0, policy="on_demand")
    assert big.total_demands == 4
    assert big.demand_misses == 3
    # C=1 -> thrashing: every demand misses = 4
    small = RS.simulate(d, capacity=1, prefetch_depth=0, policy="on_demand")
    assert small.demand_misses == 4
    # monotonic: more capacity never increases misses
    assert big.demand_misses <= small.demand_misses


def test_prefetch_reduces_misses_when_capacity_allows():
    d = _tiny_demands()
    base = RS.simulate(d, capacity=3, prefetch_depth=0, policy="on_demand")
    pref = RS.simulate(d, capacity=3, prefetch_depth=1, policy="prefetch")
    # on_demand C=3: step0 misses 0,1 (2); step1 miss 2 (1) = 3
    assert base.demand_misses == 3
    # prefetch C=3 depth1: expert 2 prefetched during step0 -> step1 hit
    assert pref.demand_misses == 2
    assert pref.prefetch_hits == 1
    assert pref.demand_misses < base.demand_misses


def test_prefetch_never_worse_than_on_demand():
    d = _tiny_demands()
    for cap in range(1, 9):
        base = RS.simulate(d, capacity=cap, prefetch_depth=0, policy="on_demand")
        for depth in (1, 2):
            pref = RS.simulate(d, capacity=cap, prefetch_depth=depth, policy="prefetch")
            # prefetch converts future demand misses into prefetch hits; it must
            # never increase the number of critical-path demand misses.
            assert pref.demand_misses <= base.demand_misses


def test_conservation():
    d = _tiny_demands()
    for cap in range(1, 9):
        for policy, depth in [("on_demand", 0), ("lru", 0), ("prefetch", 1), ("prefetch", 2)]:
            r = RS.simulate(d, capacity=cap, prefetch_depth=depth, policy=policy)
            # every load is a transfer; demand misses are a subset of transfers
            assert r.transfers >= r.demand_misses
            # total demands conserved
            assert r.total_demands == 4
            # prefetch hits cannot exceed prefetch transfers
            assert r.prefetch_hits <= r.transfers
            # miss rate in [0,1]
            assert 0.0 <= r.miss_rate <= 1.0


def test_determinism():
    d = _tiny_demands()
    a = RS.simulate(d, capacity=3, prefetch_depth=1, policy="prefetch")
    b = RS.simulate(d, capacity=3, prefetch_depth=1, policy="prefetch")
    assert a.to_dict() == b.to_dict()


def test_timing_model_monotonic_bandwidth():
    d = _tiny_demands()
    slow = RS.PlatformCost("p", 9437184, 8e9 / 8, 2e-6, 2)   # 8 Gbps
    fast = RS.PlatformCost("p", 9437184, 64e9 / 8, 2e-6, 2)  # 64 Gbps
    r_slow = RS.simulate(d, 3, 0, "on_demand", cost=slow, per_layer_compute_time_s=200e-6)
    r_fast = RS.simulate(d, 3, 0, "on_demand", cost=fast, per_layer_compute_time_s=200e-6)
    # faster link -> less stall
    assert r_fast.total_stall_time_s < r_slow.total_stall_time_s
    assert r_slow.total_time_s > r_fast.total_time_s


def test_ordering_respected():
    # timestamps within a batch must be processed in ascending layer_step
    d = [
        RS.LayerDemand("0", 0, [0], {0: 1}),
        RS.LayerDemand("0", 1, [1], {1: 1}),
        RS.LayerDemand("0", 2, [0], {0: 1}),
    ]
    r = RS.simulate(d, capacity=1, prefetch_depth=0, policy="on_demand")
    # 0 -> miss, 1 -> miss(evict0), 0 -> miss(evict1): 3 misses
    assert r.demand_misses == 3
