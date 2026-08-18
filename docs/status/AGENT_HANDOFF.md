# AGENT_HANDOFF

最近一次 session 的交接記錄。**每個 session 完工時把新記錄加在最上面**，不刪除舊記錄。

跨 session 的權威狀態是 `governance/stage_ledger.yaml`；本文件是人類可讀的敘述補充。**衝突時以 ledger 與實際重跑的指令為準**，不以本文件的敘述為準。

---

## 2026-08-19 · STAGE_A2 · measured raw → 九類 Canonical IR（純 CPU）

**Session 目標** 把真實 GPU 量測第一次接進九類 Canonical IR。做的是 OFF-E-PR3 expert 容量掃描（15 點）——欄位最全、counters 決定性、有可驗證 byte 守恆式。**不含**讓引擎消費（A3）。

**進入檢查** 全通過：`make verify-evidence` 4423 OK；`make test` 0 failed；`make doctor` pass；`STAGE_A2` NOT_STARTED；15 個 CAP 點、`canonical_ir.py`、mock adapter、phase2 43 tests 皆在。

**產出**
```text
ADAPTER   explorations/moe_cycle_simulator/phase7/adapters/off_e_pr3_measured_adapter.py
          measured adapter；與 vllm_mock_adapter.py（未動，作 fixture）並存
          純 Python .npy(uint8) reader（venv 無 numpy）；object id = layer*8+expert
TEST      explorations/moe_cycle_simulator/phase7/tests/test_off_e_pr3_measured_adapter.py
          8 tests（IR1、九類齊、byte 守恆、routing 可回溯、AGGREGATE 無 scores、
          claim boundary 綁定每筆、mock sha256 pin）
RUN       runs/20260819T000000Z__stage_a2_off_e_pr3_measured_ir/
          bundle/（Arrow+Zstd 九分區，MEASURED，33203 records）
          artifacts/{claim_boundary,conservation_report,dropped_fields,summary}.json + manifest.json
```

**IR1 結果** `validate_records(bundle_evidence_class=MEASURED)` 對 33203 筆全通過；`write_bundle`→`read_bundle` round-trip 再驗證通過。記錄數：ModelIR 1 · PlatformIR 15 · WorkloadIR 15 · RoutingIR 32 · PlacementIR 15 · ClockAlignmentIR 1 · EventIR 33094 · CalibrationIR 15 · ResultIR 15。

**守恆與可回溯** 15 點全部 `h2d_bytes == demand_load_count × 352,321,536`（`conservation_report.json` all_ok=true）；另 `hit+demand==10176`、`len(transfer_events)==demand_load_count`、`len(terminal_resident)==capacity_objects`。`sha256(routing .npy) == routing_sha256 == 0a9225ec…`，15 點共用單一 routing trace。

**三個關鍵決定（詳見 DECISION_LOG P-015）**
- **RoutingIR = AGGREGATE scope**：量測 .npy 只有 selected expert ids（`vllm.CompletionOutput.routed_experts`, uint8），**無 gate scores**，TOKEN-scope 無法誠實建構。per-token 排序落入 dropped-fields（A3 從 .npy 依 token-major/layer-major 重建）。
- **每點各一 PlatformIR**（device residency budget = 該點 capacity_bytes）：既忠實（掃的就是 on-device residency 預算），又讓 15 個 PlacementIR 落在 15 個不同 (model, platform) 群組，避免 cross-IR 把它們當成一條不存在的 migration 鏈。
- **claim boundary 綁定**：provenance 無自由文字欄，故 `claim_boundary.json` 的 sha256 進每筆 `source_content_ids`；下游可查、不可洗掉。

**MEASURED/no-claim 邊界** CalibrationIR fidelity=UNAVAILABLE、無 profile hash、measured==predicted、formal_pass=False；training/held_out 只是 schema 需要的參照，**不宣稱任何 held-out 驗證**（§7）。無任何時序/效能/calibrated 主張；IR **尚未**被引擎消費（A3）。

**DROPPED_FIELDS（A3 輸入，見 `artifacts/dropped_fields.json`）** routing per-token scores（無）→ AGGREGATE；per-token 排序 demand（AGGREGATE 丟排序，misses-only 事件）→ A3 從 .npy 重建；setup H2D（排除於 demand path）；transfer 的 h2d_start/decision_ns（折進 service_demand）；logical_evicted_object_id（EventIR 無 eviction-target 欄）；非 expert 權重張量（不在殘留掃描內）。

**基線** 352 Python（tests/ 123 · simulator 36 · phase1 16 · phase2 43 · phase7 134，+48 subtests）+ 14 CTest、0 failed；HEAD 8924d8e（PREP-1）時基線 344，本 session +8 phase7 A2 tests。evidence 4423/4423 未動；mock adapter sha256 未變（32f1c0a7…）；doctor pass。

**NEXT** A3（IR→C++ 引擎 loader + 位元精確 replay；SIM0 用本 bundle 的 hit/demand/discard）。可續做其餘家族（SWAP-K2 → SERV-P0-25 → controlled matrix → component/transfer），adapter 架構已可延伸；本 session 只做 A2 驗收所要求的 OFF-E-PR3。**OWNER 決策**：無（未觸及需停下詢問的條件）。

---

## 2026-08-19 · TRACK_GPU_PREP · PREP-1（純 CPU 前置準備）

**Session 目標** 把 GPU 量測從「到時候再想」變成「貼上就跑」。純 CPU，不碰 GPU/SSH/serving。STAGE_A2 為 NOT_STARTED → 只做 PREP-1，PREP-2 留給 A2 完成後。

**進入檢查** 全通過：`make verify-evidence` 4423 OK；`make test` 0 failed；`make doctor` pass；`STAGE_A2` NOT_STARTED；`measurement_gaps.json` 存在。

**產出（全部設計 / CPU 行為，無任何 GPU 主張）**
```text
CONTRACT       experiments/specs/gpu_measurement_contract_v1.yaml (FROZEN_PREP1)
               五項各一節; time_estimate 全有值 (25/60/0/15/160 min)
PROBES         measurement/probes/long_context_kv_probe.py    (priority 2, 從零寫)
               measurement/probes/inserving_dispatch_probe.py (priority 1)
               measurement/probes/mock_backend.py             (CPU mock; gpu backend 註冊但拒絕執行)
               兩支皆 CPU smoke 通過並過自己的 validator; evidence=cpu_smoke_test_not_measurement
PARSERS        measurement/parsers/{common,longctx_kv_parser,dispatch_parser,
               sealed_manifest_validator,component_eval_parser,serving_tail_parser}.py
               每個 parser 對壞形狀 raise (非靜默略過; 對治 hf_sample_download WARN 前例)
FIXTURES       tests/fixtures/gpu_prep/ 每 parser 1 正常 + >=1 失敗; 失敗確實 raise
SEALED SPLIT   calibration/sealed/holdout_split_v1_manifest.json
               102 cells (fit64/val18/holdout20); 逐 cell SHA-256 + assignment hash;
               封存於任何新量測之前 (§7.2 不洩漏); STAGE_A4 開封一次
GAP-5          calibration/GAP5_LAUNCH_GRANULARITY_RESOLUTION.md
               RESOLVED_BY_CODE: cpu_calls=decode-step launches; expert_tokens=8×decode positions
               (routing_width=8 trace 事實); 殘差 1.87x = window_replay 兩個未量測 argsort = V2-GAP-C(需GPU)
WINDOW PLAN    experiments/specs/gpu_measurement_window_plan_v1.md
               target5 arrival-bound ~2.65h 主導; 1+2+4 ≈100min 可入單一 2h 獨占窗口
```

**PENDING_A2（硬規則 5）** 探針中依賴 A2 IR 評估點 schema 的輸出欄位一律標 `PENDING_A2`，**未自行猜測**：dispatch 探針的 `break_even_decomposition_fields` 與兩支探針的 `ir_evaluation_point_fields`。PREP-2 待 A2 完成後定案，驗收方式為「新探針輸出能不經 join 直接生成 IR 評估點」。

**基線** 344 Python（tests/ 123，+13 `tests/test_gpu_prep.py`）+ 14 CTest、0 failed；evidence 4423/4423 未動；doctor pass。

**NEXT** TRACK_GPU 的 priority 4/5 已完全規格化可跑；priority 1/2 的探針已備但輸出欄位待 PREP-2。GAP-5 的 GPU 需求已消除（僅殘 V2-GAP-C）。PREP-2 需先跑 STAGE_A2。

**OWNER_DECISION_NEEDED** 無（所有設計均在授權邊界內；長上下文掃描未觸及需 owner 裁決的 VRAM 上限或窗口取捨）。

---

## 2026-08-19 · Stage A1 · cal_model_form_repair_v2 implementation + FIT-side evaluation

**Session 目標** owner 核可 3 個 review points 後，實作三候選並在既有 evidence 上 FIT 側評估。評分順序嚴格為 correctness → subgroup gates → generalization → MAPE → aggregate（低 aggregate 不得蓋 subgroup gate）。不開 GPU。

**結果（全部 FIT 側 / diagnostic，無 calibrated PASS）**
```text
A PCIe two-regime   ACCEPT。aggregate 1.038% / small 2.508% / bulk 0.058% / h2d 0.875% /
                    d2h 1.201% / maxAPE 4.752%（vs v1 19.821% / old stream-factor 66.879%）。
                    物理約束 hard-fail（無 clamp）。production 單 object S=1 → 12.449ms
                    （合實測 ~12.45ms），S>1 → UNSUPPORTED。
B ProfileKNN        INSUFFICIENT_EVIDENCE。LOOWO prefill per-op：selected_expert 41.2% /
                    grouped_gemm 39.4% / gather_scatter 17.6%（皆 ≥15%），且 KNN 在
                    selected_expert & grouped_gemm 上比 global_affine 還差（33.1/33.8%）。
                    同 workload 診斷優勢（q0→q1 aggregate 10.9%）無法 generalize → overfit。
                    不升格；開 V2-GAP-B。decode 低 MAPE 全是 exact-token lookup，已單獨揭露。
C replay            BLOCKED_ON_MEASUREMENT。顯式 graph 對齊 window_replay()（2×argsort+3×GEMM
                    +gather+scatter）；缺 argsort_route/argsort_inverse 量測 → 不能 FIT-closed。
                    固定 tau（2 結構配置）DIAGNOSTIC_ONLY；throughput 派生。開 V2-GAP-C。
D dequant           PROXY_ONLY。
```

**新增檔案** `calibration/models_v3.py`、`calibration/refit_v3.py`、`tests/test_models_v3.py`（14 production-path 測試）、`calibration/fits/v3/{summary,pcie_report,component_report,replay_report,self_check}.json`、`runs/20260819T160456Z__stage_a1_cal_model_form_v2_fitside_eval/`。未動 `calibrated_backend.py`（保 v2 可重現）、未動 evidence。

**Provenance 修正** 兩段 commit：H1=`528ae01`（只含生成程式 models_v3/refit_v3/tests）；於乾淨 H1 tree 執行 → manifest `code_commit=H1` + 兩腳本 SHA-256；artifacts 於 H2 加入。解決先前 manifest 指向 parent commit 的問題。

**驗證** `make test` 331 Python（tests/ 96→110）+ 14 CTest、0 失敗；`make verify-evidence` 4423/4423；`make doctor` pass；v3 `self_check.json` all_ok=true。

**A1 狀態** IN_PROGRESS。closure 明確 blocked 在 V2-GAP-B（更密 prefill shape sweep）與 V2-GAP-C（sort/permute microbench）。依 OWNER_RESOLUTION，這類 targeted FIT-side 量測可開，但**不得與 A4 sealed held-out 混用**。

**GPU_REQUIRED_FOR_NEXT_A1_STEP** YES（B、C 的 closure 需要）；但 PCIe two-regime 可先在既有 evidence 上整合。**新增決策** P-013。

---

## 2026-08-18 · Stage A1 · cal_model_form_repair_v2 preregistration（文件，無實作）

**Session 目標** 依 reviewer 覆核結論，撰寫第二輪模型形式演化的事前登記；**只出文件，不實作、不 refit**（owner 指示：prereg 完成前不得動手）。

**產出** `experiments/specs/cal_model_form_repair_v2.yaml`（created_at 2026-08-18T15:35:15Z，早於任何 v3 fit 輸出；目前 `calibration/fits/` 僅 v2）。四候選，各含模型形式／參數化／擬合協定／**事前寫死的三值接受判準**：

```text
A PCIe two-regime  T=max(alpha_d+beta_d(S-1), A_d+B/BW_d)。ACCEPT bar: aggregate<5% 且
                   small<10% 且物理約束滿足。關鍵 open item 已登記: production 端 S 語意
                   —— 單筆搬運用 S=1；S>1 標 UNSUPPORTED 直到有多筆聚合量測(V2-GAP-A)。
B component KNN    phase-partitioned per-op log-token local interpolator。登記全部超參
                   (k=3, log 距離, envelope, INTERPOLATED/EXTRAPOLATED/UNSUPPORTED) 與必測
                   (LOOWO/LOSRO/k-sens/distance-sens/decode-lookup 揭露/bootstrap)。
                   ACCEPT 僅當 prefill-only LOOWO per-op<15%；否則判 INSUFFICIENT 並開
                   prefill shape sweep 缺口(V2-GAP-B)。事前承認很可能 INSUFFICIENT。
C replay operator  顯式 graph 對齊 window_replay() (2×argsort+3×GEMM+gather+scatter)。
                   固定 tau_route = DIAGNOSTIC_ONLY；throughput=1000/TPOT 派生。
                   routing/sort 項 BLOCKED_ON_MEASUREMENT (缺 T_sort/T_permute 量測, V2-GAP-C)。
                   → replay 無法在既有 evidence 上 FIT 閉合，誠實記錄。
D dequant          維持 DEQUANT_PROXY_ONLY (V2-GAP-D 解除)。
```

**待 owner 裁決（3 點，見 spec owner_review_points）** (1) PCIe production S 語意的 default_rule；(2) component 的 <15% LOOWO 門檻與 INSUFFICIENT 分流；(3) replay 只到「顯式 graph+診斷 tau」、routing/sort 留 GPU 軌的範圍界定。

**未做** 任何實作 / refit / v3 輸出。A1 維持 IN_PROGRESS。

**下一步** owner 核可 prereg（或調整 3 個 review points）後，才實作三候選並輸出 `calibration/fits/v3/`（不覆寫 v2），run manifest 綁最終 commit hash。GPU 窗口仍不需開——先 FIT 側 closure。

---

## 2026-08-18 · Stage A1 P0 follow-up：移除 single-shape fallback

**Session 目標** 在 7a76a8f 之上做最小 P0 follow-up：堵住 non-physical fallback 的第二個出口。

**改動**
```text
calibrated_backend.py  名單外 gpu_service op 若 distinct shape axis < 2，fit_parameters()
                       一律 raise CalibrationError；移除 flat_fallback_single_shape_group
                       production fallback。dequant 的 flat_by_registered_exclusion 不變。
refit_v2.py            self-check 新增 fit_parameters_single_shape_non_excluded（必須 raise）；
                       保留 negative-slope／negative-intercept raise 與 dequant no-raise control。
tests/（owner 授權）    合成 fixture 原用單一 shape 的 selected_expert 探針會觸發新 raise；
                       改為兩個 distinct shape 點，第一點 expert_tokens=1 對齊 component 評估點
                       tokens_per_launch=1.0，使既有 MAPE 斷言完全不變。
```

**驗證** `make test` 317 Python + 14 CTest、0 失敗；`self_check.json` 六 case 全綠；`make verify-evidence` 4423/4423；evidence 未動；真實校準資料三個仿射 op 皆多 shape，無 spurious raise，四項 FIT 側 MAPE 不變。

**未做** cal_model_form_repair_v2 refit（PCIe／KNN／replay）——**prereg 完成前不得實作或 refit**。A1 維持 IN_PROGRESS。決策更新於 P-012 follow-up、ADDENDUM_2。

**下一步** author `cal_model_form_repair_v2` preregistration。

---

## 2026-08-18 · Stage A1 Principal Reviewer 覆核 + P0 calibration-safety 修正

**Session 目標** 對 A1 model-form evolution 做獨立覆核（不調參、不辯護），用 raw evidence 重現各項主張，判斷哪些演算法可進下一輪 preregistered implementation；並修正發現的 blocker。

**覆核方法** 全部在 platform venv 對 `evidence/**/result.json` 與 `measurement/gpu_run_package_v2/scripts/benchmark.py` 原始碼重現，不採信 harness 轉錄數字。（外部參考 harness 在 `/home/a/a1_algorithm_evolution_test_harness/`，其 pytest 需 sklearn，本環境無法重跑，但每一項 load-bearing 數字均已獨立重現。）

**發現並修正的 P0 blocker（本 session 已改碼）**
```text
fit_parameters() 對非物理 gpu_service 擬合 catch CalibrationError → 靜默 fallback 常數，
違反 root spec §8.3「拒絕非物理解、無 fallback 常數」。self_check 只測 _line_fit()，
測不到 production path。執行證明：修正前負斜率輸入回傳 flat_fallback_non_physical_slope
不 raise；修正後正確 raise。
修法：改事前名單 SHAPE_INSENSITIVE_OPERATIONS={dequant}；名單外 op 一律 raise。
self-check 擴充覆蓋 fit_parameters() production path。詳見 P-012。
```

**其餘覆核結論（全部 FIT 側 / diagnostic，非 held-out）**
```text
PCIe two-regime        ACCEPT。自 raw 重現 30 點 1.04% MAPE（小尺寸 48.8→2.5%、bulk 0.5→0.06%）。
                       benchmark.py:316-334 證實 copy_streams＝固定總位元組切 S 塊、S streams、
                       wait-all → 舊 q0 per-transfer 乘數語意錯、現行 A1 對小尺寸也不對。two-regime
                       是物理正確形式。待定：production 端 stream 語意。
ProfileKNN             MORE_TESTS。aggregate 10.9% 被 18/18 decode 精確 token lookup 灌水；
                       真正 prefill 內插 18.2%（>15%；per-op 22.4/20.9/11.2%）。
MoE replay operator    window_replay() 實測含 2×argsort＋3×GEMM＋gather＋scatter，登記 metadata
                       只記 grouped_gemm＋gather_scatter → 系統性遺漏 routing/sort。固定 tau_route
                       僅 2 結構點 → DIAGNOSTIC_ONLY；正式版改顯式 operator。
routing 守恆           Σn_e＝num_tokens×top_k，672 步 0 違反（CONFIRMED）。
dequant                raw 標 synthetic_symmetric_int4_proxy_not_checkpoint_awq → 維持 PROXY_ONLY。
throughput 措辭         60.658%→75.898% 是「變差」，先前寫「方向仍是改善」是誤導，已改正。
```

**治理更正** STAGE_A1 由 COMPLETE 退回 **IN_PROGRESS**；VALIDATION_MATRIX「拒絕非物理解 = PASS」是假 PASS，已改正。

**變更檔案** `src/edgeflow/calibrated_backend.py`（加 SHAPE_INSENSITIVE_OPERATIONS、移除 fallback）；`calibration/refit_v2.py`（self-check 覆蓋 production path、build_notes 更新）；重生 `calibration/fits/v2/{parameters,self_check,residual_report,measurement_gaps}.json`（四項 MAPE 數字不變，dequant 改標 flat_by_registered_exclusion）；`governance/stage_ledger.yaml`（僅 STAGE_A1）；`docs/status/{CURRENT_STATUS,VALIDATION_MATRIX,DECISION_LOG,AGENT_HANDOFF}.md`。

**驗證** `make test` 317 Python + 14 CTest、0 失敗；`make verify-evidence` 4423/4423；`tests/test_calibrated_backend.py` 既有 7 項全過；`self_check.json` production_path_covered=true。evidence 未動。**新增決策** P-012。

**下一個最高資訊增益的動作** 撰寫 `cal_model_form_repair_v2` preregistration（PCIe two-regime 的 production stream 語意、component predictor 的 prefill-only 分階段門檻＋leave-one-workload-out、MoE replay 顯式 routing/permutation operator），再重擬合。**先完成 FIT 側 scientific closure，再碰新的 sealed held-out；GPU 窗口暫不需開。**

**仍然禁止的主張** 任何 calibrated PASS；以 FIT 側殘差作 held-out 證據；break-even／accelerator／長上下文。

---

## 2026-08-18 · GPU 量測軌拆段 + 統籌 session

**Session 目標** 回應 owner 兩點：(1) GPU 軌執行前先把量測目標與程式準備好，避免 GPU 閒置——需要先跑哪些 stage；(2) 另開一個統籌 session 集中排程與驗收，避免長上下文影響判斷。

**答 (1)：GPU 前不需要跑任何既有 stage，需要的是一條新的純 CPU 前置軌。** 盤點五項量測的就緒度發現它與資訊增益順序**幾乎相反**——優先序 1（A2 dispatch）、2（A6 長上下文）要寫最多程式，4、5 今天就能跑。因此新增 `TRACK_GPU_PREP`（純 CPU）：凍結 contract、寫長上下文 runner 與 dispatch 儀測、CPU smoke test、封存 sealed split、追 GAP-5。`TRACK_GPU` 改以「contract 已凍結」為前置（優先序 4/5 例外）。

**答 (2)：** 新增 `SESSION_ORCHESTRATOR.md` + ledger 頂層 `orchestrator:` 區塊。統籌 session 讀取權限與其他 session 相反（可讀全部指引但只讀第 1/2/7 節），換來不得執行工作、不得改 `stages:` 的約束。

**盤點時修正兩項事實**
```text
A2 掛載點  非「無任何量測」：evidence 有 56 筆 gather_scatter，但是 benchmark.py:463
           的同裝置合成 proxy（index_select 來回作用於 torch.randn），只給 execute 項。
           準確敘述：execute 側有合成 kernel proxy，系統層搬運與控制結構未量測。
GAP-5      可能不需 GPU：measurement_gaps.json 第一條解法是純讀 harness 程式碼，
           線索在 benchmark.py:440-441。前置軌須先試這條再排量測。
A6 缺口     全倉庫 grep max_model_len/cpu_offload_gb/swap_space/kv_offload 零命中，
           runner/config/parser 全部要新寫，是最大準備缺口。
```

**新增／修改**
```text
新增 docs/session_guides/TRACK_GPU_PREP.md
     docs/session_guides/SESSION_ORCHESTRATOR.md
修改 governance/stage_ledger.yaml（新增 TRACK_GPU_PREP 列、orchestrator: 區塊、規則 5；
       TRACK_GPU 加前置與 readiness）
     docs/session_guides/{README,TRACK_GPU_MEASUREMENT}.md
     docs/status/{CURRENT_STATUS,VALIDATION_MATRIX,DECISION_LOG,AGENT_HANDOFF}.md
```

**驗證** `make test` 317 Python + 14 CTest、0 失敗；`make verify-evidence` 4423/4423；`make doctor` pass；ledger YAML 可解析（11 stages + orchestrator）。未動任何原始碼或 evidence。

**新增決策** P-011。

**可立即並行的兩個 session** A2（解鎖最多下游，定義 IR 評估點 schema）與 GPU 前置準備軌（endpoint 是唯一有時限資源）。

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
