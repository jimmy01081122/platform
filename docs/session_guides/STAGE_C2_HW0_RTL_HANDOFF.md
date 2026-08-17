# Stage C2 · HW0 需求與 LM18 RTL handoff 規格包

```text
GUIDE_COMPLETENESS = RULES_ONLY
```

**本指引固定目標、約束、驗收與交接格式；各 HW0 row 的數值待 C1 的 break-even 面確定後填入。**

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 Stage C2 的執行者。

工作區：/home/a/platform

本 session 的單一目標：把 C1 的 break-even 面轉成可追溯的硬體規格需求（HW0），
並產出 LM18 handoff 規格包，讓後續 RTL 工作不需猜測介面與語意。

本平台不實作 RTL——只出規格。純 CPU 工作。

注意：本指引標記為 RULES_ONLY——約束與驗收已固定，數值需你依 C1 結果填入。

開始前完整讀取：
  docs/session_guides/STAGE_C2_HW0_RTL_HANDOFF.md   ← 本階段指引
  PLATFORM_FLOW_SPECIFICATION.md                     ← 根規格，特別是 §11 HW0 與 LM18
  docs/methodology/RTL_FULL_DATAPATH_DEFINITION.md   ← full datapath 的完成定義
  governance/stage_ledger.yaml                       ← 狀態真相來源

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不讀其他階段的指引。
  3. 進入檢查任一項不符即停止並回報。

本階段特有：證據不足的 row 保持 UNAVAILABLE_WITH_CONSEQUENCE，不得猜值。
若全部掛載點都未達 break-even，交付 no-accelerator boundary —— 那是有效交付。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 目標

把 C1 的 break-even 結果轉成十二項可追溯的硬體規格需求，並產出六份 RTL handoff 文件。

---

## 2. 進入檢查

```bash
cd /home/a/platform
make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：0 failed
grep -A3 'id: STAGE_C2' governance/stage_ledger.yaml   # 預期：status: NOT_STARTED
grep -A3 'id: STAGE_C1' governance/stage_ledger.yaml   # 預期：status: COMPLETE
```

**必須先讀 C1 的交接記錄**，特別是 `BREAK_EVEN_BY_ATTACHMENT_POINT`、`NO_BENEFIT_BOUNDARIES` 與各項的 fidelity 標記。

---

## 3. 授權邊界

**可修改／新增**：`hardware/RTL_SCOPE.md`（**舊 repo 要求兩次但從未建立**）、LM18 六份產出、`experiments/specs/`、`runs/`、`docs/status/`、ledger 的 `STAGE_C2` 列。

**不可修改**：`evidence/**`、`hardware/rtl/`（本平台不實作 RTL）、`hardware/{syn,sta,formal,verification}/` 的既有結果、`dse/` 的 C1 產出、ledger 其他列。

---

## 4. HW0 的十二項需求（規格 §11.1）

```text
HW0-COMMAND-THROUGHPUT       HW0-MAX-CONTROL-LATENCY      HW0-READY-DEPENDENCY-QUEUE
HW0-DMA-DESCRIPTOR-RATE      HW0-COPY-CONCURRENCY         HW0-METADATA-CAPACITY
HW0-METADATA-BANDWIDTH       HW0-RESIDENCY-IDENTITY-WIDTH HW0-PREFETCH-INTERFACE
HW0-COMPLETION-TAGGING       HW0-SYNCHRONIZATION          HW0-BACKPRESSURE-FALLBACK
```

**每個 row 必須具備**：source evidence、公式、單位、選定分位數、不確定度、適用 envelope、break-even 或 no-benefit boundary。

**兩條硬規則**

1. **證據不足者保持 `UNAVAILABLE_WITH_CONSEQUENCE`，不得猜值。** 並寫明後果：缺這一項會導致哪個結論無法成立。
2. **禁止以單一最大值定規格。** 要用分位數並附不確定度。

---

## 5. 與既有硬體證據對照（規格 §11.2）

推導出的需求必須對照 `hardware/` 內既有的合成與時序結果：

| 設計 | 結果 |
|---|---|
| seqbuf residency engine | ME=32 → 346.07 MHz / 11,982.768 µm²；ME=128 → 99.25 MHz；ME=256 → 50.56 MHz / 62,850.746 µm²；ME=384 → 35.03 MHz / 100,993.55 µm² |
| banked LRU victim | N=128/B=16 → 200.33 MHz / 8,918.182 µm²（達 200 MHz）；N=256/B=16 → 107.14 MHz；N=256/B=32 → 119.78 MHz；N=384/B=24 → 83.11 MHz |
| argmin 微架構 | 組合式 66.50 MHz / 5,641.594 µm² vs 暫存器化掃描 236.24 MHz / 5,626.964 µm²（**近乎零面積代價**） |
| expert decompressor | NB4/L8 → 811.08 MHz / 4,901.848 µm²，16 GB/s 輸入需 39 lanes；NB8/L16 → 307.31 MHz / 23,815.246 µm² |

來源：`hardware/sta/out_scale/sta_scale.csv`、`hardware/sta/out_banked/sta_banked.csv`、`hardware/syn/out_sta/argmin_fix.csv`、`hardware/sta/out_decomp/sta_decomp.csv`。

**誠實性邊界**：以上是 **pre-layout、wire-load model、ideal clock** 的相對架構 DSE，**非 sign-off**。不得據此宣稱實體面積、功耗或產品可行性（`AGENTS.md` §3.6）。

**一個有意義的對照**：Mixtral 有 256 個 expert 物件。既有 STA 顯示 seqbuf 引擎在 ME=256 只到 50.56 MHz、banked victim 在 N=256 只到 107–120 MHz。若 C1 推導出的控制速率需求遠低於這些數字，那本身就是結論的一部分。

---

## 6. LM18 handoff 規格包（規格 §11.3）

```text
RTL-ARCH      架構：資料路徑、狀態、介面
RTL-SCHEMA    資料結構與欄位定義
RTL-GOLDEN    golden vectors（參考輸入輸出）
RTL-STIMULUS  測試激勵
RTL-ACTIVITY  activity 資料（供功耗估算，非 sign-off）
RTL-HANDOFF   總覽與使用說明
```

**目標是讓後續 RTL 工作不需猜測** identity、capacity、ordering、completion、error 與 backpressure 語意。

另須建立 `hardware/RTL_SCOPE.md`——舊 repo 的 `AGENT_BOOTSTRAP_TASK.md` step 6 與 `explorations/README.md` 都要求，但從未建立。內容依 `docs/methodology/RTL_FULL_DATAPATH_DEFINITION.md`：**控制 FSM 或 MMIO scaffold 不算 full datapath。**

---

## 7. 驗收條件

| 項目 | 判準 |
|---|---|
| HW0 完整性 | 十二項各具 evidence、公式、單位、分位數、不確定度、envelope |
| 不猜值 | 證據不足者標 `UNAVAILABLE_WITH_CONSEQUENCE` 並寫明後果 |
| 不用單一最大值 | 規格以分位數表示並附不確定度 |
| 硬體對照 | 與既有 STA 結果對照，且標明 pre-layout 邊界 |
| LM18 齊備 | 六份產出皆存在且內容可用 |
| RTL_SCOPE | `hardware/RTL_SCOPE.md` 已建立且符合 full datapath 定義 |
| 可追溯 | 每項需求可回溯到 C1 的哪個 break-even 結果、再回溯到哪次量測 |
| 基線未退步 | `make test` → 0 failed |

---

## 8. Claim boundary

**可新增**

- 「在 ⟨envelope⟩ 內，掛載點 ⟨X⟩ 的硬體需求為 ⟨具體規格 + 不確定度⟩，可追溯至 ⟨證據鏈⟩。」
- 「掛載點 ⟨Y⟩ 未達 break-even，交付 no-accelerator boundary：⟨具體條件⟩。」

**仍然禁止**

- 以 cell count 直接宣稱實體面積、功耗或產品可行性。
- 把既有 STA（pre-layout、wire-load model、ideal clock）當作 sign-off。
- 為證據不足的 row 填入猜測值。
- 以單一最大值定規格。
- 宣稱 RTL 已驗證——本平台不實作 RTL。

---

## 9. 待補的細節

執行時需依 C1 結果決定：各 row 的具體數值與分位數選擇、不確定度的傳播方式、哪些 row 因證據不足而 `UNAVAILABLE`、LM18 golden vectors 的涵蓋範圍。

決定後補回本指引，並在 ledger 把 `guide_completeness` 改為 `EXECUTABLE`。

---

## 10. 失敗處理與必須詢問 owner 的條件

**必須停下並詢問**：所有掛載點都未達 break-even（這是有效交付，但範圍值得確認）；某 row 的證據強度不足以支撐 RTL 決策而 owner 可能想補量測；需要修改既有 `hardware/` 結果；需要修改 `evidence/`。

**全部未達 break-even 是有效交付。** 依 `AGENTS.md` §10，把它寫成可重用的 boundary condition，說明在什麼條件下值得重新評估。

---

## 11. 完工交付

依 `README.md` 的標準流程。

```text
STAGE: C2
STATUS: COMPLETE | BLOCKED
HW0_ROWS:
  <每項>: <數值 + 單位 + 分位數 + 不確定度 + envelope + 來源>
  或 UNAVAILABLE_WITH_CONSEQUENCE: <後果說明>
UNAVAILABLE_COUNT: <十二項中有幾項證據不足>
STA_CROSS_CHECK: <與既有合成結果的對照結論>
LM18_ARTIFACTS: <六份產出的路徑>
RTL_SCOPE: <路徑；是否符合 full datapath 定義>
TRACEABILITY: <每項需求到 C1 到量測的鏈路>
NO_ACCELERATOR_BOUNDARIES: <未達 break-even 者的 boundary condition>
BASELINE: <make test 輸出>
EVIDENCE_UNCHANGED: <make verify-evidence 輸出>
FILES_CHANGED / CLAIMS_ADDED / CLAIMS_STILL_FORBIDDEN
NEXT / OWNER_DECISION_NEEDED
```
