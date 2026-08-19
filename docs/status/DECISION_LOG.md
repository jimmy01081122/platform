# DECISION_LOG

新平台 `/home/a/platform` 的決策記錄。編號 `P-nnn`（platform）以與舊 repo 的 `D-nnn` 區隔。

舊 repo 的 `D-010`…`D-119` 屬舊契約，**不自動繼承**；需要沿用時必須在此重新登記並說明適用理由。

---

## P-001 · 建立新平台，舊 repo 凍結為 evidence-of-record

**日期** 2026-08-17
**決定** 以 `/home/a/platform`（`platform.git`）為唯一開發線；舊 workspace（`dis.git` @ `e804b163`）凍結唯讀，作為原始證據與稽核來源。
**理由** 舊 workspace 18 GB 中僅約 3% 是不可再生資產，其餘為 vendored 環境、模型權重、HF cache 與遞迴快照；且舊 repo 累積四套並存的階段命名與大量舊契約 status 文件，冷啟動極易誤判。
**後果** 新 repo 需自帶 lineage；已建立 `governance/lineage.yaml` 與兩份 SHA-256 manifest，遷移經 4423/4423 檔 checksum 驗證。

## P-002 · 保留來源目錄路徑，不採用規格提案的 `ir/` `engine/` 分層

**日期** 2026-08-17
**決定** `explorations/moe_cycle_simulator/phaseN` 與 `src/edgeflow` 路徑原樣保留。
**理由** 實測 119 個非證據檔引用該字面路徑或模組路徑，其中含 governance JSON——那些雜湊正是專案偵測竄改的機制；另有 2 個 evidence 檔記錄該路徑且不可修改。phase 名稱另外烘進 CMake target（`moe_sim_phase3`）與 C API 符號（`moe_phase3_engine_create`）。改名代價遠大於收益。
**後果** 規格 §12 改為描述實際結構；概念分層記於 `governance/lineage.yaml` 的 `conceptual_mapping`。

## P-003 · `src/` 層必須保留

**日期** 2026-08-17
**決定** 還原 `src/edgeflow`，不攤平為 `edgeflow`。
**理由** repo root 解析以固定層數向上走，少一層會把 root 解成 `/home/a`，導致 schema 查找失敗（2 個測試失敗）。`pyproject.toml` 亦宣告 `packages.find where = ["src"]`。
**後果** 這是遷移中我造成的回歸，已還原並驗證。

## P-004 · Accel-Sim / detailed GPU cycle model 不納入

**日期** 2026-08-17
**決定** 不納入，登記為 deferred capability。
**理由** (1) 現有量測顯示此 regime 搬運受限（每 336 MiB expert 物件 H2D 12.49 ms），`TOOL_SELECTION.md` 已記錄 gem5 因 transfer-bound margin 79–8460× 而 deferred，同一論證適用；(2) GPU 常駐可用，kernel 時間直接量測精度遠高於模擬；(3) 舊 repo 的 `detailed_gpu/` 只剩兩個孤兒 `.pyc`，原始碼從未進版控；(4) 規格禁止詳細 compute 模型成為 v1 前置阻塞。
**重啟觸發條件** 某候選 function 的 break-even 取決於 GPU SM 級競爭，且該競爭無法以實機量測區分時。

## P-005 · 舊量測資料一律降級為 FIT

**日期** 2026-08-17
**決定** `evidence/` 中所有既有量測只能作 FIT 與模型開發用；held-out 主張一律需要新量測。
**理由** 診斷 calibration 模型形式缺陷時檢視過 validation split 的殘差，獨立性已受汙染。資料集內部既有的 `fit_role`（含 `HELD_OUT`、`CONTROL`）在本平台不得作為 held-out 宣稱。
**後果** 建立 sealed held-out 協定（規格 §7）：split 先 hash 封存、模型凍結後只開封一次、開封次數必須可稽核。

## P-006 · KV 開放效能治理，並列為候選加速目標

**日期** 2026-08-17
**決定** 解除舊 repo 對 `SWAP-K2/K3/K5 performance or serving benefit` 的 claim 禁令；KV 同時列為候選加速目標（掛載點 A5/A6），優先序次於 routing/dispatch/transfer/decompression 線。
**理由** owner 裁決。KV 是長上下文的容量強制因素（Mixtral 每 token 128 KiB，1M context 需 128 GiB，超過 96 GB VRAM），必須能被建模與比較。
**限制** 治理放寬不改變資料品質：SWAP-K2 事件 `block_size=0`，位元組帳目由 runtime shape/dtype 推導，故 KV 時序仍須靠已校準的 PCIe 模型或新量測，不得宣稱來自 K2/K3。

## P-007 · 每階段開新 session，交接以可機器驗證的 ledger 為準

**日期** 2026-08-18
**決定** A1–C2 與 GPU 軌各自開獨立 session；建立 `governance/stage_ledger.yaml` 為單一狀態真相來源，每份 session 指引的進入檢查是具體指令與預期輸出。
**理由** 避免上下文汙染與長對話造成的判斷劣化。舊 repo 的失效模式是敘述性 ledger 過期——`active_gpu_guard` 仍指向已失聯的 EXT10K attempt、`next_gpu_unit_after_inflight` 停在 guard 執行前的舊值，接手者需額外一輪才能釐清。
**後果** 冷啟動 session 不得以前一份報告的敘述作為依據，必須重跑進入檢查指令。

## P-008 · `project/` 保留章程，`governance/` 專責溯源與階段狀態

**日期** 2026-08-18
**決定** `charter.yaml`、`evidence_levels.yaml`、`capability_registry.yaml` 還原至 `project/`；`governance/` 保留 `lineage.yaml`、`lineage/`、`stage_ledger.yaml`。
**理由** 遷移時誤移致 `make doctor` 失敗（`scripts/projectctl.py:15` 要求 `project/charter.yaml`）。實測 4 個檔案引用 `project/`（projectctl、w3_mem_recheck、TOOL_SELECTION、edgeflow/`__init__`），僅 1 個檔案引用 `governance/`（新寫的規格）。改 1 個文件優於改 4 個程式與方法論文件。
**後果** `make doctor` 回復 `workspace_contract: pass`。

## P-009 · component_latency 的 contention 乘數在 shape-aware 評估點上停用

**日期** 2026-08-17
**決定** `_component_latency()` 新增規則：當評估點的 `features` 帶有 `tokens`（即 STAGE_A1 新增的 tokens_per_launch batching 模型已生效）時，不再套用 `contention.per_extra_concurrency` 的全域乘數；只有在完全無 shape 訊號時才沿用舊制。
**理由** STAGE_A1 修復缺陷 2/3（component shape + batching）之後執行殘差分析，發現 decode phase、concurrency=4 的點被系統性高估 5–6 倍。追查得知 `contention.per_extra_concurrency`（≈1.0046）是從一個「shape 固定、只有 concurrency 變化」的探針擬合出來的，代表真正的資源競爭效應；但在新的 batching 模型裡，concurrency 同時也驅動了 `tokens_per_launch` 的成長，兩者疊加即是對同一個 concurrency 效應算了兩次。這是事前登記之外新發現的模型形式問題，依規格 §8 的規則先補登記（`experiments/specs/cal_model_form_repair_v1.yaml` 的 `addenda` 區塊，時間戳早於任何受影響的擬合輸出）再改程式。
**後果** `moe_replay_tpot` 的 FIT 側 MAPE 從 174.7%（只修 shape、未修雙重計算）降到 43.2%；`component_latency` 從同一批修正前的高估降到 20.3%。**未完全解決**：目前無法把「batching 已解釋的 concurrency 效應」與「剩餘的純資源競爭」分離，因為 contention 探針只有 1 個樣本。已記錄為量測缺口（`calibration/fits/v2/measurement_gaps.json` GAP-2），餵給 GPU 軌量測優先序第 4 項。

## P-010 · 第三方 routing 語料稽核、補抓與登記修正

**日期** 2026-08-18
**決定** 對 `core12345/MoE_expert_selection_trace` 執行完整稽核；補抓語料使全部 cell 達到專案自訂的 k\*=14；修正 `dataset_structure.json` 的探查缺漏；為 `hf_sample_download.py` 新增可選的 `subjects` 指定欄位。

**理由** 這是 `evidence/` 之外唯一的大型資料來源，全部 `w3_*` DSE 結論建立其上。稽核發現七項缺陷，其中三項具決策影響：

1. **專案自訂 k\*=14，卻只有 1/11 個 cell 達標**，其餘全部 n=3（低 4.7 倍）。這是自訂標準與實際執行的落差。
2. **全資料集最長 query 約 721 tokens**（prefill 上限 593 + decode 硬上限 128），距 1M context 三個數量級。這無法靠補抓解決。
3. **`dataset_structure.json` 探查分頁截斷**，Kimi-K2 的 mmlu 登記 45 科實際 57 科，低估 4,421 檔；且此缺漏導致補抓時 Kimi 的 `professional_law` 被**靜默跳過**（WARN 只進 stderr，exit code 仍為 0）。

**後果**
- 語料 60 → 354 檔、147 → 805 MB；cell 達標 1/11 → **21/21**。
- 新增兩條分析軸：序列長度（`professional_law` 的 prefill 比 `abstract_algebra` 長 4–6 倍）與工作負載類型（`aime_2024` 數學推理，先前完全未涵蓋）。
- `total_json_files` 修正為 103,961；修正記錄寫入該檔的 `corrections` 欄位。
- **補抓只解決資料可得性，不自動修正已發表結論。** `data/canonical/moe_routing_v1/` 下 13 份 `w3_*` 結果仍是舊樣本產物，必須由 C1 重跑。在重跑前，所有引用一律標註 `n=3 per cell, below own k*=14`。

**限制** 長上下文證據（缺陷 6）無法由本語料提供，只能自行量測。此決定使 GPU 軌的長上下文量測從「資訊增益最高」升級為「唯一能支撐長上下文論點的來源」。

完整稽核見 `docs/status/EXTERNAL_CORPUS_AUDIT_20260818.md`。

## P-011 · GPU 量測軌拆為前置準備（純 CPU）與實機量測兩段；新增統籌 session

**日期** 2026-08-18
**決定**
1. 把 GPU 量測拆成 `TRACK_GPU_PREP`（純 CPU、無前置，凍結 contract、寫探針、測 parser、封存 sealed split、追 GAP-5）與 `TRACK_GPU`（拿到 endpoint 後純執行）。量測軌以「contract 已凍結」為前置，例外是優先序第 4、5 項（A1 已完整規格化，可先跑）。
2. 新增 `SESSION_ORCHESTRATOR.md` 與 ledger 頂層 `orchestrator:` 區塊，作為排程與跨 session 驗收的統籌 session。它不執行階段工作、不改 `stages:` 任何一列，只獨立重跑 verification 並整理 owner 裁決事項。

**理由**
- **GPU endpoint 是唯一有時限的資源**，其他都不是。把設計工作留到窗口內做會浪費窗口。盤點五項量測的就緒度發現它與資訊增益順序幾乎相反：優先序 1（A2 dispatch 系統層搬運）、2（A6 長上下文，全倉庫零命中）要寫最多程式，而 4、5 今天就能跑。拆軌讓窗口只剩執行，且量測軌的 session 可維持短上下文（繼承凍結的 contract，不重新推導設計）。
- **GAP-4 是前例**：component_latency 評估點不帶 `expert_tokens`，A1 只能 join 回原始記錄。成因是量測 schema 先於消費（IR）schema 凍結。因此前置軌切成 PREP-1（不依賴 A2）與 PREP-2（用 A2 的 IR 評估點 schema 定案輸出欄位），不得自行猜欄位。
- **統籌 session** 回應 owner「避免長上下文影響判斷、另開 session 統籌」的要求。它與其他 session 讀取權限相反（可讀全部指引但只讀第 1/2/7 節），換來的約束是不得執行工作、不得改 `stages:`——否則「只有該階段 session 能改自己那一列」的 anti-contamination 規則形同虛設。最容易犯的越界是把多階段摘要串成「整體結論」，該結論無任何階段驗證卻因出自統籌 session 而顯得更權威；claim boundary 明令禁止。

**盤點時發現並修正的兩項事實**
- A2 掛載點先前記為「無任何量測」不精確：`evidence/` 有 56 筆 `gather_scatter`，但那是 `benchmark.py:463` 的同裝置合成 proxy（`index_select` 來回作用於 `torch.randn`），只給 execute 項，無跨裝置搬運。準確敘述：execute 側有合成 kernel proxy，系統層搬運與控制結構未量測。已修正 `VALIDATION_MATRIX.md`。
- GAP-5（moe_replay `cpu_calls` 與 gpu_service `expert_tokens` 約 8 倍落差）可能不需 GPU：`measurement_gaps.json` 給的第一條解法是純讀 harness 程式碼，線索在 `benchmark.py:440-441`。前置軌須先試這條再排量測。

**後果**
- 新增 `docs/session_guides/TRACK_GPU_PREP.md`、`docs/session_guides/SESSION_ORCHESTRATOR.md`。
- ledger 新增 `TRACK_GPU_PREP` 列、`orchestrator:` 區塊、規則 5；`TRACK_GPU` 新增前置與 readiness 註記。
- README、CURRENT_STATUS、VALIDATION_MATRIX、TRACK_GPU_MEASUREMENT 同步更新。
- 現在可立即並行的兩個 session：A2 與 GPU 前置準備軌。

## P-012 · 修正 calibration harness 的非物理解 fallback（P0 safety）；A1 退回 IN_PROGRESS

**日期** 2026-08-18
**決定**
1. `src/edgeflow/calibrated_backend.py` 的 `fit_parameters()` 移除 gpu_service 迴圈裡 `try/except CalibrationError` 的 fallback 分支。改為事前名單 `SHAPE_INSENSITIVE_OPERATIONS = frozenset({"dequant"})`：名單內的 op 直接以 flat mean 建模並標 `model_form="flat_by_registered_exclusion"`（labeled PROXY_ONLY）；名單外的 op 若擬合非物理（slope ≤ 0 或 intercept < 0）一律讓 `CalibrationError` 往外拋，**不再 fallback 為常數**。
2. `calibration/refit_v2.py` 的 self-check 擴充到覆蓋 **production path**：新增 `fit_parameters_non_physical_non_excluded`（非名單 op 的負斜率輸入必須使 `fit_parameters()` raise）與 control `fit_parameters_excluded_operation_is_flat_not_error`（dequant 走 flat 不 raise）。`self_check.json` 新增 `production_path_covered: true`。
3. `governance/stage_ledger.yaml` 的 STAGE_A1 由 `COMPLETE` 退回 `IN_PROGRESS`；`VALIDATION_MATRIX.md` 把「拒絕非物理解 = PASS」改為 IN_PROGRESS 並註明先前是假 PASS。

**理由** 2026-08-18 Principal Reviewer 獨立覆核（用 raw evidence 重現）發現一項 P0 級 calibration-safety 違約：root spec §8.3 與 `calibration/README.md` 明文承諾 harness「拒絕非物理擬合、無 fallback 常數」，但 `fit_parameters()` 對 gpu_service 的仿射回歸包了 `try/except`，`_line_fit` 判定非物理後被 catch 並改回 flat 常數（dequant 即踩到此路徑）。原 `self_check.json` 只直接呼叫 `_line_fit()`，沒有測 `fit_parameters()` 這層 wrapper，因此無法證明——事實上也沒有證明——production path 會拒絕非物理解。以執行驗證：修正前，一組負斜率的 gpu_service 輸入使 `fit_parameters()` 回傳 `model_form=flat_fallback_non_physical_slope` 而**不 raise**；修正後同一輸入正確 raise `CalibrationError`。

**後果**
- 名單機制與 catch-fallback 的差別在於**意圖可稽核**：dequant 是事前宣告的 shape-insensitive proxy（raw 標記 `synthetic_symmetric_int4_proxy_not_checkpoint_awq`），不是一個「試過仿射、失敗、退回常數」的 op。數值上 dequant 結果不變（仍是 flat mean、per_token=0），只是 `model_form` 改標為 `flat_by_registered_exclusion`。其餘三個 op（grouped_gemm/gather_scatter/selected_expert）本來就物理，不受影響；`calibration/fits/v2/*` 的四項 FIT 側 MAPE 數字完全不變。
- `make test` 仍 317 Python + 14 CTest、0 失敗；`tests/test_calibrated_backend.py` 既有 7 項仍全過；evidence 未動（4423/4423）。
- **A1 暫留 IN_PROGRESS**：本輪只修 P0 safety 與治理措辭，尚未做 reviewer 接受的 v2 重擬合（PCIe two-regime、component predictor 的 prefill-only 門檻、MoE replay 的顯式 routing/permutation operator）。這些屬 `cal_model_form_repair_v2` preregistration，須先登記再重擬合。本輪不宣稱任何 calibrated PASS。

**Reviewer 覆核的其餘結論（全部 FIT 側 / diagnostic，非 held-out；記錄供 v2 preregistration）**
- **PCIe two-regime** `T=max(alpha_d+beta_d·(S-1), A_d+B/BW_d)`：ACCEPT。自 raw 獨立重現 30 點 1.04% MAPE（小尺寸 48.8%→2.5%）。`benchmark.py:316-334` 證實 `copy_streams`＝固定總位元組切 S 塊、S streams、wait-all，故舊 q0 的 per-transfer 乘數語意錯誤、現行 A1 對小尺寸也不對。待定：production 端 stream 語意。
- **ProfileKNN**：MORE_TESTS。aggregate 10.9% 被 18/18 decode 精確 token lookup 灌水；prefill 內插為 18.2%（＞15%）。
- **MoE replay operator graph**：`window_replay()` 實測含 2 次 argsort＋3 GEMM＋gather＋scatter，登記 metadata 只記 grouped_gemm＋gather_scatter，系統性遺漏 routing/sort 項。固定 `tau_route` 僅 2 結構點 → DIAGNOSTIC_ONLY；正式版改顯式 operator。
- **routing 守恆** Σn_e＝num_tokens×top_k：672 步 0 違反（CONFIRMED）。
- **dequant**：維持 `DEQUANT_PROXY_ONLY`，不得升格為真實 AWQ dequant。

**未做（明確留給後續）** `tests/` 目前不在 A1 授權範圍，故 production-path 覆蓋以 `calibration/refit_v2.py` 內的 runtime self-check 完成；若要正式 pytest 覆蓋需 owner 額外授權。

**Follow-up（2026-08-18，同 P0 主題的第二個出口）** 同一契約漏洞還有第二個出口：single-shape fallback。`fit_parameters()` 對名單外 op 若只有 <2 個 distinct shape 值，原本退回 `flat_fallback_single_shape_group` 常數——與 non-physical fallback 同一類違約。現移除該 production fallback：名單外 op 若 distinct shape axis < 2，一律 raise `CalibrationError`（真正無 shape 依賴的 op 必須顯式加入 `SHAPE_INSENSITIVE_OPERATIONS`，不得靜默降級）。self-check 新增 `fit_parameters_single_shape_non_excluded`（必須 raise），保留 negative-slope／negative-intercept raise 與 dequant exclusion no-raise control。**owner 授權**下最小更新 `tests/test_calibrated_backend.py` 的合成 fixture（原用單一 shape 的 selected_expert 探針，現改兩個 distinct shape 點；第一點 `expert_tokens=1` 對齊 component 評估點的 `tokens_per_launch=1.0`，使既有 MAPE 斷言完全不變）。驗證：`make test` 317 Python + 14 CTest、0 失敗；`self_check.json` 六個 case 全綠（含新 single-shape）；`make verify-evidence` 4423/4423；evidence 未動；真實校準資料上三個仿射 op 皆多 shape，無 spurious raise，四項 FIT 側 MAPE 數字不變。A1 維持 IN_PROGRESS；cal_model_form_repair_v2 preregistration 完成前不得實作或 refit PCIe／KNN／replay 新模型。

## P-013 · cal_model_form_repair_v2 實作 + FIT 側評估結果（A ACCEPT / B INSUFFICIENT / C BLOCKED）

**日期** 2026-08-19
**決定** owner 核可三個 review points 後，實作三候選並在既有 evidence 上做 FIT 側評估（`calibration/models_v3.py`、`calibration/refit_v3.py`；輸出 `calibration/fits/v3/`；run `20260819T160456Z__stage_a1_cal_model_form_v2_fitside_eval`，manifest `code_commit=528ae01…`）。全部 FIT 側 / diagnostic（P-005），無 calibrated PASS。裁決依 prereg 事前判準，且**評分順序：correctness → subgroup gates → generalization → MAPE → aggregate**，低 aggregate 不得蓋過 subgroup gate。

**Candidate A — PCIe two-regime：ACCEPT（FIT 側）**
- 自 raw 擬合並評估 30 點：aggregate **1.038%**、small **2.508%**、bulk **0.058%**、h2d 0.875%、d2h 1.201%、max APE 4.752%。三方對照：v1 stored 19.821%、old q0 stream-factor 66.879%（後者 bulk 竟 84%，因把 bulk 乘上 stream factor——證實舊 per-transfer 乘數語意錯誤）。
- 物理約束 hard-fail（無 clamp）已實作並測試：A<0 / BW≤0 / alpha<0 / beta<0 一律 raise。
- production stream 語意（OWNER_RESOLUTION）：單一 object S=1 → 336 MiB expert 預測 **12.449 ms**（與實測 ~12.45 ms H2D 相符）；S>1 → **UNSUPPORTED**。multi-object concurrency 不借用 S。

**Candidate B — component ProfileKNN：INSUFFICIENT_EVIDENCE（不升格）**
- 嚴格照 prereg：k=3、log-token、operation/phase hard partition。跑了 LOOWO、LOSRO、k-sensitivity、distance-sensitivity、bootstrap。
- **關鍵 generalization 結論**：leave-one-workload-out 下，prefill per-op MAPE = selected_expert **41.2%**、grouped_gemm **39.4%**、gather_scatter **17.6%**（三者皆 ≥15%），且 KNN 在 selected_expert / grouped_gemm 上**比 global_affine 還差**（affine 33.1% / 33.8%）。換言之：同 workload 的 q0→q1 診斷優勢（aggregate 10.9%）**無法 generalize 到未見 workload**——這正是「shape locality vs lookup-table overfit」問題的答案：是 overfit。
- 依 owner 指示（gate 未達 → INSUFFICIENT_EVIDENCE，不調 k / 不挑 workload / 不改 threshold）判 INSUFFICIENT；ProfileKNN **不升格為正式 component 模型**。generalization_warning 明載「meets prereg reject_if；因 3-workload LOOWO 過稀，記 INSUFFICIENT 而非 REJECT，實務效果相同」。decode 的低 MAPE（0.96–4.34%）為 exact-token lookup（LOOWO 36 個 decode 點全 exact），已單獨揭露，不得用來補 gate。dequant 不進 component aggregate。
- 開 **V2-GAP-B**（更密 prefill shape sweep）。

**Candidate C — replay：BLOCKED_ON_MEASUREMENT / INSUFFICIENT_EVIDENCE**
- 顯式 operator graph 已對齊 `window_replay()`（2×argsort + 3×GEMM + gather + scatter）。登記 metadata 只記 grouped_gemm+gather_scatter，系統性遺漏 **argsort_route、argsort_inverse**。
- 既有 evidence 無法識別這兩個 sort 項 → replay **不能 FIT-closed**。固定 tau（僅 2 個結構配置）維持 **DIAGNOSTIC_ONLY**（tau=0.0738，tpot 43.18%→1.25%，但那是 2 點擬 1 參，不算）。throughput 一律派生 1000/TPOT。
- 開 **V2-GAP-C**（sort/permute microbenchmark）。

**Candidate D — dequant：PROXY_ONLY**（不變）。

**後果**
- **A1 續留 IN_PROGRESS。** closure 明確 blocked 在 V2-GAP-B 與 V2-GAP-C 兩個 targeted FIT-side 量測上。依 OWNER_RESOLUTION，這類 targeted FIT-side 量測日後可開，但**資料不得與 A4 sealed held-out 混用**。PCIe two-regime 已 ACCEPT，可先整合（本輪未動 `calibrated_backend.py`，以保 v2 可重現）。
- **run manifest provenance 修正**：採兩段 commit——先 commit 生成程式（`528ae01`，H1），再於乾淨 H1 tree 執行產生 artifacts，manifest 記 `code_commit=H1` 並附兩支腳本的 SHA-256，artifacts 於下一個 commit（H2）加入。徹底解決先前「manifest 指向不含生成程式的 parent commit」的問題。
- 基線：331 Python（tests/ 96→110，+14 v3 production-path regression tests）+ 14 CTest、0 失敗；evidence 4423/4423 未動；`make doctor` pass。

---

## P-014 · TRACK_GPU_PREP PREP-1 完成；探針依賴欄位標 PENDING_A2 不猜

**日期** 2026-08-19
**決定** 執行 GPU 量測前置準備軌 PREP-1（純 CPU）。凍結五項量測 contract、從零寫兩支探針（長上下文 KV、in-serving dispatch）並在 CPU mock backend 上 smoke test、寫 5 個 parser/validator 各附正常+失敗 fixture、設計並封存 A4 sealed held-out split、由讀程式碼解決 GAP-5、產出窗口計畫。**凡輸出欄位依賴 A2 的 IR 評估點 schema 者，一律標 `PENDING_A2`，不自行猜測**——GAP-4 正是量測 schema 先於消費 schema 凍結所致（TRACK_GPU_PREP 硬規則 5）。

**GAP-5 結論（RESOLVED_BY_CODE）** `cpu_calls` = window_replay 的 decode-step launch 數；`expert_tokens` = 8 ×（decode token 位置數），routing_width=8 為 trace 事實（`workloads/windows.json` decode step `selected_experts` [1,8]）。8× 正規化差由「兩套 campaign 分母不同」完全解釋（benchmark.py:520-590 vs 436-441）。**殘差 1.87× 不屬 mapping 問題**：`window_replay` 在計時區內做兩次 argsort（benchmark.py:538,541），standalone 探針把 order/inverse 預算在計時外（445-446），故是**未量測的 sort/permute operator = V2-GAP-C**，需 GPU。詳見 `calibration/GAP5_LAUNCH_GRANULARITY_RESOLUTION.md`。

**理由** GPU 窗口是有時限資源；未凍結 contract 就開窗會產生無法使用的資料（GAP-4 前例）。CPU-first 讓窗口內只剩執行。PENDING_A2 切分避免對 GAP-4 原樣再犯。

**後果**
- TRACK_GPU_PREP 狀態 → `IN_PROGRESS`（PREP-1 完成；PREP-2 blocked on STAGE_A2）。
- TRACK_GPU 的 priority 4 缺口清單中 GAP-5 由 GPU 需求降為「已由程式碼解決」，僅殘餘 V2-GAP-C 需 GPU。
- sealed held-out split（102 cells）已在**任何新量測之前**封存，符合 §7.2 的不洩漏要求；STAGE_A4 開封評分一次。
- 基線：**344 Python**（tests/ 123，+13 `tests/test_gpu_prep.py`）+ 14 CTest、0 失敗；evidence 4423/4423 未動；`make doctor` pass。
- **未跑任何 GPU**：無 GPU 效能 / calibrated / break-even / accelerator / 長上下文主張。CPU smoke test 僅證明 argv/序列化/錯誤處理正確，不證明量測可行性。

---

## P-015 · STAGE_A2：OFF-E-PR3 measured → 九類 IR 的三個結構決定

**日期** 2026-08-19
**決定** 把 OFF-E-PR3 expert 容量掃描（15 點）轉成單一九類 Canonical IR bundle（MEASURED），過 phase2 IR1。三個結構決定：

1. **RoutingIR 用 AGGREGATE scope，不用 TOKEN scope。** 量測 routing `.npy` 是 uint8、只含 selected expert ids（`vllm.CompletionOutput.routed_experts`），**無 gate scores/logits**。schema 的 TOKEN scope 要求 canonical_scores（長度=experts）並由分數重導 k_boundary/ambiguity——無分數則無法誠實建構。故發 32 筆 per-layer AGGREGATE（`aggregate_expert_demand`，每層 sum=159×2=318）。byte-exact routing 仍以 `routing_sha256` 於 provenance 可回溯。per-token 排序 demand 落入 dropped-fields，A3 依凍結 traversal（token-major/layer-major/top-k、object id=layer×8+expert）從 `.npy` 重建。

2. **每個容量點各發一筆 PlatformIR**，device residency-budget domain 的 capacity = 該點 `capacity_bytes`。理由有二：(a) 忠實——這個實驗掃的就是 on-device residency 預算；(b) 讓 15 個 PlacementIR 落在 15 個不同的 (model_record_id, platform_record_id) 群組，避免 cross-IR 的 placement 版本鏈檢查把它們當成一條**不存在的** migration lineage（該檢查對同群組要求 version 遞增 + predecessor + migration events）。物理 VRAM（nvidia-smi 97887 MiB）另存為 `device_vram_physical` domain，不與預算混。

3. **claim boundary 以 content-id 綁定，不放自由文字。** provenance schema `additionalProperties:false`、無文字欄。故把限制寫進 `artifacts/claim_boundary.json`，其 sha256 進**每一筆**記錄的 `provenance.source_content_ids`——下游可查、不可洗掉，且密碼學上綁定。限制含：OFF-E-PR3 單一物件搬移代理、routing 無 scores、AGGREGATE 丟排序、placement 為 terminal snapshot、alignment 為 AGGREGATE_ONLY（ns 量化 ±0.5ns）。

**理由** 三者都是「忠實 + 讓 fail-closed 契約通過」的最小侵入解，皆不動 `canonical_ir.py`（2051 行、43 tests，預設正確）。CalibrationIR fidelity=UNAVAILABLE、無 profile hash、measured==predicted、formal_pass=False——**不宣稱任何 held-out 驗證**（§7 舊資料一律 FIT 側）；training/held_out 僅為 schema 需要的參照。

**後果**
- STAGE_A2 → `COMPLETE`（僅 OFF-E-PR3 家族）。解鎖 A3（IR→引擎 loader，SIM0 用本 bundle 的 hit/demand/discard counters）與 PREP-2（探針欄位可依本 bundle 的 IR 評估點定案）。
- 其餘量測家族（SWAP-K2/SERV-P0-25/controlled matrix/component/transfer）**未轉**；adapter 架構可延伸。
- 基線 352 Python（+8 phase7 A2 tests）+ 14 CTest、0 failed；evidence 4423/4423 未動；mock adapter sha256 未變。
- **仍禁止**：IR 已被引擎消費、任何時序/效能/calibrated/break-even/accelerator 主張、把 IR 過驗證說成研究鏈打通。

---

## P-016 · TRACK_GPU_PREP PREP-2 完成：探針依 A2 CalibrationIR schema 填實 IR 評估點欄位

**日期** 2026-08-19
**前置** STAGE_A2 COMPLETE（P-015；measured raw → 九類 Canonical IR）。
**決定** 執行 PREP-2：把 PREP-1 標為 `PENDING_A2` 的探針輸出欄位，逐項對照 A2 產出的 **CalibrationIR** 評估點 schema（`explorations/moe_cycle_simulator/phase2/schemas/canonical_ir.schema.json` `$defs.calibration`）填實。

**做法**
- 每個量測點把 operand shape **直接**放進 `evaluation_coordinate`（longctx=`[seq_len]`；dispatch=`[expert_tokens, concurrency]`），配對 `calibration_envelope`（dimensions 名稱集合須與 coordinate 相等，值為 exact-decimal 字串），並帶 `metric/unit/measured_value/evidence_class/fidelity/range_status` 等必填欄。
- dispatch 探針新增 break-even 分解欄位 `T_prepare/T_queue/T_sync/T_move`（root spec §10.4），由 mock backend 產生合成值（stamped `cpu_smoke_test_not_measurement`），使 GPU 端可直接填實測值。
- 新增 `measurement/probes/ir_evaluation_point.py`（`{longctx,dispatch}_result_to_points`：**只用探針自身輸出**建點，不 join 回 raw）與 `measurement/parsers/ir_point_validator.py`（用 jsonschema 對 A2 真實 schema 驗證）。

**驗收（guide 步驟 7）** `test_ir_points_validate_against_real_a2_schema`：探針輸出的每個 IR 點在 jsonschema 下對 A2 CalibrationIR schema 全部通過，且 operand shape 直接攜帶 → **不需要 join 回 raw 解析 case 字串**。這正是 GAP-4 的反面：GAP-4 因 component 評估點沒帶 `expert_tokens` 而被迫 join，本軌的新探針不會重犯。`component_eval_parser --enforce-gap4` 亦對既有 component 評估點強制此規則。

**claim boundary** 工作區分是：operand shape 直接攜帶（GAP-4 的主題）；workload/model/platform 的 record-id 是 IR 組裝期綁定（非 shape，非「join 回 raw 解析」），由 A2/A3 pipeline 填，CPU smoke 用 placeholder rid。mock backend 產生的 IR 點**值**是合成的，不得當實測。

**後果**
- TRACK_GPU_PREP 狀態 → `COMPLETE`（PREP-1 + PREP-2）。contract `status: FROZEN_PREP2`，五項 `output_fields` 無 `PENDING_A2` 殘留。
- TRACK_GPU 現在可直接執行：priority 1/2 探針的輸出欄位已定案且對 A2 schema 有效。
- 基線：**358 Python**（tests/ 129，PREP-2 +6 tests）+ 14 CTest、0 失敗；evidence 4423/4423 未動；`make doctor` pass。**未跑任何 GPU**。

---

## P-017 · A3：residency 位元精確走 phase5 引擎；服務時間走 phase4；phase3 kService 字面修改留 owner

**日期** 2026-08-19
**前置** STAGE_A2 COMPLETE（九類 IR bundle）。STAGE_A3 目標：IR→引擎 loader，15 點 residency counters 位元精確重現。

**決定 1（demand 順序來源）** A2 的 RoutingIR 是 AGGREGATE scope（量測 .npy 無 gate scores，per-token 有序序列被 drop，記於 A2 `dropped_fields.json`）。依 A2 交接指示，A3 從凍結 routing `.npy` 重建 10176 長度有序 demand 序列：C-order flatten，`object_id = layer*8 + expert`，`layer = (i // top_k) % layers`（token-major）。每點 `sha256(.npy)` 對回 RoutingIR provenance 才使用（唯讀 evidence，未改）。

**決定 2（引擎路徑）** 用既有 phase5 `RoutingResidencyModel`（`EvictionPolicy::kLru`、CLEAN_IMMUTABLE、byte 計價 catalog）作 residency 引擎，它驅動 phase4 `SingleGpuModel` 產生時序。新增 loader 模組（不改引擎演算法）：C++ executable `moe_sim_phase5_ir_loader`（`phase5/tools/ir_replay_loader.cpp`，link 既有 phase5）建 `PolicyPlan` 並回傳 counters/digests/timeline；Python `phase7/loaders/ir_to_engine.py` 從 A2 bundle 讀結構、產 plan spec、比對量測。counter 對映：`demand_load_count←metrics.loads`、`immutable_discard_count←metrics.clean_evictions`、`hit_count←routing_demands − loads`。

**決定 3（cap=100 退化 control）** 量測全容量點（capacity_objects=256=catalog）是 "actual all-resident control"，`demand_load_count=0`。判定規則（可從 IR 導出）：當 `device_residency_budget capacity_bytes ≥ catalog 總 bytes` → `base_resident = 全 catalog`（all-resident 初始），否則空初始快取（`DETERMINISTIC_LRU_EMPTY_INITIAL_CACHE`）。**這不是容差掩蓋**：cap=099（capacity 253）的 259 loads 已含全部 256 個 cold loads，證明 cap=100 的 0 loads 是真預載而非統計歸零。忠實對應量測 `physical_transfer_semantics: "No demand H2D: actual all-resident control"`。

**決定 4（服務時間走 phase4，非改 phase3）** guide §6 列「Action::kService 實際消耗 service_demand」。該項屬 phase3 core。phase3 有 **r5 凍結治理**：`phase3/governance/reviews/phase3_r5_model_benchmark.json` 明載「REPLAY_VALIDATE preserves and hashes service_demand but **does not derive completion latency**」，`contracts/engine_profile.json` 標 `service_demand: ACCOUNTING_ONLY` / `completion_generation: FORBIDDEN`，且 `governance/checksums.sha256` 釘住該檔。**直接改 phase3 kService 以 service_demand 推進完成時間會推翻此治理決定，屬結構性契約變更**（根規格 §0：語意衝突須停止回報、不得自選較易一方；§8：phase3–6 結構性改動須 owner）。
故本階段**不改 phase3**，服務時間經 **phase4** 接上（PHASE3_RESULT.md 自述服務時間屬 Phase 4）：每個 demand H2D load 是 phase4 H2D Operation（work=object bytes），phase4 `service_duration` 以量測 PCIe 頻寬 28298591668 B/s 轉換、單 H2D lane 串行。驗證：每物件 H2D = 90823799123064345/7295 = **12450143814087 fs**，與 A2 PlatformIR 校準 `h2d_expert_object_service_min duration_fs` 完全相符。counters 與時序無關（純順序 LRU），故此不影響 SIM0。

**OWNER 待裁決** phase3 `Action::kService` 是否要字面修改為消耗 service_demand。若要，需重開 r5 review、更新 `engine_profile.json`（`ACCOUNTING_ONLY`→consumed）與 `phase3/governance/checksums.sha256`，屬跨 review 的治理動作。本 A3 的三條 ledger verification（SIM0/SIM1/health）不含此項且全部滿足。

**後果**
- STAGE_A3 → `COMPLETE`。SIM0 15/15 位元精確、SIM1 15/15 決定性、15/15 QUIESCENT。
- 新增：`phase5/tools/ir_replay_loader.cpp`、`phase5/CMakeLists.txt`（+executable target，phase5 CTest 維持 4）、`phase7/loaders/`、`phase7/tests/test_stage_a3_loader.py`（+7 tests）。引擎原始碼（phase3–6）未改。
- run `runs/20260819T134458Z__stage_a3_ir_to_engine_replay/`（含 sim0/sim1/health/timing artifacts、每點 engine_result、environment）。
- 基線：**365 Python**（358 PREP-2 基線 + 7 A3；tests/ 129 含 PREP-2 的 +6）+ 14 CTest、0 失敗；evidence 4423/4423 未動；`make doctor` pass。**未跑任何 GPU；無任何時序準確度 / calibrated / break-even / accelerator 主張**。

---

## P-018 · B2：候選處理器建成可掃描元件 + 六動詞 ABI + 掛載點 A1-A6（全 ANALYTICAL/PROJECTED）

**日期** 2026-08-20
**前置** STAGE_A3 COMPLETE。STAGE_B2 目標（RULES_ONLY）：把候選 support processor 建成模擬器裡可掃描的第一等公民元件，並定義六個掛載點。純 CPU。

**決定 1（套件位置與 import）** `accelerator/` 建為 repo-root Python 套件（`PYTHONPATH` 含 root，`import accelerator` 可用），與 `src/edgeflow/` 平行。不改任何引擎（phase3-6）或 A2 IR bundle。

**決定 2（fidelity 硬隔離在建構時強制）** `accelerator/fidelity.py` 只允許 `ANALYTICAL` / `PROJECTED`；`require_accelerator_fidelity()` 對 `MEASURED_SURROGATE`（及 `CYCLE_ACCURATE`/`CYCLE_RESOLVED`/`EVENT_DRIVEN`/`STATISTICAL`）在建構時 raise。`AcceleratorBackend.__init__` 與 `Provenance.__post_init__` 都過此閘。理由：候選處理器無矽、無實機量測，measured-surrogate 標記是跨 fidelity 層謊報（根規格 §3.1/§14.8）。這把 guide §4.4 的硬規則從「靠審查」變成「建構時擋掉」。

**決定 3（九參數 + 時間紀律）** `resource_model.py` 的九個可掃描參數（§6.1）：`pipeline_latency_cycles`、`issue_width`、`local_sram_capacity_bytes`、`memory_bandwidth_bytes_per_s`、`queue_depth`、`operations_per_cycle`、`clock_frequency_hz`、`area_proxy_um2`、`power_proxy_mw`。時脈為**精確有理數（Fraction）**，`cycle_period_fs = floor(den*1e15/num)`，與引擎 `edge_time` 的 floor 慣例一致（§4，無浮點漂移）。`ResourceSweep` 產笛卡爾積並有 `max_points` guardrail（預設 config 積 69120 點 < 100000）。

**決定 4（六動詞 ABI + 防偽 registry）** `abi.py`：`reset`/`can_accept`/`submit`/`advance`/`poll_completions`/`snapshot_counters`（§6.3）。`BackendRegistry` 對未註冊名稱 raise `BackendNotRegistered`、不靜默替換（鏡像 `src/edgeflow/multifidelity.py` 的防偽設計，該檔**未動**）。三個下游 backend（`RTL_TRACE_REPLAY`/`VERILATOR_COSIM`/`RTL_CALIBRATED_SURROGATE`）只保留介面、宣告 reserved 但**不註冊** → dispatch 即拒絕，且其建構子直接 raise（stub 不能偽裝成可用 backend）。

**決定 5（三個 backend 共用同一 datapath 語意）** `backends/_core.py` 一次實作 queue+pipeline（backpressure、issue-width、in-flight 完成排序、counters），`FUNCTIONAL_POLICY` / `CYCLE_RESOLVED_MODEL` / `REFERENCE_MOCK` 只覆寫 `service_cycles`。理由：根規格 §14.4 禁止 simulator/software/firmware/RTL 用不同演算法語意卻宣稱跨層一致。差別是服務時間模型：FUNCTIONAL_POLICY 只計結構延遲（pipeline+ops，**不**計頻寬競爭）；CYCLE_RESOLVED_MODEL 另加 `bytes/memory_bandwidth` 搬運項。兩者皆 ANALYTICAL，非 cycle-accurate、非 measured surrogate。

**決定 6（掛載點三件事 + A2/A6 無量測）** `attachment_points.py` 定義 A1-A6，每點三件事（work-unit+baseline 成本 / 候選處理器成本模型 / 搬運成本）。粒度（C1 敏感度軸）：A1=per_layer、A3/A4/A5/A6=per_block、A2=per_layer。baseline 引用實機量測路徑（routing .npy [159,32,2]、transfer 微基準 v1-v4、expert_decompressor.sv 307-811 MHz、SWAP-K1/2/5），候選處理器成本一律 ANALYTICAL 模型、無收益主張。**A2 與 A6 無量測**：`measured=False` + `PROJECTED` + `performance_conclusion_allowed()=False`；`AttachmentPoint.__post_init__` 強制 A2/A6 不得 `measured=True`。搬運成本（第三件事、常是 break-even 決定因素）逐點寫明；A3 用 §8.1 修正後的**聚合頻寬**模型（N stream 共享、單筆延遲不變）。

**範圍界線** 本階段建成的是「可掃描元件 + ABI + reference mock + A1-A6 定義（成本模型形式）」。成本模型的**校準值**屬 A4 calibrated envelope；A5/A6 的 KV 細節屬 B1；accelerator 收益/break-even 屬 C1。皆未在此宣稱。

**後果**
- STAGE_B2 → `COMPLETE`；guide_completeness `RULES_ONLY`→`EXECUTABLE`（實作決定補回 guide §7）。
- 新增：`accelerator/`（`__init__`、`fidelity`、`resource_model`、`abi`、`backends/`、`attachment_points`）、`configs/accelerator/resource_model_default.yaml`、`scripts/stage_b2_emit_model.py`、`tests/test_accelerator.py`（+34 tests）。`src/edgeflow/multifidelity.py` 未動（git diff 空），其防偽測試仍過。
- run `runs/20260819T201446Z__stage_b2_accelerator_model/`（manifest + metrics + environment + artifacts 五份：resource_sweep / abi_registry / reference_mock_paths / attachment_points / fidelity_audit）。
- 基線：**399 Python**（tests/ 129→163）+ 14 CTest、0 失敗；evidence 4423/4423 未動；`make doctor` pass。**純 CPU；無 accelerator 收益/break-even 主張；A2/A6 無效能結論；零元件標 MEASURED_SURROGATE**。
