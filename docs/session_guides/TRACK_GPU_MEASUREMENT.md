# GPU 量測軌

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 GPU 量測軌的執行者。

工作區：/home/a/platform

本 session 的單一目標：依優先序取得實機量測，補上目前完全沒有證據的缺口。
本軌可與任一階段並行，由獨立 session 執行。

開始前完整讀取：
  docs/session_guides/TRACK_GPU_MEASUREMENT.md   ← 本軌指引
  PLATFORM_FLOW_SPECIFICATION.md                  ← 根規格，特別是 §9 GPU 量測流程
  governance/stage_ledger.yaml                    ← 狀態真相來源與量測優先序
  docs/PHASE_NAMING_MAP.md                        ← 五套並存命名的對照

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不讀其他階段的指引。
  3. 進入檢查任一項不符即停止並回報。

本軌特有的硬規則：
  4. 發現不屬於本 session 的 GPU 或 serving process：不傳 signal、不 attach profiler、
     不改任何 config，回報後等待 owner 決定。禁用 kill / pkill / killall。
  5. canonical domain 不符即停止 GPU dispatch 並回報，不得用其他 GPU 補數據。

第一個動作：執行指引第 2 節的進入檢查。無 GPU endpoint 時只做本機準備工作。
```

---

## 1. 這個 session 的單一目標

依優先序取得實機量測。**優先序由證據缺口決定，不照舊矩陣全跑。**

---

## 2. 進入檢查

### 2.1 本機（無論有無 endpoint 都要做）

```bash
cd /home/a/platform

make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：0 failed
make doctor                   # 預期：workspace_contract: pass

grep -A5 'id: TRACK_GPU$' governance/stage_ledger.yaml
# 確認 measurement_priority 與目前要做的項目一致

# 量測 contract 是否已凍結（TRACK_GPU_PREP 的產出）
test -f experiments/specs/gpu_measurement_contract_v1.yaml && echo CONTRACT_OK
```

**`CONTRACT_OK` 沒印出來**：只能執行優先序第 4、5 項（已由 A1 完整規格化）。第 1、2、3 項必須先跑 `TRACK_GPU_PREP`。

### 2.2 無 GPU endpoint 時

回報 `BLOCKED_ON_GPU_ENDPOINT`，**不執行任何 SSH、server、GPU、vLLM 或 serving 指令**。

本機準備工作屬 `TRACK_GPU_PREP`（獨立 session，純 CPU）。**不要在本 session 內做**——那會讓本 session 累積大量與執行無關的設計上下文，違背每階段獨立 session 的目的。回報後結束。

### 2.3 取得 endpoint 後——read-only preflight（規格 §9.2）

第一輪只做低干擾的唯讀探查：

```text
host / GPU 型號 / UUID / VRAM / driver / CUDA / runtime identity
模型路徑、file ledger、revision、checksum
目前的 GPU compute process
vLLM / server / runner process
既有的 experiment 與 run namespace
輸出 namespace 是否可寫
```

**任一項不符即停止 dispatch 並回報。不得自動下載、安裝、fallback 或清理。**

---

## 3. 授權邊界

**可以修改／新增**

```text
measurement/                    ← runner、hook、collector 的最小修復
新量測的輸出 namespace（新目錄）
experiments/specs/<id>.yaml
runs/<run_id>/
docs/status/ 五份
governance/stage_ledger.yaml    ← 只改 TRACK_GPU 那一列
configs/platform/               ← 新 GPU 型號需要新 platform profile 時
```

**絕對不可修改**

```text
evidence/**                     驗證與備份完成前，新量測不進 evidence/
其他階段在 ledger 中的列
既有的 platform profile 與 calibration package（新平台建新的，不改舊的）
```

---

## 4. 進入時的事實基線

### 4.1 Canonical domain（目前主力，非永久假設）

```text
GPU      NVIDIA RTX PRO 6000 Workstation Edition 96 GB
Model    mistralai/Mixtral-8x7B-Instruct-v0.1
Revision eba92302a2861cdc0098cc54bc9f17cb2c47eb61
Runtime  vLLM 0.23.0
Weights/KV  BF16 / BF16
TP/PP/EP    1 / 1 / 1
```

**GPU 型號可更換**（規格 §3.2）。更換時：建立獨立的 platform profile 與獨立的 calibration package，**不得跨平台套用參數**；舊平台的結論須標明適用邊界。

### 4.2 量測優先序

權威清單在 `governance/stage_ledger.yaml` 的 `TRACK_GPU.measurement_priority`。摘要：

| 序 | 目標 | 為什麼 | 就緒度 |
|---|---|---|---|
| 1 | **掛載點 A2——MoE dispatch 資料搬運** | 系統層搬運與控制結構完全沒有量測。沒有它，A2 的 break-even 不可計算 | 需 PREP 的 in-serving 儀測 |
| 2 | **掛載點 A6——長上下文 offloaded KV attention** | 完全無證據，資訊增益最高，且是 Hybe 類 GPU+專用單元系統的核心切入點 | **最大缺口**，runner 全部要新寫 |
| 3 | **sealed held-out 驗證集** | Stage A4 的前置。split 必須先 hash 封存 | 封存屬 PREP 步驟 5 |
| 4 | component service model 缺的 operand shape 與 concurrency 掃描點 | 依 A1 的殘差分析決定，見 `calibration/fits/v2/measurement_gaps.json` | **今日即可執行** |
| 5 | SERV-P0-25 tail-CI 補測 | 須從 request 0 重跑完整 10K，新 attempt ID，**不得接續任何既有部分進度** | **今日即可執行** |

**優先序（資訊增益）與就緒度幾乎相反。** 第 4、5 項已由 A1 完整規格化，今天就能跑；第 1、2 項要寫最多程式。因此：

- 正常情況下，`TRACK_GPU_PREP` 先完成，本軌拿到凍結的 contract 直接執行；
- 若 endpoint 早於 PREP 完成而出現，**先跑第 4、5 項**，不要為了「按順序」而閒置，也不要在窗口內臨時設計第 1、2 項的探針。

**關於第 1 項的一個修正**：`evidence/` 內有 56 筆 `gather_scatter` 記錄，但那是 `benchmark.py:463` 的**同裝置合成 proxy**（`x.index_select(0,idx).index_select(0,inv)` 作用在 `torch.randn` 上），只給 execute 項，不含 `T_prepare`/`T_queue`/`T_sync`/`T_move`。所以要補的是 in-serving 儀測，不是重寫 kernel benchmark。

**為什麼長上下文是關鍵**：Mixtral 每 token 的 KV 是 128 KiB（實測：block 2,097,152 B / 16 tokens）。1M context 需約 128 GiB，**超過 96 GB VRAM**，因此長上下文必然強制 KV offload——這正是候選處理器可能發揮作用的區間，而目前完全沒有量測。

**為什麼現有結論不能外推**：既有的 expert residency 量測全部來自單請求、eager、159 tokens、`max_num_seqs=1`。該區間的控制決策率僅 111–3073 decisions/s，看起來控制路徑很閒——但那是窄域事實，不是結論。

### 4.3 已有的量測（避免重跑）

`evidence/` 已含 581 MB / 4423 檔。重跑前先確認是否已有合格證據：

```text
evidence/phase7/                47 campaigns：OFF-E-PR0–PR4（含 15 點容量掃描）、
                                OFF-W0–W3、SWAP-K0–K5、expert catalog、session guards
evidence/measurement_backups/   18 個備份：SERV-P0-25、controlled matrix、K0–K11、
                                W0–W3、transfer 與 component 微基準、資格認證 cells
evidence/gpu_measurements/      q0 fitted parameters 與 q1 validation report
```

---

## 5. Non-interference 規則（規格 §9.3）

**這一節是硬規則，不是建議。**

若發現不屬於本 session、仍健康運行的 serving 或 GPU process：

1. 不傳送任何 signal；
2. 不 attach profiler；
3. 不改 config、environment、priority、affinity 或 filesystem；
4. 不啟動會競爭 CPU／GPU／PCIe／儲存的 hash、copy 或壓縮作業；
5. 回報 process identity 與可能的衝突，等待 owner 決定。

**禁止使用 `kill`、`pkill`、`killall`、重啟 driver 或清理不明 process。** 只有本 session 自己建立、且已確認失敗的 process，才可依既有停止流程終止。

敏感量測獨占 GPU；checksum 掃描、備份與壓縮移到量測窗口之外。不得為了維持 GPU 使用率而跑 filler workload。

---

## 6. 工作步驟

### 步驟 1 — 確認量測 contract 已凍結

在 dispatch 之前必須有：目標、自變量、樣本數、重複次數、停止條件、失敗條件、預期產出欄位、exact argv、時間估計。

**這一步不屬於本軌。** 它是 `TRACK_GPU_PREP`（純 CPU，無前置）的產出，位於 `experiments/specs/gpu_measurement_contract_v1.yaml`。

本軌開始時檢查該檔存在且涵蓋要跑的項目。**不存在就停下**，回報需要先跑 `TRACK_GPU_PREP`——不要在 GPU 窗口內臨時設計量測，那正是窗口被浪費的主要方式。

例外見 §4.2：優先序第 4、5 項已由 A1 完整規格化，可在 contract 尚未完成時先執行。

### 步驟 2 — Preflight（見 §2.3）

### 步驟 3 — Session-local guard canary

即使歷史 guard 曾通過，新 host 仍以**新 attempt ID** 重新執行最小 guard，確認 host、runtime 與隱藏機制狀態。這是 session／platform identity guard，不是重跑既有量測。

### 步驟 4 — 正式量測

只在確認 GPU 無其他 owner 後建立新 namespace 並啟動。

### 步驟 5 — Raw 保存（規格 §9.4）

每個 attempt 保存：exact argv、環境與 runtime identity、模型 identity 與 checksum、輸入 fixture、輸出 token IDs 與 finish reason、原始時序、telemetry、stdout/stderr、失敗分類。

**失敗的 run 同樣保存。** Raw 一律唯讀；轉換產物另存並帶 provenance；轉換前後重新雜湊輸入以確保未被修改。

### 步驟 6 — 備份與驗證後才納入 evidence/

新量測先放獨立 namespace，完成 checksum 驗證與本機備份後，才納入 `evidence/` 並更新 `governance/lineage/EVIDENCE_SHA256SUMS`。

納入後執行 `make seal-evidence` 恢復唯讀。

---

## 7. 驗收條件

| 項目 | 判準 |
|---|---|
| domain 相符 | canonical domain 一致；或已建立獨立 platform profile 與獨立 calibration package |
| 未干擾他人 | 無任何 signal、profiler attach 或 config 變更施加於非本 session 的 process |
| raw 完整 | 每個 attempt 含 §6 步驟 5 所列全部欄位；失敗 attempt 同樣保存 |
| checksum 驗證 | 新量測納入 `evidence/` 前後 checksum 一致 |
| lineage 已更新 | `EVIDENCE_SHA256SUMS` 含新檔案，`make verify-evidence` 通過 |
| 唯讀已恢復 | `make seal-evidence` 已執行 |
| 基線未退步 | `make test` → 0 failed |

---

## 8. Claim boundary

**進入時已成立**：`evidence/` 逐檔一致；§4.3 所列的既有量測事實（附來源路徑）。

**本軌可新增**：所量測項目的**原始事實**，附 exact argv、環境 identity 與來源路徑。

**仍然禁止**

- 跨平台套用 calibration 參數。
- 以量測數量代替量測品質。
- 任何 calibrated 主張——判定屬 A4。
- 任何 break-even 或 accelerator 主張——屬 C1。
- 把 capability canary 的通過說成機制效能通過。

最後一項要特別留意：canary 證明的是「這個機制存在且可被觸發」，不是「這個機制有效能收益」。舊 repo 在這一點上有明確的分界，本平台沿用。

---

## 9. 失敗處理與必須詢問 owner 的條件

**可自主做的最小修復**：runner／collector／parser／hook／序列化／等價 API 適配問題。修復前後保存 diff 與回歸，失敗的 attempt 不得消失。

**必須停下並詢問 owner**

- 新 server 不是 canonical GPU／domain；
- model／revision／precision／runtime identity 不符；
- 只能透過改 workload、開 offload、縮短長度或改門檻才能執行；
- GPU Xid／ECC／reset、資料毀損或持續性 runtime failure；
- 需要刪除或覆寫既有證據；
- 需要額外付費、安裝或重新下載；
- 發現他人的 GPU／serving process 與本次量測衝突。

---

## 10. 完工交付

1. 產出清單。
2. `runs/<run_id>/` 完整目錄（含失敗 run 與 failure classification）。
3. 新量測納入 `evidence/`，更新 `EVIDENCE_SHA256SUMS`，執行 `make seal-evidence`。
4. 更新 `governance/stage_ledger.yaml` 的 `TRACK_GPU` 列。
5. 更新五份 status。
6. commit + push。
7. 回報。

### 交接記錄格式

```text
TRACK: GPU
STATUS: COMPLETE | BLOCKED_ON_GPU_ENDPOINT | BLOCKED_OTHER
GPU_ENDPOINT_IDENTITY: <host / GPU 型號 / UUID / VRAM / driver / CUDA / runtime>
DOMAIN_MATCH: <與 canonical domain 是否相符；不符則列出新 platform profile>
PREFLIGHT: <各項結果>
FOREIGN_PROCESS_CHECK: <發現的 process 與處置；無則 NONE>
GUARD_CANARY: <attempt ID 與結果>
TARGETS_MEASURED: <本次量測的項目與優先序編號>
ATTEMPTS: <每個 attempt 的 ID、狀態、raw 路徑、sha256>
FAILED_ATTEMPTS: <保存位置與 failure classification>
EVIDENCE_UPDATED: <新增檔案數；EVIDENCE_SHA256SUMS 新 hash>
EVIDENCE_VERIFY: <make verify-evidence 輸出>
BASELINE: <make test 輸出>
CLAIMS_ADDED: <本次可支持的原始事實>
CLAIMS_STILL_FORBIDDEN: calibrated / break-even / accelerator / 跨平台套參數
REMAINING_PRIORITY: <優先序中尚未完成的項目>
NEXT: <下一個動作>
OWNER_DECISION_NEEDED: <若有>
```
