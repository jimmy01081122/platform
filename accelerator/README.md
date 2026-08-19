# accelerator/ — 候選處理器模型

**狀態：Stage B2 已建成骨架（可掃描的模擬器元件 + 六動詞 ABI + 掛載點 A1–A6）。**
**無任何 accelerator 收益 / break-even 主張（屬 C1）。A2 與 A6 無量測，不得產生效能結論。**

規格見 [`../PLATFORM_FLOW_SPECIFICATION.md`](../PLATFORM_FLOW_SPECIFICATION.md) §6。

## 模組地圖

| 檔案 | 內容 |
|---|---|
| [`fidelity.py`](fidelity.py) | fidelity 標記閘門：本目錄只允許 `ANALYTICAL` / `PROJECTED`，`MEASURED_SURROGATE`（及一切實機/週期精確標記）在建構時直接拒絕。`Provenance` 攜帶證據錨與 claim limit。 |
| [`resource_model.py`](resource_model.py) | 九個可掃描參數（§6.1）：`ResourceModel`（單點）、`ResourceSweep`（笛卡爾積 + 大小 guardrail）、`from_config`。時間為 fs 整數、時脈為有理數（§4，無漂移）。 |
| [`abi.py`](abi.py) | 六動詞 ABI（§6.3）：`AcceleratorBackend`（`reset`/`can_accept`/`submit`/`advance`/`poll_completions`/`snapshot_counters`）、`Transaction`/`Completion`/`Counters`、`BackendRegistry`（未註冊 backend 直接拒絕的防偽 registry）。 |
| [`backends/`](backends/) | `FUNCTIONAL_POLICY`、`CYCLE_RESOLVED_MODEL`、`REFERENCE_MOCK` 三個已註冊 backend + 共用 queue/pipeline core；`reserved.py` 的三個 RTL/cosim backend 只保留介面、**不註冊**（dispatch 即拒絕）。 |
| [`attachment_points.py`](attachment_points.py) | 掛載點 A1–A6（§6.2），每點三件事：work unit + baseline 成本、候選處理器成本模型、搬運成本。A2/A6 標 `measured=False`、`PROJECTED`、禁止效能結論。 |

配置：[`../configs/accelerator/resource_model_default.yaml`](../configs/accelerator/resource_model_default.yaml)（九參數的預設掃描範圍，錨定 §11.2 STA）。
交付 run：`runs/<ts>__stage_b2_accelerator_model/`（由 [`../scripts/stage_b2_emit_model.py`](../scripts/stage_b2_emit_model.py) 產生）。
測試：[`../tests/test_accelerator.py`](../tests/test_accelerator.py)。

## 九個可掃描資源參數（§6.1）

`pipeline_latency_cycles`、`issue_width`、`local_sram_capacity_bytes`、
`memory_bandwidth_bytes_per_s`、`queue_depth`、`operations_per_cycle`、
`clock_frequency_hz`（時脈域，有理數）、`area_proxy_um2`、`power_proxy_mw`。
全部可由 config 掃描；C1 掃它們。

## 六動詞 backend ABI（§6.3）

```text
reset   can_accept   submit   advance   poll_completions   snapshot_counters
```

已註冊（本階段可執行）：`FUNCTIONAL_POLICY`、`CYCLE_RESOLVED_MODEL`、`REFERENCE_MOCK`。
保留介面（不註冊、下游用）：`RTL_TRACE_REPLAY`、`VERILATOR_COSIM`、`RTL_CALIBRATED_SURROGATE`。
reference mock 用來驗證 transaction adapter、clock stepping、backpressure、completion、counter 五條路徑。

## 六個掛載點（§6.2）

| ID | 功能 | 優先序 | 量測 |
|---|---|---|---|
| A1 | routing / gating 決策計算、top-k | 主 | routing `.npy`、CTRL-PX0-\*-routing、OFF-E-PR\* |
| A2 | MoE dispatch 資料搬運 | 主 | **無**（GPU 軌優先序 1） |
| A3 | transfer 排程 / DMA descriptor / prefetch 發射 | 主 | transfer 微基準 v1–v4 |
| A4 | expert 解壓縮 / 壓縮搬運 | 主 | `expert_decompressor.sv` 307–811 MHz |
| A5 | KV block 管理 / offload | 次 | SWAP-K1/K2/K5（block_size=0 限制） |
| A6 | offloaded KV 上的 attention | 次 | **無**（GPU 軌優先序 2） |

> 掛載點 A1–A6 與**階段** A1–A4 是不同的東西，見 [`../docs/PHASE_NAMING_MAP.md`](../docs/PHASE_NAMING_MAP.md)。

work-unit 粒度是 C1 的敏感度軸（guide §7）：A1=per_layer、A3/A4/A5/A6=per_block、A2=per_layer（皆列為可翻轉 break-even 的 C1 掃描軸）。

## 硬性規則（不可放寬）

- 本目錄所有元件標 `ANALYTICAL` 或 `PROJECTED`，**不得**標 `MEASURED_SURROGATE`（`fidelity.py` 在建構時強制）。
- 未註冊的 backend 必須直接拒絕執行，不得靜默替換為較低 fidelity 的實作（`abi.py` 的 registry 強制；另見 [`../src/edgeflow/multifidelity.py`](../src/edgeflow/multifidelity.py) 既有的防偽設計，未動）。
- A2 與 A6 沒有量測支撐，在取得實機資料前不得產生效能結論（`attachment_points.py` 以 `measured=False` + `PROJECTED` 強制）。
