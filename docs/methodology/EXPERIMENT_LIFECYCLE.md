# Experiment Lifecycle

## S0 Contract and Environment

輸入：問題敘述、platform profile、workload source、初始 baseline。

必要輸出：

- experiment spec。
- parameter provenance。
- environment/tool record。
- falsification condition。

S0 不限制後續工具，但要求問題可被反證，且未知參數必須標記為 measured、derived、vendor、assumed 或 swept。

## S1 Workload and Trace Characterization

目的：確認真正的事件、資料流、瓶頸、飽和區與變異來源。

最少包含：

- trace schema validation。
- workload distribution，而非單一點。
- measurement overhead 或 trace completeness 說明。
- discrete/integrated profile 的語意映射。
- 失敗與缺失資料紀錄。

## S2 Executable Reference Model and Simulator

建立可執行 reference model，定義演算法的正確輸出；再建立一個或多個速度／精度不同的 simulator。

Simulator 必須有：

- resource model。
- queue/backpressure。
- data movement 或 shared-memory contention。
- parameter provenance。
- calibration report。
- sensitivity range。

不要一開始耦合所有 cycle-level 工具。只有當較粗模型無法區分候選設計時才增加精度。

## S3 Algorithm Exploration Loop

每個演算法 revision 都要保存：

- semantic revision。
- baseline set。
- correctness oracle。
- parameter sweep。
- failure cases。
- resource demand。

允許 agent 使用 heuristic、search、optimization、ML predictor 或 hand-designed policy。限制不是演算法形式，而是必須能重播、比較與映射到 software/firmware/RTL。

## S4 Software/Firmware/Hardware Break-even

同一演算法至少比較：

1. host software。
2. firmware 或 embedded control core。
3. hardware fast path estimate / RTL-backed path。

輸出不是單一勝負，而是 workload size、event rate、queue depth、transfer cost 與 clock assumptions 下的 break-even surface。

## S5 Basic Full-Datapath RTL

RTL 不要求完整 CPU/GPU SoC，但必須包含 proposed 機制的完整資料路徑與 backpressure。詳見 `RTL_FULL_DATAPATH_DEFINITION.md`。

必要輸出：

- transaction-level reference equivalence。
- normal、boundary、error、backpressure tests。
- reproducible simulation command。
- synthesis constraints。
- unsupported behavior list。

## S6 Physical Feasibility and DSE

先執行低成本 DSE，再對 Pareto 候選執行較昂貴流程。

輸出：

- parameter space 與 invalid region。
- Pareto front。
- timing/area/power provenance。
- activity source。
- simulator-to-RTL calibration delta。

## S7 Cross-layer Closure and Reproduction

固定一個 experiment pack，在乾淨環境依序重跑：

```text
trace validation
-> reference model
-> simulator baseline/proposed
-> break-even
-> RTL regression
-> synthesis/physical subset
-> summary report
```

只有 S7 能宣稱整條流程可重複；仍不得超出平台與 workload 邊界。
