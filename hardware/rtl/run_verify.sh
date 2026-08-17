#!/usr/bin/env bash
# Lint + build + run the residency engine scoreboard testbench (Verilator).
# Intended to run inside the dedicated RTL Docker image.
set -euo pipefail

ROOT="${ROOT:-/work}"
DEMANDS="${1:?usage: run_verify.sh <demands_file> <caps_csv>}"
CAPS="${2:-8,16,24,28,32}"
OUT="${OUT_DIR:-/work/out/rtl}"
mkdir -p "$OUT"

RTL_SRCS=(
  "$ROOT/rtl/common/moe_pkg.sv"
  "$ROOT/rtl/datapath/lru_victim.sv"
  "$ROOT/rtl/datapath/residency_engine.sv"
  "$ROOT/rtl/interfaces/dma_model.sv"
  "$ROOT/rtl/top/moe_residency_top.sv"
)

echo "== verilator version ==" >&2
verilator --version >&2

LINT_WAIVERS="-Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-IMPORTSTAR -Wno-UNUSEDPARAM"
# Rebuild cleanly: incremental --build can silently reuse a stale binary when only
# +define+ options change, which would run the wrong engine variant.
rm -rf "$OUT/obj_dir"
# Optional extra Verilator defines, e.g. EXTRA_VDEFS="-DMOE_SEQ_ARGMIN" or
# "-DMOE_BANKED_ARGMIN" to build a registered victim-search variant.
EXTRA_VDEFS="${EXTRA_VDEFS:-}"
# Architecture knobs: residency-table width and banked-argmin bank count. The tb's
# randomized-property expert count follows MOE_MAX_EXPERTS (via TB_MAX_EXPERTS) so we
# verify the engine at the REAL large-MoE widths, not just the default 32.
ME="${MOE_MAX_EXPERTS:-32}"
BANKS_N="${MOE_BANKS:-4}"
ARCH_DEFS="-DMOE_MAX_EXPERTS=$ME -DMOE_BANKS=$BANKS_N"

echo "== lint (defs: ${EXTRA_VDEFS:-none} ME=$ME BANKS=$BANKS_N) ==" >&2
verilator --lint-only -Wall $LINT_WAIVERS $ARCH_DEFS $EXTRA_VDEFS --top-module moe_residency_top "${RTL_SRCS[@]}"

# DMA interface config: default overlap-friendly; STRESS forces heavy backpressure.
DMA_DEPTH="${DMA_DEPTH:-4}"
DMA_LATENCY="${DMA_LATENCY:-8}"

echo "== build (DMA_DEPTH=$DMA_DEPTH DMA_LATENCY=$DMA_LATENCY ME=$ME) ==" >&2
verilator --cc --exe --build -O2 -Wno-fatal $LINT_WAIVERS $ARCH_DEFS $EXTRA_VDEFS \
  --top-module moe_residency_top \
  -GDMA_DEPTH=$DMA_DEPTH -GDMA_LATENCY=$DMA_LATENCY \
  --Mdir "$OUT/obj_dir" \
  -CFLAGS "-I$ROOT/firmware -O2 -DTB_MAX_EXPERTS=$ME" \
  "${RTL_SRCS[@]}" \
  "$ROOT/rtl/verification/tb_residency.cpp" \
  "$ROOT/firmware/scheduler.c" \
  -o Vsim

echo "== run ==" >&2
"$OUT/obj_dir/Vsim" "$DEMANDS" "$CAPS"
