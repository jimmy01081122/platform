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

## 待驗證（進入 A1–A4 前尚未成立）

| ID | 假設 | 標籤 | 驗證方式 |
|---|---|---|---|
| PA-101 | 修正 contention 施加位置後，PCIe MAPE 可降至個位數 | `assumed` | A1 已重擬合：FIT 側 MAPE 66.879%→19.821%（大幅改善但未達個位數，也未達 15% 門檻）。大尺寸單筆延遲驗證極佳（~84MiB 處 streams 1/2/4 誤差 <0.2%），但新發現小尺寸（64KiB）多 stream 下有相反方向的殘留效應（`calibration/fits/v2/measurement_gaps.json` GAP-6）。**FIT 側數字，仍待 A4 sealed held-out 判定**，不得作為結論 |
| PA-102 | 以 operand shape 參數化可修復 component service model | `assumed` | A1 已重擬合：FIT 側 MAPE 304.418%→20.324%。4 個 op 中 3 個（grouped_gemm/gather_scatter/selected_expert）收斂為 tokens 仿射回歸；dequant 回歸非物理，退回 flat model（真正驅動變數應為 expert 權重位元組數，本階段量測無法分離，見 GAP-1）。**FIT 側數字，仍待 A4 sealed held-out 判定** |
| PA-103 | C++ 引擎在接上 phase4 service model 後可重現 15 點的 hit/miss/evict | `assumed` | A3 的 SIM0 驗收 |
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
