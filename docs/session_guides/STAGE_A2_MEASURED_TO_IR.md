# Stage A2 · Measured raw 轉九類 Canonical IR

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 Stage A2 的執行者。

工作區：/home/a/platform

本 session 的單一目標：把真實 GPU 量測轉成九類 Canonical IR。純 CPU 工作，不需要 GPU。
這是研究鏈的第一個斷點——目前唯一的 adapter 是 mock，真實量測從未進過任何 IR。

開始前完整讀取：
  docs/session_guides/STAGE_A2_MEASURED_TO_IR.md   ← 本階段指引
  PLATFORM_FLOW_SPECIFICATION.md                    ← 根規格，特別是 §4 九類 IR 契約
  governance/stage_ledger.yaml                      ← 狀態真相來源
  docs/PHASE_NAMING_MAP.md                          ← 五套並存命名的對照

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不讀其他階段的指引。
  3. 進入檢查任一項不符即停止並回報。

另外兩條本階段特有：
  4. 不得以 fixture、空值或 synthetic 值替代真實量測（規格 §4.2 IR0）。
  5. 既有的 mock adapter 必須保留作 fixture，不得刪除或覆寫。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 這個 session 的單一目標

建立 measured raw → 九類 Canonical IR 的 adapter，並讓各量測家族通過 IR1 驗證。

**不包含**讓引擎消費這些 IR——那是 Stage A3。

---

## 2. 進入檢查

```bash
cd /home/a/platform

make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：317 Python passed + 14 CTest，0 failed
make doctor                   # 預期：workspace_contract: pass

grep -A2 'id: STAGE_A2' governance/stage_ledger.yaml
# 預期：status: NOT_STARTED

# 必要輸入
ls evidence/phase7/master_remaining/*/remote_raw/ | head
ls explorations/moe_cycle_simulator/phase2/canonical_ir.py
ls explorations/moe_cycle_simulator/phase7/adapters/vllm_mock_adapter.py

# IR 層自身的測試必須先是綠的
.venv/bin/python -m pytest explorations/moe_cycle_simulator/phase2/tests -q -p no:cacheprovider
# 預期：43 passed
```

**A2 不依賴 A1。** 兩者可由不同 session 並行執行。

---

## 3. 授權邊界

**可以修改／新增**

```text
explorations/moe_cycle_simulator/phase7/adapters/   ← 新增 measured adapters
calibration/ 之外的新 IR 產出目錄
experiments/specs/<id>.yaml
runs/<run_id>/
docs/status/ 五份
governance/stage_ledger.yaml   ← 只改 STAGE_A2 那一列
```

**絕對不可修改**

```text
evidence/**
explorations/moe_cycle_simulator/phase7/adapters/vllm_mock_adapter.py  ← 保留作 fixture
explorations/moe_cycle_simulator/phase2/canonical_ir.py                ← 除非發現真實缺陷，且須先回報
src/edgeflow/calibrated_backend.py                                     ← 屬 A1
hardware/**  measurement/**
governance/stage_ledger.yaml 的其他列
```

`canonical_ir.py`（2051 行）已通過 43 項測試，其 femtosecond 整數時間、有理時脈、no-float ABI、typed reference 與 semantic root 機制都是既有資產。**預設它是對的**——若真的需要改，先回報再改。

---

## 4. 進入時的事實基線

### 4.1 目前的斷點

唯一的 trace adapter 是 `explorations/moe_cycle_simulator/phase7/adapters/vllm_mock_adapter.py`（825 行），檔案自述為 mock：不 import vLLM、不查詢 GPU、不下載模型、不宣稱 synthetic observation 是量測。

已 grep 確認：**無任何 code path 從真實 `.npy` routing array 進到 `EventIR` 或 `RoutingIR`。**

`phase2` 的 IR 實作成熟但只吃 `phase2/fixtures/canonical_ir_bundle.json` 這個 synthetic fixture。

### 4.2 九類 IR（規格 §4）

```text
WorkloadIR  ModelIR  RoutingIR  PlacementIR  PlatformIR
EventIR     ClockAlignmentIR    CalibrationIR    ResultIR
```

核心規則（已由 `canonical_ir.py` 強制，不需重新實作）：時間為 femtosecond 整數以十進位字串表示；時脈為有理數；禁浮點；canonical JSON + NFC；每筆帶 `provenance` 與 `semantic_descriptor_hash`。

### 4.3 核心設計決定：統一的 residency-managed object 抽象

**expert 物件與 KV block 在 `PlacementIR` 中是同一類 object**，共用 identity、容量歸屬、搬運語意、eviction 語意、ownership。兩者共爭同一條鏈路與 copy engine。

這個耦合是共同設計的關鍵（規格 §4.1），也是舊 repo 完全沒建模的部分。本階段要把它做進 IR 的資料模型，即使 KV 的行為建模要等 B1。

### 4.4 主要來源與已驗證的數字

**Expert 容量掃描**（15 點）：`evidence/phase7/master_remaining/*/remote_raw/OFF-E-PR3-CAP-*/off_e_pr3_trace/capacity_replay.json`

每點欄位含 `capacity_label`、`capacity_objects`、`capacity_bytes`、`fit_role`、`logical_demand_count`、`hit_count`、`demand_load_count`、`immutable_discard_count`、`h2d_bytes`、`total_h2d_cuda_elapsed_ms`、`transfer_events`、`routing_sha256`、`routing_shape`、`expert_object_bytes`、`logical_policy`、`physical_transfer_semantics`，以及 setup／first H2D／compute 的 monotonic ns 時戳。

已驗證的事實：

```text
logical_demand_count = 10176（每點相同）
routing_shape        = [159, 32, 2]   （tokens × layers × top-k）
expert_object_bytes  = 352,321,536 B  （336 MiB）
catalog_objects      = 256
capacity 025→100 對應 hit rate 28.3% → 100%
每物件 H2D 時間 12.454–12.499 ms（σ≈0.1%），有效頻寬 28.19–28.29 GB/s
logical_policy = DETERMINISTIC_LRU_EMPTY_INITIAL_CACHE
```

**byte 守恆式**（IR1 的關鍵驗收）：

```text
h2d_bytes == demand_load_count × expert_object_bytes
CAP-050 已核對：4852 × 352,321,536 = 1,709,464,092,672  ✓
```

**KV 結構**：`evidence/phase7/master_remaining/*/remote_raw/SWAP-K2-*/derived_*_event_lineage_and_capacity.json`

```text
runtime_block_size_tokens = 16
bytes_per_full_block      = 2,097,152 B
runtime_cache_shape       = [2308, 8, 32, 2, 16, 128]，dtype bfloat16
→ 每 token 128 KiB
```

**Serving 錨點**：`evidence/measurement_backups/20260811T175500Z__phase7_fit_anchor_backup/raw/runs/*SERV-P0-25*/result.json`

1000 筆 request 記錄，每筆含 `client_scheduled_arrival_monotonic_ns`、`server_observed_arrival_monotonic_ns`、`submitted_monotonic_ns`、`first_yield_monotonic_ns`、`completed_monotonic_ns`、`ttft_ns`、`input_tokens`、`output_tokens`、`decode_updates`、輸入輸出 SHA-256。Poisson 開環、rate `1.0472460793856333`、seed `20260812`、concurrency 8。

**其他家族**：controlled matrix、component、transfer 微基準見 `evidence/measurement_backups/`（各家族結構見該目錄的 `manifest.json`）。

### 4.5 必須隨 IR 傳遞的 claim boundary

原始量測的限制**必須寫入 IR 的 provenance 欄位，下游不得洗掉**（規格 §4.3）：

| 限制 | 內容 |
|---|---|
| OFF-E-PR3 單一物件代理 | `physical_transfer_semantics` 明載：每次 miss 都搬**同一個** layer-0 expert-0 物件。位元組與時間為實測，但 per-object 搬移多樣性未被實測 |
| SWAP-K2 事件缺陷 | 事件 `block_size = 0`，位元組帳目由 runtime shape/dtype 推導而非事件本身 |

若本階段也把第三方 routing 語料（`data/canonical/moe_routing_v1/`）納入 IR，還必須寫入下列限制（來源 `docs/status/EXTERNAL_CORPUS_AUDIT_20260818.md`）：

| 限制 | 內容 |
|---|---|
| 無 router scores | 資料集本身不含 gate 分數／logits／機率。任何需要信心值的 predictor 不可能實作 |
| 無時序 | 只有 expert ID。時間必須由 service model 提供 |
| 序列長度上限 | 全資料集單一 query 最長約 **721 tokens**（prefill ≤593 + decode 硬上限 128）。**不得用於任何長上下文推論** |
| 每 cell 樣本數 | 語料已於 2026-08-18 補抓至 21/21 cell 達 k\*=14，但既有的 `w3_*` 衍生結果仍是舊的 n=3 樣本產物，尚未重跑（屬 C1） |
| 架構差異 | Llama-4-Maverick 是 top_k=1、24/48 層為 MoE，與其餘三個 top_k=8 的模型結構不同，不得並列後取平均 |

---

## 5. 工作步驟

### 步驟 1 — 決定家族優先序

建議先做 **expert 容量掃描（15 點）**：它欄位最完整、counters 決定性、且有可驗證的 byte 守恆式，是最容易確立 adapter 正確性的家族。

之後依序：SWAP-K2（KV 結構）→ SERV-P0-25（serving）→ controlled matrix → component / transfer。

### 步驟 2 — 建立 measured adapter

在 `explorations/moe_cycle_simulator/phase7/adapters/` 新增，與 mock adapter **並存**。

每個 adapter 的職責：讀取該家族的 raw → 映射到九類 IR → 附上 provenance（來源路徑、SHA-256、claim boundary）。

### 步驟 3 — 映射九類

| IR | 內容 |
|---|---|
| `WorkloadIR` | 159 tokens 的工作負載定義；serving 家族則為 request 序列 |
| `ModelIR` | Mixtral：8 experts、top-2、32 layers、8 KV heads、head_dim 128 |
| `RoutingIR` | routing `.npy`，shape `[159, 32, 2]`，附 `routing_sha256` |
| `PlacementIR` | residency-managed object：expert 256 × 352,321,536 B；KV block 2,097,152 B / 16 tokens；容量與 ownership |
| `PlatformIR` | GPU 型號、VRAM、鏈路頻寬——**全部是參數，不得寫死**（規格 §3.2） |
| `EventIR` | transfer events、demand / hit / evict、serving 的 arrival／submit／first-yield／complete |
| `ClockAlignmentIR` | monotonic ns → femtosecond 整數 |
| `CalibrationIR` | 該家族與校準的關聯（本階段可留最小集合） |
| `ResultIR` | hit/miss/evict counters、bytes、elapsed |

### 步驟 4 — IR1 驗證

沿用 `explorations/moe_cycle_simulator/phase2/validate_phase2.py` 既有的 schema／semantic root／clock alignment／守恆檢查，加上本階段的家族特定守恆式。

### 步驟 5 — 記錄無法映射的欄位

某些 raw 欄位可能沒有對應的 IR 位置。**明確記錄哪些欄位被丟棄以及為什麼**，不要默默略過。這份清單是 A3 的重要輸入——引擎可能需要其中某些欄位。

---

## 6. 驗收條件

| 項目 | 判準 |
|---|---|
| 容量掃描完整 | 15 個點全部產出 IR bundle 並通過 IR1 |
| byte 守恆 | 每點 `h2d_bytes == demand_load_count × expert_object_bytes` 成立 |
| routing 可回溯 | `routing_sha256` 可對回原始 `.npy` |
| 無 synthetic 替代 | IR 中無 fixture、空值或推估值冒充量測（IR0） |
| claim boundary 已傳遞 | 兩項限制寫入 provenance 且可查詢 |
| mock 仍在 | `vllm_mock_adapter.py` 未被刪改 |
| 證據未被動 | `make verify-evidence` → 4423/4423 |
| 基線未退步 | `make test` → 0 failed，測試數不低於 317 |
| 丟棄欄位已記錄 | 有清單且說明理由 |

---

## 7. Claim boundary

**進入時已成立**：`evidence/` 逐檔一致；基線全綠；第 4.4 節所列的量測事實（附來源路徑）。

**本階段可新增**

- 「真實量測已進入九類 Canonical IR 並通過守恆與 schema 驗證。」
- 「〈家族〉的 IR bundle 可回溯到 〈原始路徑 + SHA-256〉。」

**仍然禁止**

- 「IR 已被引擎消費」——屬 A3。
- 任何時序、效能、calibrated、break-even 或 accelerator 主張。
- 把 IR 通過驗證說成研究鏈已打通——IR 只是第一個斷點。

---

## 8. 失敗處理與必須詢問 owner 的條件

**可自主處理**：parser、序列化、schema 適配問題。保存 diff 與回歸測試。

**必須停下並詢問 owner**

- 需要修改 `canonical_ir.py` 或九類 IR 的 schema；
- 某量測家族的欄位與 IR 契約根本不相容，需要擴充 IR 定義；
- 發現 raw 資料自相矛盾（例如 byte 守恆不成立），因為那會動搖既有結論；
- 需要修改 `evidence/` 內任何檔案。

**byte 守恆不成立是重大訊號**，不是四捨五入問題。停下回報，不要自行加容差。

---

## 9. 完工交付

1. 產出清單（新增／修改／刪除）。
2. `runs/<run_id>/` 完整目錄（含失敗 run 與 failure classification）。
3. 更新 `governance/stage_ledger.yaml` 的 `STAGE_A2` 列，貼上實際指令輸出。
4. 更新五份 status 文件。
5. commit + push。
6. 回報。

### 交接記錄格式

```text
STAGE: A2
STATUS: COMPLETE | BLOCKED
FAMILIES_CONVERTED: <家族清單與各自點數>
IR1_RESULT: <各家族通過情形>
BYTE_CONSERVATION: <逐點結果>
ROUTING_TRACEABLE: <sha256 對照結果>
CLAIM_BOUNDARY_PROPAGATED: <寫入 provenance 的限制清單>
DROPPED_FIELDS: <無法映射的欄位與理由 —— A3 的輸入>
MOCK_ADAPTER_INTACT: <checksum>
EVIDENCE_UNCHANGED: <make verify-evidence 輸出>
BASELINE: <make test 輸出>
FILES_CHANGED: <清單>
CLAIMS_ADDED: <本階段新增>
CLAIMS_STILL_FORBIDDEN: 引擎消費 / 時序 / calibrated / break-even / accelerator
NEXT: <下一個動作>
OWNER_DECISION_NEEDED: <若有>
```
