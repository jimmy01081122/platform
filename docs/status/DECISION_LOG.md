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
