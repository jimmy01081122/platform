# Software / Firmware / Hardware Break-even Protocol

## 1. 比較單位

三種實作必須處理相同 work unit，例如：

- 一個 task descriptor。
- 一個 completion event。
- 一次 expert dispatch。
- 一次 tile scheduling decision。
- 一次 prefetch/residency update。

禁止用 software 的完整流程與 hardware 的單一 primitive 直接比較。

## 2. 成本分解

```text
T_total = T_prepare + T_queue + T_execute + T_sync + T_move + T_recovery
```

至少量測或掃描：

- fixed invocation cost。
- per-item cost。
- batching effect。
- queueing delay。
- cache/warm-state effect。
- data transfer or shared-memory contention。
- synchronization and completion cost。
- firmware instruction/cycle count。
- hardware pipeline latency and initiation interval。

## 3. 實作層級

### Host software

- native optimized baseline。
- thread affinity、compiler flags、warmup 與 timing source 必須記錄。
- 可包含同步與非同步版本。

### Firmware

- 使用真實 firmware code，而不是只用固定 cycle 常數。
- 可在 ISA simulator、gem5、QEMU、RTL core 或開發板上執行。
- 記錄 instruction count、cycles、memory accesses、interrupt/MMIO cost。

### Hardware

- S4 初期可用 analytical pipeline model。
- S5 後以 RTL cycle count 與 synthesis frequency 回填。
- 必須包含 queue、buffer、table、memory interface 與 backpressure 成本。

## 4. Break-even 輸出

輸出至少包含：

- `event_rate x work_size` surface。
- `queue_depth x burstiness` surface。
- `software_frequency x firmware_frequency x rtl_frequency` sensitivity。
- discrete transfer cost 或 integrated contention cost。
- 95% interval 或多次 run dispersion。

Break-even 不是「硬體一定較快」，而是標出：

- software preferred region。
- firmware preferred region。
- hardware fast-path preferred region。
- no-valid-solution region。

## 5. 決策

只有當硬體優勢區域與目標 workload 分布有實際交集，才進入 full-datapath RTL。若 firmware 已足夠，保留 firmware 架構並停止硬體化是有效結果。
