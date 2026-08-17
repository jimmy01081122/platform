# VALIDATION_MATRIX

各項驗收條件的當前狀態。三值判定：`PASS` / `FAIL` / `INSUFFICIENT_EVIDENCE`；尚未執行者標 `NOT_RUN`。

**規則**：資料不足判 `INSUFFICIENT_EVIDENCE`，不得寫成 `PASS` 或 `FAIL`（規格 §7.3）。

---

## Stage 0 · 遷移與基線

| 項目 | 判定 | 證據 | 日期 |
|---|---|---|---|
| 證據逐檔完整性 | **PASS** | `make verify-evidence` → 4423/4423 SHA-256 通過 | 2026-08-17 |
| 來源↔目的檔數一致 | **PASS** | source 4423 / dest 4423，`rsync -acn` 差異 0 | 2026-08-17 |
| Python 測試基線 | **PASS** | 317 通過、0 失敗 | 2026-08-17 |
| C++ CTest 基線 | **PASS** | 14 通過、0 失敗（2/2 · 3/3 · 4/4 · 5/5） | 2026-08-17 |
| workspace contract | **PASS** | `make doctor` → `workspace_contract: pass` | 2026-08-18 |
| 根規格已凍結 | **PASS** | `PLATFORM_FLOW_SPECIFICATION.md` v1.0 | 2026-08-17 |
| session 指引系統 | **PASS** | `governance/stage_ledger.yaml` + `docs/session_guides/` 10 份 | 2026-08-18 |

## 研究鏈的三個斷點

| 斷點 | 判定 | 說明 | 負責階段 |
|---|---|---|---|
| 量測 → 九類 IR | **FAIL** | 唯一 adapter 為 mock；已 grep 確認無真實 `.npy` → `EventIR`/`RoutingIR` 路徑 | A2 |
| IR → C++ 引擎 | **FAIL** | 無 loader；現有 DSE 數字出自 `src/edgeflow/residency.py` 而非引擎 | A3 |
| 量測校準 | **FAIL** | 四項 MAPE gate 全失敗（見下） | A1 → A4 |

## 校準 gate（門檻：MAPE ≤ 15%、single-point APE ≤ 20%）

來源 `evidence/gpu_measurements/rtx-pro-6000-v3-20260718/rtx-q1-validation-report.json`（**保留為證據，不得刪改**）。

| metric | MAPE | 判定 | 通過點數 |
|---|---|---|---|
| component_latency | 304.418% | **FAIL** | 9 / 48 |
| moe_replay_tpot | 293.936% | **FAIL** | 0 / 6 |
| pcie_transfer_latency | 66.879% | **FAIL** | 10 / 30 |
| moe_replay_throughput | 60.658% | **FAIL** | 0 / 6 |
| **合計** | — | **FAIL** | **17 / 90** |

根因為模型形式錯誤（三項結構性缺陷，見 `calibration/README.md`），非量測品質問題。修復後的重新判定屬 A4 的 sealed held-out，**不得在 A1 內自行宣告通過**。

## 掛載點的證據覆蓋

| 掛載點 | 功能 | 優先序 | 證據狀態 |
|---|---|---|---|
| A1 | routing / gating 決策計算 | 主 | 有 routing trace（`[159,32,2]`）與 CTRL-PX0-*-routing |
| A2 | MoE dispatch 資料搬運 | 主 | **NOT_RUN** — 無任何量測 |
| A3 | transfer 排程 / DMA descriptor | 主 | 有 transfer 微基準 v1–v4 與 `transfer_events` |
| A4 | expert 解壓縮 / 壓縮搬運 | 主 | 有 `expert_decompressor.sv` 合成結果與 `w3_compression_dse` |
| A5 | KV block 管理 / offload | 次 | 有 SWAP-K1/K2/K5，但事件 `block_size=0`（`LIMITED`） |
| A6 | offloaded KV 上的 attention | 次 | **NOT_RUN** — 無任何量測 |

## 後續階段（全部尚未執行）

| 階段 | 驗收條件 | 判定 |
|---|---|---|
| A1 | 模型形式變更已事前登記；重擬合收斂且拒絕非物理解；舊 fail 報告仍在 | NOT_RUN |
| A2 | 各量測家族通過 IR1；byte 守恆逐點成立；`routing_sha256` 可回溯 | NOT_RUN |
| A3 | 15 點 hit/miss/evict 與量測完全相等；兩次 replay 位元相同；無 deadlock/Zeno | NOT_RUN |
| A4 | 封存 split 只開封一次；MAPE ≤15%、APE ≤20%，三值判定 | NOT_RUN |
| B1 | 重現 SERV-P0-25 的 TTFT 與 completion latency 分布（p50/p95/p99） | NOT_RUN |
| B2 | reference mock 跑通六動詞路徑；未註冊 backend 仍正確拒絕 | NOT_RUN |
| C1 | baseline 齊備；causal predictor 為主結論；break-even 附不確定度 | NOT_RUN |
| C2 | 每個 HW0 row 具 evidence、公式、單位、分位數、不確定度、envelope | NOT_RUN |

## 目前允許與禁止的主張

**允許**：evidence 內容與來源逐檔一致；遷移後基線 317+14 全綠；`evidence/` 所載的原始量測事實（附來源路徑）。

**禁止**：任何 calibrated 主張；任何 break-even 或 accelerator 收益主張；任何長上下文或高並發的效能主張；把 Phase 1–7 測試通過解讀為研究鏈已打通；以完成率百分比宣稱進度。
