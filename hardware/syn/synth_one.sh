#!/usr/bin/env bash
# Synthesize moe_residency_top for one DSE point (MAX_EXPERTS x TS_W).
# Produces tech-independent area proxy (2-input gate count, DFF count) and a
# critical-path proxy (longest topological logic-path length). These are PROXIES,
# NOT real um^2/ns/mW (no PDK liberty / STA here) -- see project contract.
set -euo pipefail

ME="${1:?max_experts}"
TS="${2:?ts_w}"
OUT="${3:?out_dir}"
ROOT="${ROOT:-/work}"
mkdir -p "$OUT"

V="$OUT/flat_me${ME}_ts${TS}.v"

# 1) lower SystemVerilog (package/enum/typedef) to Verilog-2005
sv2v -DMOE_MAX_EXPERTS="$ME" -DMOE_TS_W="$TS" \
  "$ROOT/rtl/common/moe_pkg.sv" \
  "$ROOT/rtl/datapath/residency_engine.sv" \
  "$ROOT/rtl/interfaces/dma_model.sv" \
  "$ROOT/rtl/top/moe_residency_top.sv" > "$V"

# 2) synthesize + map to 2-input gates; report area proxy + logic depth
yosys -q -p "
read_verilog $V;
synth -top moe_residency_top -flatten;
abc -g AND;
opt_clean;
tee -q -o $OUT/stat_me${ME}_ts${TS}.txt stat;
tee -q -o $OUT/ltp_me${ME}_ts${TS}.txt ltp -noff;
" 2> "$OUT/yosys_me${ME}_ts${TS}.log"

echo "OK me=$ME ts=$TS"
