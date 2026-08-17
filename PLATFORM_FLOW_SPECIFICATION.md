# MoE Inference Processor 共同設計基底平台：流程計畫說明書

```text
DOCUMENT_STATUS  = FOUNDATIONAL_SPECIFICATION
VERSION          = v1.0
DATE             = 2026-08-17
REPO             = git@github.com:jimmy01081122/platform.git
ROOT             = /home/a/platform
SOURCE_REPO      = git@github.com:jimmy01081122/dis.git (frozen, evidence-of-record)
SOURCE_COMMIT    = e804b1633a376f63d57aeba60e7fd15068181ea4
SOURCE_BRANCH    = codex/c1-quality-contract-v2-20260718
STATUS           = 規格已凍結；實作尚未開始；無任何 calibrated 或 accelerator 主張
```

---

## 0. 本文件的用途與權威順序

本文件是 `/home/a/platform` 的**根規格**。任何實作、量測、分析與結論都必須可回溯到本文件的某一節。

**權威順序**（衝突時由上而下）：

1. owner 在當前 session 的明確指令；
2. 本文件與 `AGENTS.md` 的工程契約（同級；`AGENTS.md` §3 的八條禁令不得被任何下游文件放寬）；
3. `governance/stage_ledger.yaml` 的階段狀態與 claim boundary；
4. `project/charter.yaml`、`project/evidence_levels.yaml`、`project/capability_registry.yaml`；
5. 各階段的 `docs/session_guides/STAGE_*.md`；
6. 各階段的 `experiments/specs/<id>.yaml` 事前登記內容；
7. 承襲自舊 repo 的方法論文件（`docs/methodology/`）；
8. 舊 repo 的 status／ledger 文件——**僅作待驗證證據，不得直接當結論**。

若 session 指引與本文件衝突，以本文件為準，並回報該指引需要修正。

若出現實質語意衝突，停止受影響的分支並回報；不得自行選擇較容易的一方。檔名、時間戳記、Markdown 排版與非必要 metadata 不得成為 blocker。

---

## 1. 專案目的

本平台的產出**不是模擬器本身**，而是回答一個共同設計問題：

> 要加速 MoE inference，系統需要什麼架構與規格的客製化 processor？哪一項功能值得做成硬體、規格下限是多少、什麼條件下不值得做？

模擬器是回答這個問題的量具。**驗收標準不是「模擬器跑得動」，而是「break-even 面與規格需求可追溯到實機量測，敢拿去指導 RTL」。**

### 1.1 研究鏈

```text
真實大型 MoE 執行與 trace
  -> canonical IR（九類）
  -> cycle-resolved resource/dependency simulator
  -> measurement-calibrated surrogate
  -> routing / dispatch / transfer / residency / KV 的 DSE
  -> sensitivity、ablation、break-even analysis
  -> support processor / accelerator requirements
  -> RTL 介面與 handoff 規格
  -> FPGA／ASIC pre-tape-out 驗證（不含 tape-out）
```

### 1.2 本平台明確不做的事

- 不實作新的 support processor RTL（只出 handoff 規格包）。
- 不做邏輯合成 sign-off、CTS、繞線後時序或功耗簽核。
- 不重建 proprietary GPU compute pipeline（GPU kernel 以量測校正的 service model 表示）。
- 不以模擬結果宣稱矽後效能。

---

## 2. 證據基線

### 2.1 已有的真實量測（581 MB，全部搬入 `evidence/`，唯讀）

Canonical domain（**此為目前主力，非永久假設**）：

```text
GPU:      NVIDIA RTX PRO 6000 Workstation Edition 96 GB
Model:    mistralai/Mixtral-8x7B-Instruct-v0.1
Revision: eba92302a2861cdc0098cc54bc9f17cb2c47eb61
Runtime:  vLLM 0.23.0
Weights/KV: BF16 / BF16
TP/PP/EP: 1/1/1
```

| 群組 | 內容 | 大小 |
|---|---|---|
| `artifacts/phase7/` | 47 campaigns：OFF-E-PR0–PR4（含 15 點 expert 容量掃描）、OFF-W0–W3、SWAP-K0–K5、expert catalog、session guards | 124 MB |
| `artifacts/gpu_measurements/` | q0 fitted parameters + q1 validation report（2026-07-18 兩輪） | 0.7 MB |
| `runs/*__phase7_fit_anchor_backup` | SERV-P0-25 serving：1000 requests、Poisson 開環、concurrency 8 | 187 MB |
| `runs/*__phase7_remote_controlled_matrix_backup` | CTRL-P0-2048/8192、CTRL-DEC0/DEC1、CTRL-PX0-\*（clean/routing/telemetry 三變體） | 105 MB |
| `runs/*__phase7_remote_k_profile_backup` | K0–K11 profiles | 75 MB |
| `runs/*__phase7_remote_natural_matrix_backup` | W0–W3 natural matrix + profiler repairs | 62 MB |
| `runs/*__phase7_remote_sampling_pairs_backup` | sampling pairs | 4.3 MB |
| `runs/*__phase7_remote_transfer_backup` | transfer 微基準 v1–v4（L/E/O/EQ 變體） | 4.0 MB |
| `runs/*__phase7_remote_component_backup` | component 微基準（A/L/M 變體） | 2.6 MB |
| 其餘 10 個 backup | F0-GPU、C0A、C0BC、F1、T-CANARY、R0（含 failure）、step trace ×2、gputw existing raw | 約 8 MB |

**共 18 個量測備份目錄 + artifacts，合計 581 MB。**

### 2.2 已知的關鍵量測事實

**Expert 容量掃描（OFF-E-PR3，15 點）** — 決定性 LRU 於凍結 routing trace 上重放：

- 每點 `logical_demand_count = 10176`；`routing_shape = [159, 32, 2]`（tokens × layers × top-k）。
- expert 物件 `352,321,536 B`（336 MiB），catalog 共 `256` 物件。
- **每物件 H2D 時間跨 14 個非零點常數為 12.454–12.499 ms**（σ≈0.1%），有效頻寬 28.19–28.29 GB/s。
- 容量 025→100 對應 hit rate 28.3%→100%，搬移量 2.57 TB→0。
- 資料內已標 `fit_role`：8 FIT / 6 HELD_OUT / 1 CONTROL（在本平台的新協定下**全部降級為 FIT 側**，見 §7）。
- claim boundary：每次 miss 都搬**同一個** layer-0 expert-0 物件，per-object 搬移多樣性未被實測。

**KV 結構（SWAP-K2）**：

- `runtime_block_size_tokens = 16`，`bytes_per_full_block = 2,097,152`。
- `runtime_cache_shape = [2308, 8, 32, 2, 16, 128]`，dtype bfloat16。
- 推得 **每 token 128 KiB**；1M context → **128 GiB**，超過 96 GB VRAM，長上下文必然強制 KV offload。
- 限制：事件 `block_size = 0`，位元組帳目由 runtime shape/dtype 推導而非事件本身。

**Serving 錨點（SERV-P0-25）**：

- 1000 requests、Poisson 開環、rate `1.0472460793856333` rps、seed `20260812`、concurrency 8、128-in／32-out。
- completion latency p50 868.2 ms、p95 1431.3 ms、p99 1585.9 ms、max 1743.9 ms；首筆 TTFT 96.344 ms。
- 每筆含 `client_scheduled_arrival` / `server_observed_arrival` / `submitted` / `first_yield` / `completed` 的 monotonic ns，以及 input/output SHA-256。

**已失敗的校準（q1，2026-07-18）** — 保留為證據，不得刪改：

| metric | MAPE | gate |
|---|---|---|
| component_latency | 304.418% | FAIL |
| moe_replay_tpot | 293.936% | FAIL |
| pcie_transfer_latency | 66.879% | FAIL |
| moe_replay_throughput | 60.658% | FAIL |

90 點中 17 通過。門檻為 MAPE 15%、single-point APE 20%。

### 2.3 校準失敗的根因診斷（模型形式錯誤，非量測品質）

逐點比對後確認三項結構性缺陷：

1. **contention 施加位置錯誤**。`copy_engine.stream_latency_factors = {1:1.0, 2:1.7384584, 4:2.7629926}` 被當成 per-transfer latency 乘數；但實測 1/2/4 stream 的**單筆**延遲皆為 ~3.113 ms。因此 30 個 PCIe 點中恰好只有 10 個 1-stream 點通過，2-stream 一律高估 1.74 倍、4-stream 一律高估 2.76 倍——誤差比例精確等於那組 factor。同一參數檔的 `contention.per_extra_concurrency = 1.0046`（幾乎無競爭）與之自相矛盾，**光靠 calibration split 即可證明模型形式有誤**。
2. **component service model 對 shape 無感**。`gpu_service.operation_ms` 僅四個常數（dequant 0.1476092、gather_scatter 0.0292579、grouped_gemm 0.2431867、selected_expert 0.2201017）；48 個量測點跨 0.016–1.136 ms（70 倍），預測值只取 8 個相異值。
3. **MoE replay 無 batching 項**。預測值在 concurrency 1 與 4 間幾乎不變（0.2727 / 0.2726 ms/token；3667 / 3654 tok/s），實測則有約 4 倍差異。

量測本身極可信：n=5、95% CI 寬約 0.0002 ms、跨 split 重現到小數第四位。**因此校準可在不消耗新 GPU 時間的前提下大幅修復。**

> **記錄在案**：上述診斷過程曾檢視 validation split 的殘差。因此舊資料的 held-out 獨立性已受汙染，全部降級為 FIT 側（見 §7）。

---

## 3. 平台架構與 fidelity 規則

```text
真實 GPU 量測（GPU 型號為參數，非假設）
   └─> Canonical IR（九類）
        └─> cycle-resolved 引擎
             ├─ GPU service model         [MEASURED_SURROGATE]
             ├─ Memory / transfer         [MEASURED_SURROGATE]
             ├─ Residency-managed object  [CYCLE_RESOLVED]
             ├─ KV + continuous batching  [CYCLE_RESOLVED]
             └─ 候選 processor            [ANALYTICAL] / [PROJECTED]
                  └─ 掛載點 A1–A6
        └─> co-design DSE：function × processor 規格 × workload regime
             └─> break-even 面 → HW0 需求 → LM18 RTL handoff
```

### 3.1 Fidelity label（每個 component 必標）

```text
CYCLE_ACCURATE      已知並明確建模到週期
CYCLE_RESOLVED      有時脈域、佇列、仲裁、backpressure，但非週期精確
EVENT_DRIVEN        事件順序正確，服務時間來自他處
MEASURED_SURROGATE  以實機量測擬合的服務模型
ANALYTICAL          解析式，無實機支撐
STATISTICAL         由分布抽樣
PROJECTED           外推至未量測區間
```

**硬性隔離**：GPU 與搬運路徑標 `MEASURED_SURROGATE`；候選處理器路徑標 `ANALYTICAL`；長上下文等未量測區間標 `PROJECTED`。**結論不得跨 fidelity 層誇大**。整體工具的正式名稱為 *calibrated cycle-resolved heterogeneous MoE system simulator*，不得簡稱 cycle-accurate。

### 3.2 平台無關性

GPU 型號、VRAM 容量、鏈路頻寬、copy engine 數量一律為 `PlatformIR` 參數，**不得寫死在程式碼**。每個平台建立獨立 calibration package；**不得將一個平台的 calibration parameter 套用至另一平台**。更換 GPU 型號時建立新 platform profile，舊平台的結論須標明適用邊界。

---

## 4. 九類 Canonical IR 契約

```text
WorkloadIR  ModelIR  RoutingIR  PlacementIR  PlatformIR
EventIR     ClockAlignmentIR    CalibrationIR    ResultIR
```

沿用舊 repo `phase2/canonical_ir.py`（2051 行）已驗證的核心規則：

- **時間為 femtosecond 整數**，以十進位字串表示（`^(0|[1-9][0-9]*)$`），**禁止浮點事件時間**與重複累加取整；模擬器對有理參考時脈的漂移必須恰為 0 fs。
- 時脈以有理數（numerator/denominator + phase offset）表示，`edge_time(n) = phase_offset_fs + floor(n*10^15*den/num)`。
- canonical JSON、NFC 正規化、拒絕浮點、拒絕重複鍵、exact-decimal 欄位以字串表示。
- 每筆記錄帶 `provenance` 與 `semantic_descriptor_hash`；typed reference 以 effective descriptor 計算 `target_semantic_root`。
- 分區以 Arrow + Zstd 儲存，含大小與解壓比上限保護。

### 4.1 統一的 residency-managed object 抽象（本平台的核心設計決定）

**expert 物件（336 MiB）與 KV block（2 MiB）在 `PlacementIR` 中是同一類 object**，共用：

```text
object identity（type, layer, index）
byte size
容量歸屬（哪個 memory domain、占多少 capacity）
搬運語意（方向、granularity、是否可 writeback）
eviction 語意（LRU/FIFO、immutable discard 或 writeback）
ownership 與 pin 狀態
```

兩者**共爭同一條鏈路與 copy engine**。這個耦合是共同設計的關鍵，也是舊 repo 完全沒有建模的部分。

### 4.2 IR 驗收（IR0 / IR1）

- **IR0**：measured raw → 九類 IR，**不得以 fixture、空值或 synthetic 值替代**。
- **IR1**：schema 與 semantic root、identity、clock alignment、routing／event／byte／capacity 守恆。
  - 例：expert 容量點須滿足 `h2d_bytes == demand_load_count × expert_object_bytes`
    （已核對 CAP-050：`4852 × 352,321,536 = 1,709,464,092,672` ✓）。
  - `routing_sha256` 須可回溯到原始 `.npy`。

### 4.3 Claim boundary 必須隨 IR 傳遞

原始量測的限制（OFF-E-PR3 的單一物件搬移代理、SWAP-K2 的 `block_size=0`）必須寫入 IR 的 provenance 欄位，**下游不得洗掉**。

---

## 5. 引擎與 service model 分工

沿用舊 repo 已驗證的 C++ 離散事件引擎（`phase3`–`phase6`，10.3 KLOC）：

| 層 | 職責 | 檔案規模 |
|---|---|---|
| core | 全域時間（128-bit fs）、事件全序、資源 acquire/release、CDC bridge、deadlock/Zeno 偵測、checkpoint + replay 驗證 | `engine.cpp` 1396 行 |
| single_gpu | **服務時間模型**：ServiceClass（compute/memory/H2D/D2H）各自 lane 池 + 共用 fabric | `single_gpu_model.cpp` 889 行 |
| residency | routing／residency／prefetch／eviction 政策，byte 計價 catalog | `routing_residency_policy.cpp` 1026 行 |
| multi_domain | 異質拓撲、DirectedLink、UMA fabric、coherence 狀態 | `multi_domain_scheduler.cpp` 2576 行 |

### 5.1 必須修正的已知缺口

core 的 `Action::kService` 目前**不消耗** `service_demand`（僅記錄後即 `mark_complete`）。必須實際接上 single_gpu 的服務時間模型，否則時間軸只有事件順序而無服務時間。

### 5.2 SIM0 / SIM1 驗收

- **SIM0（位元精確）**：15 個 expert 容量點的 `hit_count` / `demand_load_count` / `immutable_discard_count` 必須與量測**完全相等**。資料是凍結 routing trace 上的決定性 LRU，沒有模糊空間。不符即為 adapter 或模型錯誤，**須修到相符，不得調參掩蓋**。
- **SIM1**：同一 bundle 重跑兩次結果位元相同；無 dependency cycle、無資源守恆違反、無 deadlock、無 Zeno。

---

## 6. 候選處理器模型與掛載點

### 6.1 參數化資源模型

候選 processor 以可掃描參數表示：

```text
pipeline latency        issue width         local SRAM capacity
memory bandwidth        queue depth         operations per cycle
clock domain            area proxy          power proxy
```

### 6.2 六個掛載點

| ID | 候選功能 | 優先序 | 目前由誰承擔 | 現有量測 |
|---|---|---|---|---|
| **A1** | routing / gating 決策計算、top-k 選擇 | 主 | GPU kernel | routing `.npy`、OFF-E-PR\*、CTRL-PX0-\*-routing |
| **A2** | MoE dispatch 資料搬運（token permutation、gather/scatter） | 主 | GPU kernel | **無獨立量測** |
| **A3** | transfer 排程 / DMA descriptor / prefetch 發射 | 主 | CPU + copy engine | `transfer_events`、transfer 微基準 v1–v4 |
| **A4** | expert 解壓縮 / 壓縮搬運 | 主 | baseline 無 | `expert_decompressor.sv` 307–811 MHz |
| **A5** | KV block 管理 / offload | 次 | vLLM CPU pool | SWAP-K1/K2/K5 |
| **A6** | offloaded KV 上的 attention | 次 | GPU | **無量測** |

每個掛載點必須定義三件事：

1. **可卸載的工作單位**（work unit）與其在 baseline 的成本；
2. 在候選處理器上的成本模型；
3. 把資料送過去與取回的搬運成本。

**A2 與 A6 是主線與次線中最沒證據者，列為 GPU 量測第一優先，不得以推估取代。**

### 6.3 Backend ABI

穩定的 backend ABI 為六個動詞：

```text
reset   can_accept   submit   advance   poll_completions   snapshot_counters
```

本平台實作兩個 backend：`FUNCTIONAL_POLICY` 與 `CYCLE_RESOLVED_MODEL`，另附一個 reference mock 元件，用來驗證 transaction adapter、clock stepping、backpressure、completion 與 counter 路徑。其餘三個（`RTL_TRACE_REPLAY`、`VERILATOR_COSIM`、`RTL_CALIBRATED_SURROGATE`）保留介面、留給下游。

**未註冊的 backend 必須直接拒絕執行**，不得靜默替換為較低 fidelity 的實作。此為刻意的防偽設計。

---

## 7. Sealed held-out 協定（本平台的乾淨度契約）

### 7.1 舊資料一律降級

`evidence/` 中所有既有量測（q0/q1、8/11 各家族、8/13–14 campaigns）**只能作 FIT 與模型開發用**。理由：模型形式缺陷的診斷過程曾檢視 validation split 殘差（§2.3），獨立性已受汙染。資料集內部既有的 `fit_role`（含 `HELD_OUT`、`CONTROL`）在本平台**不得作為 held-out 宣稱**。

### 7.2 新量測的封存流程

```text
1. 事前登記模型形式與預期方向 -> experiments/specs/<id>.yaml
2. 設計新量測的 fit / validation / held-out split
3. split 定義與資料 hash 封存（sealed manifest，記錄 SHA-256）
4. 只用 fit 擬合；validation 可用於模型選擇
5. 模型與參數凍結（凍結物 hash 記錄）
6. held-out 開封，評分一次
7. 結果寫入報告，無論通過與否
```

**開封後若未通過，記錄未通過並重新設計實驗；不得回頭調整模型再開第二次。** 每個 sealed set 的開封次數必須為 1，並可稽核。

### 7.3 驗證門檻與三值判定

```text
MAPE <= 15%            single-point APE <= 20%
PASS                   信賴區間上界 <= 門檻
FAIL                   信賴區間下界 > 門檻
INSUFFICIENT_EVIDENCE  區間跨越門檻
```

資料不足時判為 `INSUFFICIENT_EVIDENCE`，**不得寫成 PASS 或 FAIL**。

### 7.4 誤差分解

模擬誤差須分解為：trace error、timing-model error、resource-model error、measurement noise、instrumentation overhead、unmodeled runtime behavior。

---

## 8. 校準方法

### 8.1 待修正的模型形式（事前登記後才實作）

| 項目 | 現況 | 修正方向 |
|---|---|---|
| copy-engine contention | per-transfer latency 乘數 | 改為聚合頻寬／佔用模型：N 條 stream 共享 copy engine 頻寬，單筆延遲不變、總完成時間變長 |
| component service | 4 個常數 | 以 operand shape 參數化回歸（tokens × hidden × experts × dtype），每個 operation class 一組係數 |
| MoE replay | 無 concurrency 項 | 加入 batching / concurrency 項 |
| 小尺寸傳輸 | 單一 intercept 0.0153 ms | 改 piecewise 或 `max(fixed_overhead, linear)`；實測下限約 0.037 ms。**KV block 2 MiB 正落在此區間** |

### 8.2 校準流程

```text
component microbenchmark -> parameter extraction -> subsystem calibration
-> end-to-end calibration -> sealed held-out validation -> residual analysis
```

至少校準：transfer startup latency、bandwidth vs size、concurrent transfer contention、copy-engine arbitration、kernel latency、launch overhead、allocation overhead、synchronization overhead、decompression latency、runtime scheduling overhead。

### 8.3 校準 harness 的既有保護（沿用，不重寫）

`calibrated_backend.py`（516 行）已具備：四個 split 強制、artifact 路徑重用拒絕、`record_id` 跨 split 碰撞拒絕、SHA-256 驗證、環境 manifest 強制、非物理擬合拒絕（slope ≤ 0 或 intercept < 0）、無 fallback 常數。**只替換模型形式函式。**

---

## 9. GPU 量測流程

### 9.1 量測優先序（由缺口決定，不照舊矩陣全跑）

1. **A2 MoE dispatch 資料搬運** — 主線掛載點中唯一無獨立量測者。
2. **A6 長上下文 / KV offload attention** — 完全無證據，資訊增益最高。
3. **sealed held-out 驗證集** — §7 所需。
4. 依 A1 殘差補 component service model 缺的 operand shape 與 concurrency 掃描點。
5. `SERV-P0-25` tail-CI 補測：從 request 0 重跑完整 10K、新 attempt ID，**不得接續任何既有部分進度**。

### 9.2 連線後的順序

```text
read-only preflight（host/GPU/UUID/VRAM/driver/CUDA/runtime identity、模型與 checksum、
                     現有 GPU/serving process、輸出 namespace 可寫）
-> session-local guard canary（新 attempt ID）
-> 正式量測
```

任一 preflight 項目不符即停止 dispatch 並回報；不得自動下載、安裝、fallback 或清理。

### 9.3 Non-interference 規則

若發現不屬於本 session、仍健康運行的 serving 或 GPU process：

1. 不傳送 signal；2. 不 attach profiler；3. 不改 config／environment／priority／affinity／filesystem；4. 不啟動競爭 CPU/GPU/PCIe/儲存的 hash、copy 或壓縮作業；5. 回報 process identity 與可能衝突。

**禁止使用 `kill`、`pkill`、`killall`、重啟 driver 或清理不明 process。** 只有本 session 自己建立且已確認失敗的 process 才可依既有停止流程終止。

敏感量測獨占 GPU；checksum 掃描、備份與壓縮移到量測窗口外。不得為維持 GPU 使用率而跑 filler workload。

### 9.4 Raw 保存

每個 attempt 保存：exact argv、環境與 runtime identity、模型 identity 與 checksum、輸入 fixture、輸出 token IDs 與 finish reason、原始時序、telemetry、stdout/stderr、失敗分類。**失敗的 run 同樣保存**，含 manifest、log 與 failure classification。

Raw 一律唯讀。轉換產物另存，並帶 provenance。轉換前後須重新雜湊輸入以確保未被修改。

---

## 10. DSE 與 break-even

### 10.1 掃描維度

**候選 function（A1–A6） × processor 規格 × workload regime**

敏感度軸：expert 容量、KV 容量、H2D/D2H 頻寬與延遲、queue depth、prefetch lookahead、predictor 品質、expert placement、壓縮率與解壓成本、copy 併發度、arrival pattern 與 batch、context 與 output 長度。

### 10.2 Baseline 要求

至少包含 no-prefetch、LRU、FIFO、static popularity。**不得只與明顯過弱的 baseline 比較。**

### 10.3 Prefetch 誠實性

主結論必須使用 **causal predictor**（僅用過去資訊）。完美 lookahead oracle 只能標示為上界。

既有量測（`data/canonical/moe_routing_v1/w3_prefetch_predictability.json`，四個生產級 MoE 模型）的 retained median：persistence 在四個模型上皆為 **0.0%**；frequency 為 0.0–2.6%；markov1 為 **−0.5% 至 15.4%**。同一份資料的 oracle stall reduction median 為 90–99.9%。

即最好的 causal predictor 只保留約 15% 的 oracle 收益，最差為負值。用 oracle 當主結論會系統性且大幅高估加速器價值。

> **⚠ 此組數字已降級，引用時必須標註。** 2026-08-18 的語料稽核發現：上述數字基於每 cell **n=3**，低於專案自訂收斂門檻 k\*=14 達 4.7 倍，且跨 benchmark 變異與效應量同量級（Llama 在不同 benchmark 間甚至變號：livecodebench −1.3% vs mmlu_ZH_CN +2.5%）。語料已補抓至 21/21 cell 達標，但 **`w3_*` 分析尚未重跑**。在 C1 重跑前，引用一律加註 `n=3 per cell, below own k*=14, pending C1 re-run`。詳見 `docs/status/EXTERNAL_CORPUS_AUDIT_20260818.md`。

### 10.4 Break-even 分解

```text
T_total = T_prepare + T_queue + T_execute + T_sync + T_move + T_recovery
```

**禁止以 software 的完整流程對比 hardware 的單一 primitive。** firmware 必須是真實程式碼，不得以週期常數代替。

輸出必須是**面**而非單一勝負，並涵蓋「無可行解」區域。每項改善同時報告：收益、控制成本、記憶體成本、頻寬成本、失敗案例、regression case、敏感度、break-even。

### 10.5 允許的結論

「此 operating region 不需要加速器」是**有效且必須保留**的結論，須轉成可重用的 boundary condition，不視為專案失敗。

---

## 11. HW0 需求與 LM18 handoff 產出

### 11.1 HW0 requirement rows

```text
HW0-COMMAND-THROUGHPUT      HW0-MAX-CONTROL-LATENCY     HW0-READY-DEPENDENCY-QUEUE
HW0-DMA-DESCRIPTOR-RATE     HW0-COPY-CONCURRENCY        HW0-METADATA-CAPACITY
HW0-METADATA-BANDWIDTH      HW0-RESIDENCY-IDENTITY-WIDTH HW0-PREFETCH-INTERFACE
HW0-COMPLETION-TAGGING      HW0-SYNCHRONIZATION         HW0-BACKPRESSURE-FALLBACK
```

每個 row 必須具備：source evidence、公式、單位、選定分位數、不確定度、適用 envelope、break-even 或 no-benefit boundary。

**證據不足者保持 `UNAVAILABLE_WITH_CONSEQUENCE`，不得猜值。** 禁止以單一最大值定規格。

### 11.2 與既有硬體證據對照

推導出的需求必須對照舊 repo 已驗證的合成／時序結果（承襲至 `hardware/`）：

| 設計 | 結果 |
|---|---|
| seqbuf residency engine | ME=32 → 346.07 MHz / 11,983 µm²；ME=128 → 99.25 MHz；ME=256 → 50.56 MHz / 62,851 µm²；ME=384 → 35.03 MHz / 100,994 µm² |
| banked LRU victim | N=128/B=16 → 200.33 MHz / 8,918 µm²；N=256 以上不達 200 MHz |
| argmin 微架構 | 組合式 66.50 MHz / 5,641 µm² vs 暫存器化掃描 236.24 MHz / 5,627 µm²（近乎零面積代價） |
| expert decompressor | NB4/L8 → 811.08 MHz / 4,902 µm²；NB8/L16 → 307.31 MHz / 23,815 µm² |

**注意**：以上為 pre-layout、wire-load model、ideal clock 的相對架構 DSE，非 sign-off。不得據此宣稱實體面積、功耗或產品可行性。

### 11.3 LM18 handoff 規格包

```text
RTL-ARCH      RTL-SCHEMA      RTL-GOLDEN
RTL-STIMULUS  RTL-ACTIVITY    RTL-HANDOFF
```

目標是讓後續 RTL 工作**不需猜測** identity、capacity、ordering、completion、error 與 backpressure 語意。另須補上舊 repo 要求但從未建立的 `RTL_SCOPE.md`。

---

## 12. 目錄結構與 lineage

實際結構（來源路徑刻意保留，理由見 `governance/lineage.yaml` 的 `structure_deviation`）：

```text
platform/
├── PLATFORM_FLOW_SPECIFICATION.md   本文件（根規格）
├── README.md   AGENTS.md   Makefile   pyproject.toml   .gitignore
├── project/       charter · evidence_levels · capability_registry
├── governance/    lineage.yaml · lineage/（checksum manifests）· stage_ledger.yaml
├── explorations/moe_cycle_simulator/
│     phase1–2     九類 Canonical IR
│     phase3–6     C++ cycle-resolved 引擎（core / single-GPU / residency / multi-domain）
│     phase7       GPU campaign runner · hooks · adapters · schemas
├── src/edgeflow/  trace 轉換 · routing 正規化 · residency 模型 · calibrated backend · multifidelity
├── accelerator/   參數化資源模型 · 六動詞 ABI · 掛載點 A1–A6 · reference mock（待建）
├── calibration/   模型形式 · fit/held-out · sealed holdout 協定（待建）
├── dse/           co-design DSE（待建）
├── hardware/      rtl · firmware · formal · syn · sta · verification · mem
├── measurement/   GPU collectors · schemas · scheduler · trace capture plan
├── evidence/      不可變量測證據（唯讀，581 MB / 4423 檔）
├── configs/       platform profiles · model dims · fidelity · sampling · calibration
├── schemas/  scripts/  experiments/  tests/  data/  runs/
└── docs/          methodology · tutorials · status · session_guides · PHASE_NAMING_MAP.md
```

概念分層（`ir/`、`engine/`）與實際目錄的對照見 `governance/lineage.yaml` 的 `conceptual_mapping`。
未採用該分層是因為 119 個非證據檔引用 `explorations/moe_cycle_simulator` 字面路徑，其中含 governance JSON——那些雜湊正是專案偵測竄改的機制。

### 12.1 搬遷範圍

**搬入**：全部真實量測證據；`phase1–7` 程式與契約（**排除 `governance/history` 遞迴快照**）；`src/edgeflow/`；`scripts/`；`configs/`；`schemas/`；`tests/`；`project/`；`experiments/`；`docs/methodology/` 與關鍵 status；`rtl/`、`firmware/`、`formal/`、`mem/`；`syn/`、`sta/`、`verification/` 的**結果 CSV/log + 生成腳本 + Dockerfile**；`gpu_run_package_v2` 的真實原始碼與 schema。

**不搬**：

- 439 MB 第三方 HuggingFace routing 語料（只帶 manifest、stats、registry 與抓取腳本）；
- `gpu_run_package_v2` 的 16.7 GB 環境（vendored runtime、模型權重、HF cache）；
- `governance/history` 遞迴原始碼快照；
- sta/syn netlist 與 Verilator build 產物（可由 Docker 重生）。

### 12.2 Lineage

`governance/lineage.yaml` 記錄每個搬入檔案的來源 commit（`e804b163…`）、來源路徑與 SHA-256，確保新 repo 可回溯舊 provenance 鏈。舊 repo 打 tag 後保持唯讀，作為 evidence-of-record。

---

## 13. 階段、驗收與 push 規則

| 階段 | 內容 | 驗收 |
|---|---|---|
| **0** | 說明書、遷移、環境修復 | 說明書完成；lineage checksum 全通過；`pytest` 可收集且全綠 |
| **A1** | calibration 模型形式修復 | 變更已事前登記；重擬合收斂且拒絕非物理解；舊 fail 報告仍在 |
| **A2** | measured raw → 九類 IR | 各家族通過 IR1；byte 守恆逐點成立；`routing_sha256` 可回溯 |
| **A3** | IR → 引擎 loader + replay | 15 點 hit/miss/evict 與量測**完全相等**；兩次 replay 位元相同；無 deadlock/Zeno |
| **A4** | sealed held-out 校準驗證 | 封存 split 只開封一次；三值判定 |
| **B1** | KV + continuous batching | 重現 SERV-P0-25 的 TTFT 與 completion latency 分布 |
| **B2** | 參數化候選處理器 + 掛載點 | reference mock 跑通六動詞路徑；未註冊 backend 仍正確拒絕 |
| **C1** | co-design DSE + break-even | baseline 齊備；causal predictor 為主結論；break-even 附不確定度 |
| **C2** | HW0 需求 + LM18 handoff | 每個 row 具 evidence、公式、單位、分位數、不確定度、envelope |

每階段結束時：更新狀態文件、產出完整 `runs/<run_id>/`（含 `manifest.json`、`resolved_config.yaml`、`logs/`、`metrics.json`、`artifacts/`、`environment/tool_versions.json`，失敗 run 亦保留），並 **push 到 `platform.git`**。

### 13.1 每階段開新 session

每個階段在**獨立 session** 執行，避免上下文汙染與長對話造成的判斷劣化。因此冷啟動的 session 沒有前一階段的記憶，交接不得依賴敘述性報告。

| 載體 | 用途 |
|---|---|
| `governance/stage_ledger.yaml` | 跨 session 的**單一狀態真相來源**：各階段狀態、前置條件、可複驗指令、claim boundary |
| `docs/session_guides/STAGE_*.md` | 每階段一份作業指引，含可直接貼上的啟動 prompt |
| `docs/session_guides/README.md` | 索引與通用規則 |
| `docs/status/` 五份 | `AGENTS.md §5` 要求的狀態文件，每階段結束更新 |

**三條跨 session 硬規則：**

1. **進入檢查是指令，不是敘述。** 每份指引的進入檢查列出具體指令與預期輸出。任一不符即停止並回報，不得「看起來差不多就繼續」。前一個 session 的報告只是參考，`stage_ledger.yaml` 加上實際重跑的指令才是依據。
2. **只讀本階段指引。** 不讀其他階段的指引，避免提前套用後期階段的假設或把後期的結論當成已成立。
3. **狀態改為 `COMPLETE` 前，`verification` 每一條都必須實際執行並貼上實際輸出。** 只有該階段的 session 可以改自己那一列，不得改動或刪除其他階段的列。

---

## 14. 禁止事項

1. 修改 `evidence/` 內任何 raw 資料。
2. 以未註冊來源的頻寬、延遲、時脈、功耗、expert size 或 service time 產生結論。
3. 只與明顯過弱的 baseline 比較。
4. 讓 simulator、software、firmware、RTL 使用不同演算法語意卻宣稱跨層一致。
5. 以控制 FSM、MMIO register 或無法實際搬運資料的 scaffold 取代 full datapath。
6. 以 cell count 直接宣稱實體面積、功耗或產品可行性。
7. 覆寫失敗結果、只保留最佳 run、或手動修改 generated metrics。
8. 將模型估計寫成實機量測，或將 RTL pass 寫成端到端效能已驗收。
9. 放寬已登記的驗證門檻，或在 held-out 開封後移動 split。
10. 以完成率百分比（如 `44/286`）宣稱專案進度——該類 ledger 尚未完整展開 children，分母不可信。

---

## 15. 停止與詢問條件

**須停止並詢問 owner**：GPU 或 domain 與登記不符；model／revision／precision／runtime identity 不符；只能藉由改變 workload、開啟 offload、縮短長度或放寬門檻才能執行；fit/held-out 出現洩漏；GPU Xid／ECC／reset、資料毀損或持續性 runtime failure；需要刪除或覆寫既有證據；需要額外付費、安裝或重新下載。

**可自主進行的最小修復**：runner／collector／parser／hook／序列化／等價 API 適配問題。修復前後保存 diff 與回歸測試；失敗的 attempt 不得消失。

**科學驗證 FAIL 不是程式錯誤。** 保留結果，不移動 held-out，不刪除慢樣本，不放寬門檻。

---

## 16. 停止某條探索的正當條件

- 問題在可接受的 profiling 精度下不存在；
- proposed 不優於合理 baseline，且敏感度分析沒有可行區域；
- software／firmware 已足夠，不需要硬體 fast path；
- full datapath 的成本抵銷系統收益；
- 關鍵參數無法取得、校正或以範圍方式處理。

停止一條探索不是專案失敗；**必須把負面結果轉成可重用的 boundary condition**。
