# Stage A1 · Calibration 模型形式修復與重擬合

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 Stage A1 的執行者。

工作區：/home/a/platform

本 session 的單一目標：修復 calibration 的模型形式缺陷並重新擬合。純 CPU 工作，不需要 GPU。

開始前完整讀取：
  docs/session_guides/STAGE_A1_CALIBRATION_MODEL_FORM.md   ← 本階段指引
  PLATFORM_FLOW_SPECIFICATION.md                            ← 根規格（權威來源）
  governance/stage_ledger.yaml                              ← 狀態真相來源
  docs/PHASE_NAMING_MAP.md                                  ← 五套並存命名的對照，避免誤判進度

三條紅線：
  1. 不修改 evidence/ 內任何檔案。特別是既有的 rtx-q1-validation-report.json
     四項 FAIL，那是證據，必須原封保留。
  2. 不讀其他階段的指引。
  3. 進入檢查任一項不符即停止並回報，不得「看起來差不多就繼續」。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 這個 session 的單一目標

修正四項模型形式缺陷，並用 `evidence/` 內既有的微基準重新擬合。

**不包含**判定校準是否通過——那屬於 Stage A4 的 sealed held-out。本階段結束時**不得**宣稱任何 calibrated PASS。

---

## 2. 進入檢查

逐條執行，任一不符即停止並回報。

```bash
cd /home/a/platform

# 1) 證據完整性
make verify-evidence
# 預期：evidence integrity: OK (4423 files)

# 2) 基線
make test
# 預期：317 Python passed + 14 CTest passed，0 failed

# 3) workspace contract
make doctor
# 預期：workspace_contract: pass

# 4) ledger 狀態
grep -A2 'id: STAGE_A1' governance/stage_ledger.yaml
# 預期：status: NOT_STARTED

# 5) 必要輸入存在
ls evidence/gpu_measurements/rtx-pro-6000-v3-20260718/rtx-q1-validation-report.json
ls evidence/gpu_measurements/rtx-pro-6000-v3-20260718/rtx-q0-fitted-parameters.json
ls evidence/measurement_backups/20260811T171000Z__phase7_remote_component_backup/raw/components/
ls evidence/measurement_backups/20260811T171000Z__phase7_remote_transfer_backup/raw/transfers/
# 預期：全部存在
```

若 `make test` 少於 317 或有失敗，先確認環境（`make venv`，並確認 `PYTHONDONTWRITEBYTECODE=1`）。仍不符即停止回報。

---

## 3. 授權邊界

**可以修改／新增**

```text
calibration/                                   ← 本階段主要產出
src/edgeflow/calibrated_backend.py             ← 只改模型形式函式
experiments/specs/cal_model_form_repair_v1.yaml ← 事前登記
runs/<run_id>/                                 ← 執行記錄
docs/status/ 五份                              ← 完工時更新
governance/stage_ledger.yaml                   ← 只改 STAGE_A1 那一列
```

**絕對不可修改**

```text
evidence/**                     任何檔案，尤其 rtx-q1-validation-report.json
explorations/**                 本階段不碰模擬器
hardware/**  measurement/**     與本階段無關
governance/stage_ledger.yaml    STAGE_A1 以外的任何一列
governance/lineage.yaml         遷移溯源記錄
```

**不要讀其他階段的指引。** 需要背景時讀根規格。

**不要重寫 calibration harness。** `src/edgeflow/calibrated_backend.py`（516 行）已具備：四個 split 強制、artifact 路徑重用拒絕、`record_id` 跨 split 碰撞拒絕、SHA-256 驗證、環境 manifest 強制、非物理擬合拒絕（slope ≤ 0 或 intercept < 0）、無 fallback 常數。這些保護必須保留，只替換模型形式函式。

---

## 4. 進入時的事實基線

### 4.1 目前的失敗（來源：`evidence/gpu_measurements/rtx-pro-6000-v3-20260718/rtx-q1-validation-report.json`）

門檻 MAPE ≤ 15%、single-point APE ≤ 20%。

| metric | MAPE | 通過點數 |
|---|---|---|
| component_latency | 304.418% | 9 / 48 |
| moe_replay_tpot | 293.936% | 0 / 6 |
| pcie_transfer_latency | 66.879% | 10 / 30 |
| moe_replay_throughput | 60.658% | 0 / 6 |
| 合計 | — | 17 / 90 |

**量測本身可信**：n=5、95% CI 寬約 0.0002 ms、跨 split 重現到小數第四位。失敗歸因於模型形式。

### 4.2 四項結構性缺陷（規格 §8.1）

**缺陷 1 — contention 施加位置錯誤。**

`rtx-q0-fitted-parameters.json` 的 `copy_engine.stream_latency_factors = {1: 1.0, 2: 1.7384584273483465, 4: 2.762992620065082}` 被當成 per-transfer latency 乘數。

但實測顯示 1/2/4 stream 的**單筆**延遲幾乎相同：

```text
measured 3.112996 -> predicted 3.108040  ratio 0.998  PASS
measured 3.113913 -> predicted 5.403199  ratio 1.735  FAIL
measured 3.115659 -> predicted 8.587492  ratio 2.756  FAIL
```

三個 ratio（1.0 / 1.735 / 2.756）**精確等於那組 stream factor**。30 個 PCIe 點中恰好只有 10 個 1-stream 點通過。

同一份參數檔的 `contention.per_extra_concurrency = 1.0046`（幾乎無競爭）與這組 factor 自相矛盾——**光靠 calibration split 即可證明模型形式有誤，不需動用 held-out**。

修正方向：stream factor 屬於**聚合頻寬／佔用**模型（N 條 stream 共享 copy engine 頻寬，單筆延遲不變、總完成時間變長），不是 per-transfer latency 乘數。

**缺陷 2 — component service model 對 operand shape 無感。**

`gpu_service.operation_ms` 只有四個常數：`dequant 0.1476092`、`gather_scatter 0.0292579`、`grouped_gemm 0.2431867`、`selected_expert 0.2201017`。

48 個量測點跨越 0.016–1.136 ms（70 倍），預測值卻只取 8 個相異值。

修正方向：以 operand shape 參數化回歸，每個 operation class 一組係數。

**缺陷 3 — MoE replay 無 batching 項。**

預測值在 concurrency 1 與 4 之間幾乎不變（0.2727 / 0.2726 ms/token；3667 / 3654 tok/s），實測有約 4 倍差異。

修正方向：加入 batching / concurrency 項。

**缺陷 4 — 小尺寸傳輸區間被系統性低估。**

實測小傳輸下限約 0.037 ms，模型 intercept 僅 0.0153 ms，小尺寸點低估 55–59%。

修正方向：改為 piecewise 或 `max(fixed_overhead, linear)`。

> **這一項對後續階段特別重要**：KV block 是 2 MiB，正落在此區間（規格 §2.2）。B1 的 KV 時序會依賴這裡修好的模型。

### 4.3 可用於重擬合的微基準

**這是本階段的關鍵資產**——比 q0/q1 那組（15 H2D + 15 D2H）強得多。

**Transfer**：`evidence/measurement_backups/20260811T171000Z__phase7_remote_transfer_backup/raw/transfers/`

6 個 attempt 目錄，合計 135 筆記錄。每筆為一行 JSON，欄位含 `family`、`id`、`bytes_label`、`direction`、`host_memory`、`allocation_node`、`measured_repetition_count`、`measurement_class`，以及 `cpu_enqueue_ns` 與 `gpu_duration_ns` 的完整統計（`mean`/`median`/`min`/`max`/`stddev`/`count`/`ci95_halfwidth`/`ci_rule_stable`）。

```text
XFER-L0  4KiB · 1MiB · 16MiB · E · 4E
XFER-L1  64KiB · 4MiB · 0.5E · 2E
XFER-L2  4KiB · 1MiB · 16MiB · E · 4E
XFER-L3  E · 2E
XFER-E1/E2/E3 · XFER-O0–O3 · XFER-Q0/Q1
directions: H2D · D2H · H2D+D2H
```

其中 `E` 指 expert 物件大小（352,321,536 B）。

- **4KiB → 16MiB 的尺寸掃描**直接支撐缺陷 4 的小尺寸 regime。
- **`H2D+D2H` 雙向記錄**是缺陷 1 真正的競爭證據——用它判斷競爭該施加在頻寬還是延遲。
- `XFER-O*`（overlap）與 `XFER-Q*`（queue）家族與 overlap／佇列行為相關。

**Component**：`evidence/measurement_backups/20260811T171000Z__phase7_remote_component_backup/raw/components/`

3 個 attempt 目錄，合計 54 筆記錄。欄位含 `family`、`id`、`input_bytes`、`expert_weight_bytes`、`dispatch_semantics`、`kernel_identity_source`，以及同樣的 `cpu_enqueue_ns`／`gpu_duration_ns` 統計。

```text
CMP-A  input_bytes 1 MiB → 224 MiB（8 點）
CMP-M  input_bytes 64 KiB → 597 MiB（13 點）
CMP-L  input_bytes 8 KiB（1 點）
```

`input_bytes` 橫跨約 5 個數量級——這正是取代四個常數所需的 shape 軸。

**注意**：`ci_rule_stable` 欄位標示該點的信賴區間是否穩定。`cpu_enqueue_ns` 有不少點為 `false`（CPU 側抖動大），`gpu_duration_ns` 多為 `true`。擬合時應納入此標記，不要把不穩定的點與穩定的點等權處理。

---

## 5. 工作步驟

### 步驟 1 — 事前登記（必須先於任何擬合）

建立 `experiments/specs/cal_model_form_repair_v1.yaml`，在**看到任何評分之前**寫死：

- 四項結構性變更的具體形式（函式形態、參數化軸、係數個數）；
- 每項的預期方向（例如「PCIe 的 2-stream 與 4-stream 點的系統性高估應消失」）；
- 使用哪些量測家族擬合、哪些保留不用；
- 明確記載：**舊資料一律只作 FIT，本階段不產生 held-out 判定**（決策 P-005）。

事前登記的目的是讓「改模型形式」與「照著答案調參」可被區分。這份檔案的時間戳與內容會在驗收時被檢查。

### 步驟 2 — 實作四項修正

在 `src/edgeflow/calibrated_backend.py` 中**只替換模型形式函式**，保留所有既有保護。新的模型形式與擬合程式放 `calibration/`。

建議順序（由證據最強、最容易驗證者先做）：

1. **缺陷 1（contention）**——證據最明確（ratio 精確等於 stream factor），且可用 `H2D+D2H` 雙向記錄交叉驗證。
2. **缺陷 4（小尺寸 regime）**——有 4KiB→16MiB 完整掃描，直接可擬。
3. **缺陷 2（component shape）**——有 `input_bytes` 五個數量級的掃描。
4. **缺陷 3（batching 項）**——資料最少，最後做。

### 步驟 3 — 重新擬合

只用 FIT 側資料擬合。保持 `calibrated_backend.py` 既有的非物理解拒絕（slope ≤ 0 或 intercept < 0）。

輸出寫到**新目錄**（例如 `calibration/fits/v2/`），不覆寫 `evidence/` 內任何檔案。

### 步驟 4 — 殘差分析

對每項 metric 做殘差分析，並記錄：

- 修正後仍然偏離的點與其特徵（尺寸區間？方向？family？）；
- 哪些點的 `ci_rule_stable = false`，是否與殘差相關；
- **哪些量測缺口會限制後續判定**——這份清單直接餵給 GPU 軌的量測優先序第 4 項。

### 步驟 5 — 誠實記錄

如果某項缺陷修正後仍然無法收斂，**記錄為未收斂並說明原因**。不要為了讓數字好看而放寬形式或挑選資料。規格 §15 明文：科學驗證 FAIL 不是程式錯誤。

---

## 6. 驗收條件

| 項目 | 判準 |
|---|---|
| 事前登記 | `experiments/specs/cal_model_form_repair_v1.yaml` 存在，且其內容早於任何擬合輸出 |
| 證據未被動 | `make verify-evidence` → 4423/4423 通過 |
| 舊失敗仍在 | `evidence/gpu_measurements/rtx-pro-6000-v3-20260718/rtx-q1-validation-report.json` 未被修改（checksum 不變） |
| 基線未退步 | `make test` → 0 failed，測試數不低於 317 |
| 非物理解仍被拒 | 既有的 slope ≤ 0 / intercept < 0 拒絕邏輯保留且有測試覆蓋 |
| 殘差分析已產出 | 含仍偏離的點、`ci_rule_stable` 關聯、量測缺口清單 |
| 未越權 | 變更檔案清單全部落在第 3 節的可修改範圍內 |

---

## 7. Claim boundary

**進入本階段時已成立**

- `evidence/` 內容與來源 workspace 逐檔一致（4423/4423）。
- 遷移後基線 317 Python + 14 CTest 全綠。
- q1 的四項 MAPE 失敗是既成事實，且量測本身可信（n=5、CI 寬約 0.0002 ms）。

**本階段可新增的主張**

- 「模型形式缺陷已修正，且變更經事前登記。」
- 「在 FIT 側資料上，修正後的模型形式殘差為 ⟨具體數字⟩。」——必須註明這是 **FIT 側**，不是驗證。

**本階段結束後仍然禁止**

- **任何 calibrated PASS 主張。** 判定屬 A4 的 sealed held-out，不得在本階段自行宣告。
- 以舊資料的 held-out 分數作為驗證證據（舊資料一律降級為 FIT，決策 P-005）。
- 任何 break-even、accelerator、長上下文或高並發的主張。
- 把 FIT 側殘差改善說成「校準已改善」——前者是擬合品質，後者需要 held-out。

---

## 8. 失敗處理與必須詢問 owner 的條件

**可自主處理**：parser／序列化／等價 API 適配問題。修復前後保存 diff 與回歸測試，失敗的 attempt 不得消失。

**必須停下並詢問 owner**

- 需要修改或刪除 `evidence/` 內任何檔案；
- 需要放寬已登記的驗證門檻，或變更 fit/held-out 的劃分；
- 發現既有量測資料本身有問題（例如欄位矛盾、單位不一致），而修正會改變既有結論；
- 事前登記的模型形式在資料上明顯不成立，需要改採未登記的形式。

最後一項要特別說明：**改用未登記的形式本身是允許的科學行為，但必須先補登記並說明理由**，不能默默改掉再宣稱符合原計畫。

---

## 9. 完工交付

1. **產出清單**：列出所有新增、修改、刪除的檔案。
2. **執行記錄**：`runs/<run_id>/` 含 `manifest.json`、`resolved_config.yaml`、`logs/{command,stdout,stderr}.log`、`metrics.json`、`artifacts/`、`environment/tool_versions.json`。失敗的 run 同樣保留並附 failure classification。
3. **更新 ledger**：`governance/stage_ledger.yaml` 的 `STAGE_A1` 那一列——`status`、`last_verified.date`、`last_verified.actual`（貼上實際指令輸出）。**不動其他列。**
4. **更新五份 status**：`CURRENT_STATUS`（階段狀態）、`AGENT_HANDOFF`（新記錄加在最上面）、`DECISION_LOG`（若有新決策）、`ASSUMPTION_REGISTER`（新增或驗證的假設）、`VALIDATION_MATRIX`（A1 那一列）。
5. **commit + push**：commit 訊息說明改了什麼模型形式、為什麼、FIT 側殘差如何、以及**仍未驗證**這件事。
6. **回報**：新增假設與來源；通過／失敗／未執行的檢查；下一個最高資訊增益的動作。

### 交接記錄格式

```text
STAGE: A1
STATUS: COMPLETE | BLOCKED
PRE_REGISTRATION: <spec 檔路徑與建立時間>
DEFECTS_ADDRESSED: <1/2/3/4 各自的狀態：修正 / 未收斂 / 未處理>
FIT_RESIDUALS: <每項 metric 的 FIT 側殘差>
NOT_CONVERGED: <未收斂項目與原因>
MEASUREMENT_GAPS: <餵給 GPU 軌的量測缺口清單>
EVIDENCE_UNCHANGED: <make verify-evidence 輸出>
BASELINE: <make test 輸出>
FILES_CHANGED: <清單>
CLAIMS_ADDED: <本階段新增的主張>
CLAIMS_STILL_FORBIDDEN: calibrated PASS / break-even / accelerator / 長上下文
NEXT: <下一個動作>
OWNER_DECISION_NEEDED: <若有>
```
