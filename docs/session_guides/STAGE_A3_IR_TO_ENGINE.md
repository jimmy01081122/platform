# Stage A3 · IR 轉引擎 loader 與位元精確 replay

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 Stage A3 的執行者。

工作區：/home/a/platform

本 session 的單一目標：建立 Canonical IR → C++ cycle-resolved 引擎的 loader，
並讓 15 個 expert 容量點的 residency counters 位元精確重現。純 CPU 工作。

這是研究鏈的第二個斷點——引擎已存在且測試通過，但從未被真實 trace 驅動過。

開始前完整讀取：
  docs/session_guides/STAGE_A3_IR_TO_ENGINE.md   ← 本階段指引
  PLATFORM_FLOW_SPECIFICATION.md                  ← 根規格，特別是 §5 引擎與 service model 分工
  governance/stage_ledger.yaml                    ← 狀態真相來源
  docs/PHASE_NAMING_MAP.md                        ← 五套並存命名的對照

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不讀其他階段的指引。
  3. 進入檢查任一項不符即停止並回報。

本階段特有：SIM0 的驗收是「完全相等」，不是「接近」。資料是凍結 routing trace 上的
決定性 LRU，沒有模糊空間。不符即修到相符，不得調參數或加容差掩蓋。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 這個 session 的單一目標

建立 IR bundle → C++ 引擎的 loader，接上 service model，並通過 SIM0（位元精確）與 SIM1（決定性）。

**不包含**校準判定（A4）或任何時序準確度主張。

---

## 2. 進入檢查

```bash
cd /home/a/platform

make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：317 Python + 14 CTest，0 failed
make test-cpp                 # 預期：phase3 2/2 · phase4 3/3 · phase5 4/4 · phase6 5/5

grep -A3 'id: STAGE_A3' governance/stage_ledger.yaml
# 預期：status: NOT_STARTED

grep -A3 'id: STAGE_A2' governance/stage_ledger.yaml
# 預期：status: COMPLETE   ← A3 依賴 A2

# A2 的 IR 產出必須存在且可讀
# （實際路徑見 A2 在 ledger 中登記的 deliverables）
```

**A3 依賴 A2。** 若 A2 尚未 COMPLETE，停止並回報。

---

## 3. 授權邊界

**可以修改／新增**

```text
explorations/moe_cycle_simulator/phase3/    ← 只在必要時，且須極保守
explorations/moe_cycle_simulator/phase4/    ← service model 接線
explorations/moe_cycle_simulator/phase5/    ← residency 政策對映
新增的 loader 模組
experiments/specs/<id>.yaml
runs/<run_id>/
docs/status/ 五份
governance/stage_ledger.yaml   ← 只改 STAGE_A3 那一列
```

**絕對不可修改**

```text
evidence/**
A2 產出的 IR bundle（本階段是消費者，不是生產者）
src/edgeflow/**    ← 屬 A1
hardware/**  measurement/**
governance/stage_ledger.yaml 的其他列
```

**改動 C++ 引擎要極度保守。** phase3–6 共 10.3 KLOC、14 個 CTest 全綠，且具備 deadlock/Zeno 偵測、checkpoint + replay 驗證、資源守恆檢查。這些是資產。任何改動都必須保持既有 CTest 全綠。

---

## 4. 進入時的事實基線

### 4.1 引擎現況

| 層 | 職責 | 規模 |
|---|---|---|
| phase3 core | 全域時間（128-bit fs）、事件全序、資源 acquire/release、CDC bridge、deadlock/Zeno 偵測、checkpoint + replay 驗證 | `engine.cpp` 1396 行 |
| phase4 single_gpu | **服務時間模型**：ServiceClass（compute / memory / H2D / D2H）各自 lane 池 + 共用 fabric | `single_gpu_model.cpp` 889 行 |
| phase5 residency | routing／residency／prefetch／eviction 政策，byte 計價 catalog | `routing_residency_policy.cpp` 1026 行 |
| phase6 multi_domain | 異質拓撲、DirectedLink、UMA fabric、coherence 狀態 | `multi_domain_scheduler.cpp` 2576 行 |

### 4.2 必須修正的已知缺口

**phase3 的 `Action::kService` 目前不消耗 `service_demand`**——它只記錄後即 `mark_complete`。`PHASE3_RESULT.md` 自述服務時間模型屬 Phase 4。

**必須實際接上 phase4 的服務時間模型**，否則時間軸只有事件順序而沒有服務時間，後續校準無從談起。

### 4.3 現成的掛載點

`explorations/moe_cycle_simulator/phase3/src/c_api.cpp`（263 行）與 `phase3/python/moe_sim_phase3.py`（209 行，ctypes adapter）是既有的 Python↔C++ 介面。**不需要新設計 ABI。**

phase5 已有 `PrefetchMode`、`kLru`／`kFifo` eviction、CLEAN_IMMUTABLE 語意、`useful_prefetches`／`wasted_prefetches` 計數——直接對應量測資料的 LRU + immutable discard 語意。

### 4.4 SIM0 的驗收資料

15 個 expert 容量點，來源 `evidence/phase7/master_remaining/*/remote_raw/OFF-E-PR3-CAP-*/off_e_pr3_trace/capacity_replay.json`。

資料是**凍結 routing trace 上的決定性 LRU**（`logical_policy: DETERMINISTIC_LRU_EMPTY_INITIAL_CACHE`），每點 `logical_demand_count = 10176`。

因此 `hit_count`、`demand_load_count`、`immutable_discard_count` **必須完全相等**，沒有統計誤差的空間。這是本階段最有力的驗收——它能在不依賴任何時序模型的前提下，證明 residency 語意被正確重現。

參考值（部分點）：

```text
cap  objects  role      demands  hits   misses
025  64       FIT       10176    2881   7295
050  128      FIT       10176    5324   4852
0625 160      HELD_OUT  10176    6691   3485
090  230      FIT       10176    9287   889
100  256      CONTROL   10176    10176  0
```

> `fit_role` 欄位在本平台**不作為 held-out 宣稱**（決策 P-005），此處僅為資料標籤。

`cap=100`（零搬移）是很好的退化案例檢查。

---

## 5. 工作步驟

### 步驟 1 — 對映設計

| IR | 引擎對應 |
|---|---|
| `PlacementIR` | phase5 的 `ExpertKey` 與 capacity |
| `RoutingIR` | demand 序列 |
| `EventIR` | phase3 的 `Event` |
| `PlatformIR` | phase4 的 `ServiceClass` lanes 與 `shared_fabric_lanes` |
| `ClockAlignmentIR` | phase3 的 `Clock`（有理數 + phase offset） |

先確認 A2 交接記錄中的 `DROPPED_FIELDS` 清單——若引擎需要其中某些欄位，要回到 A2 的 adapter 補（此時需與 owner 確認是否重開 A2）。

### 步驟 2 — 實作 loader

透過既有 C API 掛載。先讓最簡單的路徑跑通：單一容量點 → 引擎 → 產出 counters。

### 步驟 3 — 接上 phase4 service model

處理 4.2 節的缺口。這一步會改變事件的時間推進，因此**每改一次都要重跑 `make test-cpp` 確認 14 個 CTest 仍全綠**。

### 步驟 4 — SIM0：位元精確

15 點逐一 replay，比對三個 counter。

**任何一點不符就是 adapter 或模型的錯誤。** 診斷順序建議：

1. 先確認 demand 序列的長度與順序與 routing trace 一致（10176）；
2. 再確認初始狀態為空 cache；
3. 再確認 eviction 是 LRU 且 tie-break 規則一致；
4. 最後確認 immutable discard 的計數時機。

**不得**用調參數、加容差、或改比對方式來讓它通過。

### 步驟 5 — SIM1：決定性

同一 bundle 連跑兩次，結果必須位元相同。同時確認引擎的既有偵測未觸發：無 deadlock、無 Zeno、無資源守恆違反。

### 步驟 6 — 記錄時序現況

SIM0 通過後，引擎會產生時間軸。**記錄它與量測時間的差距，但不要在本階段調整以縮小差距**——那是 A1 的模型形式與 A4 的校準判定要處理的。本階段只誠實記錄。

---

## 6. 驗收條件

| 項目 | 判準 |
|---|---|
| SIM0 | 15 點的 `hit_count` / `demand_load_count` / `immutable_discard_count` 與量測**完全相等** |
| SIM1 | 同一 bundle 兩次 replay 結果位元相同 |
| 引擎健康 | 無 deadlock、無 Zeno、無資源守恆違反 |
| service model 已接上 | `Action::kService` 實際消耗 `service_demand` |
| C++ 基線未退步 | `make test-cpp` → 14 CTest 全綠 |
| 整體基線未退步 | `make test` → 0 failed，測試數不低於 317 |
| 證據未被動 | `make verify-evidence` → 4423/4423 |
| 退化案例 | `cap=100`（零搬移）正確處理 |

---

## 7. Claim boundary

**進入時已成立**：`evidence/` 逐檔一致；基線全綠；A2 已將真實量測轉入九類 IR 並通過守恆驗證。

**本階段可新增**

- 「C++ cycle-resolved 引擎可被真實量測 IR 驅動。」
- 「15 個容量點的 residency counters 位元精確重現。」
- 「同一 bundle 的 replay 具決定性。」

**仍然禁止**

- **任何時序準確度主張。** 位元精確指的是 **counters**，不是時間。時間的準確度要等 A4 的 sealed held-out 判定。
- 任何 calibrated、break-even、accelerator 主張。
- 把 SIM0 通過說成「模擬器已驗證」——SIM0 驗證的是 residency 語意，不是效能預測能力。

這個區分很重要：counters 位元精確是**必要條件**而非充分條件。它證明語意對了，不證明時間對了。

---

## 8. 失敗處理與必須詢問 owner 的條件

**可自主處理**：loader、序列化、ctypes 綁定、對映邏輯的錯誤。保存 diff 與回歸。

**必須停下並詢問 owner**

- SIM0 在窮盡診斷後仍無法對上，且懷疑是量測資料本身的問題；
- 需要對 phase3–6 做結構性改動（不只是接線）；
- 需要回到 A2 修改 adapter（跨階段變更）；
- 引擎觸發 deadlock 或資源守恆違反，且原因指向引擎既有邏輯而非新 loader；
- 需要修改 `evidence/` 內任何檔案。

**不要為了讓 SIM0 通過而放寬比對。** 若真的無法對上，那是重要的科學發現，記錄下來比通過重要。

---

## 9. 完工交付

1. 產出清單。
2. `runs/<run_id>/` 完整目錄（含失敗 run）。
3. 更新 `governance/stage_ledger.yaml` 的 `STAGE_A3` 列，貼上實際輸出。
4. 更新五份 status。
5. commit + push。
6. 回報。

### 交接記錄格式

```text
STAGE: A3
STATUS: COMPLETE | BLOCKED
SIM0_RESULT: <15 點逐點比對結果；不符者列出差異>
SIM1_RESULT: <兩次 replay 是否位元相同>
ENGINE_HEALTH: <deadlock / Zeno / 資源守恆 檢查結果>
SERVICE_MODEL_WIRED: <Action::kService 的處理方式與驗證>
TIMING_OBSERVED: <引擎時間軸 vs 量測時間的差距 —— 僅記錄，不調整>
CPP_BASELINE: <make test-cpp 輸出>
BASELINE: <make test 輸出>
EVIDENCE_UNCHANGED: <make verify-evidence 輸出>
FILES_CHANGED: <清單>
CLAIMS_ADDED: <本階段新增>
CLAIMS_STILL_FORBIDDEN: 時序準確度 / calibrated / break-even / accelerator
NEXT: <下一個動作>
OWNER_DECISION_NEEDED: <若有>
```
