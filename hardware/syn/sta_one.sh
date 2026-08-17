#!/usr/bin/env bash
# Gate-level STA-lite for one DSE point using an academic standard-cell library
# (Nangate45 / FreePDK45, typical corner). Reports REAL cell-level area (um^2) and
# critical-path delay (ns) from abc timing-driven mapping. NOTE: cell delays only,
# no wire-load / routing parasitics and no sign-off STA -> higher evidence than the
# AIG proxy but still not production sign-off. FreePDK45 is academic-use.
set -euo pipefail

ME="${1:?max_experts}"; TS="${2:?ts_w}"; OUT="${3:?out_dir}"
ROOT="${ROOT:-/work}"
LIB="${LIB:-$ROOT/syn/lib/nangate45.lib}"
mkdir -p "$OUT"
V="$OUT/sta_flat_me${ME}_ts${TS}.v"
YS="$OUT/sta_me${ME}_ts${TS}.ys"

# STA_DEFS: extra sv2v defines, e.g. STA_DEFS="-DMOE_SEQ_ARGMIN" for the
# sequential-argmin engine variant.
STA_DEFS="${STA_DEFS:-}"
sv2v -DMOE_MAX_EXPERTS="$ME" -DMOE_TS_W="$TS" $STA_DEFS \
  "$ROOT/rtl/common/moe_pkg.sv" "$ROOT/rtl/datapath/residency_engine.sv" \
  "$ROOT/rtl/interfaces/dma_model.sv" "$ROOT/rtl/top/moe_residency_top.sv" > "$V"

# STA_BUFFER=<N> enables fanout legalization (buffer tree + cell sizing) after
# mapping, better reflecting how real synthesis handles high-fanout nets (e.g. the
# idx-indexed 32-wide array updates). Unset => raw mapping (fanout-slew pessimistic).
STA_BUFFER="${STA_BUFFER:-}"
ABC="$OUT/abc_me${ME}_ts${TS}.do"
if [[ -n "$STA_BUFFER" ]]; then
  printf 'strash\ndch\nmap\nbuffer -N %s\nupsize\ndnsize\ntopo\nstime\n' "$STA_BUFFER" > "$ABC"
else
  printf 'strash\ndch\nmap\ntopo\nstime\n' > "$ABC"
fi

cat > "$YS" <<EOF
read_verilog $V
synth -top moe_residency_top -flatten
dfflibmap -liberty $LIB
abc -liberty $LIB -script $ABC
stat -liberty $LIB
EOF

yosys -s "$YS" 2>&1 | tee "$OUT/sta_me${ME}_ts${TS}.log" \
  | grep -iE "Delay =|Chip area" || true
echo "OK sta me=$ME ts=$TS"
