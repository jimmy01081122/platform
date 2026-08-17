# Tagged DMA DV evidence

## Scope and exit criteria

This DV slice covers the tagged request/completion path without changing the
synthesizable DUT:

- every accepted request produces at most one matching completion;
- accepted = completed + occupancy at every checked cycle;
- completion payload remains stable while `valid && !ready`;
- residency can rise only after a matching completion handshake;
- request and completion stalls are reached, rather than leaving the protocol
  properties vacuous.

The reusable components are:

- `rtl/verification/dma_transaction_scoreboard.hpp`: transaction scoreboard and
  seeded variable-latency responder;
- `rtl/verification/ready_valid_fifo_assertions.sv`: bindable ready/valid,
  overflow/underflow, stability, and occupancy assertions;
- `rtl/verification/dma_model_assert_bind.sv`: DMA-specific assertion adapter;
- `rtl/verification/dma_model_formal.sv` and
  `rtl/verification/residency_completion_formal.sv`: small-parameter harnesses.

## Reproduction

```bash
scripts/verify_dma.sh
scripts/formal_dma.sh
scripts/formal_residency_completion.sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/work -w /work \
  -e OUT_DIR=/work/verification/out/residency_legacy \
  edgehetero-rtl:1 bash rtl/run_verify.sh \
  /work/data/fixtures/switch_base32_mbpp_bs4_len128/demands.txt \
  8,16,24,28,32
```

Images used by the scripts are `edgehetero-rtl:1` (Verilator 5.006) and
`edgehetero-syn:1` (sv2v plus Yosys 0.23 native SAT).

## Results recorded on 2026-07-18

Verilator regression:

- DMA block: 500 accepted and 500 completed transactions in 1119 cycles;
  439 request-stall cycles, 295 completion-stall cycles, 318 simultaneous
  request/completion cycles, 1114 occupancy checks, zero stability errors.
- Residency integration: 120/120 steps completed; 187 requests and completions;
  13 observed response latencies, 43 output-stall cycles, 187 resident rises,
  zero premature-resident observations, and 4012 occupancy checks.
- Existing full-residency scoreboard remained compatible: all ten directed
  capacity/depth cases, empty/invalid/reset cases, and 300/300 fixed-seed random
  trials passed with zero failures.
- Verilator line coverage emitted for the DMA block/checker build:
  126/128 points hit (98.44%). No branch metric was emitted.

Formal:

- `formal/out/formal_dma.json`: bounded proof to depth 12, DMA depth 3 and
  three-bit monotonically allocated formal tags. Stall and completion are both
  reachable. Conservation, underflow prevention, completion stability, no
  spurious/duplicate completion, and payload match are proven within the bound.
- `formal/out/formal_residency_completion.json`: bounded proof to depth 24,
  four experts and capacity two. Request and completion are reachable.
  Single-outstanding conservation, tagged payload match, and
  completion-before-resident are proven within the bound.

Raw evidence:

- `verification/out/dma_verilator/regression.log`
- `verification/out/dma_verilator/obj_dir/coverage.dat`
- `verification/out/dma_verilator/obj_dir/coverage.info`
- `verification/out/residency_legacy/regression.log`
- `formal/out/formal_dma.log`
- `formal/out/formal_residency_completion.log`

## Remaining coverage gaps

- The SAT results are bounded safety proofs, not unbounded induction and not a
  liveness proof under permanent completion backpressure.
- Formal uses reduced expert/tag parameters; production-width tag wrap and long
  reuse windows remain regression targets.
- Code coverage was collected only for the DMA block assertion build. The
  residency variable-latency integration test has no code-coverage artifact.
- Verilator emitted no branch metric. The two unhit line points are the
  compile-time latency-jitter branch markers in `dma_model.sv`; a separate
  `LATENCY_JITTER=0` coverage build was not run.
- No gate-level, CDC, reset-domain, or AXI/PCIe protocol verification is part of
  this transaction-level slice.
