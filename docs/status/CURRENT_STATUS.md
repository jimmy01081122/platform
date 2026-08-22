# CURRENT_STATUS

```text
updated       : 2026-08-22（TRACK_GPU Phase 1/2：V2-GAP-A measured PASS；GPU stopped；本機探針、adapter 與證據閘門完成 hardening）
platform      : /home/a/platform
repo          : git@github.com:jimmy01081122/platform.git
current stage : Stage 0 完成；A1 IN_PROGRESS（FIT 側殘差，closure blocked 在 GPU 量測 V2-GAP-B/C）；A2 COMPLETE；A3 COMPLETE；B2 COMPLETE（可掃描元件、六動詞 ABI、reference mock 五路徑、A1-A6 定義，全 ANALYTICAL/PROJECTED）；TRACK_GPU_PREP 全數完成（PREP-1/2/3，含 V2-GAP-A 前置，經統籌獨立 CONFIRMED）
next dispatch : GPU instance 48381628 已 stop（未 destroy）。再次 start 前先由 owner 裁決 target_1/2 的低層 observability 路徑，並補 target_2 batching/prefix/canary-seq 與 GAP-1 weight grid；start 後第一件事是跑 93.4-GB model content manifest。target_4 FIT subset、V2-GAP-C 與 target_5 driver 已在本機 CPU 測試完成，尚未 GPU 執行。
```

## 一句話狀態

平台已建立、基線全綠、規格與 session 指引系統就緒；A1 已修復 calibration 的四項模型形式缺陷並在 FIT 側資料上重新擬合，殘差大幅改善但**仍未達 15%/20% 門檻、也未做任何 held-out 判定**——**研究鏈的另外兩個斷點（量測→IR、IR→引擎）還沒接上，因此仍沒有任何 calibrated PASS、break-even 或 accelerator 主張。**

## 階段狀態

| 階段 | 狀態 | 說明 |
|---|---|---|
| Stage 0 | **COMPLETE** | 遷移、基線、根規格、session 指引系統 |
| A1 calibration 模型形式修復 | **IN_PROGRESS** | 4 缺陷修正並事前登記；P0 safety 修正（P-012）。v2 候選已 FIT 側評估（P-013）：**A PCIe two-regime ACCEPT（1.04%）、B ProfileKNN INSUFFICIENT（LOOWO 不 generalize，不升格）、C replay BLOCKED_ON_MEASUREMENT**。closure blocked 在 V2-GAP-B/C 量測。未做 held-out；無 calibrated PASS |
| A3 IR→引擎 loader + 位元精確 replay | **COMPLETE** | 15 點 residency counters（hit/load/discard）經 phase5→phase4 引擎位元精確重現、SIM1 兩次 replay 決定性、15/15 QUIESCENT。service model 經 phase4 接上（per-object H2D=12450143814087 fs 對回校準值）。run `20260819T134458Z__stage_a3_ir_to_engine_replay`。**僅 residency 語意，無任何時序準確度/calibrated 主張**。phase3 kService 字面修改因 r5 凍結契約留 owner（P-017） |
| A2 measured → 九類 IR | **COMPLETE** | OFF-E-PR3 expert 容量掃描 15 點 → 單一九類 Canonical IR bundle（MEASURED，33203 records）過 IR1 且 round-trip 再驗證。byte 守恆 15/15；routing_sha256 可回溯；claim boundary 綁定每筆 provenance。RoutingIR 為 AGGREGATE（量測無 gate scores）。mock adapter 未動。**無時序/效能/calibrated 主張；IR 尚未被引擎消費（A3）**。run `20260819T000000Z__stage_a2_off_e_pr3_measured_ir` |
| A4 sealed held-out 驗證 | NOT_STARTED | gated：需 A1 closure + A3（已 COMPLETE）+ 新 GPU held-out 量測 |
| B1 KV + continuous batching | NOT_STARTED | 指引為 RULES_ONLY |
| B2 參數化候選處理器 | **COMPLETE** | accelerator/ 套件：九參數可掃描資源模型 + 六動詞 ABI（防偽 registry）+ FUNCTIONAL_POLICY/CYCLE_RESOLVED_MODEL/REFERENCE_MOCK + 掛載點 A1-A6（三件事）。reference mock 五路徑跑通；未註冊 backend 拒絕；零元件標 MEASURED_SURROGATE；A2/A6 標無量測。run `20260819T201446Z__stage_b2_accelerator_model`。**無收益/break-even 主張（C1）；A2/A6 無效能結論** |
| C1 co-design DSE | NOT_STARTED | 指引為 RULES_ONLY |
| C2 HW0 + LM18 handoff | NOT_STARTED | 指引為 RULES_ONLY |
| GPU 前置準備軌 | **COMPLETE** | PREP-1（P-014）＋ **PREP-2 完成**（P-016）：A2 完成後，依 A2 CalibrationIR schema 把探針依賴欄位全部填實，operand shape 直接進 evaluation_coordinate，探針輸出可**不經 join** 生成對 A2 schema 有效的 IR 評估點（GAP-4 類缺陷不重演）。無 GPU 主張 |
| GPU 量測軌 | **IN_PROGRESS** | target_4 legacy raw 已本機保存並以 canonical converter 救回 90 點（48 component 為 `legacy_join_recovered`；非新標準）。FlashInfer HEADERFIX guard 在成本閘門前 PASS。V2-GAP-A attempt1 instrumentation failure 完整保存；未放寬 parser，attempt2 的 24 unique cells×n=5、strict parser、grid audit 全 PASS。新 target_4 probes 已按 sealed assignment 隔離 fit=41/validation=11/holdout=12；例行 FIT run無明確 A4 授權不能碰 holdout。target_5 fresh 10K arrival driver、本機 model manifest generator 與 attempt wrapper 已備。target_1/2 adapter 的 engine-control path 已實作，但 source audit 發現 frozen measured fields 不可由 vLLM 0.23 現有 Python/metrics 邊界直接觀測，因此明確拒絕量測；live control audit NOT_RUN。全 raw 拉回後 instance stop、不 destroy |
| 統籌 session | — | 排程與驗收，隨時可開 |

權威狀態記錄為 `governance/stage_ledger.yaml`；本文件是人類可讀摘要，衝突時以 ledger 為準。

## 已驗證的基線（2026-08-22 TRACK_GPU Phase 1 本機重跑）

```text
證據完整性   4423 / 4423 檔 SHA-256 通過（make verify-evidence）
Python 測試  424 通過、1 skipped、0 失敗
             tests 188 · simulator/tests 36 · phase1 16 · phase2 43 · phase7 141(+48 subtests)
C++ CTest    14 通過、0 失敗（phase3 2/2 · phase4 3/3 · phase5 4/4 · phase6 5/5）
workspace    make doctor -> workspace_contract: pass
```

> 歷史基線（2026-08-17 遷移時）為 317 Python；A3 後為 365；B2 新增 tests/test_accelerator.py（+34，129→163）→ 399，皆為允許的上升。

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

- **掛載點 A2（系統層 dispatch 搬運）與 A6（offloaded KV 上的 attention）仍沒有量測。** backend/config/engine-control adapter 已存在，但不偽造不可觀測欄位：FlashInfer CUTLASS 的單一 fused MoE boundary 無法把 `T_move` 與 execute 分開；native offload stats 只有 transfer bytes/time、且讀取會 drain/reset，沒有 per-request GPU/CPU residency gauge。target_1/2 因此等待 owner 裁決低層 hook，而不是改用 parent wall time或推導 residency。
- 長上下文與高並發區間沒有量測；目前所有 expert residency 結論都限於單請求、eager、159 tokens、`max_num_seqs=1`。
- `accelerator/`、`dse/` 目前只有 README 骨架，無實作。`calibration/` 現有 `refit_v2.py` 與 `fits/v2/` 輸出（A1 產出），仍缺 A4 的 sealed held-out 實作。
- A1 殘差分析發現的新缺口（見 `calibration/fits/v2/measurement_gaps.json`）：dequant 延遲的真正驅動變數是 expert 權重位元組數而非 token 數，本階段量測無法把兩者分離；小尺寸 PCIe 傳輸在多 stream 下仍有未建模的延遲成長（大尺寸區間已驗證修復，小尺寸區間相反方向的殘留效應是新發現，不在本階段四項缺陷登記範圍內）；moe_replay 的 `cpu_calls`/`expert_tokens` 兩種量測慣例之間有約 8 倍的正規化落差，尚未釐清換算關係。
- SWAP-K2 的 KV 事件 `block_size=0`，位元組帳目由 runtime shape/dtype 推導；治理已開放 KV 效能主張，但此資料品質限制不因此消失。

## 下一步（2026-08-19 統籌 session 排定）

A2 完成後，**現在可以同時開兩個 session，兩者無相依、無檔案衝突、都不需要 GPU：**

| session | 指引 | 為什麼 |
|---|---|---|
| **A3** IR → 引擎 loader | `docs/session_guides/STAGE_A3_IR_TO_ENGINE.md` | 前置（A2 COMPLETE + make test-cpp 14 CTest）已滿足。在關鍵路徑上，解鎖下游最多（B1/B2 前置皆為 A3，且為 A4 前置之一）。目前最高槓桿 |
| **PREP-2** GPU 前置軌第二階段 | `docs/session_guides/TRACK_GPU_PREP.md` | 之前 blocked on A2 schema，現已解鎖。依 A2 IR 評估點 schema 定案探針輸出欄位、移除 PENDING_A2，封頂 GPU 就緒度。GPU endpoint 是唯一有時限資源 |

**關鍵路徑** `A2(DONE) → A3 → B2 → C1 → C2`，且 C1 需要 A4、A4 需要 A1 closure + A3 + **GPU endpoint**。CPU 端可推進至 A3 → B1/B2；之後全部瓶頸收斂到單一 GPU endpoint（A1 closure / A4 / TRACK_GPU）——**GPU endpoint 是當前唯一的關鍵路徑瓶頸**。

**待 owner 確認**：目前無可用 GPU endpoint。A1 closure、A4、TRACK_GPU 全部 gated 在它上；窗口計畫已備（`experiments/specs/gpu_measurement_window_plan_v1.md`）。需 owner 確認 endpoint 是否/何時可得。

權威狀態與本次驗收記錄見 `governance/stage_ledger.yaml` 的 `orchestrator:` 區塊。
