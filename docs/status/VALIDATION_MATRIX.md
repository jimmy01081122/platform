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

根因為模型形式錯誤（四項結構性缺陷，見 `calibration/README.md` 與 `experiments/specs/cal_model_form_repair_v1.yaml`），非量測品質問題。上表數字為 evidence-of-record，**未被本階段修改**。修復後的重新判定屬 A4 的 sealed held-out，**不得在 A1 內自行宣告通過**。

## A1 · FIT 側殘差（非 held-out 判定，僅供對照）

來源 `calibration/fits/v2/residual_report.json`。**下表全部是 FIT 側數字，三值判定不適用（三值判定僅用於 sealed held-out，見規格 §7.3），因此標示為 `NOT_APPLICABLE_FIT_ONLY` 而非 PASS/FAIL。**

| metric | 舊 MAPE（evidence-of-record） | 新 FIT 側 MAPE | 判定 |
|---|---|---|---|
| component_latency | 304.418% | 20.324% | `NOT_APPLICABLE_FIT_ONLY` |
| pcie_transfer_latency | 66.879% | 19.821% | `NOT_APPLICABLE_FIT_ONLY` |
| moe_replay_tpot | 293.936% | 43.176% | `NOT_APPLICABLE_FIT_ONLY` |
| moe_replay_throughput | 60.658% | 75.898%（**變差**，見下方修正） | `NOT_APPLICABLE_FIT_ONLY` |

**throughput 措辭修正（2026-08-18，Principal Reviewer）**：先前把 `moe_replay_throughput` 的 60.658%→75.898% 說成「方向仍是改善」是誤導。就 throughput（=1000/tpot）本身的 MAPE 定義，它**變差了**。正確敘述應拆成兩句：TPOT 的擬合明顯改善（293.936%→43.176%）；但 throughput 這個指標本身的 MAPE 因 reciprocal 轉換對百分比誤差不對稱而上升（TPOT 被低估時，1/TPOT 會被等比放大地高估）。這不是模型退步，但也不能說成 throughput 改善。正確做法是把 throughput 當作 1000/TPOT **派生量**，讓它誠實地繼承 TPOT 的改善，而不是當作獨立擬合目標。

非收斂／新缺口：dequant（GAP-1，2026-08-18 起改標為 a-priori 名單排除 `flat_by_registered_exclusion`／`DEQUANT_PROXY_ONLY`，非「catch 後 fallback」）、聚合 contention 模型未被評估點驗證（GAP-2）、floor_ms 樣本量少（GAP-3）、component_latency 評估點原始 schema 缺 shape 特徵（GAP-4）、moe_replay `cpu_calls`/`expert_tokens` 正規化落差約 8 倍（GAP-5）、小尺寸 PCIe 傳輸多 stream 交互作用未建模（GAP-6）。完整說明見 `calibration/fits/v2/measurement_gaps.json`。

**Principal Reviewer 覆核結論（2026-08-18，全部 FIT 側 / diagnostic，非 held-out）**：
- **P0 calibration-safety 違約（已修正）**：`fit_parameters()` 先前對非物理 gpu_service 擬合 catch 後 fallback 為常數，違反 root spec §8.3。已改為事前名單 `SHAPE_INSENSITIVE_OPERATIONS`；非名單 op 一律 raise。self-check 已覆蓋 production path。見 P-012。
- **PCIe two-regime**：ACCEPT（進 v2 preregistration）。獨立自 raw 重現 30 點 1.04% MAPE（小尺寸 48.8%→2.5%、bulk 0.5%→0.06%），且 `benchmark.py:316-334` 證實 `copy_streams`＝固定總位元組切成 S 塊、S streams、wait-all，two-regime 是物理正確形式。待定：production 端 stream 語意。
- **ProfileKNN**：MORE_TESTS。aggregate 10.9% 被 18/18 個 decode 精確 token lookup 灌水；真正的 prefill 內插為 18.2%（>15%；per-op selected_expert 22.4%、grouped_gemm 20.9%、gather_scatter 11.2%）。
- **MoE replay operator graph**：`window_replay()` 實測含 2 次 argsort（排序＋逆排列）＋3 個 GEMM＋gather＋scatter，但登記 metadata 只記 `grouped_gemm`＋`gather_scatter`——系統性遺漏 routing/sort 項。固定 `tau_route` 僅 2 個結構點，DIAGNOSTIC_ONLY；正式版應改為顯式 sort/permute operator。
- **routing 守恆**：Σn_e＝num_tokens×top_k，672 步 0 違反（CONFIRMED）。
- **dequant**：raw 標記 `synthetic_symmetric_int4_proxy_not_checkpoint_awq`，維持 `DEQUANT_PROXY_ONLY`。

## 第三方 routing 語料（2026-08-18 稽核）

完整稽核見 `EXTERNAL_CORPUS_AUDIT_20260818.md`。

| 項目 | 判定 | 證據 |
|---|---|---|
| 轉換正確性（prefill/decode、dense layer、router_scores=null） | **PASS** | 354 檔 `validation_errors` 全部為 0 |
| 每檔 provenance（sha256 + source_revision） | **PASS** | `data/registry/hf_downloads.json` |
| revision 一致性 | **PASS** | live `main` = `27febb7b…` = 登記值，無漂移 |
| 取樣充分性（專案自訂 k\*=14） | **PASS**（補抓後） | 21/21 cells n≥14。**補抓前為 1/11** |
| 分析已用新語料重跑 | **NOT_RUN** | `data/canonical/moe_routing_v1/` 下 13 份 `w3_*` 結果仍是 60 檔舊樣本產物 → C1 |
| `dataset_structure.json` 完整性 | **PASS**（修正後） | Kimi mmlu 45→57 科；`total_json_files` 99,540→103,961。原缺漏為分頁截斷 |
| 長上下文覆蓋 | **FAIL** | 全資料集最長 query 約 721 tokens，距 1M context 三個數量級。**無法靠補抓解決** |

**目前禁止引用的語料衍生數字**：`w3_prefetch_predictability`、`w3_capacity_dse`、`w3_copy_engine_dse`、`w3_compression_dse`、`w3_robustness`、`convergence` 等 13 份結果。它們基於每 cell n=3（低於自訂 k\*=14 達 4.7 倍），且跨 benchmark 變異與效應量同量級。引用時必須標註 `n=3 per cell, below own k*=14, pending C1 re-run`。

## 掛載點的證據覆蓋

| 掛載點 | 功能 | 優先序 | 證據狀態 |
|---|---|---|---|
| A1 | routing / gating 決策計算 | 主 | 有 routing trace（`[159,32,2]`）與 CTRL-PX0-*-routing |
| A2 | MoE dispatch 資料搬運 | 主 | **PARTIAL** — 見下方修正 |
| A3 | transfer 排程 / DMA descriptor | 主 | 有 transfer 微基準 v1–v4 與 `transfer_events` |
| A4 | expert 解壓縮 / 壓縮搬運 | 主 | 有 `expert_decompressor.sv` 合成結果與 `w3_compression_dse` |
| A5 | KV block 管理 / offload | 次 | 有 SWAP-K1/K2/K5，但事件 `block_size=0`（`LIMITED`） |
| A6 | offloaded KV 上的 attention | 次 | **NOT_RUN** — 無任何量測（全倉庫 grep `max_model_len`/`cpu_offload_gb`/`swap_space`/`kv_offload` 零命中） |

**A2 掛載點的修正（2026-08-18，GPU 前置準備盤點時發現）**

本表先前記為「無任何量測」，不精確。實際上 `evidence/` 有 56 筆 `gather_scatter` 記錄。但那個探針（`measurement/gpu_run_package_v2/scripts/benchmark.py:463`）是

```python
def gather_scatter(x=activations, idx=order, inv=inverse):
    return x.index_select(0, idx).index_select(0, inv)
```

——作用在 `torch.randn` 合成張量上的**同裝置** gather 後反向 gather，是 shape-faithful 的獨立 kernel 計時，**沒有任何跨裝置搬運**。

因此準確的敘述是：**execute 側有合成 kernel proxy；系統層的 dispatch 搬運與控制結構（`T_prepare`/`T_queue`/`T_sync`/`T_move`）完全沒有量測。**

這個修正改變量測計畫——要補的是 in-serving 儀測，不是從零寫一個 gather/scatter kernel benchmark。

## 後續階段（全部尚未執行）

| 階段 | 驗收條件 | 判定 |
|---|---|---|
| A1 | 模型形式變更已事前登記；重擬合收斂且拒絕非物理解；舊 fail 報告仍在 | **IN_PROGRESS**（事前登記✓、舊 fail 報告未改✓；「拒絕非物理解」先前為**假 PASS**——production path 其實 fallback 常數，2026-08-18 已修正 P-012 並以 production-path self-check 驗證。仍待 v2 preregistration 重擬合 reviewer 接受的候選；非 calibrated PASS） |
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
