#!/usr/bin/env bash
# Bounded proof that residency transitions are completion-driven.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="${SYN_IMAGE:-edgehetero-syn:1}"
DEPTH="${FORMAL_DEPTH:-24}"
OUT="$ROOT/formal/out"
mkdir -p "$OUT"
V="$OUT/residency_completion_formal.v"
INSTRUMENTED="$OUT/residency_engine_formal_instrumented.sv"
LOG="$OUT/formal_residency_completion.log"
JSON="$OUT/formal_residency_completion.json"

# Instrument only the generated formal copy. The synthesizable DUT source is
# untouched, while the assertion is placed lexically beside the private
# `resident` bitmap so sv2v/Yosys can resolve it without hierarchical taps.
python3 - "$ROOT/rtl/datapath/residency_engine.sv" "$INSTRUMENTED" <<'PY'
from pathlib import Path
import sys

src = Path(sys.argv[1]).read_text()
checker = r'''
  logic [MAX_EXPERTS-1:0] formal_resident_q;
  logic formal_p_completion_fire;
  logic [EID_W-1:0] formal_p_completion_expert;
  logic formal_rise_has_completion;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      formal_resident_q <= '0;
      formal_p_completion_fire <= 1'b0;
      formal_p_completion_expert <= '0;
    end else begin
      formal_resident_q <= resident;
      formal_p_completion_fire <= dma_cmpl_valid && dma_cmpl_ready;
      formal_p_completion_expert <= dma_cmpl_expert;
    end
  end

  always_comb begin
    formal_rise_has_completion = 1'b1;
    for (int formal_e = 0; formal_e < MAX_EXPERTS; formal_e++)
      if (resident[formal_e] && !formal_resident_q[formal_e] &&
          !(formal_p_completion_fire &&
            formal_p_completion_expert == formal_e[EID_W-1:0]))
        formal_rise_has_completion = 1'b0;
  end

  always_ff @(posedge clk)
    if (rst_n)
      assert (formal_rise_has_completion);
'''
head, sep, tail = src.rpartition("endmodule")
if not sep or tail.strip():
    raise SystemExit("cannot locate final residency_engine endmodule")
Path(sys.argv[2]).write_text(head + checker + "\nendmodule\n")
PY

docker run --rm -v "$ROOT":/work -w /work "$IMG" bash -lc \
  "sv2v -DMOE_MAX_EXPERTS=4 -DFNE=4 -DFCAP=2 \
     rtl/common/moe_pkg.sv rtl/datapath/lru_victim.sv \
     formal/out/residency_engine_formal_instrumented.sv \
     rtl/verification/residency_completion_formal.sv \
     > formal/out/residency_completion_formal.v"

run_sat() {
  local signal="$1" value="$2" verify="${3:-}"
  docker run --rm -v "$ROOT":/work -w /work "$IMG" \
    yosys -p "read_verilog formal/out/residency_completion_formal.v;
      hierarchy -top residency_completion_formal; proc; async2sync; flatten;
      opt -keepdc; sat -seq $DEPTH -set-init-zero -prove $signal $value $verify"
}
run_asserts() {
  docker run --rm -v "$ROOT":/work -w /work "$IMG" \
    yosys -p "read_verilog -formal formal/out/residency_completion_formal.v;
      hierarchy -top residency_completion_formal; proc; async2sync; flatten;
      opt -keepdc; sat -seq $DEPTH -set-init-zero -prove-asserts -verify"
}

set +e
request_raw="$(run_sat request_obs "1'b0" 2>&1)"; request_rc=$?
completion_raw="$(run_sat completion_obs "1'b0" 2>&1)"; completion_rc=$?
prove_raw="$(run_sat prop_all "1'b1" "-verify" 2>&1)"; prove_rc=$?
assert_raw="$(run_asserts 2>&1)"; assert_rc=$?
set -e

result_of() {
  if grep -qi "no model found: SUCCESS" <<<"$1"; then echo "NO_MODEL"
  elif grep -qi "model found: FAIL" <<<"$1"; then echo "MODEL_FOUND"
  else echo "TOOL_ERROR"
  fi
}
request_result="$(result_of "$request_raw")"
completion_result="$(result_of "$completion_raw")"
prove_result="$(result_of "$prove_raw")"
assert_result="$(result_of "$assert_raw")"
vars="$(grep -oE 'Solving problem with [0-9]+ variables' <<<"$prove_raw" |
        grep -oE '[0-9]+' | tail -1 || true)"

{
  echo "DEPTH: $DEPTH"
  echo "REQUEST_REACH_RESULT: $request_result (rc=$request_rc)"
  echo "COMPLETION_REACH_RESULT: $completion_result (rc=$completion_rc)"
  echo "PROVE_RESULT: $prove_result (rc=$prove_rc)"
  echo "ASSERT_PROVE_RESULT: $assert_result (rc=$assert_rc)"
  echo "PROVE_VARS: ${vars:-0}"
  echo
  echo "===== PROOF RAW LOG ====="
  echo "$prove_raw"
  echo
  echo "===== REQUEST REACH RAW LOG ====="
  echo "$request_raw"
  echo
  echo "===== COMPLETION REACH RAW LOG ====="
  echo "$completion_raw"
  echo
  echo "===== COMPLETION-BEFORE-RESIDENT ASSERT RAW LOG ====="
  echo "$assert_raw"
} > "$LOG"

request_reachable=false
completion_reachable=false
[[ "$request_result" == "MODEL_FOUND" ]] && request_reachable=true
[[ "$completion_result" == "MODEL_FOUND" ]] && completion_reachable=true
proof='"TOOL_ERROR"'
[[ "$prove_result" == "NO_MODEL" && $prove_rc -eq 0 &&
   "$assert_result" == "NO_MODEL" && $assert_rc -eq 0 ]] &&
  proof='"PROVEN_BOUNDED"'
[[ "$prove_result" == "MODEL_FOUND" || "$assert_result" == "MODEL_FOUND" ]] &&
  proof='"COUNTEREXAMPLE"'

cat > "$JSON" <<EOF
{
  "schema_version": "formal-residency-completion-v1",
  "depth": $DEPTH,
  "num_experts": 4,
  "capacity": 2,
  "request_reachable": $request_reachable,
  "completion_reachable": $completion_reachable,
  "proof": $proof,
  "properties": [
    "single_outstanding_conservation",
    "tagged_completion_payload_match",
    "completion_before_resident"
  ],
  "sat_variables": ${vars:-0}
}
EOF

cat "$JSON"
if [[ "$proof" != '"PROVEN_BOUNDED"' ||
      "$request_reachable" != true ||
      "$completion_reachable" != true ]]; then
  echo "formal residency-completion regression failed; inspect $LOG" >&2
  exit 1
fi
