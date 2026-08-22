# ASSUMPTION_REGISTER

每條假設必須帶 provenance tag。允許的標籤（沿用 `docs/methodology/REPRODUCIBILITY_STANDARD.md`）：

```text
measured     實機量測，附來源路徑
derived      由 measured 推導，附公式
vendor_spec  廠商規格書
tool_default 工具預設值
assumed      假設 —— 不得作為單點使用，必須以範圍掃描
swept        以範圍掃描處理
```

**`assumed` 永遠不能單點使用。** 若某參數只能 `assumed`，該參數必須進 DSE 的掃描軸，或對應結論標 `UNAVAILABLE_WITH_CONSEQUENCE`。

---

## 平台層級

| ID | 假設 | 標籤 | 來源／後果 |
|---|---|---|---|
| PA-001 | 每個 336 MiB expert 物件的 H2D 傳輸時間為 12.454–12.499 ms（有效頻寬 28.19–28.29 GB/s） | `measured` | `evidence/phase7/.../OFF-E-PR3-CAP-*/off_e_pr3_trace/capacity_replay.json`，14 個非零容量點，σ≈0.1% |
| PA-002 | expert 物件大小 352,321,536 B，catalog 共 256 物件 | `measured` | 同上；`expert_object_bytes` 與 `catalog_objects` 欄位 |
| PA-003 | KV block 2,097,152 B / 16 tokens，即每 token 128 KiB | `measured` | `evidence/phase7/.../SWAP-K2-*/derived_*_event_lineage_and_capacity.json`，`runtime_cache_shape [2308,8,32,2,16,128]`、dtype bfloat16 |
| PA-004 | Mixtral-8x7B 1M context 的 KV 需求約 128 GiB，超過 96 GB VRAM | `derived` | 由 PA-003 × 1e6 tokens 推得。後果：長上下文必然強制 KV offload |
| PA-005 | 15 點容量掃描的 LRU counters 完全決定性 | `measured` | `logical_policy: DETERMINISTIC_LRU_EMPTY_INITIAL_CACHE`，每點 `logical_demand_count = 10176`。後果：A3 的 SIM0 可用位元精確作為驗收 |
| PA-006 | 控制決策率為 111–3073 decisions/s | `derived` | 由 OFF-E-PR3 各點的 demand 數與 wall time 推得。**僅適用於單請求、eager、159 tokens、`max_num_seqs=1`**，不得外推 |
| PA-007 | q0/q1 量測本身可信（n=5、95% CI 寬約 0.0002 ms、跨 split 重現到小數第四位） | `measured` | `evidence/gpu_measurements/rtx-pro-6000-v3-20260718/`。後果：MAPE 失敗歸因於模型形式而非量測 |
| PA-008 | 每個 decode step 的 routing width = 8（`selected_experts` flatten numel），故 gpu_service `expert_tokens` = 8 ×（decode token 位置數） | `measured` | `measurement/gpu_run_package_v2/workloads/windows.json` decode step `selected_experts` shape `[1,8]`。後果：GAP-5 的 8× 正規化差完全由此解釋（P-014、`calibration/GAP5_LAUNCH_GRANULARITY_RESOLUTION.md`） |
| PA-009 | GPU 量測 contract 的 `time_estimate`（25/60/0/15/160 min）為估算，非實測 | `derived` | 由 PA-001/PA-003、SERV-P0-25 arrival rate 等推得（`experiments/specs/gpu_measurement_contract_v1.yaml`）。**不得當作實測耗時**；量測時須驗證，尤以 target_2（長 prefill+offload）與 target_5（arrival-bound ~2.65h）不確定度最高 |
| PA-010 | 新探針的 IR 評估點對映到 A2 的 CalibrationIR schema，operand shape 直接進 `evaluation_coordinate` | `derived` | PREP-2（P-016）依 A2 `$defs.calibration` schema 定案；`measurement/probes/ir_evaluation_point.py` 產生的點經 jsonschema 對 A2 真實 schema 驗證通過。**mock backend 產生的點值為 CPU 合成，非實測**（stamped `cpu_smoke_test_not_measurement`） |
| PA-010 | A2 PlatformIR 的 PCIe 有效頻寬 28,298,591,668 B/s（`bandwidth_bytes_per_second`） | `derived` | 由 15 點最快單物件 H2D（min `cuda_elapsed_ms`=12.4501）與 352,321,536 B 推得（`off_e_pr3_measured_adapter.py`）。與 PA-001 一致 |
| PA-011 | A2 ModelIR `total_parameter_count`/`active_parameter_count` = 46,702,792,704 / 12,879,302,656 | `vendor_spec` | Mixtral-8x7B model-card 名目值，非本平台量測；IR 僅用於 `active ≤ total` 關係，不驅動任何 A2 主張 |
| PA-012 | A2 PlatformIR `service_rate_units_per_second`（gpu0=2,805,000,000；cpu0=1,000,000,000） | gpu0 `measured`（nvidia-smi clocks.gr 2805 MHz）／cpu0 `assumed` | schema 必填且須 >0。**不是校準輸入、不驅動任何 A2 時序主張**（A2 無時序主張）。cpu0 為 nominal 佔位；真值待 B/C 需要時再定，屆時進掃描或標 UNAVAILABLE_WITH_CONSEQUENCE |
| PA-013 | A2 PlatformIR PCIe `latency_fs = 0`（每筆傳輸 PCIe 起始延遲未單獨量測） | `assumed`（UNAVAILABLE_WITH_CONSEQUENCE） | 量測只給 per-object 總時間與有效頻寬，未分離固定延遲。填 0 為占位；**任何需要 PCIe 固定延遲的下游結論須先量測或標 PROJECTED**，不得沿用此 0 |
| PA-014 | A2 ClockAlignmentIR：monotonic ns→fs 為精確單位換算（scale 1e6），量化不確定度以 ±0.5 ns（±500,000 fs）95% CI 表示，grade=AGGREGATE_ONLY | `derived` | 單一 host monotonic 時鐘，非跨時鐘漂移；ns 量化即不確定度來源。**不宣稱 CYCLE_GRADE 對齊** |
| PA-015 | A3 對 cap=100 退化 control 使用 base_resident=全 catalog（all-resident 初始），其餘 14 點空初始快取 | `derived` | 判定規則由 IR 導出：`device_residency_budget capacity_bytes ≥ catalog 總 bytes` → 全常駐。忠實對應量測 `physical_transfer_semantics: "No demand H2D: actual all-resident control"`（demand_load=0）。**非容差掩蓋**：cap=099 的 259 loads 已含 256 cold loads，證明 cap=100 的 0 loads 是真預載（P-017） |
| PA-016 | HEADERFIX canonical guard 在 system CUDA 13.0.88 + matching nvrtc overlay + Python 3.10 include 下成功；不需修改 header | `measured` | run `MECH-G0-KV-G0-OS-SWAP-G0-UM-G0-CANARY-HEADERFIX-PYINC-20260822T114227Z-TRACKGPU`。torch 2.11.0+cu130、vLLM 0.23.0、driver 580.95.05；FLASH_ATTN + FlashInfer CUTLASS MoE markers 齊全。僅證明此 guard domain 可啟動與完成 1 次量測，不外推 target_1/2 效能 |
| PA-017 | target_2 formal 的 140-GiB host KV offload 容量足以覆蓋 1M-token KV 的估算 | `derived` | P-020：1M×128 KiB=128 GiB total KV；GPU 側估約 5.7 GiB，host 承接約 122–128 GiB；140 GiB 留碎片餘裕，並保留約 38 GiB 給 process/pinned/OS。須由 16-GiB mechanism canary 先驗機制；canary PASS 也不得宣稱 offload 效能已驗證 |
| PA-018 | V2-GAP-A 的 24-cell 多物件 wait-all transfer sweep 已完成，軸為 `num_objects`、每物件 `copy_streams=1` | `measured` | run `20260822T121400Z__track_gpu_v2gapa_multistream_attempt2`；RTX PRO 6000、driver 580.95.05、torch 2.11.0+cu130；24 unique cells（N=1/2/4/8 × 64KiB/2MiB/336MiB × H2D/D2H）各 n=5，parser + grid audit PASS。**FIT-side；IR/production S>1 mapping 仍 PENDING/UNSUPPORTED，不得當 calibrated 結論** |
| PA-019 | vLLM 0.23 現有 Python/metrics 邊界不足以直接填 target_1 的四項 dispatch decomposition 與 target_2 的 per-request residency 欄位 | `derived` | P-023 source audit：FlashInfer CUTLASS MoE 為單一 fused call；native `OffloadingConnectorStats` 只有 transfer bytes/time 且讀取 drain/reset，無 residency gauge。後果：strict adapter 明確 refusal；在 owner 授權低層 instrumentation 前，A2/A6 成本仍 `UNAVAILABLE`，不得以 parent wall time或算術推導代替 |
| PA-020 | `max_num_batched_tokens=1024` 是 target_1/target_5 的顯式凍結值，不是 runner default | `measured` | HEADERFIX attempt exact argv/requested_engine_args 與既有 SERV-P0-25 C8 evidence 都記 1024；相對地 `gpu_campaign_runner.py` default=256、`serving_burst_runner.py` default=512。target_2 不繼承此值，仍待 owner 定案 |
| PA-021 | component sealed assignment 在 64 格中為 fit=41 / validation=11 / holdout=12 | `derived` | 由 `holdout_split_v1_manifest.json` 的 pinned assignment SHA `b73da79c…` 重算。新 probe 每 attempt 只允許一個 split；holdout 另需 A4 authorization，防止 FIT leakage |

## 待驗證（進入 A1–A4 前尚未成立）

| ID | 假設 | 標籤 | 驗證方式 |
|---|---|---|---|
| PA-101 | 修正 contention 施加位置後，PCIe MAPE 可降至個位數 | `assumed` | A1 已重擬合：FIT 側 MAPE 66.879%→19.821%（大幅改善但未達個位數，也未達 15% 門檻）。大尺寸單筆延遲驗證極佳（~84MiB 處 streams 1/2/4 誤差 <0.2%），但新發現小尺寸（64KiB）多 stream 下有相反方向的殘留效應（`calibration/fits/v2/measurement_gaps.json` GAP-6）。**FIT 側數字，仍待 A4 sealed held-out 判定**，不得作為結論 |
| PA-102 | 以 operand shape 參數化可修復 component service model | `assumed` | A1 已重擬合：FIT 側 MAPE 304.418%→20.324%。4 個 op 中 3 個（grouped_gemm/gather_scatter/selected_expert）收斂為 tokens 仿射回歸；dequant 回歸非物理，退回 flat model（真正驅動變數應為 expert 權重位元組數，本階段量測無法分離，見 GAP-1）。**FIT 側數字，仍待 A4 sealed held-out 判定** |
| PA-103 | C++ 引擎在接上 phase4 service model 後可重現 15 點的 hit/miss/evict | `measured`（A3 已驗證） | **A3 SIM0 已通過**：15/15 hit/load/discard 經 phase5→phase4 引擎位元精確重現、SIM1 決定性、15/15 QUIESCENT（run `20260819T134458Z__stage_a3_ir_to_engine_replay`，P-017）。服務時間走 phase4（per-object H2D=12450143814087 fs 對回校準值）。**僅 residency 語意，非時序準確度** |
| PA-104 | 既有 PCIe 服務模型可用於 2 MiB KV block 的時序 | `assumed` | A1 已修小尺寸 intercept 低估問題（floor_ms，見 PA-101），但殘差分析顯示 2 MiB 附近（bytes=1048576 校準點）在 streams>1 時仍有 30–60% 的 FIT 側誤差（GAP-6 的中間尺度延伸）。**2 MiB KV block 的時序仍不可視為已由 A1 修復**，須待更多量測或 A4 判定 |

## 第三方 routing 語料（`core12345/MoE_expert_selection_trace`）

完整稽核見 `EXTERNAL_CORPUS_AUDIT_20260818.md`。

| ID | 假設／事實 | 標籤 | 來源／後果 |
|---|---|---|---|
| PA-301 | 四個模型的架構參數（experts 128–384、top_k 1 或 8、MoE 層 24–94） | `measured` | `data/canonical/moe_routing_v1/routing_stats.json`。expert 物件數跨度 3,072–23,040，而自產量測的 Mixtral 只有 256（90× 差距）。**這是 HW0 metadata 相關 row 的主要證據來源** |
| PA-302 | decode 階段每層每 token 恰好 top_k 個 expert（`ws_decode_mean` == top_k） | `measured` | 同上。後果：任意架構的控制負載可直接算為 `MoE層 × top_k` 查表/token，不需重新量測 |
| PA-303 | 全資料集單一 query 最長約 721 tokens（prefill ≤593 + decode 固定 128） | `measured` | 全部 103,961 檔的 metadata 掃描（`bytes = a×prefill + b`，四模型 R²=1.0000）。**距 1M context 三個數量級，且無法靠補抓解決** |
| PA-304 | 語料無 router scores、無時序 | `measured` | 資料集本身性質。後果：不可能有基於 gate 分數的 predictor；時序須由 service model 提供，因此本語料的時序價值受 A1／A4 收斂狀況牽制 |

## 待驗證（語料補抓後需重跑）

| ID | 假設 | 標籤 | 驗證方式 |
|---|---|---|---|
| PA-311 | causal predictor 僅保留 oracle 收益的 ≤15%，top_k=1 為負 | `assumed` | 補抓前數字（persistence 全 0.0%、frequency 0.0–2.6%、markov1 −0.5%–15.4%）**基於每 cell n=3，低於專案自訂 k\*=14 達 4.7 倍**，且跨 benchmark 變異與效應量同量級（Llama 甚至變號）。語料已補到 21/21 cell 達標，**須由 C1 重跑 `w3_prefetch_predictability` 後才可引用** |
| PA-312 | 既有 `w3_*` 容量／copy-engine／壓縮 DSE 結論 | `assumed` | 同上，全部基於 60 檔舊樣本。C1 須以 354 檔重跑 |
| PA-313 | Kimi-K2 可作為跨架構 hold-out（H5 宣稱） | `assumed` | 補抓前僅 6 檔／2 cell／n=3。現為 62 檔／4 cell／n≥14，但 **H5 效力須待重跑後重新評估** |

## 明確標為不可得

| ID | 項目 | 狀態 | 後果 |
|---|---|---|---|
| PA-201 | 掛載點 A2（MoE dispatch 資料搬運）的成本 | `UNAVAILABLE` | 無任何量測。在 GPU 軌補齊前，A2 的 break-even 不可計算 |
| PA-202 | 掛載點 A6（offloaded KV 上的 attention）的成本 | `UNAVAILABLE` | 無任何量測。長上下文結論一律標 `PROJECTED` |
| PA-203 | KV block 級的搬移時序 | `LIMITED` | SWAP-K2 事件 `block_size=0`，位元組由 runtime shape/dtype 推導。不得宣稱 K2/K3 提供 KV 時序 |
| PA-204 | per-object 搬移多樣性 | `LIMITED` | OFF-E-PR3 每次 miss 都搬同一個 layer-0 expert-0 物件（`physical_transfer_semantics`）。位元組與時間為實測，但物件多樣性未被實測 |
| PA-205 | cycle 級控制延遲 | `UNAVAILABLE` | 舊 repo 的 CLK4 未執行。無 cycle 級 support-processor 控制延遲主張 |
