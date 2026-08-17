# Stage A4 · Sealed held-out 校準驗證

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 Stage A4 的執行者。

工作區：/home/a/platform

本 session 的單一目標：以 sealed held-out 協定判定校準是否通過。
這是本專案第一次有資格宣稱 calibrated —— 也可能判定為不通過。

需要 GPU：是。held-out 必須是新量測；evidence/ 內的舊資料一律只能作 FIT。

開始前完整讀取：
  docs/session_guides/STAGE_A4_SEALED_HOLDOUT.md   ← 本階段指引
  PLATFORM_FLOW_SPECIFICATION.md                    ← 根規格，特別是 §7 sealed held-out 協定
  governance/stage_ledger.yaml                      ← 狀態真相來源
  docs/status/DECISION_LOG.md                       ← 決策 P-005 說明舊資料為何降級

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不讀其他階段的指引。
  3. 進入檢查任一項不符即停止並回報。

本階段最重要的一條：held-out 只能開封一次。開封後若未通過，記錄未通過並重新設計
實驗，不得回頭調模型再開第二次。開封次數必須可稽核。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 這個 session 的單一目標

依 sealed held-out 協定，對修正後的模型做一次、且僅一次的獨立驗證，並以三值判定記錄結果。

**判定為 FAIL 或 INSUFFICIENT_EVIDENCE 是有效且必須誠實記錄的結果。**

---

## 2. 進入檢查

```bash
cd /home/a/platform

make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：0 failed，測試數不低於 317
make doctor                   # 預期：workspace_contract: pass

grep -A3 'id: STAGE_A4' governance/stage_ledger.yaml   # 預期：status: NOT_STARTED
grep -A3 'id: STAGE_A1' governance/stage_ledger.yaml   # 預期：status: COMPLETE
grep -A3 'id: STAGE_A3' governance/stage_ledger.yaml   # 預期：status: COMPLETE

# A1 的事前登記必須存在
ls experiments/specs/cal_model_form_repair_v1.yaml

# 舊的失敗報告必須仍在且未被修改
sha256sum evidence/gpu_measurements/rtx-pro-6000-v3-20260718/rtx-q1-validation-report.json
grep 'rtx-q1-validation-report' governance/lineage/EVIDENCE_SHA256SUMS
# 兩者必須一致
```

**A4 依賴 A1 與 A3 皆為 COMPLETE。** 缺一即停止。

**還需要有效的 GPU endpoint。** 若 owner 尚未提供，本階段只能做到「設計 split 並封存」，實際量測與開封必須等 endpoint 就緒——此時應把 `status` 記為 `BLOCKED` 並說明卡在哪。

---

## 3. 授權邊界

**可以修改／新增**

```text
calibration/                    ← sealed manifest、凍結參數、評分報告
experiments/specs/<id>.yaml
runs/<run_id>/
docs/status/ 五份
governance/stage_ledger.yaml    ← 只改 STAGE_A4 那一列
新量測的輸出 namespace（新目錄，不進 evidence/ 直到完成驗證與備份）
```

**絕對不可修改**

```text
evidence/**                     尤其舊的 q1 fail 報告
A1 的事前登記檔                  ← 已凍結的登記不得追改
封存後的 split manifest          ← 封存即凍結
governance/stage_ledger.yaml 的其他列
```

---

## 4. 進入時的事實基線

### 4.1 為什麼舊資料只能作 FIT

決策 P-005：診斷 calibration 模型形式缺陷時**檢視過 validation split 的殘差**，獨立性已受汙染。

因此 `evidence/` 中所有既有量測（q0/q1、8/11 各家族、8/13–14 campaigns）一律降級為 FIT 側。資料集內部既有的 `fit_role` 欄位（含 `HELD_OUT`、`CONTROL`）在本平台**不得作為 held-out 宣稱**——那是舊契約下的標籤。

held-out 必須是**新的量測**。

### 4.2 sealed held-out 協定（規格 §7.2）

```text
1. 事前登記模型形式與預期方向      -> experiments/specs/<id>.yaml
2. 設計新量測的 fit / validation / held-out split
3. split 定義與資料 hash 封存      -> sealed manifest，記錄 SHA-256
4. 只用 fit 擬合；validation 可用於模型選擇
5. 模型與參數凍結                  -> 凍結物 hash 記錄
6. held-out 開封，評分一次
7. 結果寫入報告，無論通過與否
```

**開封次數必須為 1 且可稽核。**

### 4.3 判定門檻與三值判定（規格 §7.3）

```text
MAPE <= 15%            single-point APE <= 20%

PASS                   信賴區間上界 <= 門檻
FAIL                   信賴區間下界 > 門檻
INSUFFICIENT_EVIDENCE  區間跨越門檻
```

**資料不足時判 `INSUFFICIENT_EVIDENCE`，不得寫成 PASS 或 FAIL。**

### 4.4 誤差分解（規格 §7.4）

判定之外，誤差須分解為：trace error、timing-model error、resource-model error、measurement noise、instrumentation overhead、unmodeled runtime behavior。

這個分解比單一 MAPE 數字更有價值——它決定下一步該補量測還是改模型。

### 4.5 GPU 量測規則

新量測必須遵守 GPU 軌的所有規則（見 `TRACK_GPU_MEASUREMENT.md`，但**本階段不需要讀那份指引**，以下摘要即足夠）：

- canonical domain 不符即停止並回報，不得用其他 GPU 補數據；
- 若 GPU 型號與既有 calibration 不同，必須建立**獨立的 platform profile 與獨立的 calibration package**，不得跨平台套用參數（規格 §3.2）；
- 發現不屬於本 session 的 GPU process：不傳 signal、不 attach profiler、不改 config，回報後等待；
- 禁用 `kill`／`pkill`／`killall`；
- 每個 attempt 完整保存 raw，含失敗的 attempt。

---

## 5. 工作步驟

### 步驟 1 — 設計 split 並事前登記

在**取得任何新量測數據之前**，寫下：

- held-out 要涵蓋哪些條件（尺寸區間、方向、concurrency、operand shape）；
- 為什麼這些條件足以檢驗 A1 修正的四項缺陷；
- 判定門檻（沿用 §4.3，不得放寬）；
- 樣本數與重複次數。

**設計要點**：held-out 應針對 A1 四項缺陷各自的關鍵區間。例如缺陷 1（contention）需要多 stream 的點；缺陷 4（小尺寸）需要小傳輸的點。若 held-out 只涵蓋容易的區間，即使 PASS 也沒有說服力。

### 步驟 2 — 封存

產生 sealed manifest：split 定義 + 每筆資料的 SHA-256 + 封存時間。封存後 split **不得變動**。

### 步驟 3 — 擬合與模型選擇

只用 fit 側。validation 側可用於模型選擇（例如決定回歸階數），但**不得**用於判定。

### 步驟 4 — 凍結

模型與參數凍結，記錄凍結物的 hash。凍結之後不得再改。

### 步驟 5 — 開封並評分一次

開封 held-out，計算 MAPE 與 per-point APE，套用三值判定。

**記錄開封時間與操作者。**

### 步驟 6 — 誤差分解與結論

依 §4.4 分解誤差。撰寫報告，涵蓋：

- 四項 metric 各自的判定；
- 與舊 q1 結果的對照（舊：component 304.418% / TPOT 293.936% / PCIe 66.879% / throughput 60.658%，17/90 通過）；
- 誤差分解；
- **適用 envelope**——通過的話，通過的是哪個區間？超出該區間的一律標 `PROJECTED`。

### 步驟 7 — 若未通過

記錄未通過，分析原因，提出下一輪的實驗設計。**不得**回頭調模型再開第二次同一個 sealed set。若要再驗，必須設計新的 held-out 並重新封存。

---

## 6. 驗收條件

| 項目 | 判準 |
|---|---|
| 事前登記在先 | split 設計檔的時間早於任何新量測數據 |
| 封存完整 | sealed manifest 含 split 定義與逐筆 SHA-256 |
| 凍結在開封前 | 模型凍結物的 hash 記錄時間早於開封時間 |
| 開封一次 | 開封記錄唯一且可稽核 |
| 三值判定 | 四項 metric 各自有明確判定，資料不足者為 `INSUFFICIENT_EVIDENCE` |
| 誤差分解 | 六類誤差來源皆有討論 |
| 適用 envelope | 若判 PASS，明確界定適用區間 |
| 舊失敗仍在 | q1 報告 checksum 與 `EVIDENCE_SHA256SUMS` 一致 |
| 證據未被動 | `make verify-evidence` → 4423/4423 |
| 基線未退步 | `make test` → 0 failed |

---

## 7. Claim boundary

**進入時已成立**：A1 已修正模型形式並事前登記；A3 已讓引擎被真實 IR 驅動且 counters 位元精確；`evidence/` 逐檔一致。

**本階段可新增（視判定而定）**

- 判 PASS：「在 ⟨明確界定的 envelope⟩ 內，模型通過 held-out 驗證，MAPE ⟨數字⟩。」
- 判 FAIL：「模型在 ⟨條件⟩ 下未通過 held-out，MAPE ⟨數字⟩，主要誤差來源為 ⟨分解結果⟩。」
- 判 INSUFFICIENT：「現有樣本不足以判定，需要 ⟨具體補測⟩。」

**仍然禁止**

- 把 PASS 外推到 envelope 之外而不標 `PROJECTED`。
- 跨平台套用 calibration 參數。
- 任何 break-even 或 accelerator 主張（屬 C1）。
- 長上下文或高並發的效能主張——除非 held-out 實際涵蓋該區間。
- 開封後回頭調模型再開第二次。

---

## 8. 失敗處理與必須詢問 owner 的條件

**科學驗證 FAIL 不是程式錯誤**（規格 §15）。保留結果，不移動 held-out，不刪慢樣本，不放寬門檻。

**必須停下並詢問 owner**

- GPU 或 domain 與登記不符；
- 只能藉由改 workload、開 offload、縮短長度或放寬門檻才能執行；
- 發現 fit／held-out 洩漏；
- 需要刪除或覆寫既有證據；
- 需要額外付費、安裝或重新下載；
- **held-out 開封後想再開第二次**——這需要 owner 明確裁決，且必須記錄為新的 sealed set。

---

## 9. 完工交付

1. 產出清單。
2. `runs/<run_id>/` 完整目錄（含失敗 run）。
3. 新量測的 raw 完整保存並備份，checksum 驗證後才納入 `evidence/`，同時更新 `governance/lineage/EVIDENCE_SHA256SUMS`。
4. 更新 `governance/stage_ledger.yaml` 的 `STAGE_A4` 列。
5. 更新五份 status——特別是 `VALIDATION_MATRIX` 的校準 gate 那一節。
6. commit + push。
7. 回報。

### 交接記錄格式

```text
STAGE: A4
STATUS: COMPLETE | BLOCKED
GPU_ENDPOINT: <identity 或 BLOCKED_ON_ENDPOINT>
PLATFORM_PROFILE: <是否與既有相同；不同則列出新 profile>
SPLIT_DESIGN_SPEC: <路徑與時間>
SEALED_MANIFEST: <路徑與 sha256>
MODEL_FROZEN: <凍結物 hash 與時間>
UNSEAL_TIME: <開封時間；必須晚於凍結時間>
UNSEAL_COUNT: 1
VERDICT:
  component_latency:      PASS | FAIL | INSUFFICIENT_EVIDENCE  <MAPE>
  pcie_transfer_latency:  PASS | FAIL | INSUFFICIENT_EVIDENCE  <MAPE>
  moe_replay_tpot:        PASS | FAIL | INSUFFICIENT_EVIDENCE  <MAPE>
  moe_replay_throughput:  PASS | FAIL | INSUFFICIENT_EVIDENCE  <MAPE>
COMPARISON_TO_OLD_Q1: <與 304% / 294% / 67% / 61% 的對照>
ERROR_DECOMPOSITION: <六類來源>
APPLICABLE_ENVELOPE: <若 PASS，適用區間>
OLD_FAIL_REPORT_INTACT: <checksum>
EVIDENCE_UNCHANGED: <make verify-evidence 輸出>
BASELINE: <make test 輸出>
FILES_CHANGED: <清單>
CLAIMS_ADDED: <本階段新增>
CLAIMS_STILL_FORBIDDEN: envelope 外推 / 跨平台套參數 / break-even / accelerator
NEXT: <下一個動作>
OWNER_DECISION_NEEDED: <若有>
```
