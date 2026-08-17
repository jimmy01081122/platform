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

## 待驗證（進入 A1–A4 前尚未成立）

| ID | 假設 | 標籤 | 驗證方式 |
|---|---|---|---|
| PA-101 | 修正 contention 施加位置後，PCIe MAPE 可降至個位數 | `assumed` | A1 重擬合後由 A4 的 sealed held-out 判定。**目前僅為預期，不得作為結論** |
| PA-102 | 以 operand shape 參數化可修復 component service model | `assumed` | 同上 |
| PA-103 | C++ 引擎在接上 phase4 service model 後可重現 15 點的 hit/miss/evict | `assumed` | A3 的 SIM0 驗收 |
| PA-104 | 既有 PCIe 服務模型可用於 2 MiB KV block 的時序 | `assumed` | 2 MiB 落在目前模型最差的小尺寸區間（實測下限 ~0.037 ms vs intercept 0.0153 ms）。須待 A1 修好小尺寸 regime 後再評估 |

## 明確標為不可得

| ID | 項目 | 狀態 | 後果 |
|---|---|---|---|
| PA-201 | 掛載點 A2（MoE dispatch 資料搬運）的成本 | `UNAVAILABLE` | 無任何量測。在 GPU 軌補齊前，A2 的 break-even 不可計算 |
| PA-202 | 掛載點 A6（offloaded KV 上的 attention）的成本 | `UNAVAILABLE` | 無任何量測。長上下文結論一律標 `PROJECTED` |
| PA-203 | KV block 級的搬移時序 | `LIMITED` | SWAP-K2 事件 `block_size=0`，位元組由 runtime shape/dtype 推導。不得宣稱 K2/K3 提供 KV 時序 |
| PA-204 | per-object 搬移多樣性 | `LIMITED` | OFF-E-PR3 每次 miss 都搬同一個 layer-0 expert-0 物件（`physical_transfer_semantics`）。位元組與時間為實測，但物件多樣性未被實測 |
| PA-205 | cycle 級控制延遲 | `UNAVAILABLE` | 舊 repo 的 CLK4 未執行。無 cycle 級 support-processor 控制延遲主張 |
