#!/usr/bin/env bash
# S7 single-entry reproduction: from the committed switch-base-32 demands fixture,
# reproduce the cross-layer equivalence (python == C == rv64 == RTL) and the
# P-D/P-I sensitivity, in the dedicated containers. Captures exit codes.
#
# Usage: scripts/reproduce_all.sh [out_dir] [--with-synth] [--with-sta] [--with-moe]
#   --with-synth : optional S6 synthesis DSE (proxy gates/ltp)
#   --with-sta   : optional S6+ real gate-level STA-lite (Nangate45) + argmin fix
#   --with-moe   : optional W2/W3 large-MoE routing reproduction (canonical -> W3
#                  summaries, determinism check) + P-I DRAM-timing calibration
#                  (Ramulator2, if edgehetero-mem:1 is present); skips gracefully if
#                  the HF-derived canonical traces / mem image are not present locally.
# Requires: docker, python3. Images auto-built if missing.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/runs/_repro_tmp}"
WITH_SYNTH=0; WITH_STA=0; WITH_MOE=0
for a in "$@"; do
  [[ "$a" == "--with-synth" ]] && WITH_SYNTH=1
  [[ "$a" == "--with-sta"   ]] && WITH_STA=1
  [[ "$a" == "--with-moe"   ]] && WITH_MOE=1
done
mkdir -p "$OUT"
DEM="$ROOT/data/fixtures/switch_base32_mbpp_bs4_len128/demands.txt"
CAP=28; DEPTH=1
declare -A RC

echo "== environment =="
uname -a; docker --version; python3 --version

ensure_image() { docker image inspect "$1" >/dev/null 2>&1 || docker build -t "$1" -f "$2" "$3"; }
ensure_image edgehetero-fw:1  "$ROOT/firmware/Dockerfile" "$ROOT/firmware"
ensure_image edgehetero-rtl:1 "$ROOT/rtl/Dockerfile"      "$ROOT/rtl"

echo "== [1/4] python reference kernel =="
python3 "$ROOT/scripts/py_kernel_demands.py" --demands "$DEM" --capacity $CAP --depth $DEPTH \
  | tee "$OUT/py.json"; RC[py]=${PIPESTATUS[0]}

echo "== [2/4] firmware (native C + RV64) =="
docker run --rm -v "$ROOT":/work -v "$(dirname "$DEM")":/data -w /work edgehetero-fw:1 \
  bash firmware/build_and_measure.sh /data/$(basename "$DEM") $CAP $DEPTH 50 \
  > "$OUT/fw.jsonl" 2>"$OUT/fw.log"; RC[fw]=$?
cat "$OUT/fw.jsonl"

echo "== [3/4] RTL scoreboard (Verilator, golden=scheduler.c) =="
docker run --rm -v "$ROOT":/work -v "$(dirname "$DEM")":/data -w /work -e OUT_DIR=/work/out/repro_rtl \
  edgehetero-rtl:1 bash rtl/run_verify.sh /data/$(basename "$DEM") $CAP \
  > "$OUT/rtl.jsonl" 2>"$OUT/rtl.log"; RC[rtl]=$?
docker run --rm -v "$ROOT":/work -w /work edgehetero-rtl:1 rm -rf /work/out >/dev/null 2>&1 || true
grep -E '"summary"|"capacity":28,"depth":1' "$OUT/rtl.jsonl" || true

echo "== [4/4] P-D / P-I cross-platform sensitivity =="
python3 "$ROOT/scripts/p_i_sensitivity.py" --out-csv "$OUT/p_i_sensitivity.csv" \
  --out-json "$OUT/p_i_sensitivity.json" >/dev/null; RC[pi]=$?
python3 - "$OUT/p_i_sensitivity.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("  transfer dominates all swept P-D/P-I points:", d["transfer_dominates_on_all_swept_points"],
      "| min ratio:", d["min_ratio_transfer_over_ctrlmax"])
PY

if [[ $WITH_SYNTH -eq 1 ]]; then
  echo "== [opt] S6 synthesis DSE =="
  ensure_image edgehetero-syn:1 "$ROOT/syn/Dockerfile" "$ROOT/syn"
  docker run --rm -v "$ROOT":/work -w /work -e OUT_DIR=/work/syn/out_repro edgehetero-syn:1 \
    bash syn/dse.sh > "$OUT/synth.log" 2>&1; RC[synth]=$?
  docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 chown -R "$(id -u):$(id -g)" /work/syn/out_repro >/dev/null 2>&1 || true
fi

if [[ $WITH_STA -eq 1 ]]; then
  echo "== [opt] S6+ real gate-level STA-lite (Nangate45) + argmin fix =="
  ensure_image edgehetero-syn:1 "$ROOT/syn/Dockerfile" "$ROOT/syn"
  bash "$ROOT/scripts/fetch_pdk.sh" >> "$OUT/sta.log" 2>&1 || true
  docker run --rm -v "$ROOT":/work -w /work -e OUT_DIR=/work/syn/out_repro_sta edgehetero-syn:1 \
    bash syn/sta_dse.sh >> "$OUT/sta.log" 2>&1; RC[sta]=$?
  python3 "$ROOT/syn/analyze_sta.py" "$ROOT/syn/out_repro_sta/sta_dse.csv" | tee "$OUT/sta_summary.txt" || true
  echo "-- argmin equivalence (seq == comb == reference) --"
  docker run --rm -v "$ROOT":/work -w /work edgehetero-rtl:1 bash -lc '
    rm -rf /tmp/lvo
    verilator --cc --exe --build -O2 -Wno-fatal -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-IMPORTSTAR -Wno-WIDTH \
      --top-module lru_victim_tb_top --Mdir /tmp/lvo \
      /work/rtl/datapath/lru_victim.sv /work/rtl/top/lru_victim_tb_top.sv /work/rtl/verification/tb_lru_victim.cpp \
      -o Vlv >/dev/null 2>&1 && /tmp/lvo/Vlv' | tee "$OUT/argmin_equiv.json"; RC[argmin]=${PIPESTATUS[0]}
  echo "-- slice-2 streaming decompressor STA (sv2v->yosys->OpenSTA wire-load) --"
  bash "$ROOT/scripts/sta_decompressor.sh" > "$OUT/sta_decomp.log" 2>&1 || true
  cat "$ROOT/sta/out_decomp/sta_decomp.csv" 2>/dev/null | tee "$OUT/sta_decomp_summary.csv" || true
  docker run --rm -v "$ROOT":/work -w /work edgehetero-syn:1 chown -R "$(id -u):$(id -g)" /work/syn/out_repro_sta >/dev/null 2>&1 || true
fi

if [[ $WITH_MOE -eq 1 ]]; then
  echo "== [opt] W2/W3 large-MoE routing reproduction (canonical -> W3 summaries) =="
  CANON="$ROOT/data/canonical/moe_routing_v1"
  SAMPLE="$(python3 -c "import json;print(json.load(open('$CANON/manifest.json'))['queries'][0]['canonical_path'])" 2>/dev/null || true)"
  if [[ -n "$SAMPLE" && -f "$CANON/$SAMPLE" ]]; then
    python3 "$ROOT/scripts/w3_device_timing.py"   > "$OUT/w3_device.log" 2>&1; RC[w3dev]=$?
    python3 "$ROOT/scripts/w3_capacity_dse.py"    > "$OUT/w3_cap.log"    2>&1; RC[w3cap]=$?
    python3 "$ROOT/scripts/w3_copy_engine_dse.py" > "$OUT/w3_copy.log"   2>&1; RC[w3copy]=$?
    python3 "$ROOT/scripts/w3_robustness.py"      > "$OUT/w3_robust.log" 2>&1; RC[w3rob]=$?
    # determinism: the committed W3 summaries must regenerate byte-identical
    if git -C "$ROOT" diff --quiet -- \
         data/canonical/moe_routing_v1/w3_device_timing.json \
         data/canonical/moe_routing_v1/w3_capacity_dse.json \
         data/canonical/moe_routing_v1/w3_copy_engine_dse.json \
         data/canonical/moe_routing_v1/w3_robustness.json; then
      echo "  W3 summaries regenerate byte-identical from canonical data (deterministic)"
      RC[w3det]=0
    else
      echo "  W3 summaries DIFFER after regeneration (non-deterministic or stale commit)"
      RC[w3det]=1
    fi
    echo "  copy-engine E* (bandwidth-bound => 1):"
    python3 -c "import json;d=json.load(open('$CANON/w3_copy_engine_dse.json'));[print('   ',v,'E*=',i['e_star'],i['regime']) for v,i in d['models'].items()]" || true
    echo "  robustness over ALL sampled queries (transfer-bound / E*=1):"
    python3 -c "import json;d=json.load(open('$CANON/w3_robustness.json'));[print('   ',v,'tb='+i['transfer_bound_queries'],'E*=1:'+i['e_star_1_queries'],'miss_red median',i['miss_reduction']['median'],'%') for v,i in d['models'].items()]" || true

    echo "  -- P-I DRAM-timing calibration (Ramulator2, cycle-level) --"
    if docker image inspect edgehetero-mem:1 >/dev/null 2>&1; then
      python3 "$ROOT/scripts/mem_calibrate.py"  > "$OUT/mem_cal.log"    2>&1; RC[memcal]=$?
      python3 "$ROOT/scripts/w3_mem_recheck.py" > "$OUT/mem_recheck.log" 2>&1; RC[memrec]=$?
      if git -C "$ROOT" diff --quiet -- \
           data/canonical/moe_routing_v1/mem_timing.json \
           data/canonical/moe_routing_v1/w3_mem_recheck.json; then
        echo "    mem_timing / w3_mem_recheck regenerate byte-identical (deterministic DRAM sim)"
        RC[memdet]=0
      else
        echo "    mem calibration DIFFERS after regeneration (non-deterministic or stale commit)"
        RC[memdet]=1
      fi
      python3 -c "import json;d=json.load(open('$CANON/w3_mem_recheck.json'));print('    SW-vs-HW flip under DRAM timing:',d['flip_detected'],'| min transfer/ctrl ratio:',d['min_ratio_transfer_over_ctrlmax'],'x | crossover',d['sw_vs_hw_crossover_GBs'],'GB/s vs max eff',d['max_dram_effective_GBs'],'GB/s')" || true
    else
      echo "    edgehetero-mem:1 absent; build to enable: docker build -t edgehetero-mem:1 mem/"
      echo "    (documented boundary; DRAM-timing calibration is optional over the core W3 chain)"
    fi
  else
    echo "  canonical query traces absent locally (HF dataset, not committed)."
    echo "  fetch then canonicalize to enable this stage:"
    echo "    python3 scripts/hf_sample_download.py --config configs/sampling/round1.json"
    echo "    python3 scripts/moe_canonicalize.py"
    echo "  (documented reproduction boundary; the core chain above is self-contained)"
  fi

  # Slice-2 (compression) does NOT need the HF traces: the codec RD uses model configs +
  # a seeded weight distribution model, and the decompressor equivalence is self-contained.
  echo "  -- slice-2: expert-weight codec rate-distortion + decompressor equivalence --"
  python3 "$ROOT/scripts/slice2_codec_rd.py" > "$OUT/slice2_rd.log" 2>&1; RC[s2rd]=$?
  if git -C "$ROOT" diff --quiet -- data/canonical/moe_routing_v1/slice2_codec_rd.json; then
    echo "    slice2_codec_rd regenerates byte-identical (deterministic)"; RC[s2rddet]=0
  else
    echo "    slice2_codec_rd DIFFERS after regeneration (non-deterministic or stale commit)"; RC[s2rddet]=1
  fi
  if docker image inspect edgehetero-rtl:1 >/dev/null 2>&1; then
    bash "$ROOT/scripts/verify_decompressor.sh" > "$OUT/slice2_dec.log" 2>&1 || true
    python3 -c "import json,sys;d=json.load(open('$ROOT/explorations/moe_orchestration/slice2_decompressor_verify.json'));ok=all(x.get('pass') for x in d);print('    decompressor RTL == golden decode_fixed (NB=2/4/8):','PASS' if ok else 'FAIL');sys.exit(0 if ok else 1)"; RC[s2dec]=$?
  else
    echo "    edgehetero-rtl:1 absent; decompressor equivalence skipped (documented boundary)"
  fi
fi

echo "== equivalence check (python == C == rv64 == RTL) =="
python3 - "$OUT" $CAP $DEPTH <<'PY'
import json,sys,re,glob
out,cap,depth=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])
keys=["demand_misses","prefetch_hits","transfers","evictions"]
def norm(d): return tuple(d[k] for k in keys)
res={}
res["python"]=json.load(open(f"{out}/py.json"))
for line in open(f"{out}/fw.jsonl"):
    line=line.strip()
    if not line.startswith("{"): continue
    r=json.loads(line); res[r["target"]]=r     # native, rv64
# RTL: prefetch C=28 line
for line in open(f"{out}/rtl.jsonl"):
    if '"case":"prefetch"' in line and '"capacity":28' in line:
        r=json.loads(line); res["rtl"]={k:r["rtl"][{"demand_misses":"miss","prefetch_hits":"hit","transfers":"xfer","evictions":"evict"}[k]] for k in keys}
        res["rtl_golden"]={k:r["golden"][{"demand_misses":"miss","prefetch_hits":"hit","transfers":"xfer","evictions":"evict"}[k]] for k in keys}
vals={name:norm(res[name]) for name in ["python","native","rv64","rtl"] if name in res}
print("  layer counters (misses,hits,transfers,evictions):")
for name,v in vals.items(): print(f"    {name:8s}: {v}")
ok = len(set(vals.values()))==1 and len(vals)==4
print("  EQUIVALENCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PY
RC[equiv]=$?

echo "== summary =="
STATUS=PASS
for k in "${!RC[@]}"; do echo "  rc[$k]=${RC[$k]}"; [[ "${RC[$k]}" -ne 0 ]] && STATUS=FAIL; done
python3 - "$OUT" "$STATUS" <<PY
import json,sys
json.dump({"status":sys.argv[2],"exit_codes":{$(for k in "${!RC[@]}"; do printf '"%s":%s,' "$k" "${RC[$k]}"; done)}},
          open(sys.argv[1]+"/repro_manifest.json","w"),indent=2)
PY
echo "REPRODUCTION: $STATUS  (manifest: $OUT/repro_manifest.json)"
[[ "$STATUS" == "PASS" ]]
