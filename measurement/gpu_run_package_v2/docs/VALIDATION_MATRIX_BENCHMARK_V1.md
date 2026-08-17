# Benchmark Validation Matrix V1

## 判讀規則

- `completed`：repo 內已整合的 `gpu_run_package_v2` 有可直接檢查的程式、
  設定或真實 artifact。
- `partial`：已有可執行垂直切片，但樣本、重複、硬體或模型範圍不足。
- `blocked`：必要硬體、工具、正式模型或統計尚未完成。
- tiny-random Qwen2MoE M0 只證明 pipeline/correctness。其 latency 僅為
  `vertical smoke`，不可用於品質、泛化或正式效能結論。

本表只採用使用者本輪 benchmark 新契約的立即任務 1–28；不沿用舊 charter
任務編號。狀態評估的是各任務所要求的實作或已指定垂直 smoke，不等同論文級
release。Release 缺口另列於本表後段。

## 立即任務 1–28

| # | 任務 | 狀態 | 已整合套件的證據或阻擋 |
|---:|---|---|---|
| 1 | registry | completed | `configs/test_suites/benchmark_registry.yaml`：GSM8K、MMLU、HumanEval、C-Eval 的 pinned revision、license、snapshot 與 hash contract |
| 2 | suite | completed | `configs/test_suites/moe_trace_suite_v1.yaml`、`configs/test_suites/frozen/v1.4.0/inventory.json`：T0–T8、810 samples、unresolved gates 0 |
| 3 | generator | completed | `scripts/generate_sample_manifest.py`、`configs/test_suites/sample_manifest_v1.jsonl`；`tests/test_benchmark_suite.py` 驗證兩次生成 byte-identical |
| 4 | GSM8K split | completed | `configs/test_suites/benchmark_registry.yaml`、`configs/test_suites/splits/v1.yaml`；T1 共 132 筆，含 smoke/calibration/validation/sample holdout |
| 5 | MMLU | completed | `configs/test_suites/benchmark_registry.yaml`、`configs/test_suites/splits/v1.yaml`；T2 共 369 筆，subject-stratified 且含 domain holdout |
| 6 | code | completed | `configs/test_suites/benchmark_registry.yaml` 的 HumanEval T3、`scripts/benchmark_quality.py` 的 static/isolated code evaluator；共 132 筆 |
| 7 | 中文 | completed | `configs/test_suites/benchmark_registry.yaml` 的 C-Eval T5、`scripts/benchmark_quality.py` 的 choice/language validity；共 152 筆 |
| 8 | instruction | completed | `configs/test_suites/prompt_templates/v1.yaml` 的 fixed T4 instruction/conversation；共 4 筆 |
| 9 | long-context | completed | `configs/test_suites/moe_trace_suite_v1.yaml` 的 T6 buckets 128/512/2048/4096/8192；frozen suite 共 5 筆 |
| 10 | synthetic | completed | `configs/test_suites/moe_trace_suite_v1.yaml` 的 T8 八種 controlled routing stress pattern；frozen suite 共 8 筆 |
| 11 | templates | completed | `configs/test_suites/prompt_templates/v1.yaml`，revision v1.2.0 |
| 12 | generation | completed | `configs/test_suites/generation_configs/v1.yaml`：deterministic/code/long-context generation 與 revision/backend/config 記錄契約 |
| 13 | serving | completed | `configs/test_suites/serving_schedules/v1.yaml` 與 T7 serving schedule samples |
| 14 | cal/val/holdout | completed | `configs/test_suites/splits/v1.yaml` 與 `splits/v1.4.0/`：sample/domain 集合 hash-disjoint；model/hardware 為正交 manifests，不再使用逐 sample holdout 欄位；hardware 為 unassigned/inactive |
| 15 | quality | completed | `scripts/benchmark_quality.py` 與 `tests/test_benchmark_suite.py`：T0–T8 evaluator/安全 code policy 已實作；現存 tiny-random M0 品質為 0，不構成論文品質證據 |
| 16 | model×benchmark compatibility | completed | `configs/test_suites/model_benchmark_matrix.yaml`：M0–M3 × T0–T8 × F/Q/L/T 契約；M1–M3 實機執行仍是 release gate |
| 17 | benchmark metadata trace schema | completed | `schemas/benchmark_trace_record.schema.json`、`schemas/canonical_moe_ir.schema.json` |
| 18 | sample/prompt/config hashes | completed | `configs/test_suites/sample_manifest_v1.jsonl` 保存 sample/raw/prompt hashes；`artifacts/m0_benchmark_smoke/run_manifest.json` 保存 generation config、model revision 與 snapshot aggregate hash |
| 19 | multi-pass orchestrator | completed | `scripts/capture_orchestrator.py`、`configs/capture_matrices/m0_rtx3050_vertical_v1.json`；新 plan 綁 v1.4.0 的 8 個合法 smoke samples；外部既有 M0 evidence 仍是 historical v1.2.0 |
| 20 | audit | completed | `scripts/trace_audit.py`、`artifacts/m0_provenance_package/session/TRACE_COMPLETENESS_REPORT.json`：status complete、findings 0（僅指 M0 provenance 契約） |
| 21 | package verify | completed | `scripts/trace_package_verify.py`、`artifacts/m0_provenance_package/INGEST_REPORT.json`：archive/audit/verify complete |
| 22 | T0 smoke | completed | `scripts/t0_fixture_runner.py`、`artifacts/t0/SESSION_MANIFEST.json`、`artifacts/t0/raw_traces/RAW_INVENTORY.json` |
| 23 | executable MoE GSM8K | completed | `artifacts/m0_benchmark_smoke/run_manifest.json`：RTX 3050 上 4 筆 GSM8K measured；限定 tiny-random M0 correctness/vertical smoke，品質 0 不等於未執行 |
| 24 | executable model MMLU | completed | `artifacts/m0_benchmark_smoke/run_manifest.json`：RTX 3050 上 4 筆 MMLU measured；限定 tiny-random M0 correctness/vertical smoke，品質 0 不等於未執行 |
| 25 | provenance package | completed | 外部 `artifacts/m0_provenance_package/INGEST_REPORT.json`：40 measured records，archive SHA-256 `2a58c33abacacdbd87d8318dc370c5e400607b53407be52c3a3950c24231193d` |
| 26 | canonical IR | completed | `scripts/canonicalize_trace.py`、`artifacts/m0_provenance_package/session/canonical_traces/`；由 measured raw 經 versioned converter 轉換 |
| 27 | simulator | completed | `scripts/workload_expand.py`、`scripts/system_simulate.py`、`artifacts/m0_provenance_package/session/derived/m0_route_analysis.json`；輸出是 derived/estimate，沒有 hardware latency claim |
| 28 | runbook/validation matrix | completed | `docs/TRACE_ACQUISITION_RUNBOOK.md`、本文件、`docs/BENCHMARK_SUITE_V1.md`、`README_RUN.md` |

## 論文級 release gates 尚未完成

以下缺口不回寫成任務 1–28 的「未執行」，但仍共同阻擋論文級 benchmark
release：

| Release gate | 狀態 | 缺口 |
|---|---|---|
| 正式模型 M1–M3 | blocked | model×benchmark 契約已建立，但未完成 executable hardware runs |
| repetitions/statistics/convergence | blocked | 現存 M0 只有 1 repetition；尚無正式 CI、變異與 sampling convergence |
| P5 長度 | blocked | 現存 telemetry 1.329 秒，低於 5 秒 minimum |
| P4 availability | blocked | isolated runtime 無 Nsight Compute；P4 為 unsupported，無 hardware counters |
| cross-GPU | blocked | 尚無同模型、同 revision/config/workload 的跨 GPU 比較 |
| hardware holdout | blocked | D-062 後為 `unassigned_pending_future_decision` 且不參與 active split；未來須由新決策明確指派 |
| quality-capable model | blocked | evaluator 已實作；tiny-random M0 quality 0，沒有品質/泛化結論 |
| paid GPU maximal | blocked | 尚未在付費 GPU 完成正式 maximal multi-pass、audit、package 與 verify |
| MAPE/quality formal | blocked | schema、正負向測試與 formal release verifier 已實作；目前沒有通過的正式模型/跨硬體 MAPE 與 quality reports |
| paid governance | blocked | D-062 未 supersede；D-063 只封存 NO-GO baseline。三角色 approval 不能解除 active D-062 |
| monotonic runner deadline | blocked | capture plan 只有 contract，runner 尚未實作，不宣稱 105/120 分鐘 deadline enforcement |

## Pass 判讀

| Pass | 狀態 | 可宣稱範圍 |
|---|---|---|
| P0 | measured | clean M0 vertical smoke；latency 不可外推 |
| P1 | measured, instrumented | timeline/profiler evidence；不可與 P0 作 latency 比較 |
| P2 | measured | tiny M0 routing capture |
| P3 | measured | tiny M0 memory/transfer capture |
| P4 | unsupported | isolated runtime 找不到 Nsight Compute；沒有 counters |
| P5 | measured but insufficient | 23 samples、1.329 s；低於 5 s minimum，無正式 telemetry 統計 |
| P6 | optional_not_run | 本次 standard slice 未要求 cycle/detailed simulator |

## Evidence-class boundary

`artifacts/m0_benchmark_smoke/` 是 measured RTX 3050 M0 evidence；
`artifacts/m0_provenance_package/session/canonical_traces/` 是由 measured raw
轉換的 canonical data；`derived/`、`--expand-workload` 與
`--simulate-workload` 的結果是 derived/simulator estimate。後兩者不可改寫為
GPU 實測 latency、正式 TPOT、throughput 或品質。
