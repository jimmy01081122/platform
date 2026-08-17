# AGENT_HANDOFF

最近一次 session 的交接記錄。**每個 session 完工時把新記錄加在最上面**，不刪除舊記錄。

跨 session 的權威狀態是 `governance/stage_ledger.yaml`；本文件是人類可讀的敘述補充。**衝突時以 ledger 與實際重跑的指令為準**，不以本文件的敘述為準。

---

## 2026-08-18 · 第三方 routing 語料稽核與補抓

**Session 目標** 確認 `core12345/MoE_expert_selection_trace` 的可使用性與完整性；owner 懷疑先前有不完整或錯誤使用。

**方法** 全資料集 metadata 掃描（不下載內容；以已知 60 檔建立 `bytes = a×prefill + b` 換算，四模型 R²=1.0000）+ 本地已轉換資料逐項核對 + live API 與登記結構比對。

**七項缺陷，六項已修正**

```text
1 取樣覆蓋 0.06%，3 個 benchmark 完全未碰   已修正（補抓）
  根因：hf_sample_download.py 用 sorted(subjects)[:N]，永遠只取字母序第一個
2 自訂 k*=14 但僅 1/11 cell 達標             已修正（21/21）
3 Kimi-K2 hold-out 僅 6 檔卻宣稱 validated   已修正（62 檔），H5 宣稱待重跑後重評
4 Llama top_k=1、24/48 MoE 層，不宜並列      已標註，待 C1 處理
5 n=3 時跨 benchmark 變異 >= 效應量           資料已補，待 C1 重跑
6 全資料集最長 query 約 721 tokens            無法修正，資料集本身缺乏
7 dataset_structure.json 分頁截斷             已修正
```

**缺陷 6 是定案結論**：prefill 上限 593（Qwen mmlu/professional_law）+ decode 硬上限 128 = 約 721 tokens，距 1M context 約 1,386 倍。專案自產的 Mixtral 量測是 159 tokens。**因此本專案全部 routing 證據，第三方與自產加總，皆限於約 150–721 tokens。長上下文只能自己量。**

**缺陷 7 的實際危害**：登記記 Kimi mmlu 45 科、實際 57 科（缺的 12 科在字母序上連續，是分頁截斷）。這使補抓時 Kimi 的 `professional_law` 被**靜默跳過**——WARN 只進 stderr，程序仍 exit 0。若非逐 cell 核對不會被發現。已修正並補抓。

**補抓結果** 60 → 354 檔、147 → 805 MB（政策上限 10 GB 的 7.9%）；cell 達標 1/11 → 21/21；新增序列長度軸（`professional_law`，prefill 比 `abstract_algebra` 長 4–6 倍）與工作負載類型軸（`aime_2024`）。

**新增／修改**
```text
新增 docs/status/EXTERNAL_CORPUS_AUDIT_20260818.md
     configs/sampling/round3_{convergence_topup,long_prefill,math_reasoning}.json
修改 scripts/hf_sample_download.py（新增可選 subjects 欄位，向後相容）
     data/registry/{dataset_structure,hf_downloads}.json
     PLATFORM_FLOW_SPECIFICATION.md §10.3、docs/session_guides/STAGE_{A2,C1}_*.md、dse/README.md
     docs/status/{CURRENT_STATUS,DECISION_LOG,ASSUMPTION_REGISTER,VALIDATION_MATRIX}.md
```

**降級處置** 語料衍生的 13 份 `w3_*` 結果全部標註 `n=3 per cell, below own k*=14, pending C1 re-run`，已在規格 §10.3、C1 指引、`dse/README.md` 三處加註。**補抓只解決資料可得性，不自動修正已發表結論。**

**新增決策** P-010（稽核、補抓與登記修正）。**新增假設** PA-301–304（語料事實）、PA-311–313（待重跑驗證）。

**這份語料的保留價值** 它是唯一能提供**架構規模**證據的來源：expert 物件數 3,072–23,040，而自產量測的 Mixtral 只有 256（90× 差距）。直接決定 `HW0-RESIDENCY-IDENTITY-WIDTH`、`HW0-METADATA-CAPACITY`、`HW0-METADATA-BANDWIDTH` 三個 row。**若規格只從 Mixtral 推導，會為現代 MoE 中最小的一個定規格。**

**仍然禁止的主張** 任何基於未重跑 `w3_*` 數字的結論；任何長上下文主張；calibrated PASS；break-even；accelerator 收益。

**下一個最高資訊增益的動作** A2（不受本次影響，可立即開始）或 GPU 軌（長上下文量測已升級為唯一來源）。C1 開始時必須先重跑全部 `w3_*`。

---

## 2026-08-17 · Stage A1 · Calibration 模型形式修復與重擬合

**Session 目標** 修復四項已知模型形式缺陷、用既有微基準重新擬合，不做 held-out 判定。

**進入檢查** 全數通過：`make verify-evidence` 4423/4423；`make test` 317 Python + 14 CTest、0 失敗；`make doctor` `workspace_contract: pass`；`STAGE_A1` ledger 狀態為 `NOT_STARTED`；5 個必要輸入檔案全部存在。

**事前登記** `experiments/specs/cal_model_form_repair_v1.yaml`（建立於 2026-08-17T17:25:08Z，補一則 addendum 於 2026-08-17T17:31:56Z），皆早於 `calibration/fits/v2/` 的任何輸出。

**四項缺陷處理狀態**

```text
缺陷1 contention 施加位置   已修正並在大尺寸(88MiB)驗證（streams 1/2/4 預測誤差<0.2%）；
                            但發現小尺寸(64KiB)區間有相反方向的殘留效應（新缺口，見下）
缺陷2 component shape       4 個 op 中 3 個（grouped_gemm/gather_scatter/selected_expert）
                            收斂為仿射回歸；dequant 回歸非物理（斜率不穩定），
                            退回 flat model 並標注 model_form=flat_fallback_non_physical_slope
缺陷3 MoE replay batching   與缺陷2 共用同一組 tokens_per_launch 仿射模型；額外發現並修正
                            一個未登記的雙重計算問題（見 addendum：contention 乘數與
                            tokens_per_launch 對同一個 concurrency 效應算了兩次）
缺陷4 小尺寸傳輸 floor      已修正（floor_ms 取自 Aug-11 XFER-L0 4KiB 穩定樣本）
```

**FIT 側殘差**（`calibration/fits/v2/residual_report.json`，全部為 FIT 側，非 held-out）

```text
component_latency       304.418% -> 20.324%
pcie_transfer_latency    66.879% -> 19.821%
moe_replay_tpot         293.936% -> 43.176%
moe_replay_throughput    60.658% -> 75.898%（tpot 改善的 reciprocal 轉換，方向仍是改善）
```

**未收斂／新發現的缺口**（`calibration/fits/v2/measurement_gaps.json`，6 項，全部餵給 `TRACK_GPU` 優先序第 4 項）dequant 需要 weight-bytes 量測維度；聚合 contention 模型未被任何評估點實際使用過；floor_ms 樣本數少；component_latency 評估點原始 schema 缺 shape 特徵（本階段靠 join 補回）；moe_replay 的 `cpu_calls` 與 gpu_service 探針的 `expert_tokens` 之間有約 8 倍正規化落差，換算關係未明；小尺寸 PCIe 傳輸的 stream 交互作用與大尺寸相反，未建模。

**驗證** `make test` 仍為 317 Python + 14 CTest、0 失敗；`make verify-evidence` 仍 4423/4423；舊 `rtx-q1-validation-report.json` 未被修改（`git status --short evidence/` 為空）；非物理解拒絕仍生效（`calibration/fits/v2/self_check.json`：三個刻意構造的非物理案例全部被拒）。

**變更檔案** `src/edgeflow/calibrated_backend.py`（只改模型形式函式：`_pcie_latency`、`_component_latency`、`fit_parameters` 的 gpu_service 段落、新增 `_operand_tokens` 輔助函式；`load_evidence`/`evaluate`/`evaluate_split`/split 隔離/sha256 驗證等既有保護未動）；新增 `calibration/refit_v2.py`、`calibration/fits/v2/{parameters,residual_report,self_check,measurement_gaps}.json`；`experiments/specs/cal_model_form_repair_v1.yaml`；`runs/20260817T172500Z__stage_a1_calibration_model_form_repair/`；`governance/stage_ledger.yaml`（僅 STAGE_A1 列）；本五份 status 文件。

**已知限制** 依授權邊界，`tests/` 不在本階段可修改範圍內，因此非物理解拒絕的回歸驗證是用 `calibration/refit_v2.py` 內嵌的 runtime self-check（寫入 `self_check.json`）完成，不是新增的 pytest。若 owner 希望有正式 pytest 覆蓋，需要額外授權修改 `tests/`。

**仍然禁止的主張** 任何 calibrated PASS（判定屬 A4 sealed held-out）；以本階段 FIT 側殘差作為 held-out 證據；任何 break-even、accelerator、長上下文或高並發主張。

**下一個最高資訊增益的動作** A2（measured raw → 九類 Canonical IR，與 A1 無依賴、可獨立排隊）或啟動 GPU 量測軌以縮小上述 6 項缺口（優先序第 4 項）。

---

## 2026-08-18 · Stage 0 收尾與 session 指引系統

**Session 目標** 補完 Stage 0 遺留缺口，建立跨 session 的交接契約。

**新增**

```text
governance/stage_ledger.yaml
docs/PHASE_NAMING_MAP.md
docs/status/{CURRENT_STATUS,AGENT_HANDOFF,DECISION_LOG,ASSUMPTION_REGISTER,VALIDATION_MATRIX}.md
docs/session_guides/README.md
docs/session_guides/STAGE_{A1,A2,A3,A4,B1,B2,C1,C2}_*.md
docs/session_guides/TRACK_GPU_MEASUREMENT.md
```

**移動** `governance/{charter,evidence_levels,capability_registry}.yaml` → `project/`

**修改** `PLATFORM_FLOW_SPECIFICATION.md`（§0 權威來源路徑、§12 改為實際結構、新增 §13.1 每階段開新 session）、`README.md`

**執行的檢查**

| 指令 | 結果 |
|---|---|
| `make doctor` | `workspace_contract: pass`（修復前為 `missing_required_paths: project/charter.yaml`） |
| `make verify-evidence` | 4423/4423 通過 |
| `make test` | 317 Python + 14 CTest，0 失敗 |

**修正的問題**

`make doctor` 先前失敗：遷移時把 `project/*.yaml` 移到 `governance/`，但 `scripts/projectctl.py:15` 仍要求 `project/charter.yaml`。實測 4 個檔案引用 `project/`、僅 1 個引用 `governance/`，因此還原路徑並改該份文件（決策 P-008）。

**新增假設** 無新的 `measured` 假設。新增 4 條 `assumed`（PA-101–PA-104），全部標明須由 A1/A3/A4 驗證，不得作為結論。

**仍然禁止的主張** 任何 calibrated、break-even、accelerator 主張；任何長上下文或高並發效能主張。

**下一個最高資訊增益的動作** Stage A1：修四項模型形式缺陷並重擬合。這是純 CPU 工作，重擬合所需的 component 與 transfer 微基準已在 `evidence/measurement_backups/`。**動手前必須先寫 `experiments/specs/cal_model_form_repair_v1.yaml` 事前登記**（規格 §7.2）。

---

## 2026-08-17 · Stage 0 遷移

**Session 目標** 從凍結的來源 workspace 建立 `/home/a/platform`。

**結果** 18 GB → 610 MB；4423 個證據檔逐檔 checksum 驗證一致（差異 0）；317 Python + 14 CTest 全綠；commit `01e54ce` 已推送。

**排除項** 439 MB 第三方 HF 語料、16.7 GB vendored 環境與模型權重、`governance/history` 遞迴快照、生成的 netlist 與工具 log。理由與完整對照見 `governance/lineage.yaml`。

**解決的既有失敗** 來源 repo 有 45 個測試失敗，根因僅是依賴釘選未安裝（jsonschema 3.2.0 vs 需要 4.24.0）加上 `governance/history` 遞迴快照打壞 pytest collection。裝上釘選版本並排除快照後全部消失。

**遷移中造成又還原的兩個回歸**

1. 把 `explorations/moe_cycle_simulator` 改名為 `simulator/` — 119 個非證據檔引用該路徑，含 governance JSON（其雜湊是竄改偵測機制），另有 2 個 evidence 檔記錄該路徑。已還原（決策 P-002）。
2. 把 `src/edgeflow` 攤平為 `edgeflow` — repo root 解析多跳一層變成 `/home/a`。已還原（決策 P-003）。

**發現的必要條件** `PYTHONDONTWRITEBYTECODE=1` 不可省略：`phase7_d0_r4` 的治理測試斷言 application package 的精確檔案集合，自動產生的 `__pycache__` 會讓 19 項測試失敗。已寫入 Makefile。

**遺留缺口（已於 2026-08-18 補完）** `make doctor` 失敗；`docs/status/` 五份文件不存在。
