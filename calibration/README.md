# calibration/ — 模型形式、擬合與 sealed held-out

**狀態：待建（Stage A1 / A4）。目前為空骨架。**

規格見 [`../PLATFORM_FLOW_SPECIFICATION.md`](../PLATFORM_FLOW_SPECIFICATION.md) §7–8。

## 現況：四項 gate 全數失敗，根因是模型形式錯誤

`evidence/gpu_measurements/rtx-pro-6000-v3-20260718/rtx-q1-validation-report.json`（保留為證據，不得刪改）：

| metric | MAPE | gate |
|---|---|---|
| component_latency | 304.418% | FAIL |
| moe_replay_tpot | 293.936% | FAIL |
| pcie_transfer_latency | 66.879% | FAIL |
| moe_replay_throughput | 60.658% | FAIL |

90 點中 17 通過。門檻 MAPE 15% / single-point APE 20%。

**量測本身極可信**（n=5、95% CI 寬約 0.0002 ms、跨 split 重現到小數第四位）。失敗來自三項結構性缺陷：

| 項目 | 現況 | 修正方向 |
|---|---|---|
| copy-engine contention | `stream_latency_factors {1:1.0, 2:1.7385, 4:2.7630}` 被當成 per-transfer latency 乘數，但實測 1/2/4 stream 的單筆延遲皆為 ~3.113 ms，故恰好只有 1-stream 的 10 點通過 | 改為聚合頻寬／佔用模型：N 條 stream 共享 copy engine 頻寬，單筆延遲不變、總完成時間變長 |
| component service | `gpu_service.operation_ms` 僅 4 個常數；48 點跨 0.016–1.136 ms（70 倍），預測只取 8 個相異值 | 以 operand shape 參數化回歸（tokens × hidden × experts × dtype） |
| MoE replay | concurrency 1 與 4 的預測幾乎相同，實測差約 4 倍 | 加入 batching / concurrency 項 |
| 小尺寸傳輸 | 單一 intercept 0.0153 ms，實測下限約 0.037 ms，小尺寸點低估 55–59% | piecewise 或 `max(fixed_overhead, linear)`。**KV block 2 MiB 正落在此區間** |

同一參數檔的 `contention.per_extra_concurrency = 1.0046`（幾乎無競爭）與那組 stream factor 自相矛盾——光靠 calibration split 即可證明模型形式有誤，不需動用 held-out。

## Sealed held-out 協定

```text
1. 事前登記模型形式與預期方向 -> experiments/specs/<id>.yaml
2. 設計新量測的 fit / validation / held-out split
3. split 定義與資料 hash 封存
4. 只用 fit 擬合；validation 可用於模型選擇
5. 模型與參數凍結（凍結物 hash 記錄）
6. held-out 開封，評分一次
7. 結果寫入報告，無論通過與否
```

**開封次數必須為 1 且可稽核。** 開封後若未通過，記錄未通過並重新設計實驗，不得回頭調模型再開第二次。

**`evidence/` 中所有既有量測一律只能作 FIT 與模型開發用。** 理由：上述診斷過程曾檢視 validation split 殘差，獨立性已受汙染。資料集內部既有的 `fit_role`（含 `HELD_OUT`、`CONTROL`）在本平台不得作為 held-out 宣稱。

## 重用，不要重寫

`src/edgeflow/calibrated_backend.py`（516 行）已具備四 split 強制、artifact 路徑重用拒絕、`record_id` 跨 split 碰撞拒絕、SHA-256 驗證、環境 manifest 強制、非物理擬合拒絕（slope ≤ 0 或 intercept < 0）、無 fallback 常數。**只替換模型形式函式。**
