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
