#!/usr/bin/env bash
# S6 DSE: sweep two independent architecture parameters and collect synthesis
# proxies (2-input gate count = area proxy; DFF count; longest topological path =
# critical-path proxy). Proxies only -- NOT real um^2/ns/mW.
set -euo pipefail

ROOT="${ROOT:-/work}"
OUT="${OUT_DIR:-/work/syn/out_dse}"
mkdir -p "$OUT"
CSV="$OUT/dse_synth.csv"
echo "max_experts,ts_w,cells,dff,and_gates,not_gates,ltp_len,synth_ok" > "$CSV"

ME_LIST="${ME_LIST:-8 16 32 64}"
TS_LIST="${TS_LIST:-8 16 32}"

for ME in $ME_LIST; do
  for TS in $TS_LIST; do
    echo ">> synth me=$ME ts=$TS" >&2
    if bash "$ROOT/syn/synth_one.sh" "$ME" "$TS" "$OUT" >/dev/null 2>>"$OUT/dse.log"; then
      S="$OUT/stat_me${ME}_ts${TS}.txt"
      L="$OUT/ltp_me${ME}_ts${TS}.txt"
      cells=$(awk '/Number of cells:/{print $NF}' "$S"); cells=${cells:-0}
      andg=$(awk '/\$_AND_/{print $NF}' "$S" | head -1); andg=${andg:-0}
      notg=$(awk '/\$_NOT_/{print $NF}' "$S" | head -1); notg=${notg:-0}
      dff=$(awk '/\$_DFF/{s+=$NF} END{print s+0}' "$S"); dff=${dff:-0}
      ltp=$(grep -oE 'length=[0-9]+' "$L" | grep -oE '[0-9]+' | head -1); ltp=${ltp:-0}
      echo "$ME,$TS,$cells,$dff,$andg,$notg,$ltp,1" >> "$CSV"
    else
      echo "$ME,$TS,,,,,,0" >> "$CSV"
      echo ">> FAILED me=$ME ts=$TS" >&2
    fi
  done
done

echo "== DSE CSV ==" >&2
cat "$CSV"
