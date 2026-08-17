#!/usr/bin/env bash
# Generate a mapped gate-level netlist (Nangate45) for OpenSTA sign-off-lite timing.
# Runs in edgehetero-syn:1 (sv2v + yosys). Optionally fanout-legalizes (abc buffer).
#   gen_netlist.sh <me> <ts> <out_dir> <tag> [DEFS] [BUFFER_N]
set -euo pipefail
ME="${1:?}"; TS="${2:?}"; OUT="${3:?}"; TAG="${4:?}"
DEFS="${5:-}"; BUFN="${6:-}"
ROOT="${ROOT:-/work}"; LIB="${LIB:-$ROOT/syn/lib/nangate45.lib}"
mkdir -p "$OUT"
V="$OUT/flat_${TAG}.v"; NL="$OUT/netlist_${TAG}.v"; YS="$OUT/gen_${TAG}.ys"; ABC="$OUT/abc_${TAG}.do"

sv2v -DMOE_MAX_EXPERTS="$ME" -DMOE_TS_W="$TS" $DEFS \
  "$ROOT/rtl/common/moe_pkg.sv" "$ROOT/rtl/datapath/lru_victim.sv" \
  "$ROOT/rtl/datapath/residency_engine.sv" \
  "$ROOT/rtl/interfaces/dma_model.sv" "$ROOT/rtl/top/moe_residency_top.sv" > "$V"

if [[ -n "$BUFN" ]]; then
  printf 'strash\ndch\nmap\nbuffer -N %s\nupsize\ndnsize\ntopo\n' "$BUFN" > "$ABC"
else
  printf 'strash\ndch\nmap\ntopo\n' > "$ABC"
fi

cat > "$YS" <<EOF
read_verilog $V
synth -top moe_residency_top -flatten
dfflibmap -liberty $LIB
abc -liberty $LIB -script $ABC
clean
write_verilog -noattr $NL
EOF

yosys -s "$YS" > "$OUT/gen_${TAG}.log" 2>&1
echo "netlist: $NL"
