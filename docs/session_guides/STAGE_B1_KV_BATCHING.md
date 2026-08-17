# Stage B1 · KV cache 與 continuous batching 建模

```text
GUIDE_COMPLETENESS = RULES_ONLY
```

**本指引固定目標、約束、驗收與交接格式；實作細節待 A3 的引擎與 A4 的校準結果收斂後補。**

這是刻意的：B1 的建模方式取決於 A3 接上 service model 後引擎的實際行為，以及 A4 判定哪些時序模型可信。現在寫死實作細節，很可能白寫。**但下列規則不會因前階段結果而改變。**

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 Stage B1 的執行者。

工作區：/home/a/platform

本 session 的單一目標：把 paged KV cache 與 continuous batching 建成模擬器的第一等公民，
並與 expert 物件共用同一套 residency-managed object 抽象。純 CPU 工作。

注意：本指引標記為 RULES_ONLY——目標與約束已固定，實作細節需你依 A3/A4 的實際結果決定。
先讀 A3 與 A4 在 governance/stage_ledger.yaml 中的交接記錄，確認引擎與校準的現況。

開始前完整讀取：
  docs/session_guides/STAGE_B1_KV_BATCHING.md   ← 本階段指引
  PLATFORM_FLOW_SPECIFICATION.md                 ← 根規格，特別是 §4.1 統一 object 抽象
  governance/stage_ledger.yaml                   ← 狀態真相來源
  docs/PHASE_NAMING_MAP.md                       ← 五套並存命名的對照

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不讀其他階段的指引（A3/A4 的 ledger 交接記錄除外——那是你的輸入）。
  3. 進入檢查任一項不符即停止並回報。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 目標

把 KV cache 與 continuous batching 建成模擬器的第一等公民，使其與 expert 物件**共爭同一條鏈路與 copy engine**。

這個耦合是共同設計的關鍵：長上下文下 KV 是容量與頻寬的主要壓力來源，而候選處理器的收益取決於它與 expert 搬運如何競爭。

---

## 2. 進入檢查

```bash
cd /home/a/platform
make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：0 failed
grep -A3 'id: STAGE_B1' governance/stage_ledger.yaml   # 預期：status: NOT_STARTED
grep -A3 'id: STAGE_A3' governance/stage_ledger.yaml   # 預期：status: COMPLETE
```

同時閱讀 A3 的交接記錄（在 `docs/status/AGENT_HANDOFF.md`），特別是 `SERVICE_MODEL_WIRED` 與 `TIMING_OBSERVED` 兩欄——它們決定 KV 時序可以建立在什麼基礎上。

---

## 3. 授權邊界

**可修改**：`explorations/moe_cycle_simulator/phase5/`（residency 政策擴充）、新增的 KV 模型模組、`experiments/specs/`、`runs/`、`docs/status/`、ledger 的 `STAGE_B1` 列。

**不可修改**：`evidence/**`、`hardware/**`、`measurement/**`、ledger 其他列、A2 產出的 IR bundle。

---

## 4. 已固定的事實與約束

### 4.1 KV 結構（實測，規格 §2.2）

```text
runtime_block_size_tokens = 16
bytes_per_full_block      = 2,097,152 B
runtime_cache_shape       = [2308, 8, 32, 2, 16, 128]，dtype bfloat16
→ 每 token 128 KiB
→ 1M context ≈ 128 GiB，超過 96 GB VRAM
```

來源：`evidence/phase7/master_remaining/*/remote_raw/SWAP-K2-*/derived_*_event_lineage_and_capacity.json`

### 4.2 統一的 residency-managed object 抽象（規格 §4.1）

KV block（2 MiB）與 expert 物件（336 MiB）是**同一類 object**，共用 identity、容量歸屬、搬運語意、eviction 語意、ownership，並共爭同一條鏈路。

### 4.3 驗證錨點：SERV-P0-25

```text
1000 requests · Poisson 開環 · rate 1.0472460793856333 · seed 20260812
concurrency 8 · 128-in / 32-out
completion latency  p50 868.2 ms · p95 1431.3 ms · p99 1585.9 ms · max 1743.9 ms
首筆 TTFT 96.344 ms
```

來源：`evidence/measurement_backups/20260811T175500Z__phase7_fit_anchor_backup/`

**這是短工作負載**——只驗證 batching／queueing 機制本身，不驗證長上下文行為。

### 4.4 目前引擎的缺口

phase5 的 `reserved_nonexpert_bytes` 只是一個純量，沒有 per-token 成長、沒有 paged block、沒有 eviction。
phase5 的 `execution_mode: TRACE_COMPILED_NON_ADAPTIVE`、`dynamic_admission: false`——沒有 continuous batching 或 admission control。

### 4.5 不可放寬的規則

1. **長上下文行為一律標 `PROJECTED`**，除非 GPU 軌已取得該區間的實測。
2. **KV 時序不得宣稱來自 SWAP-K2/K3。** 那些事件的 `block_size = 0`，位元組帳目由 runtime shape/dtype 推導。治理已開放 KV 效能主張（決策 P-006），但**資料品質限制不因治理開放而消失**。KV 時序須建立在已校準的鏈路模型或新量測上。
3. **KV 與 expert 的競爭必須實際建模**，不得各自獨立計時後相加。

---

## 5. 驗收條件

| 項目 | 判準 |
|---|---|
| serving 錨點 | 重現 SERV-P0-25 的 TTFT 與 completion latency 分布（p50/p95/p99） |
| 統一抽象 | KV block 與 expert 物件走同一套 residency-managed object 路徑 |
| 競爭已建模 | 兩者共爭同一條鏈路與 copy engine，可從模擬輸出觀察到互相影響 |
| fidelity 標記 | 長上下文區間標 `PROJECTED`；短工作負載區間標其實際 fidelity |
| KV 時序來源可追溯 | 明確記載時序來自哪個已校準模型或哪次量測 |
| 基線未退步 | `make test` → 0 failed；`make test-cpp` → 14 CTest 全綠 |
| 證據未被動 | `make verify-evidence` → 4423/4423 |

---

## 6. Claim boundary

**可新增**：「KV cache 與 continuous batching 已建模，並在 ⟨短工作負載區間⟩ 重現 SERV-P0-25 的延遲分布。」

**仍然禁止**

- KV 時序來自 SWAP-K2/K3 的主張。
- 任何長上下文的 `MEASURED` 主張（未量測者一律 `PROJECTED`）。
- 任何 accelerator 收益或 break-even 主張（屬 C1）。
- 把短工作負載的重現說成 batching 模型已在所有 regime 驗證。

---

## 7. 待補的實作細節

執行本階段時需自行決定並記錄：

- paged block 的配置與 eviction 策略如何對映到既有 phase5 的 `kLru`／`kFifo`；
- admission control 的決策點放在事件流的哪裡；
- KV 與 expert 競爭同一條鏈路時的仲裁規則（FIFO？優先權？）——**這個選擇會直接影響 C1 的 break-even 結果，必須明確記錄並在 C1 的敏感度分析中掃描**；
- 長上下文外推的具體方法與其不確定度。

決定後把實際做法補回本指引，並在 ledger 把 `guide_completeness` 改為 `EXECUTABLE`。

---

## 8. 失敗處理與必須詢問 owner 的條件

**必須停下並詢問**：需要修改 `evidence/`；需要對 phase3–6 做結構性改動；發現 A3 接上的 service model 不足以支撐 KV 時序而需要新量測；KV 與 expert 的仲裁規則有多個合理選擇且會顯著改變結論。

最後一項要特別說明：**仲裁規則的選擇不應由本階段單方面決定**。若不同選擇會翻轉 C1 的結論，應把它列為 DSE 的掃描軸，而非寫死一個。

---

## 9. 完工交付

依 `README.md` 的標準流程：產出清單、`runs/<run_id>/`、更新 ledger 的 `STAGE_B1` 列、更新五份 status、commit + push。

```text
STAGE: B1
STATUS: COMPLETE | BLOCKED
SERVING_ANCHOR_REPRODUCTION: <p50/p95/p99 模擬 vs 量測>
UNIFIED_OBJECT_ABSTRACTION: <KV 與 expert 的共用路徑說明>
CONTENTION_MODELED: <競爭如何建模；仲裁規則>
ARBITRATION_CHOICE: <選了什麼；是否列入 C1 掃描軸>
KV_TIMING_SOURCE: <時序來自哪個已校準模型或量測>
FIDELITY_LABELS: <各區間的標記>
LONG_CONTEXT_STATUS: PROJECTED | MEASURED（後者需列出量測來源）
BASELINE: <make test / make test-cpp 輸出>
EVIDENCE_UNCHANGED: <make verify-evidence 輸出>
FILES_CHANGED: <清單>
CLAIMS_ADDED / CLAIMS_STILL_FORBIDDEN
NEXT / OWNER_DECISION_NEEDED
```
