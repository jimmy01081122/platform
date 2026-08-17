# 命名系統對照表

本專案累積了**五套並存的階段命名**，分屬不同時期與不同契約。冷啟動 session 極易把其中一套的進度誤讀成整體進度，因此這張表是必讀的導航文件。

**唯一有效的當前命名是 `Stage 0 / A1–A4 / B1–B2 / C1–C2` 加上 GPU 量測軌**，定義於 `PLATFORM_FLOW_SPECIFICATION.md` §13，狀態記錄於 `governance/stage_ledger.yaml`。其餘四套只能作為**歷史脈絡或待驗證證據**。

---

## 1. 當前命名（唯一有效）

| ID | 內容 | 指引 |
|---|---|---|
| Stage 0 | 遷移、基線、規格、session 指引系統 | `session_guides/README.md` |
| A1 | calibration 模型形式修復與重擬合 | `session_guides/STAGE_A1_CALIBRATION_MODEL_FORM.md` |
| A2 | measured raw → 九類 Canonical IR | `session_guides/STAGE_A2_MEASURED_TO_IR.md` |
| A3 | IR → 引擎 loader + 位元精確 replay | `session_guides/STAGE_A3_IR_TO_ENGINE.md` |
| A4 | sealed held-out 校準驗證 | `session_guides/STAGE_A4_SEALED_HOLDOUT.md` |
| B1 | KV cache + continuous batching 建模 | `session_guides/STAGE_B1_KV_BATCHING.md` |
| B2 | 參數化候選處理器 + 掛載點 A1–A6 | `session_guides/STAGE_B2_ACCELERATOR_MODEL.md` |
| C1 | co-design DSE + break-even | `session_guides/STAGE_C1_CODESIGN_DSE.md` |
| C2 | HW0 需求 + LM18 RTL handoff | `session_guides/STAGE_C2_HW0_RTL_HANDOFF.md` |
| GPU 軌 | 實機量測（與各階段並行） | `session_guides/TRACK_GPU_MEASUREMENT.md` |

> 掛載點也叫 A1–A6（見規格 §6），與階段 A1–A4 **是不同的東西**。
> 上下文區分：「階段 A1」指校準修復 session；「掛載點 A1」指 routing/gating 決策計算這個候選加速功能。

---

## 2. S0–S7：證據層級（仍然有效，但不是進度）

定義於 `AGENTS.md` §7 與 `project/evidence_levels.yaml`。這是**證據強度的分級**，不是時間順序，也不是完成度。每個 S 級帶一個 `claim_limit`，跨級誇大是明文禁止的。

| 級 | 名稱 | claim_limit |
|---|---|---|
| S0 | contract_and_environment | 環境與輸入可識別、可重現 |
| S1 | workload_and_trace | 僅限已觀測的工作負載行為與已量測的瓶頸 |
| S2 | executable_model_and_simulator | 僅限已登記假設下的模型行為 |
| S3 | algorithm | 僅限模型化區間內、相對於已述 baseline 的演算法效果 |
| S4 | break_even | 僅限已量測或已掃描成本下的實作放置邊界 |
| S5 | rtl_full_datapath | 僅限功能與 transaction 層級的硬體可行性 |
| S6 | physical_and_dse | 僅限具名 flow／library／約束／activity 下的 PPA 與 Pareto |
| S7 | cross_layer_closure | 僅限文件化邊界內、可重現的端到端證據包 |

**與當前階段的關係**：A1–A4 產生 S1–S2 級證據；C1 產生 S3–S4；C2 的 LM18 產出是 S5 的**輸入規格**而非 S5 證據本身（本平台不實作 RTL）。既有 `hardware/` 內的成果屬 S5–S6，但那是舊契約下取得的，重用前須依 `AGENTS.md` §9 重新映射。

---

## 3. Phase 0–10：模擬器實作階段（部分有效）

定義於 `explorations/moe_cycle_simulator/governance/MASTER_SIMULATOR_DEVELOPMENT_PLAN_AMENDMENT_2.md`。這套命名**仍活在程式碼裡**——目錄名 `phase1`…`phase7`、CMake target `moe_sim_phase3`、C API 符號 `moe_phase3_engine_create` 都用它。

| Phase | 內容 | 本平台現況 |
|---|---|---|
| 0 | governance | 已凍結於舊契約 |
| 1 | CPU/mock spike | 程式存在，測試通過（16 項） |
| 2 | Canonical IR | 程式成熟（2051 行），測試通過（43 項），**只吃 synthetic fixture** |
| 3 | event core | C++ 引擎，CTest 2/2 |
| 4 | single GPU service model | C++，CTest 3/3 |
| 5 | routing / residency policy | C++，CTest 4/4 |
| 6 | multi-GPU / UMA | C++，CTest 5/5 |
| 7 | formal adapters / real run | GPU campaign runner 與 hooks，測試通過（126 項 + 48 subtests） |
| 8 | calibration | **未開始**（對應當前 A1/A4） |
| 9 | DSE / RTL integration | **未開始**（對應當前 B2/C1/C2） |
| 10 | release | **未開始** |

**重要**：Phase 1–7 的「測試通過」只代表**程式在 synthetic 或 fixture 輸入下自洽**。Amendment 2 中每個 Phase 都標 `SAME_HASH_REVIEW: PENDING` / `FREEZE: NOT YET`，且 Phase 2 自述「不含 event simulator、模型執行、runtime profiling、GPU 證據或校準證據」。**不得**把 Phase 1–7 通過解讀為研究鏈已打通。

---

## 4. C1–C8：舊研究階段（**已停用**）

來源 `docs/planning/UPDATED_RESEARCH_ROADMAP.md`，**未遷入本平台**。內容為 C1 collector recovery → C2 medium MoE trace → C3 calibrated simulation → C4 large trace expansion → C5 strong SW baseline → C6 SW/FW/HW break-even → C7 workload-derived RTL → C8 RTL/system co-sim。

最後狀態為 `C1: C1-FAIL`、下游全部 `NOT_ELIGIBLE`、`Exact next command: BLOCKED_NO_COMMAND`。

**這套命名已停用。** 若在舊文件或 evidence 中看到 `C1`、`C2.5`、`G2.5-S4` 等字樣，那是舊契約下的 gate 編號，與本平台的 Stage C1 **完全無關**。特別注意 `C1` 在舊系統指 collector recovery，在本平台指 co-design DSE。

---

## 5. MR0–MR18：舊 ledger 執行序（歷史證據）

來源 `evidence/phase7/master_remaining/*/`。這是舊 Phase 7 campaign 的執行依賴序：MR0 reconciliation → MR1 atomic expansion → MR2 preflight/guards → MR3–MR5 adoption → MR6 formal freeze → MR7 capability canaries → MR8 formal 60-sample → MR9 serving → MR10 policy replay → MR11 triggered branches → MR12 CTRL/META/SW/HWR → MR13 XRT → MR14 IR → MR15 SIM/CAL → MR16 DSE → MR17 HW0/RTL → MR18 REP0。

**用途**：MR14–MR18 的內容與本平台 A2–C2 高度重疊，可作為**設計參考**；但其狀態欄位屬舊 ledger，且已知含過期欄位（`active_gpu_guard` 仍指向已失聯的 EXT10K attempt）。**不得**引用其狀態作為本平台的完成證據。

---

## 6. LM0–LM19：舊量測矩陣（歷史證據）

來源 `evidence/phase7/` 內的 Phase 7 patch 文件。LM0–LM4 資格/controlled/sampling/clock/routing/memory；LM5–LM6 component/catalog/transfer；LM7–LM9 formal 60-sample；LM10 expanded serving；LM11 policy/offload/swap/control；LM12 cross-runtime；LM13–LM19 IR/simulator/calibration/DSE/requirements/RTL replay。

**用途**：LM13–LM19 的驗收條件已被本平台規格吸收（見規格 §4、§7、§10、§11），其中 **LM18 一詞仍在本平台使用**，專指 C2 的 RTL handoff 規格包（`RTL-ARCH` / `RTL-SCHEMA` / `RTL-GOLDEN` / `RTL-STIMULUS` / `RTL-ACTIVITY` / `RTL-HANDOFF`）。其餘 LM 編號僅作歷史索引。

---

## 7. 常見誤讀

| 誤讀 | 事實 |
|---|---|
| 「Phase 1–7 都通過了，所以研究鏈已打通」 | Phase 1–7 只在 synthetic/fixture 輸入下自洽。量測→IR 的 adapter 仍只有 mock；IR→引擎無 loader。 |
| 「舊 ledger 記 44/286，所以完成 15%」 | 該 ledger 未完整展開 formal/serving/DSE/HW children，分母不可信。規格 §14.10 明文禁止用完成率宣稱進度。 |
| 「C1 已經失敗了」 | 舊 C1 是 collector recovery，屬已停用的命名系統。本平台的 Stage C1 是 co-design DSE，尚未開始。 |
| 「掛載點 A1 就是階段 A1」 | 兩者無關。階段 A1 是校準修復；掛載點 A1 是 routing/gating 決策計算。 |
| 「hardware/ 已有 STA 結果，所以硬體可行性已證明」 | 那是舊契約下的 pre-layout、wire-load model、ideal clock 相對架構 DSE，非 sign-off。且尚未與任何 measured break-even 連結。 |
| 「evidence/ 裡有 47 個 campaign，所以量測已完成」 | 量測涵蓋 expert residency、offload、KV swap 與 serving，但**掛載點 A2（dispatch 資料搬運）與 A6（長上下文 KV attention）完全沒有量測**。 |
