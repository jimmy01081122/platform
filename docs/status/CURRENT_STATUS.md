# CURRENT_STATUS

```text
updated       : 2026-08-19
platform      : /home/a/platform
repo          : git@github.com:jimmy01081122/platform.git
current stage : Stage 0 完成；A1 IN_PROGRESS（FIT 側殘差，未做 held-out 判定）；A2 COMPLETE（OFF-E-PR3 15 點 → 九類 IR，MEASURED，過 IR1）；TRACK_GPU_PREP PREP-1 完成（純 CPU，無 GPU 主張）
```

## 一句話狀態

平台已建立、基線全綠、規格與 session 指引系統就緒；A1 已修復 calibration 的四項模型形式缺陷並在 FIT 側資料上重新擬合，殘差大幅改善但**仍未達 15%/20% 門檻、也未做任何 held-out 判定**——**研究鏈的另外兩個斷點（量測→IR、IR→引擎）還沒接上，因此仍沒有任何 calibrated PASS、break-even 或 accelerator 主張。**

## 階段狀態

| 階段 | 狀態 | 說明 |
|---|---|---|
| Stage 0 | **COMPLETE** | 遷移、基線、根規格、session 指引系統 |
| A1 calibration 模型形式修復 | **IN_PROGRESS** | 4 缺陷修正並事前登記；P0 safety 修正（P-012）。v2 候選已 FIT 側評估（P-013）：**A PCIe two-regime ACCEPT（1.04%）、B ProfileKNN INSUFFICIENT（LOOWO 不 generalize，不升格）、C replay BLOCKED_ON_MEASUREMENT**。closure blocked 在 V2-GAP-B/C 量測。未做 held-out；無 calibrated PASS |
| A2 measured → 九類 IR | **COMPLETE** | OFF-E-PR3 expert 容量掃描 15 點 → 單一九類 Canonical IR bundle（MEASURED，33203 records）過 IR1 且 round-trip 再驗證。byte 守恆 15/15；routing_sha256 可回溯；claim boundary 綁定每筆 provenance。RoutingIR 為 AGGREGATE（量測無 gate scores）。mock adapter 未動。**無時序/效能/calibrated 主張；IR 尚未被引擎消費（A3）**。run `20260819T000000Z__stage_a2_off_e_pr3_measured_ir` |
| A3 IR → 引擎 loader | NOT_STARTED | |
| A4 sealed held-out 驗證 | NOT_STARTED | |
| B1 KV + continuous batching | NOT_STARTED | 指引為 RULES_ONLY |
| B2 參數化候選處理器 | NOT_STARTED | 指引為 RULES_ONLY |
| C1 co-design DSE | NOT_STARTED | 指引為 RULES_ONLY |
| C2 HW0 + LM18 handoff | NOT_STARTED | 指引為 RULES_ONLY |
| GPU 前置準備軌 | **IN_PROGRESS** | **PREP-1 完成**（P-014）：contract 凍結、兩探針 CPU smoke 通過、5 parser+fixture、sealed split 封存、GAP-5 由程式碼解、窗口計畫。PREP-2 blocked on A2；依賴 A2 schema 的探針欄位標 PENDING_A2 |
| GPU 量測軌 | NOT_STARTED | 需前置軌完成（優先序 4/5 例外，見下）；contract 已凍結，priority 1/2 探針已備 |
| 統籌 session | — | 排程與驗收，隨時可開 |

權威狀態記錄為 `governance/stage_ledger.yaml`；本文件是人類可讀摘要，衝突時以 ledger 為準。

## 已驗證的基線（2026-08-17 實測）

```text
證據完整性   4423 / 4423 檔 SHA-256 通過（make verify-evidence）
Python 測試  317 通過、0 失敗
             tests 96 · simulator/tests 36 · phase1 16 · phase2 43 · phase7 126(+48 subtests)
C++ CTest    14 通過、0 失敗（phase3 2/2 · phase4 3/3 · phase5 4/4 · phase6 5/5）
workspace    make doctor -> workspace_contract: pass
```

## 研究鏈的三個斷點（全部未修）

1. **量測 → IR**：**OFF-E-PR3 家族已接通（A2 COMPLETE，2026-08-19）**。`off_e_pr3_measured_adapter.py` 把 15 點真實量測（含真實 `.npy` routing、transfer events、counters、token ids）映成九類 IR 並過 IR1。mock adapter 保留作 fixture。**其餘量測家族（SWAP-K2 / SERV-P0-25 / controlled matrix / component / transfer）尚未接**——adapter 架構可延伸，屬 A2 後續或另開 session。IR→引擎（斷點 2）仍未接，故此 bundle 尚未被任何引擎消費。→ A2 家族其餘 / A3
2. **IR → 引擎**：不存在 IR bundle → C++ 引擎的 loader。目前已發表的 DSE 數字出自 `src/edgeflow/residency.py`（265 行解析模型），**不是** C++ 引擎。→ A3
3. **校準**：舊 q1 report（保留為證據，未修改）四項 MAPE gate 全數失敗（component 304.418%、TPOT 293.936%、PCIe 66.879%、throughput 60.658%；90 點中 17 通過）。根因是模型形式錯誤，非量測品質。A1 已修正四項模型形式缺陷並在 FIT 側重新擬合：component_latency 304.418%→20.324%、pcie_transfer_latency 66.879%→19.821%、moe_replay_tpot 293.936%→43.176%、moe_replay_throughput 60.658%→**75.898%（就 throughput 自身 MAPE 定義是變差，非改善**——TPOT 擬合改善，但 throughput＝1000/TPOT 的 MAPE 因 reciprocal 轉換不對稱而上升；正確做法是把 throughput 當派生量而非獨立擬合目標；細節見 `calibration/fits/v2/residual_report.json`）。**這些數字全部是 FIT 側，不是 held-out 判定** —— sealed held-out 仍待 A4。2026-08-18 Principal Reviewer 覆核：修正一項 P0 calibration-safety 違約（`fit_parameters()` 對非物理擬合曾靜默 fallback，見 P-012）；PCIe two-regime（自 raw 重現 1.04%）與 replay 的 routing/permutation operator 方向已接受進 v2 preregistration，ProfileKNN 需更多測試（prefill 內插 18.2%＞15%）。

## 可用證據

`evidence/` 共 581 MB / 4423 檔，**唯讀**：

| 群組 | 內容 |
|---|---|
| `evidence/phase7/` | 47 個 GPU campaigns（2026-08-13~14）：OFF-E-PR0–PR4 含 15 點 expert 容量掃描、OFF-W0–W3、SWAP-K0–K5、expert catalog、session guards |
| `evidence/measurement_backups/` | 18 個備份：SERV-P0-25 serving 錨點、controlled matrix、K0–K11 profiles、W0–W3、transfer 與 component 微基準、資格認證 cells |
| `evidence/gpu_measurements/` | q0 fitted parameters 與 q1 validation report（四項 gate 全失敗，保留為證據） |

量測 domain（目前主力，非永久假設）：single RTX PRO 6000 96 GB · Mixtral-8x7B-Instruct-v0.1 rev `eba92302…` · vLLM 0.23.0 · BF16/BF16 · TP/PP/EP 1/1/1。

## 第三方 routing 語料（2026-08-18 稽核 + 補抓）

`core12345/MoE_expert_selection_trace` 稽核發現七項缺陷，六項已修正。完整記錄見 `EXTERNAL_CORPUS_AUDIT_20260818.md`。

```text
檔數      60 -> 354        位元組  147 MB -> 805 MB
cell 達標  1/11 -> 21/21   （專案自訂門檻 k* = 14）
新增軸     序列長度（professional_law）、工作負載類型（aime_2024 數學推理）
登記修正   Kimi mmlu 45->57 科；total_json_files 99,540 -> 103,961（原為分頁截斷）
```

**兩項未解**：

1. **`w3_*` 分析尚未用新語料重跑**——`data/canonical/moe_routing_v1/` 下 13 份結果仍是 60 檔、多數 cell n=3 的產物。引用時一律標註 `n=3 per cell, below own k*=14, pending C1 re-run`。這是 C1 的第一件事。
2. **長上下文無解**——全資料集單一 query 最長約 721 tokens，距 1M context 三個數量級，且**無法靠補抓解決**。長上下文 routing 證據只能自行量測，這使 GPU 軌的長上下文量測成為唯一來源。

## GPU 量測就緒度（2026-08-18 盤點）

五項量測的**資訊增益順序與就緒順序幾乎相反**：

```text
序 目標                        就緒度
1  A2 dispatch 資料搬運        需新寫 in-serving 儀測
2  A6 長上下文 KV attention    最大缺口：runner/config/parser 全部要新寫
3  sealed held-out             split 須在量測前封存（已產生的資料無法誠實封存）
4  component service 缺口      今日即可執行（A1 已完整規格化 6 項）
5  SERV-P0-25 tail-CI          今日即可執行（只缺 run plan 與時間估計）
```

因此 GPU 軌拆為 `TRACK_GPU_PREP`（純 CPU，凍結 contract、寫探針、測 parser、封存 split）與 `TRACK_GPU`（拿到 endpoint 後純執行）。詳見 P-011。

**兩項盤點時發現的事實修正**：

1. **A2 掛載點不是「無任何量測」**——`evidence/` 有 56 筆 `gather_scatter`，但那是 `benchmark.py:463` 的同裝置合成 proxy（`index_select` 來回作用於 `torch.randn`），只給 execute 項。準確敘述：execute 側有合成 kernel proxy，系統層搬運與控制結構完全沒有量測。
2. **GAP-5 可能不需要 GPU**——`measurement_gaps.json` 給了兩條解法，第一條「documentation of the cpu_calls↔launch-granularity mapping」是純讀程式碼的工作。線索在 `benchmark.py:440-441`（`route = base_route.repeat(concurrency)`，`expert_tokens = route.numel()`）對上 moe_replay 側的 `tokens / cpu_calls`。前置軌須先試這條再排量測。

## 已知缺口

- **掛載點 A2（系統層 dispatch 搬運）與 A6（offloaded KV 上的 attention）沒有量測。** 兩者都在 GPU 軌的最高優先序；A6 因語料缺乏長上下文而升級為唯一可行來源，且全倉庫 grep `max_model_len`/`cpu_offload_gb`/`swap_space`/`kv_offload` **零命中**——runner 全部要新寫。
- 長上下文與高並發區間沒有量測；目前所有 expert residency 結論都限於單請求、eager、159 tokens、`max_num_seqs=1`。
- `accelerator/`、`dse/` 目前只有 README 骨架，無實作。`calibration/` 現有 `refit_v2.py` 與 `fits/v2/` 輸出（A1 產出），仍缺 A4 的 sealed held-out 實作。
- A1 殘差分析發現的新缺口（見 `calibration/fits/v2/measurement_gaps.json`）：dequant 延遲的真正驅動變數是 expert 權重位元組數而非 token 數，本階段量測無法把兩者分離；小尺寸 PCIe 傳輸在多 stream 下仍有未建模的延遲成長（大尺寸區間已驗證修復，小尺寸區間相反方向的殘留效應是新發現，不在本階段四項缺陷登記範圍內）；moe_replay 的 `cpu_calls`/`expert_tokens` 兩種量測慣例之間有約 8 倍的正規化落差，尚未釐清換算關係。
- SWAP-K2 的 KV 事件 `block_size=0`，位元組帳目由 runtime shape/dtype 推導；治理已開放 KV 效能主張，但此資料品質限制不因此消失。

## 下一步

**現在可以同時開兩個 session，兩者無相依、都不需要 GPU：**

| session | 指引 | 為什麼 |
|---|---|---|
| **A2** measured → 九類 IR | `docs/session_guides/STAGE_A2_MEASURED_TO_IR.md` | 解鎖最多下游（A3/A4/B1/B2/PREP-2 共五項）。且 A2 定義 IR 評估點 schema，是新量測輸出欄位的唯一依據——GAP-4 正是缺這個而產生的 |
| **GPU 前置準備軌** | `docs/session_guides/TRACK_GPU_PREP.md` | GPU endpoint 是有時限資源，其他都不是。只要 endpoint 可能出現，本軌就該已在跑 |

**關鍵路徑** `A2 → A3 → B2 → C1 → C2`，且 C1 需要 A4、A4 需要 GPU endpoint——**GPU 在關鍵路徑上，不是支線**。

排程與跨 session 驗收可另開統籌 session（`docs/session_guides/SESSION_ORCHESTRATOR.md`），它不執行階段工作，只排程、獨立重跑 verification、整理需要 owner 裁決的事。
