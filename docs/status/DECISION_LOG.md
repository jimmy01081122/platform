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
