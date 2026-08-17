# Benchmark Suite V1

## Frozen identity

- Suite：`moe-trace-suite`
- Revision：`v1.4.0`
- Seed：`20260718`
- Sample count：810
- Frozen manifest SHA-256：
  `4de9eda6a8eabb5e49c897563033e2fa9d9a8b62db7b81790bb9a4c871f5621e`
- Merkle root：
  `5a7b1c4c7f7ab52d7172aa229d94bd59332b5df161c3f3b0170d863836070b75`
- Evidence：`configs/test_suites/frozen/v1.4.0/inventory.json`

`./run.sh --freeze-suite` 會重建並比對已凍結 revision；既有 revision 不可覆寫。

## T0–T8 定義

| Class | 定義 | 樣本數 |
|---|---|---:|
| T0 | deterministic micro fixtures | 4 |
| T1 | GSM8K exact math | 132 |
| T2 | MMLU subject-stratified | 369 |
| T3 | HumanEval static code | 132 |
| T4 | fixed instruction/conversation | 4 |
| T5 | C-Eval Chinese | 152 |
| T6 | long-prefill context buckets | 5 |
| T7 | serving schedule replay | 4 |
| T8 | MoE router stress | 8 |

完整 evaluator、source 與 content policy 見
`configs/test_suites/moe_trace_suite_v1.yaml`。

## v1.2.0 歷史重凍結決策

早期 M0 native sample ID 曾把 GSM8K 標成 `T0`、MMLU 標成 `T1`，與正式
T0–T8 taxonomy 錯位。v1.2.0 採以下修正：

1. GSM8K 歸入 T1，MMLU 歸入 T2。
2. 以相同 `raw_sample_hash` 建立新的 v1.2.0 sample ID。
3. 不重寫、不偽造且不重跑既有 GPU native artifacts。
4. 以 `artifacts/m0_benchmark_smoke/suite_class_mapping_v1.2.0.json` 保存
   post-execution classification mapping。
5. 重建 frozen manifest、inventory 與 Merkle root；v1.2.0 現保留為已量測
   M0 artifact 的歷史 taxonomy provenance。

目前 source suite v1.4.0 在不重寫既有 M0 raw artifact 的前提下更新正式
freeze；新 capture planning 使用 v1.4.0，既有 RTX 3050 M0 provenance
仍明確標為 historical v1.2.0。

此修正只改分類/provenance，不把先前 tiny-random M0 提升為品質或正式效能
證據。

## v1.4.0 正交 split axes

- sample 與 domain split 延用 v1.3.0 的既有 hash-disjoint 集合。
- model holdout 以 `model_id` 指派；`large_moe_blind` 不再逐列貼到
  sample/domain holdout。
- hardware holdout 以 `hardware_profile_id` 指派，目前狀態是
  `unassigned_pending_future_decision`、`active_split=false`。
- `h100_blind` 僅保留為 D-062 移除的歷史紀錄，不屬 active split。
- freeze root 綁定 prompt、generation、serving、model matrix、dataset
  inventory，以及 sample/domain/model/hardware 四個 axis manifests。

## M0 vertical smoke

執行入口：

```bash
export GPU_PERSIST_ROOT=/persistent/edgehetero-m0
./run.sh --benchmark-smoke --output /persistent/edgehetero-m0/m0-smoke --device cuda
```

輸出路徑必須不存在。CPU 可用於 correctness-only：

```bash
./run.sh --benchmark-smoke --output /tmp/m0-cpu-smoke --device cpu
```

CPU 模式不產生 GPU measurement claim。

現存 RTX 3050 artifact 使用 tiny-random Qwen2MoE，8 個 measured samples、
1 repetition；P0/P1/P2/P3/P5 有真實 capture，40 筆 measured benchmark
records 已進 provenance package。P4 因 `ncu` unavailable 為 unsupported；
P6 為 optional_not_run；P5 僅 1.329 秒，未達 5 秒 minimum。

## Claim limits

- `latency_ms_vertical_smoke` 只能標為 vertical smoke。
- `quality_correct = 0`、`quality_valid = 0`，不可宣稱 benchmark 品質。
- tiny-random checkpoint 不代表正式模型、泛化、完整 MoE TPOT/throughput。
- P1/P5 有 instrumentation overhead，不可拿來替代或直接比較 P0 latency。
- 正式 release 仍需多 repetitions、統計、較長 P5、正式模型 M1–M3 與跨硬體
  validation。

## Provenance 與 simulator 邊界

Measured raw 先進入 content-addressed archive，再由 versioned converter 產生
canonical traces。可重現命令：

```bash
./run.sh --ingest-session --source artifacts/m0_benchmark_smoke --session-root /persistent/m0-session --archive /persistent/m0-provenance.tar.gz
./run.sh --canonicalize-m0 artifacts/m0_benchmark_smoke /persistent/m0-canonical
./run.sh --expand-workload /persistent/m0-canonical/m0_moe_routing.json /persistent/m0-workload.json
./run.sh --simulate-workload /persistent/m0-workload.json /persistent/m0-simulation.json
```

`canonicalize` 是 measured raw 的格式轉換；workload expansion 與 simulation
是 derived/estimate。Simulator 輸出不得標為 RTX 3050 measured latency。
