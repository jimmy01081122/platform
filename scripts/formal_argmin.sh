#!/usr/bin/env bash
# Formal (complete bounded) equivalence proof of the sequential LRU-victim argmins
# vs the combinational reference lru_victim_comb, using Yosys' native `sat` engine
# (minisat, no external SMT solver / no sby required).
#
# For each config we (1) sanity-check that `done` is REACHABLE (so the property is
# not vacuous), then (2) prove prop_ok == 1 for every step of a BMC unrolled to the
# FSM's bounded termination depth. Because valid/ts are latched at cycle 0 (and held
# stable, matching the DUT contract), one BMC to that depth is COMPLETE over all
# inputs -- upgrading the 2000-trial random equivalence to an exhaustive proof.
#
# Usage: scripts/formal_argmin.sh            (default config sweep)
# Runs in edgehetero-syn:1 (sv2v + yosys 0.23). Writes formal/out/formal_argmin.json
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="edgehetero-syn:1"
OUT="$ROOT/formal/out"; mkdir -p "$OUT"
JSON="$OUT/formal_argmin.json"

# config: DUT:N:TSW:B:DEPTH  (B ignored for seq; DEPTH >= reset + start + W + B + 2)
CONFIGS=(
  "banked:16:4:4:20"
  "banked:32:5:4:28"
  "banked:64:6:8:28"
  "seq:16:4:4:24"
  "seq:32:5:4:40"
)

run_one() {
  local dut=$1 N=$2 TSW=$3 B=$4 D=$5
  local def=""; [[ "$dut" == "seq" ]] && def="-DFORMAL_SEQ"
  local tag="${dut}_N${N}_T${TSW}_B${B}"
  local v="$OUT/formal_${tag}.v"
  docker run --rm -v "$ROOT":/work -w /work "$IMG" bash -lc "
    sv2v -DFN=$N -DFTSW=$TSW -DFB=$B $def \
      rtl/datapath/lru_victim.sv rtl/verification/lru_victim_formal.sv > /work/${v#$ROOT/} 2>/dev/null
    reach=\$(yosys -p \"read_verilog /work/${v#$ROOT/}; hierarchy -top lru_victim_formal; proc; async2sync; flatten; opt -keepdc; sat -seq $D -set-init-zero -prove d_done_obs 1'b0\" 2>&1 | grep -oiE '(no model found: SUCCESS|model found: FAIL)' | tail -1)
    prove_raw=\$(yosys -p \"read_verilog /work/${v#$ROOT/}; hierarchy -top lru_victim_formal; proc; async2sync; flatten; opt -keepdc; sat -seq $D -set-init-zero -prove prop_ok 1'b1 -verify\" 2>&1)
    echo \"REACH_RESULT: \$reach\"
    echo \"PROVE_RESULT: \$(echo \"\$prove_raw\" | grep -oiE '(no model found: SUCCESS|model found: FAIL)' | tail -1)\"
    echo \"PROVE_VARS: \$(echo \"\$prove_raw\" | grep -oE 'Solving problem with [0-9]+ variables' | grep -oE '[0-9]+' | tail -1)\"
  "
}

echo "{" > "$JSON"
echo '  "note": "Yosys native sat BMC; reach=done reachable (property non-vacuous); prove=prop_ok holds for all inputs to depth.",' >> "$JSON"
echo '  "results": [' >> "$JSON"
first=1
for cfg in "${CONFIGS[@]}"; do
  IFS=':' read -r dut N TSW B D <<< "$cfg"
  echo "########## $dut N=$N TSW=$TSW B=$B depth=$D ##########"
  log="$(run_one "$dut" "$N" "$TSW" "$B" "$D")"
  echo "$log"
  # reach: `-prove d_done_obs 0` FAILS (model found) -> done reachable -> non-vacuous
  reach="unknown"
  echo "$log" | grep "REACH_RESULT:" | grep -qi "model found: FAIL"       && reach="reachable"
  echo "$log" | grep "REACH_RESULT:" | grep -qi "no model found: SUCCESS" && reach="UNREACHABLE(vacuous!)"
  # prove: `-prove prop_ok 1` SUCCEEDS (no model) -> no counterexample -> equivalent
  prove="unknown"
  echo "$log" | grep "PROVE_RESULT:" | grep -qi "no model found: SUCCESS" && prove="PROVEN"
  echo "$log" | grep "PROVE_RESULT:" | grep -qi "model found: FAIL"       && prove="COUNTEREXAMPLE"
  vars="$(echo "$log" | grep "PROVE_VARS:" | grep -oE '[0-9]+' | tail -1)"
  [[ $first -eq 0 ]] && echo "," >> "$JSON"; first=0
  printf '    {"dut":"%s","N":%s,"TSW":%s,"B":%s,"depth":%s,"reachable":"%s","proof":"%s","sat_variables":%s}' \
    "$dut" "$N" "$TSW" "$B" "$D" "$reach" "$prove" "${vars:-0}" >> "$JSON"
done
echo "" >> "$JSON"; echo "  ]" >> "$JSON"; echo "}" >> "$JSON"
echo; echo "wrote $JSON"; cat "$JSON"
