# accelerator/ — 候選處理器模型

**狀態：待建（Stage B2）。目前為空骨架，無任何實作與主張。**

規格見 [`../PLATFORM_FLOW_SPECIFICATION.md`](../PLATFORM_FLOW_SPECIFICATION.md) §6。

## 要建什麼

1. **參數化資源模型**：pipeline latency、issue width、local SRAM capacity、memory bandwidth、queue depth、operations per cycle、clock domain、area proxy、power proxy。全部可掃描。

2. **六動詞 backend ABI**

   ```text
   reset   can_accept   submit   advance   poll_completions   snapshot_counters
   ```

   本階段實作兩個 backend：`FUNCTIONAL_POLICY`、`CYCLE_RESOLVED_MODEL`，外加一個 reference mock 元件用來驗證 transaction adapter、clock stepping、backpressure、completion 與 counter 路徑。

   `RTL_TRACE_REPLAY`、`VERILATOR_COSIM`、`RTL_CALIBRATED_SURROGATE` 只保留介面，留給下游。

3. **六個掛載點**，每點必須定義：可卸載的工作單位與其 baseline 成本、在候選處理器上的成本模型、把資料送過去與取回的搬運成本。

   | ID | 功能 | 優先序 | 現有量測 |
   |---|---|---|---|
   | A1 | routing / gating 決策計算、top-k | 主 | routing `.npy`、OFF-E-PR\*、CTRL-PX0-\*-routing |
   | A2 | MoE dispatch 資料搬運（token permutation、gather/scatter） | 主 | **無** |
   | A3 | transfer 排程 / DMA descriptor / prefetch 發射 | 主 | transfer 微基準 v1–v4 |
   | A4 | expert 解壓縮 / 壓縮搬運 | 主 | `expert_decompressor.sv` 307–811 MHz |
   | A5 | KV block 管理 / offload | 次 | SWAP-K1/K2/K5 |
   | A6 | offloaded KV 上的 attention | 次 | **無** |

## 硬性規則

- 本目錄所有元件標 `ANALYTICAL` 或 `PROJECTED`，**不得**標 `MEASURED_SURROGATE`。
- 未註冊的 backend 必須直接拒絕執行，不得靜默替換為較低 fidelity 的實作（見 `src/edgeflow/multifidelity.py` 既有的防偽設計，不要拿掉）。
- A2 與 A6 沒有量測支撐，在取得實機資料前不得產生效能結論。
