#!/usr/bin/env bash
# Bounded safety proof for the tagged DMA request/completion contract.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="${SYN_IMAGE:-edgehetero-syn:1}"
DEPTH="${FORMAL_DEPTH:-12}"
OUT="$ROOT/formal/out"
mkdir -p "$OUT"

SV2V_OUT="$OUT/dma_model_formal.v"
LOG="$OUT/formal_dma.log"
JSON="$OUT/formal_dma.json"

docker run --rm -v "$ROOT":/work -w /work "$IMG" bash -lc \
  "sv2v -DMOE_MAX_EXPERTS=8 -DMOE_DMA_TAG_W=3 \
     rtl/common/moe_pkg.sv rtl/interfaces/dma_model.sv \
     rtl/verification/dma_model_formal.sv \
     > formal/out/dma_model_formal.v"

run_sat() {
  local prove_signal="$1" prove_value="$2" verify="${3:-}"
  docker run --rm -v "$ROOT":/work -w /work "$IMG" \
    yosys -p "read_verilog -formal formal/out/dma_model_formal.v;
      hierarchy -top dma_model_formal; proc; async2sync; flatten;
      opt -keepdc; sat -seq $DEPTH -set-init-zero -set-assumes -prove $prove_signal $prove_value $verify"
}

set +e
stall_raw="$(run_sat stall_obs "1'b0" 2>&1)"
stall_rc=$?
watch_raw="$(run_sat watched_completion_obs "1'b0" 2>&1)"
watch_rc=$?
prove_raw="$(run_sat prop_all "1'b1" "-verify" 2>&1)"
prove_rc=$?
set -e

result_of() {
  local text="$1"
  if grep -qi "no model found: SUCCESS" <<<"$text"; then
    echo "NO_MODEL"
  elif grep -qi "model found: FAIL" <<<"$text"; then
    echo "MODEL_FOUND"
  else
    echo "TOOL_ERROR"
  fi
}

stall_result="$(result_of "$stall_raw")"
watch_result="$(result_of "$watch_raw")"
prove_result="$(result_of "$prove_raw")"
vars="$(grep -oE 'Solving problem with [0-9]+ variables' <<<"$prove_raw" |
        grep -oE '[0-9]+' | tail -1 || true)"

{
  echo "DEPTH: $DEPTH"
  echo "STALL_REACH_RESULT: $stall_result (rc=$stall_rc)"
  echo "WATCHED_COMPLETION_REACH_RESULT: $watch_result (rc=$watch_rc)"
  echo "PROVE_RESULT: $prove_result (rc=$prove_rc)"
  echo "PROVE_VARS: ${vars:-0}"
  echo
  echo "===== PROOF RAW LOG ====="
  echo "$prove_raw"
  echo
  echo "===== STALL REACH RAW LOG ====="
  echo "$stall_raw"
  echo
  echo "===== WATCHED COMPLETION REACH RAW LOG ====="
  echo "$watch_raw"
} > "$LOG"

stall_reachable=false
watch_reachable=false
proof='"TOOL_ERROR"'
[[ "$stall_result" == "MODEL_FOUND" ]] && stall_reachable=true
[[ "$watch_result" == "MODEL_FOUND" ]] && watch_reachable=true
[[ "$prove_result" == "NO_MODEL" && $prove_rc -eq 0 ]] &&
  proof='"PROVEN_BOUNDED"'
[[ "$prove_result" == "MODEL_FOUND" ]] && proof='"COUNTEREXAMPLE"'

cat > "$JSON" <<EOF
{
  "schema_version": "formal-dma-v2",
  "depth": $DEPTH,
  "request_tag_contract": "monotonic_unique_formal_tags",
  "stall_reachable": $stall_reachable,
  "watched_completion_reachable": $watch_reachable,
  "proof": $proof,
  "properties": [
    "occupancy_conservation",
    "no_completion_underflow",
    "completion_stable_while_stalled",
    "symbolic_tag_no_spurious_or_duplicate_completion",
    "symbolic_tag_completion_payload_match"
  ],
  "sat_variables": ${vars:-0}
}
EOF

cat "$JSON"
if [[ "$proof" != '"PROVEN_BOUNDED"' ||
      "$stall_reachable" != true ||
      "$watch_reachable" != true ]]; then
  echo "formal DMA regression failed; inspect $LOG" >&2
  exit 1
fi
