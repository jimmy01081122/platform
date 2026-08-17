# Design Space Exploration Protocol

## 1. DSE 層級

### Workload layer

- request rate、burstiness、sequence/task size。
- dense/MoE 或其他 workload class。
- reuse、skew、dependency depth、transfer size。

### Platform layer

- discrete link latency/bandwidth、VRAM capacity、copy engines。
- integrated shared bandwidth、coherence cost、cache ownership。
- CPU/GPU/firmware frequency range。

### Algorithm layer

- policy、prediction horizon、batching、priority、replacement、tile or queue strategy。

### Microarchitecture layer

- issue width、queue depth、table entries、banking、pipeline stages、number of engines。

### Physical layer

- target frequency、library/corner、SRAM implementation、area/power constraints。

## 2. 搜尋策略

不強制指定演算法。可使用：

- grid/random search。
- Latin hypercube。
- Bayesian optimization。
- evolutionary search。
- active learning。
- hand-guided local refinement。

但每個 search 必須記錄 seed、budget、pruning rule、invalid configuration 與 objective definition。

## 3. 多保真流程

```text
Level 0 analytical filter
-> Level 1 event simulator
-> Level 2 calibrated system model
-> Level 3 RTL cycle model
-> Level 4 synthesis / P&R subset
```

低保真模型必須以高保真樣本校正。禁止只在 analytical model 取最佳點後直接宣稱物理最佳。

## 4. Objectives

建議以 Pareto 而非單一加權分數保存：

- latency / p95 / p99。
- throughput。
- bytes moved / bandwidth pressure。
- CPU or firmware cycles。
- RTL area / frequency。
- dynamic/leakage power。
- energy per task/token/event。
- robustness across workloads。

## 5. 結果格式

每個 DSE point 是一個普通 run，具有完整 manifest。彙總器只讀取 run metrics，不直接修改原始結果。

必要產出：

- all_points.csv 或 parquet。
- pareto_points.csv。
- invalid_points.json。
- search_manifest.json。
- calibration_report.md。
- recommendation.md，包含適用區域與失敗區域。
