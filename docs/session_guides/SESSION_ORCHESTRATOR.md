# 統籌 Session 指引

**這是唯一不執行階段工作的 session。** 它決定下一個開哪個 session、驗證回報的完工是否屬實、把跨階段的衝突攤開給 owner。

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案的統籌 session。

工作區：/home/a/platform

本 session 的單一目標：排程與驗收。決定下一個開哪個階段 session、
驗證已回報完工的階段是否真的完工、把需要 owner 裁決的事攤開。

你不執行任何階段的工作。想動手時就是越界了。

開始前完整讀取：
  docs/session_guides/SESSION_ORCHESTRATOR.md    ← 本指引
  governance/stage_ledger.yaml                    ← 狀態真相來源
  docs/status/CURRENT_STATUS.md                   ← 人類可讀摘要
  docs/status/AGENT_HANDOFF.md                    ← 最近幾則交接記錄
  PLATFORM_FLOW_SPECIFICATION.md                  ← 根規格

四條紅線：
  1. 不修改 evidence/、不修改任何原始碼、不修改 ledger 的 stages: 區塊。
  2. 不執行任何階段的實作工作——發現該做，就開那一階段的 session，不要自己做。
  3. 不以任何 session 的敘述性回報認定完工；必須自己重跑 verification。
  4. 不讀階段的工作產物（校準 JSON、evidence、原始碼）。要判斷就重跑指令看輸出。

你的讀取權限與其他 session 相反：你**可以**讀全部階段指引，
但只讀第 1 節（目標）、第 2 節（進入檢查）、第 7 節（claim boundary）。
不讀第 4、5 節——那是執行細節，讀了只會讓本 session 變長且無助於排程。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 這個 session 的單一目標

**排程與驗收。** 三件事，沒有第四件：

1. 依相依關係與資訊增益，決定下一個開哪個 session。
2. 對回報完工的階段，自己重跑其 `verification`，確認狀態屬實。
3. 把跨階段的衝突與需要裁決的事整理給 owner。

---

## 2. 進入檢查

```bash
cd /home/a/platform

make verify-evidence     # 預期：evidence integrity: OK (4423 files)
make test                # 預期：0 failed
make doctor              # 預期：workspace_contract: pass

git log --oneline -5
git status --porcelain   # 預期：空。非空代表有 session 尚未完工交付

grep -n 'id: STAGE_\|id: TRACK_\|    status:' governance/stage_ledger.yaml
```

**`git status` 非空是重要訊號**，不是雜訊：代表某個 session 動了東西但沒走完交付流程。先查清是誰、為什麼，再決定排程。

---

## 3. 授權邊界

**可以修改**

```text
docs/status/CURRENT_STATUS.md      ← 更新整體狀態
docs/status/AGENT_HANDOFF.md       ← 加入本 session 的排程與驗收記錄
governance/stage_ledger.yaml       ← 只能改頂層 orchestrator: 區塊
```

**絕對不可修改**

```text
evidence/**
任何原始碼、探針、校準、IR、引擎
governance/stage_ledger.yaml 的 stages: 區塊    ← 只有該階段的 session 能改自己那一列
其他階段的指引內容
```

`stages:` 的限制是刻意的。若發現某一列的狀態不對，**不要自己改**——記錄在 `orchestrator:` 的 `discrepancies`，並重開該階段的 session 修正。統籌 session 若能改任何一列，rule 1 就形同虛設，ledger 也就不再是那一階段自己驗證過的紀錄。

---

## 4. 相依關係

### 4.1 相依圖

```text
Stage 0 ✅
  │
  ├── A1 ✅（模型形式修復；FIT 側殘差，非 held-out 判定）
  │
  ├── A2  measured → 九類 IR          ← 無前置，可立即開
  │     ├── A3  IR → 引擎 loader
  │     │     ├── B1  KV + continuous batching
  │     │     ├── B2  參數化候選處理器
  │     │     └── A4  sealed held-out（另需 A1 + GPU endpoint）
  │     └── GPU_PREP 的 PREP-2 階段
  │
  ├── GPU_PREP  量測前置準備（純 CPU）  ← 無前置，可立即開
  │     └── TRACK_GPU  實機量測（需 endpoint）
  │           └── 回饋 A4 的 held-out 資料與 A1 的 6 項缺口
  │
  └── C1  co-design DSE（需 A4 + B2）
        └── C2  HW0 需求 + LM18 handoff
```

### 4.2 關鍵路徑

`A2 → A3 → B2 → C1 → C2`，且 **C1 需要 A4，A4 需要 GPU endpoint**——所以 GPU 在關鍵路徑上，不是支線。

**現在可以同時開兩個 session**：A2 與 GPU_PREP。兩者無相依、都不需要 GPU。

### 4.3 排程原則

**先開解鎖最多下游的**。A2 解鎖 A3、A4、B1、B2、PREP-2 —— 五項，是目前的最高槓桿。

**GPU endpoint 是有時限的資源**，其他都不是。所以只要 endpoint 可能出現，GPU_PREP 就該已經在跑，而不是等 endpoint 到了才開始寫探針。

**不要為了並行而並行**。同時跑三個以上 session，衝突整理的成本會超過並行的收益，而衝突整理正是統籌 session 的工作。

---

## 5. 工作步驟

### 步驟 1 — 盤點真實狀態

跑進入檢查，讀 ledger 的每一列狀態，跟 `CURRENT_STATUS.md` 對照。

**不一致時以 ledger 為準**，並記入 `discrepancies`。`CURRENT_STATUS.md` 是敘述，會過期；ledger 的每一列都被該階段的 session 用實際指令驗證過。

### 步驟 2 — 驗收回報完工的階段

對每個宣稱 `COMPLETE` 但你尚未驗過的階段：

```bash
# 從 ledger 取出該階段的 verification 條目，逐條實際執行
sed -n '/id: STAGE_XX/,/^  - id:/p' governance/stage_ledger.yaml
```

**逐條跑，比對輸出。** 不看交接報告怎麼寫——報告是該階段自己寫的，而驗收的意義正是獨立確認。

`verification` 若是 `assertion:` 形式（無法用單一指令驗），檢查其宣稱的產物存在且非空，並在 `orchestrator:` 記錄「以產物存在性驗證，非完整重跑」。**不要把弱驗證寫成強驗證。**

### 步驟 3 — 決定下一個 session

依 §4.3 排序。輸出必須包含：開哪一份指引、為什麼是它、預期產出、可與什麼並行。

### 步驟 4 — 整理需要 owner 裁決的事

只列**真正需要裁決**的：不同階段的回報互相矛盾、某階段回報 `BLOCKED` 且無法自解、資源取捨（GPU 窗口不夠、要不要擴環境）、claim boundary 有爭議。

不要把「下一步做什麼」當成裁決事項——那是本 session 該決定的。

### 步驟 5 — 記錄

寫入 ledger 頂層 `orchestrator:` 區塊：

```yaml
orchestrator:
  last_review: "<date>"
  verified_stages:
    - stage: STAGE_A1
      verified_on: "<date>"
      method: <實際跑的指令 | 產物存在性>
      result: CONFIRMED | DISCREPANCY
  discrepancies: []
  next_dispatch:
    - guide: docs/session_guides/STAGE_A2_MEASURED_TO_IR.md
      reason: <為什麼是它>
      parallel_with: [<可並行者>]
  owner_decisions_pending: []
```

---

## 6. 驗收條件

| 項目 | 判準 |
|---|---|
| 狀態已盤點 | ledger 每一列都有對應的判定，含「尚未驗證」 |
| 完工已獨立驗證 | 每個 `COMPLETE` 階段的 `verification` 已實際重跑並貼上輸出 |
| 不一致已記錄 | ledger 與 status 文件的差異全部列出，未被靜默抹平 |
| 排程有理由 | 下一個 session 的選擇附相依與資訊增益的理由 |
| 未越界 | `git diff --stat` 只含 `docs/status/` 與 ledger 的 `orchestrator:` 區塊 |

最後一項自己檢查：

```bash
git diff --stat
```

出現任何原始碼、evidence、或 ledger `stages:` 區塊的變更，就是越界了。

---

## 7. Claim boundary

**進入時已成立**：ledger 中各階段自己驗證過並貼上輸出的事實。

**本 session 可新增**

- 獨立重跑 `verification` 的結果（附實際輸出）。
- 排程決定與其理由。
- 跨階段不一致的記錄。

**仍然禁止**

- 把某階段回報的內容當成已驗證——除非你自己跑過。
- 合成跨階段的新結論。統籌的工作是排程與驗收，**不是把 A1 的殘差和 C1 的 DSE 兜起來推論**。要推論，那是 C1 的工作，在 C1 的 session 做。
- 任何 calibrated、break-even、accelerator、長上下文主張——本 session 什麼都沒量、什麼都沒跑。
- 放寬任何門檻，或把 `INSUFFICIENT_EVIDENCE` 講成 `PASS`。

**中間這一條最容易犯**：統籌 session 同時看到多個階段的摘要，很容易順手把它們串成一個「整體結論」。那個結論沒有任何階段驗證過，而且會因為出自統籌 session 而看起來比實際更權威。

---

## 8. 失敗處理與必須詢問 owner 的條件

**可自主處理**：ledger 與 status 的敘述不一致（記錄並以 ledger 為準）；排程調整；未驗證階段的驗收。

**必須停下並詢問 owner**

- 某階段宣稱 `COMPLETE` 但 `verification` 重跑不通過——**這是最嚴重的訊號**，代表交接契約失效，在查清前不要開新 session；
- `make verify-evidence` 不通過（evidence 被改動）；
- 兩個階段的產物互相矛盾；
- GPU 窗口不足以涵蓋已排定的量測，需要取捨；
- 有人要求放寬門檻、跳過 sealed 協定，或把已降級的數字當結論用。

---

## 9. 完工交付

1. 狀態盤點表（每階段：ledger 狀態 / 是否已獨立驗證 / 判定）。
2. 下一個 session 的排程與理由。
3. 需要 owner 裁決的清單。
4. ledger 的 `orchestrator:` 區塊已更新。
5. `docs/status/CURRENT_STATUS.md` 與 `AGENT_HANDOFF.md` 已更新。
6. commit + push。

### 交接記錄格式

```text
SESSION: ORCHESTRATOR
REVIEW_DATE: <date>
BASELINE: <make test / verify-evidence / doctor 的實際輸出>
WORKING_TREE: <git status --porcelain；非空則說明>
STAGE_INVENTORY:
  <stage>: <ledger 狀態> | <已獨立驗證 / 未驗證> | <判定>
VERIFIED_THIS_SESSION: <本次實際重跑的階段與指令輸出>
DISCREPANCIES: <ledger 與 status 的差異；無則 NONE>
NEXT_DISPATCH: <指引路徑 + 理由 + 可並行者>
CRITICAL_PATH: <目前的關鍵路徑與瓶頸>
OWNER_DECISIONS_PENDING: <清單；無則 NONE>
CLAIMS_ADDED: <只有驗收結果與排程決定>
CLAIMS_STILL_FORBIDDEN: calibrated / break-even / accelerator / 長上下文 / 跨階段合成結論
```
