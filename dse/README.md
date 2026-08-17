# dse/ — Co-design Design Space Exploration

**狀態：待建（Stage C1）。目前為空骨架。**

規格見 [`../PLATFORM_FLOW_SPECIFICATION.md`](../PLATFORM_FLOW_SPECIFICATION.md) §10。

## 掃描維度

**候選 function（A1–A6） × processor 規格 × workload regime**

敏感度軸：expert 容量、KV 容量、H2D/D2H 頻寬與延遲、queue depth、prefetch lookahead、predictor 品質、expert placement、壓縮率與解壓成本、copy 併發度、arrival pattern 與 batch、context 與 output 長度。

## 三條不可放寬的規則

**1. Baseline 必須夠強。** 至少 no-prefetch、LRU、FIFO、static popularity。不得只與明顯過弱的 baseline 比較。

**2. Prefetch 必須用 causal predictor 當主結論。** 完美 lookahead oracle 只能標為上界。

既有量測（`data/canonical/moe_routing_v1/w3_prefetch_predictability.json`，四個生產級 MoE 模型）的 retained median：

| predictor | Qwen3-235B | DeepSeek-R1 | Llama-4-Maverick | Kimi-K2 |
|---|---|---|---|---|
| persistence | 0.0% | 0.0% | 0.0% | 0.0% |
| frequency | 2.6% | 1.4% | 0.0% | 0.8% |
| markov1 | **15.4%** | 5.3% | **−0.5%** | 7.7% |

oracle 本身的 stall reduction median 為 90–99.9%。最好的 causal predictor 只保留約 15%，最差為負值，persistence 在四個模型上完全無效。用 oracle 當主結論會系統性且大幅高估加速器價值。

**3. Break-even 必須用完整分解，輸出是面不是勝負。**

```text
T_total = T_prepare + T_queue + T_execute + T_sync + T_move + T_recovery
```

禁止以 software 的完整流程對比 hardware 的單一 primitive。firmware 必須是真實程式碼，不得以週期常數代替。結果須涵蓋「無可行解」區域。

每項改善同時報告：收益、控制成本、記憶體成本、頻寬成本、失敗案例、regression case、敏感度、break-even。

## 允許且必須保留的結論

「此 operating region 不需要加速器」是有效結論，須轉成可重用的 boundary condition。

這不是形式上的免責。現有窄域量測已經指向這個方向：expert 容量掃描顯示控制決策率僅 111–3073 decisions/s，而每個 336 MiB expert 物件的 H2D 要 12.49 ms；既有 STA 顯示 residency engine 即使最差配置（ME=256，50.56 MHz）仍有 1.6×10⁴–4.5×10⁵ cycles/決策的餘裕。**但該區間是單請求、eager、159 tokens**，長上下文與高並發完全沒有量測——所以目前這只是窄域事實，不是結論，兩個方向都還沒有資格下判斷。

## 既有可重用的 DSE 資產

`scripts/w3_*.py` 已實作並有已提交輸出：capacity DSE、copy-engine DSE、compression DSE、prefetch predictability、robustness、device timing、request schedule、convergence test。這些跑在 `src/edgeflow/residency.py` 的解析模型上，**不是** C++ 引擎——接上引擎後需重跑並比對。
