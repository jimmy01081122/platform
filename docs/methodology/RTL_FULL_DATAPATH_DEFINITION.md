# Basic Full-Datapath RTL Definition

## 1. 完成定義

「基礎 RTL Full Datapath」不是 full chip，也不是完整 GPU。它是 proposed mechanism 從輸入 transaction 到輸出 transaction 的最小完整硬體閉環。

必須包含：

```text
Input interface / descriptor
-> decode and validation
-> state tables / buffers
-> proposed algorithm datapath
-> scheduler / issue path
-> memory, DMA, compute or device request interface
-> response / completion path
-> error and backpressure handling
```

## 2. 可以抽象的部分

可以抽象：

- GPU compute unit，以 request/response latency interface 表示。
- DRAM/VRAM，以 ready/valid memory model 表示。
- CPU，以 MMIO/descriptor producer 表示。
- NoC，以 bounded-latency/credit interface 表示。

抽象介面必須保留 timing、capacity、ordering、failure 與 backpressure 語意。

## 3. 不可省略

- proposed 核心資料路徑。
- 主要 state update。
- queue/table 容量限制。
- memory request generation。
- completion/commit。
- stall、full、empty、retry 或 drop semantics。
- reset 與非法輸入處理。

只做模式 FSM、register map、固定輸出或不可實際流動資料的 mock，不符合 full datapath。

## 4. 跨層對齊

Reference model、simulator 與 RTL 必須共用：

- descriptor/event schema。
- algorithm semantic revision。
- ordering rule。
- overflow policy。
- tie-breaking rule。
- precision/quantization rule。
- error behavior。

## 5. 驗證集合

至少包含：

- single transaction。
- sustained stream。
- burst and idle gaps。
- minimum/maximum sizes。
- queue full and backpressure。
- out-of-order response。
- duplicate/stale completion。
- table miss and invalid address/state。
- reset during idle and controlled reset recovery。
- randomized reference-model comparison。

## 6. Synthesis 與 activity

- Yosys mapping 可作邏輯可綜合與初步結構分析。
- timing 必須使用具體 library 與 constraint。
- power 必須說明 activity 來源；VCD/SAIF 不得由不代表 workload 的單一 smoke test取代。
- P&R 前後結果分開報告。
