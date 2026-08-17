# Experiment Runbook

## 0. S1 一鍵重現（expert-demand characterization）

在乾淨環境（Python 3.10+，`pip install numpy pandas jsonschema pyyaml simpy`）下：

```bash
python3 -m pytest tests/test_canonical.py -q
python3 scripts/build_canonical.py \
  --raw  data/fixtures/moe_switch8_mbpp_s64_bs8_len64/batch_expert_load_trace.csv \
  --meta data/fixtures/moe_switch8_mbpp_s64_bs8_len64/run_metadata.json \
  --trace-id moe_switch8_mbpp_s64_bs8_len64 \
  --out  /tmp/canonical.jsonl \
  --characterize /tmp/characterization.json
```

預期：`761 canonical events`、`schema_valid=761/761`、`ordering_problems=0`，
characterization 中 `active_experts_per_group_mean ≈ 7.93`、
`consecutive_group_expert_jaccard_mean ≈ 0.98`。

## 0b. S5 一鍵重現（full-datapath RTL + 等價 scoreboard）

需要 Docker。凍結 C kernel（`firmware/scheduler.c`）作為 golden。

```bash
# 1) 建立專屬 RTL Docker（Verilator，不與其他專案共用）
docker build -t edgehetero-rtl:1 -f rtl/Dockerfile rtl

# 2) 從 canonical trace 匯出 demands（若尚未產生）
mkdir -p /tmp/s4
python3 scripts/export_demands.py \
  --canonical <switch-base-32 canonical.jsonl> --out /tmp/s4/demands_32.txt

# 3) lint + build + scoreboard（預設 DMA 設定）
docker run --rm -v "$PWD":/work -v /tmp/s4:/data -w /work \
  edgehetero-rtl:1 bash rtl/run_verify.sh /data/demands_32.txt 8,16,24,28,32

# 4) 重度 backpressure 壓力設定（功能 counters 應完全相同）
docker run --rm -v "$PWD":/work -v /tmp/s4:/data -w /work \
  -e DMA_DEPTH=1 -e DMA_LATENCY=32 \
  edgehetero-rtl:1 bash rtl/run_verify.sh /data/demands_32.txt 8,16,24,28,32
```

預期：`{"summary":"PASS","failures":0}`；C=28,d=1 時 RTL 與 golden 皆為
`misses=994, hits=383, transfers=1377, evictions=1349`（python==C==rv64==RTL）。

## 0c. S6 一鍵重現（綜合 proxy + DSE + Pareto）

需要 Docker。產出為 proxy（gate count / AIG ltp），非真實 um^2/ns；功耗 unavailable。

```bash
# 1) 建立專屬綜合 Docker（Yosys + sv2v，不與其他專案共用）
docker build -t edgehetero-syn:1 -f syn/Dockerfile syn

# 2) DSE 掃描（MAX_EXPERTS {8,16,32,64} x TS_W {8,16,32}）
docker run --rm -v "$PWD":/work -w /work -e OUT_DIR=/work/syn/out_dse \
  edgehetero-syn:1 bash syn/dse.sh

# 3) 量測 cycles/step（RTL）並分析 -> Pareto + boundary
docker run --rm -v "$PWD":/work -v /tmp/s4:/data -w /work -e OUT_DIR=/work/out/rtl_cyc \
  edgehetero-rtl:1 bash rtl/run_verify.sh /data/demands_32.txt 8,16,24,28,32 \
  2>/dev/null | grep '"case":"cycles"' > syn/out_dse/cycles.jsonl
python3 syn/analyze_dse.py
```

預期：12/12 可綜合；C=28,d=1 量得 cycles/step ≈ 63.4（驗證 A-013 ~64）；
Pareto 前緣 = (MAX_EXPERTS=32, TS_W=16)。

## 0d. S7 一鍵重現（全鏈跨層等價 + P-D/P-I 敏感度）

需要 Docker + python3。從 committed fixture 於乾淨容器重現整條鏈：

```bash
# 不含綜合（快速；四層等價 + P-D/P-I）
bash scripts/reproduce_all.sh /tmp/repro

# 含 S6 綜合 DSE（較慢）
bash scripts/reproduce_all.sh /tmp/repro --with-synth
```

預期：`REPRODUCTION: PASS`、所有 exit code 0；四層 counters
python==C==rv64==RTL = (994, 383, 1377, 1349)（C=28, depth-1）。
完整交付見 `docs/FINAL_REPORT.md`。

## 1. 檢查工作空間

```bash
make doctor
make validate-configs
```

## 2. 建立 experiment spec

```bash
make new-exp EXP_ID=moe_dispatch_break_even
```

編輯：

```text
experiments/specs/moe_dispatch_break_even.yaml
```

先寫可反證問題、platform profile、baseline、workload 與未知參數來源。

## 3. 初始化 run

```bash
make init-run \
  EXP_ID=moe_dispatch_break_even \
  STAGE=S2 \
  PLATFORM=discrete_edge_workstation
```

CLI 會建立 `runs/<timestamp>__<experiment>__<stage>/`，並產生 manifest 與必要檔案。

## 4. 執行工具 adapter

Adapter 可使用任意工具，但需把：

- 原始命令寫入 `logs/command.log`。
- stdout/stderr 分別保存。
- 正規化結果寫入 `metrics.json`。
- 原始圖表、trace、netlist、report 放入 `artifacts/`。
- 工具版本寫入 `environment/tool_versions.json`。

## 5. 檢查 run

```bash
make check-run RUN_DIR=runs/<run_id>
```

## 6. 更新狀態文件

每次 agent 交回前更新：

```text
docs/status/CURRENT_STATUS.md
docs/status/AGENT_HANDOFF.md
docs/status/DECISION_LOG.md
docs/status/ASSUMPTION_REGISTER.md
docs/status/VALIDATION_MATRIX.md
```

## 7. 彙總

```bash
make summary
```

## 8. 失敗處理

失敗 run 不刪除。將 manifest status 設為 `failed`、`partial` 或 `invalid`，並在 metrics 中加入 failure classification，例如：

- environment_failure
- input_invalid
- tool_build_failure
- timeout
- model_mismatch
- numerical_instability
- rtl_assertion_failure
- timing_failure
- unsupported_configuration
