# Tool Selection Methodology

Charter §"開源軟體、模擬器與既有框架的納入原則" requires, per major capability, an
explicit **reuse / extend / combine / build** decision based on evidence quality,
calibratability, reproducibility, and integration cost — and requires that adopted and
rejected tools be recorded (here, in `project/capability_registry.yaml`, and in
`docs/status/DECISION_LOG.md`). This document is the human-readable rationale; the
machine-readable per-tool provenance lives in `project/capability_registry.yaml`.

Guiding principle (charter §工具選擇原則): higher fidelity is **not** automatically
better. Where a high-fidelity tool cannot be calibrated within reach, runs too long, or
does not match the target hardware, a lower-cost **calibratable** model is the primary
backend and the high-fidelity tool is reserved for small cross-checks. All external
outputs are validated against a fixture, an internal reference, an independent tool, a
public spec, or a monotonicity/conservation check before use.

## Decision matrix (by capability)

| capability | decision | adopted | fidelity | why | rejected / deferred (+trigger) |
|---|---|---|---|---|---|
| workload capture / trace import | reuse + build | `huggingface_hub` (download) + internal `edgeflow.moe_routing` (canonicalize) | measured (W2) | no external tool emits our `moe-routing-v1` contract; download is a solved problem | — |
| reference model | build | `edgeflow.residency` (Py) + `scheduler.c` (C) | golden | must be the trusted oracle for cross-layer equivalence | — |
| event simulation | build | internal deterministic trace-driven sim | simulated | deterministic, fast, tightly bound to the canonical contract; enables the DSE volume | **SimPy** deferred — adds a dependency with no benefit for this deterministic residency model; trigger: stochastic/queueing dynamics that need a DES kernel |
| firmware execution (RV64) | reuse | `riscv64-linux-gnu-gcc` 12.2 + `qemu-user` (`qemu-riscv64` 7.2.22) | measured (instret) | real ISA-level instruction count for the FW break-even; already integrated, license-clean | **Spike** rejected — qemu-user already gives instret and is integrated; no added value now |
| system simulation (CPU/cache/DMA/MMIO) | **defer** | none (analytical FW+transfer model) | H1 (swept) | the transfer-bound verdict has a 79–8460× margin over control cost, so CPU/DMA cycles do not change the conclusion; gem5 integration cost is high | **gem5** deferred → trigger: a **compute-bound** operating region appears, or FW/DMA control overhead approaches the transfer cost (margin < ~2×) |
| GPU microarchitecture | **required optional backend; integration pending** | candidate evaluation: Accel-Sim / GPGPU-Sim | detailed | current contract requires representative selected-expert/grouped-GEMM/gather-scatter/window cross-checks, not whole-DSE cycle simulation | adopt after adapter smoke + architecture mapping + unseen-shape validation |
| memory / DRAM timing | **adopt (standalone simulator)** | Ramulator2 2.1 | cycle-simulated | provides standalone DRAM timing evidence; it is not real-hardware measurement and not simulator-in-the-loop | in-loop integration remains optional when calibrated-domain error requires it |
| RTL simulation | reuse | **Verilator 5.006** | block-level rtl-transaction / RTL-simulated | current tests cover block functionality and pipeline cycles | full `rtl-cycle` still requires AXI/PCIe/tagged completion/memory response/queue contracts |
| formal verification | build (reuse Yosys sat) | **Yosys native `sat`** (minisat) | complete_bounded_bmc | banked/seq argmin PROVEN ≡ comb reference for ALL inputs at N=16/32/64 (D-051), non-vacuous; no external solver needed | **SymbiYosys / external SMT** deferred → trigger: parameter-general (unbounded-N) induction |
| logic synthesis | reuse | **Yosys 0.23** + **sv2v 0.0.12** + bundled **ABC** | tool_default | open, scriptable, license-clean; sv2v bridges SystemVerilog → Yosys | commercial synth rejected (license/repro) |
| timing analysis | reuse | **OpenSTA 3.1.0** + Nangate45 wire-load | synthesis-derived / predictive-library pre-layout DSE | supports same-flow relative ordering and path diagnosis only | sky130 flow validation + routed SPEF/STA for selected candidate |
| power estimation | **not yet valid for current claims** | historical OpenSTA default-activity output | unknown | lacks workload VCD/SAIF, CTS clock tree and routed parasitics | output power only after valid library, clock and activity trace |
| physical design (PnR) | **adopt in two tracks** | OpenROAD/OpenLane candidate | sky130 flow-validation; ASAP7 consistent architecture DSE | new contract requires reproducible PD infrastructure while keeping PDK roles separate | candidate-specific PnR gated by calibrated workload benefit |
| design-space exploration | build | internal Python sweeps + Pareto | swept | the DSE spaces (capacity, depth, N, TS_W, engines, bandwidth) are small and enumerable; determinism matters | **Optuna / nevergrad** deferred → trigger: high-dimensional continuous search where exhaustive sweeps become intractable |
| containerization / reproducibility | reuse | **Docker 29.2.1** (pinned images) | — | pinned per-tool images give clean-env reproduction | Nix/Podman not needed given Docker works |

## Tool-credibility validation performed (charter §工具可信度驗證)

Every adopted tool's output is checked before it is trusted:

- **Verilator / C / RV64**: bit-for-bit against the Python reference (four-layer
  equivalence, `994/383/1377/1349`); directed corners + 300/300 randomized property.
- **qemu-user (RV64)**: instret consistency — same counters as native C, only the
  instruction count differs (expected).
- **Yosys/sv2v**: functional equivalence preserved (post-synth behavior implied by the
  same RTL the scoreboard verifies; argmin blocks re-verified after each redesign).
- **OpenSTA**: monotonicity/sanity — Fmax degrades with N (~1/N) as expected; unbuffered
  vs buffered ordering is consistent; E=1 sanity where two timing models coincide.
- **Analytical transfer model**: conservation (bytes, transfers) cross-checked against
  the residency counters; monotonic in bandwidth; the shared-link copy-engine model
  reduces to the optimistic model at E=1 (D-048).

## Non-adoption is a first-class, revisitable decision

The current contract supersedes the legacy deferral argument. GPU-cycle support and
sky130/ASAP7 PD infrastructure are now required platform capabilities, although they
remain optional per experiment and candidate-specific PnR is workload-gated.
Ramulator2 remains a standalone `cycle-simulated` memory tool. The analytical backend
may become the primary calibrated backend only after disjoint real-GPU validation
passes the declared MAPE gates; current margins do not substitute for that validation.
