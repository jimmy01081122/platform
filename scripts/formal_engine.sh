#!/usr/bin/env bash
# Formal (bounded) safety-property proof of the full residency_engine using Yosys'
# native `sat` engine (minisat, no external SMT / no sby). Complements the argmin
# equivalence proof (scripts/formal_argmin.sh, D-051) by proving OBSERVABLE-PORT
# safety invariants on the whole datapath+FSM (see rtl/verification/
# residency_engine_formal.sv):
#   prop_hs          : DMA request stability (valid held with same expert/kind until ready)
#   prop_mono        : event counters monotonic non-decreasing
#   prop_miss_le_xfer: demand misses <= transfers
#   prop_no_cfg_err  : no o_input_error under a valid fixed config
#
# For each build/config we (1) confirm a stalled handshake (valid & !ready) is
# REACHABLE (so prop_hs is non-vacuous), then (2) prove prop_all == 1 for every step
# of a BMC unrolled to DEPTH from reset. All step-interface and handshake inputs are
# FREE each cycle, so within DEPTH the proof covers every environment.
#
# Usage: scripts/formal_engine.sh    (default sweep; small MAX_EXPERTS for tractability)
# Runs in edgehetero-syn:1 (sv2v + yosys 0.23). Writes formal/out/formal_engine.json
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="edgehetero-syn:1"
OUT="$ROOT/formal/out"; mkdir -p "$OUT"
JSON="$OUT/formal_engine.json"

# config: BUILD:NE:CAP:TSW:BANKS:DEPTH   (BUILD in comb|banked|seq)
CONFIGS=(
  "comb:8:4:4:4:24"
  "comb:8:1:4:4:24"
  "comb:8:8:4:4:24"
  "banked:8:4:4:4:30"
  "seq:8:4:4:4:30"
)

run_one() {
  local build=$1 NE=$2 CAP=$3 TSW=$4 B=$5 D=$6
  local def=""
  [[ "$build" == "banked" ]] && def="-DMOE_BANKED_ARGMIN"
  [[ "$build" == "seq"    ]] && def="-DMOE_SEQ_ARGMIN"
  local tag="${build}_ne${NE}_c${CAP}"
  local v="$OUT/formal_eng_${tag}.v"
  docker run --rm -v "$ROOT":/work -w /work "$IMG" bash -lc "
    sv2v -DMOE_MAX_EXPERTS=$NE -DMOE_TS_W=$TSW -DMOE_BANKS=$B $def -DFNE=$NE -DFCAP=$CAP \
      rtl/common/moe_pkg.sv rtl/datapath/lru_victim.sv rtl/datapath/residency_engine.sv \
      rtl/interfaces/dma_model.sv rtl/verification/residency_engine_formal.sv \
      > /work/${v#$ROOT/} 2>/dev/null
    reach=\$(yosys -p \"read_verilog /work/${v#$ROOT/}; hierarchy -top residency_engine_formal; proc; async2sync; flatten; opt -keepdc; sat -seq $D -set-init-zero -prove stall_obs 1'b0\" 2>&1 | grep -oiE '(no model found: SUCCESS|model found: FAIL)' | tail -1)
    prove_raw=\$(yosys -p \"read_verilog /work/${v#$ROOT/}; hierarchy -top residency_engine_formal; proc; async2sync; flatten; opt -keepdc; sat -seq $D -set-init-zero -prove prop_all 1'b1 -verify\" 2>&1)
    echo \"REACH_RESULT: \$reach\"
    echo \"PROVE_RESULT: \$(echo \"\$prove_raw\" | grep -oiE '(no model found: SUCCESS|model found: FAIL)' | tail -1)\"
    echo \"PROVE_VARS: \$(echo \"\$prove_raw\" | grep -oE 'Solving problem with [0-9]+ variables' | grep -oE '[0-9]+' | tail -1)\"
  "
}

echo "{" > "$JSON"
echo '  "note": "Yosys native sat BMC on residency_engine observable ports; reach=stalled handshake reachable (prop_hs non-vacuous); prove=prop_all (hs stability + counter monotonicity + miss<=xfer + no cfg-err) holds for all environments to depth.",' >> "$JSON"
echo '  "results": [' >> "$JSON"
first=1
for cfg in "${CONFIGS[@]}"; do
  IFS=':' read -r build NE CAP TSW B D <<< "$cfg"
  echo "########## $build NE=$NE CAP=$CAP TSW=$TSW B=$B depth=$D ##########"
  log="$(run_one "$build" "$NE" "$CAP" "$TSW" "$B" "$D")"
  echo "$log"
  reach="unknown"
  echo "$log" | grep "REACH_RESULT:" | grep -qi "model found: FAIL"       && reach="reachable"
  echo "$log" | grep "REACH_RESULT:" | grep -qi "no model found: SUCCESS" && reach="UNREACHABLE(vacuous!)"
  prove="unknown"
  echo "$log" | grep "PROVE_RESULT:" | grep -qi "no model found: SUCCESS" && prove="PROVEN"
  echo "$log" | grep "PROVE_RESULT:" | grep -qi "model found: FAIL"       && prove="COUNTEREXAMPLE"
  vars="$(echo "$log" | grep "PROVE_VARS:" | grep -oE '[0-9]+' | tail -1)"
  [[ $first -eq 0 ]] && echo "," >> "$JSON"; first=0
  printf '    {"build":"%s","num_experts":%s,"capacity":%s,"TSW":%s,"banks":%s,"depth":%s,"stall_reachable":"%s","proof":"%s","sat_variables":%s}' \
    "$build" "$NE" "$CAP" "$TSW" "$B" "$D" "$reach" "$prove" "${vars:-0}" >> "$JSON"
done
echo "" >> "$JSON"; echo "  ]" >> "$JSON"; echo "}" >> "$JSON"
echo; echo "wrote $JSON"; cat "$JSON"
