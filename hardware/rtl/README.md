# Full-datapath residency/prefetch RTL (S5)

`moe_residency_top` realizes the frozen MoE expert residency/prefetch algorithm
(`semantic_revision=1`) as a real hardware datapath, not a control skeleton.

## Datapath

```
step transaction {cur_mask, fut_mask}   (valid/ready)
  -> decode & validation (out-of-range experts -> o_input_error)
  -> residency tag table {resident, pending, last_used[]} access
  -> demand phase  : per-expert; miss -> LRU victim search -> DMA issue -> occupy slot
  -> prefetch phase: depth-1 via fut_mask; protective LRU (protect = future set)
  -> DMA descriptor issue (dma_model: finite outstanding, latency, backpressure)
  -> step-complete transaction with cumulative counters (valid/ready)
```

- `rtl/common/moe_pkg.sv`      - params/types (MAX_EXPERTS=32, widths, DMA kind)
- `rtl/datapath/residency_engine.sv` - the engine (FSM: IDLE/DEMAND/PREFETCH/DONE/ERR)
- `rtl/interfaces/dma_model.sv` - DMA/memory interface model (latency+capacity+backpressure+completion)
- `rtl/top/moe_residency_top.sv` - synthesis-elaboratable top wiring engine + dma_model
- `rtl/verification/tb_residency.cpp` - Verilator scoreboard (golden = firmware/scheduler.c)

`cfg_num_experts` and `cfg_capacity` are runtime inputs (parameterized config).
`fut_mask = 0` yields the on-demand baseline; `fut_mask = next-step experts`
yields depth-1 prefetch, using the SAME hardware.

## Handled hardware concerns

finite capacity; LRU ordering (recency timestamps); backpressure (stall on
`dma_req_ready` low); queue full/empty (dma_model outstanding slots); invalid
input (range check -> error transaction); reset/recovery (sync counters clear);
parameterized configuration (runtime capacity / expert count).

## Equivalence contract

Counters are updated at DECISION (issue) time, so they match the software /
firmware / Python reference bit-for-bit. DMA latency and outstanding-depth affect
only timing (backpressure); they are verified NOT to change functional counters
(default vs stress DMA config both pass).

## Build & verify (dedicated Docker)

```bash
docker build -t edgehetero-rtl:1 -f rtl/Dockerfile rtl

# export a demands file (see scripts/export_demands.py), then:
docker run --rm -v "$PWD":/work -v /tmp/s4:/data -w /work \
  edgehetero-rtl:1 bash rtl/run_verify.sh /data/demands_32.txt 8,16,24,28,32

# heavy-backpressure stress config:
docker run --rm -v "$PWD":/work -v /tmp/s4:/data -w /work \
  -e DMA_DEPTH=1 -e DMA_LATENCY=32 \
  edgehetero-rtl:1 bash rtl/run_verify.sh /data/demands_32.txt 8,16,24,28,32
```

Checks: lint, deterministic equivalence (2 depths x 5 caps), corner cases
(empty / invalid / reset), 300 randomized property trials. Full synthesis
(timing/area) is S6.
