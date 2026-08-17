#!/usr/bin/env bash
# Real gate-level STA-lite across the DSE grid (Nangate45 typical). Reports cell
# area (um^2) and critical-path delay (ns). Cell delays only (no wire parasitics,
# no sign-off STA); FreePDK45 is academic-use. Higher evidence than the AIG proxy.
set -uo pipefail
ROOT="${ROOT:-/work}"
OUT="${OUT_DIR:-/work/syn/out_sta}"
mkdir -p "$OUT"
CSV="$OUT/sta_dse.csv"
echo "max_experts,ts_w,delay_ns,fmax_mhz,chip_area_um2,sta_ok" > "$CSV"
ME_LIST="${ME_LIST:-8 16 32 64}"
TS_LIST="${TS_LIST:-8 16 32}"

for ME in $ME_LIST; do
  for TS in $TS_LIST; do
    echo ">> sta me=$ME ts=$TS" >&2
    if bash "$ROOT/syn/sta_one.sh" "$ME" "$TS" "$OUT" >/dev/null 2>>"$OUT/sta.log"; then
      L="$OUT/sta_me${ME}_ts${TS}.log"
      dps=$(grep -oE 'Delay =[ ]*[0-9.]+ ps' "$L" | tail -1 | grep -oE '[0-9.]+' | head -1)
      area=$(grep -oE "Chip area for module '..moe_residency_top': [0-9.]+" "$L" | tail -1 | grep -oE '[0-9.]+' | tail -1)
      if [[ -n "$dps" ]]; then
        ns=$(awk -v d="$dps" 'BEGIN{printf "%.3f", d/1000.0}')
        fmax=$(awk -v d="$dps" 'BEGIN{printf "%.2f", 1e6/d}')
      else ns=""; fmax=""; fi
      echo "$ME,$TS,${ns:-},${fmax:-},${area:-},1" >> "$CSV"
    else
      echo "$ME,$TS,,,,0" >> "$CSV"
    fi
  done
done
echo "== STA CSV ==" >&2
cat "$CSV"
