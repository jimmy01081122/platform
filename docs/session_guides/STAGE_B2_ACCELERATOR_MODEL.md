# Stage B2 · 參數化候選處理器模型與掛載點

```text
GUIDE_COMPLETENESS = RULES_ONLY
```

**本指引固定目標、約束、驗收與交接格式；實作細節待 A4 的校準結果與 B1 的 KV 模型收斂後補。**

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 Stage B2 的執行者。

工作區：/home/a/platform

本 session 的單一目標：把候選 support processor 建成模擬器裡可掃描的第一等公民元件，
並定義六個掛載點。純 CPU 工作。

這是整個平台的核心能力——沒有它就無法回答「需要什麼規格的客製化 processor」。

注意：本指引標記為 RULES_ONLY——目標與約束已固定，實作細節需你依 A3/A4/B1 的實際結果決定。

開始前完整讀取：
  docs/session_guides/STAGE_B2_ACCELERATOR_MODEL.md   ← 本階段指引
  PLATFORM_FLOW_SPECIFICATION.md                       ← 根規格，特別是 §6 候選處理器與掛載點
  accelerator/README.md                                ← 目錄骨架與掛載點定義
  governance/stage_ledger.yaml                         ← 狀態真相來源

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不讀其他階段的指引。
  3. 進入檢查任一項不符即停止並回報。

本階段特有：accelerator/ 內所有元件一律標 ANALYTICAL 或 PROJECTED，
絕不可標 MEASURED_SURROGATE——它們沒有實機支撐。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 目標

建立候選處理器的參數化資源模型、六動詞 backend ABI，以及六個掛載點的成本模型骨架，使 C1 能掃描「哪個功能、什麼規格、值不值得」。

---

## 2. 進入檢查

```bash
cd /home/a/platform
make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：0 failed
grep -A3 'id: STAGE_B2' governance/stage_ledger.yaml   # 預期：status: NOT_STARTED
grep -A3 'id: STAGE_A3' governance/stage_ledger.yaml   # 預期：status: COMPLETE

# 既有的防偽設計必須完好
grep -n 'is not registered' src/edgeflow/multifidelity.py
# 預期：找到「未註冊 backend 直接拒絕」的邏輯
```

---

## 3. 授權邊界

**可修改**：`accelerator/`（主要產出）、`src/edgeflow/multifidelity.py`（**只做 backend 註冊，不動拒絕邏輯**）、`experiments/specs/`、`runs/`、`docs/status/`、ledger 的 `STAGE_B2` 列。

**不可修改**：`evidence/**`、`hardware/**`、A2 的 IR bundle、ledger 其他列。

**特別注意**：`src/edgeflow/multifidelity.py` 對未註冊 backend 直接拋錯是**刻意的防偽設計**（原始碼註解：`an unavailable detailed/RTL backend cannot be silently substituted`）。新 backend 必須正式註冊才生效；**不得**為了方便而放寬這個檢查。

---

## 4. 已固定的規格

### 4.1 參數化資源模型（規格 §6.1）

```text
pipeline latency      issue width           local SRAM capacity
memory bandwidth      queue depth           operations per cycle
clock domain          area proxy            power proxy
```

全部必須可掃描——C1 會掃它們。

### 4.2 六動詞 backend ABI（規格 §6.3）

```text
reset   can_accept   submit   advance   poll_completions   snapshot_counters
```

本階段實作兩個 backend：`FUNCTIONAL_POLICY`、`CYCLE_RESOLVED_MODEL`，外加一個 **reference mock 元件**用來驗證 transaction adapter、clock stepping、backpressure、completion 與 counter 路徑。

`RTL_TRACE_REPLAY`、`VERILATOR_COSIM`、`RTL_CALIBRATED_SURROGATE` **只保留介面**，留給下游。

### 4.3 六個掛載點（規格 §6.2）

| ID | 功能 | 優先序 | 現有量測 |
|---|---|---|---|
| A1 | routing / gating 決策計算、top-k | 主 | routing `.npy` `[159,32,2]`、CTRL-PX0-*-routing |
| A2 | MoE dispatch 資料搬運（token permutation、gather/scatter） | 主 | **無** |
| A3 | transfer 排程 / DMA descriptor / prefetch 發射 | 主 | transfer 微基準 v1–v4 |
| A4 | expert 解壓縮 / 壓縮搬運 | 主 | `expert_decompressor.sv` 307–811 MHz |
| A5 | KV block 管理 / offload | 次 | SWAP-K1/K2/K5（`block_size=0` 限制） |
| A6 | offloaded KV 上的 attention | 次 | **無** |

> 掛載點 A1–A6 與**階段** A1–A4 是不同的東西，見 `docs/PHASE_NAMING_MAP.md`。

**每個掛載點必須定義三件事**：

1. 可卸載的工作單位（work unit）與其在 baseline 的成本；
2. 在候選處理器上的成本模型；
3. 把資料送過去與取回的搬運成本。

第 3 項最容易被忽略，卻常常是 break-even 的決定因素。

### 4.4 不可放寬的規則

1. **`accelerator/` 內所有元件標 `ANALYTICAL` 或 `PROJECTED`**，絕不可標 `MEASURED_SURROGATE`。
2. **掛載點 A2 與 A6 沒有任何量測**——在 GPU 軌補齊前，兩者只能建模，不得產生效能結論。
3. **未註冊的 backend 必須直接拒絕執行**，不得靜默替換為較低 fidelity 的實作。
4. 主線 A1–A4 先做，A5／A6 次之。

---

## 5. 驗收條件

| 項目 | 判準 |
|---|---|
| reference mock | 跑通 transaction adapter、clock stepping、backpressure、completion、counter 五條路徑 |
| 防偽設計完好 | 未註冊的 backend 仍正確拒絕；有測試覆蓋此行為 |
| fidelity 標記 | `accelerator/` 內無任何元件標 `MEASURED_SURROGATE` |
| 掛載點定義完整 | A1–A4 各自的三件事（work unit／成本模型／搬運成本）皆已定義 |
| 無證據標記 | A2 與 A6 明確標記為無量測支撐 |
| 可掃描 | 九個資源參數皆可由 config 掃描 |
| 基線未退步 | `make test` → 0 failed |
| 證據未被動 | `make verify-evidence` → 4423/4423 |

---

## 6. Claim boundary

**可新增**：「候選處理器已建成可掃描的模擬器元件，六動詞 ABI 與兩個 backend 可用，掛載點 A1–A4 已定義。」

**仍然禁止**

- 任何 accelerator 收益或 break-even 主張（屬 C1）。
- 掛載點 A2 或 A6 的任何效能結論（兩者無量測）。
- 把 `ANALYTICAL` 的成本模型當成量測結果引用。
- 把 reference mock 跑通說成硬體可行性已驗證。

---

## 7. 待補的實作細節

執行時需自行決定並記錄：

- 各掛載點 work unit 的粒度（每 token？每 layer？每 batch？）——**粒度選擇會顯著影響 break-even，應列為 C1 的敏感度軸**；
- 成本模型的形式（固定延遲？隨資料量線性？分段？）；
- 候選處理器與主系統的同步語意（阻塞？非同步 + completion queue？）；
- 資源參數的預設掃描範圍與其依據。

決定後補回本指引，並在 ledger 把 `guide_completeness` 改為 `EXECUTABLE`。

---

## 8. 失敗處理與必須詢問 owner 的條件

**必須停下並詢問**：需要放寬未註冊 backend 的拒絕邏輯（**預設答案是否**）；需要修改 `evidence/`；某掛載點的 work unit 定義有多個合理選擇且會翻轉結論；需要對 phase3–6 引擎做結構性改動以容納 accelerator 元件。

---

## 9. 完工交付

依 `README.md` 的標準流程。

```text
STAGE: B2
STATUS: COMPLETE | BLOCKED
RESOURCE_MODEL_PARAMS: <九個參數的實作與掃描範圍>
ABI_BACKENDS: <FUNCTIONAL_POLICY / CYCLE_RESOLVED_MODEL 的實作狀態>
REFERENCE_MOCK: <五條路徑的驗證結果>
ATTACHMENT_POINTS_DEFINED: <A1-A6 各自的三件事完成度>
WORK_UNIT_GRANULARITY: <各掛載點的選擇；是否列入 C1 掃描軸>
UNMEASURED_POINTS: A2, A6 <確認已標記為無量測支撐>
FIDELITY_AUDIT: <確認無元件標 MEASURED_SURROGATE>
GUARD_INTACT: <未註冊 backend 拒絕邏輯的測試結果>
BASELINE: <make test 輸出>
EVIDENCE_UNCHANGED: <make verify-evidence 輸出>
FILES_CHANGED / CLAIMS_ADDED / CLAIMS_STILL_FORBIDDEN
NEXT / OWNER_DECISION_NEEDED
```
