# Stage C1 · Co-design DSE 與 break-even

```text
GUIDE_COMPLETENESS = RULES_ONLY
```

**本指引固定目標、約束、驗收與交接格式；掃描範圍與輸出格式待 A4 的 calibrated envelope 確定後補。**

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 Stage C1 的執行者。

工作區：/home/a/platform

本 session 的單一目標：掃描「候選 function × processor 規格 × workload regime」，
產出 break-even 面，回答哪一項功能值得做成硬體、規格下限是多少、什麼條件下不值得做。

這是整個專案的核心問題。純 CPU 工作。

注意：本指引標記為 RULES_ONLY——約束與驗收已固定，掃描範圍需你依 A4 的
calibrated envelope 決定。超出 envelope 的一律標 PROJECTED。

開始前完整讀取：
  docs/session_guides/STAGE_C1_CODESIGN_DSE.md   ← 本階段指引
  PLATFORM_FLOW_SPECIFICATION.md                  ← 根規格，特別是 §10 DSE 與 break-even
  docs/methodology/BREAK_EVEN_PROTOCOL.md         ← break-even 分解方法
  docs/methodology/DSE_PROTOCOL.md                ← DSE 流程與必要輸出
  governance/stage_ledger.yaml                    ← 狀態真相來源
  dse/README.md                                   ← 三條不可放寬的規則

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不讀其他階段的指引。
  3. 進入檢查任一項不符即停止並回報。

本階段特有：「此 operating region 不需要加速器」是有效且必須保留的結論。
你的任務不是證明加速器有用，是誠實回答它在什麼條件下有用。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 目標

在 calibrated envelope 內掃描候選功能與處理器規格，產出 break-even **面**（不是單一勝負），並明確標示無可行解的區域。

---

## 2. 進入檢查

```bash
cd /home/a/platform
make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：0 failed
grep -A3 'id: STAGE_C1' governance/stage_ledger.yaml   # 預期：status: NOT_STARTED
grep -A3 'id: STAGE_A4' governance/stage_ledger.yaml   # 預期：status: COMPLETE
grep -A3 'id: STAGE_B2' governance/stage_ledger.yaml   # 預期：status: COMPLETE
```

**必須先讀 A4 的判定結果與 `APPLICABLE_ENVELOPE`**（在 `docs/status/AGENT_HANDOFF.md` 與 `VALIDATION_MATRIX.md`）。

若 A4 判定為 `FAIL` 或 `INSUFFICIENT_EVIDENCE`，**本階段的所有結論都必須標 `PROJECTED`**，且不得宣稱任何 break-even 為 calibrated 支撐。此時應先與 owner 確認是否繼續。

---

## 3. 授權邊界

**可修改**：`dse/`（主要產出）、`experiments/specs/`、`runs/`、`docs/status/`、ledger 的 `STAGE_C1` 列。

**不可修改**：`evidence/**`、`accelerator/`（屬 B2）、`hardware/**`、ledger 其他列、A4 的 sealed manifest 與判定結果。

---

## 4. 三條不可放寬的規則

### 4.1 Baseline 必須夠強

至少 no-prefetch、LRU、FIFO、static popularity。**不得只與明顯過弱的 baseline 比較**（`AGENTS.md` §3.3）。

### 4.2 Prefetch 主結論必須用 causal predictor

完美 lookahead oracle **只能標為上界**。

既有量測（`data/canonical/moe_routing_v1/w3_prefetch_predictability.json`，四個生產級 MoE 模型）顯示 causal past-only predictor 保留的 oracle 收益如下（retained median）：

| predictor | Qwen3-235B | DeepSeek-R1 | Llama-4-Maverick | Kimi-K2 |
|---|---|---|---|---|
| persistence | 0.0% | 0.0% | 0.0% | 0.0% |
| frequency | 2.6% | 1.4% | 0.0% | 0.8% |
| markov1 | **15.4%** | 5.3% | **−0.5%** | 7.7% |

同一份資料的 oracle stall reduction median 為 90–99.9%。

也就是說：**最好的 causal predictor 也只保留約 15% 的 oracle 收益，最差的情況是負的**（markov1 在 Llama-4-Maverick 上比不做 prefetch 更差），而 persistence 在四個模型上完全無效。

這個差距大到足以翻轉結論——用 oracle 當主結論會系統性、且大幅高估加速器價值。

### 4.3 Break-even 必須用完整分解，輸出是面不是勝負

```text
T_total = T_prepare + T_queue + T_execute + T_sync + T_move + T_recovery
```

- **禁止以 software 的完整流程對比 hardware 的單一 primitive。**
- firmware 必須是真實程式碼，不得以週期常數代替。
- 結果須涵蓋「無可行解」區域。
- 每項改善同時報告：收益、控制成本、記憶體成本、頻寬成本、失敗案例、regression case、敏感度、break-even。

---

## 5. 掃描維度（規格 §10.1）

**候選 function（掛載點 A1–A6）× processor 規格 × workload regime**

敏感度軸：expert 容量、KV 容量、H2D/D2H 頻寬與延遲、queue depth、prefetch lookahead、predictor 品質、expert placement、壓縮率與解壓成本、copy 併發度、arrival pattern 與 batch、context 與 output 長度。

**必須納入前階段標示的選擇性軸**：B1 的 KV/expert 仲裁規則、B2 的 work unit 粒度。這些在前階段被標為「會影響結論的選擇」，此處必須掃描而非固定。

---

## 6. 誠實性要求

### 6.1 目前證據指向的方向

現有窄域量測顯示：expert 容量掃描的控制決策率僅 **111–3073 decisions/s**，而每個 336 MiB expert 物件的 H2D 要 **12.49 ms**；既有 STA 顯示 residency engine 即使最差配置（ME=256，50.56 MHz）仍有 **1.6×10⁴–4.5×10⁵ cycles/決策**的餘裕。

**但該區間是單請求、eager、159 tokens、`max_num_seqs=1`。** 長上下文與高並發完全沒有量測。

**所以目前兩個方向都還沒有資格下判斷。** 不要因為窄域數字看起來像「控制不是瓶頸」就預先收斂到那個結論；也不要因為專案目標是共同設計就預設加速器有用。

### 6.2 「不需要加速器」是有效結論

依 `AGENTS.md` §10，負面結果必須轉成**可重用的 boundary condition**：在什麼容量、頻寬、並發、context 長度之下不需要，超過哪個門檻才需要。

這比含糊的「有幫助」更有價值，也更可能是真的。

### 6.3 掛載點 A2 與 A6 無量測

兩者的成本模型是 `ANALYTICAL`。涉及它們的 break-even 一律標 `PROJECTED`，並在報告中明確列出「若要把此結論升級為 calibrated，需要哪些量測」——這份清單直接餵給 GPU 軌。

---

## 7. 驗收條件

| 項目 | 判準 |
|---|---|
| baseline 齊備 | 至少 no-prefetch / LRU / FIFO / static popularity |
| prefetch 誠實性 | 主結論用 causal predictor；oracle 僅標上界且明確標示 |
| break-even 分解 | 六項分解齊備；未以 software 全流程對比 hardware 單一 primitive |
| 輸出是面 | 含「無可行解」區域，非單一勝負 |
| envelope 遵守 | 超出 A4 calibrated envelope 者標 `PROJECTED` |
| 前階段選擇性軸已掃 | B1 的仲裁規則、B2 的 work unit 粒度皆在掃描軸內 |
| 完整報告 | 每項改善同時報告收益、控制成本、記憶體成本、頻寬成本、失敗案例、regression、敏感度、break-even |
| 必要輸出 | 依 `DSE_PROTOCOL.md`：`all_points.csv`、`pareto_points.csv`、`invalid_points.json`、`search_manifest.json`、`calibration_report.md`、`recommendation.md` |
| 基線未退步 | `make test` → 0 failed |

---

## 8. Claim boundary

**可新增（視結果而定）**

- 「在 ⟨envelope⟩ 內，掛載點 ⟨X⟩ 的 break-even 出現在 ⟨具體條件⟩。」
- 「在 ⟨envelope⟩ 內，掛載點 ⟨Y⟩ 無 break-even，boundary condition 為 ⟨具體條件⟩。」

兩者都必須附不確定度與適用區間。

**仍然禁止**

- 超出 calibrated envelope 的外推未標 `PROJECTED`。
- 以 oracle prefetch 作為主結論。
- 掛載點 A2／A6 的 `MEASURED` 等級主張。
- 任何硬體規格數值（屬 C2）。
- 以 cell count 或 pre-layout STA 宣稱實體可行性。

---

## 9. 待補的細節

執行時需依 A4 的 envelope 決定：具體掃描範圍與步長、哪些軸可固定哪些必須掃、統計方法與不確定度的計算方式、Pareto 篩選準則。

決定後補回本指引，並在 ledger 把 `guide_completeness` 改為 `EXECUTABLE`。

---

## 10. 失敗處理與必須詢問 owner 的條件

**必須停下並詢問**：A4 判定為 FAIL／INSUFFICIENT 而仍要繼續；某掛載點的結論完全取決於一個未量測的參數；掃描結果顯示所有掛載點都無 break-even（這是重要結論，但值得與 owner 確認範圍是否恰當）；需要修改 `evidence/`。

**掃描結果不如預期不是失敗。** 不要調整 baseline 強度、掃描範圍或成本模型來讓某個掛載點看起來有價值。

---

## 11. 完工交付

依 `README.md` 的標準流程。

```text
STAGE: C1
STATUS: COMPLETE | BLOCKED
A4_ENVELOPE: <引用的 calibrated envelope；A4 判定結果>
BASELINES_USED: <清單>
PREFETCH_PREDICTOR: causal <說明>；oracle 上界 <數字>
SWEPT_AXES: <掃描軸與範圍；含 B1 仲裁規則、B2 work unit 粒度>
BREAK_EVEN_BY_ATTACHMENT_POINT:
  A1: <結論 + 條件 + 不確定度 + fidelity>
  A2: <PROJECTED —— 無量測>
  A3: <...>
  A4: <...>
  A5: <...>
  A6: <PROJECTED —— 無量測>
NO_BENEFIT_BOUNDARIES: <無可行解區域的具體條件>
MEASUREMENT_NEEDED_TO_UPGRADE: <要把哪些 PROJECTED 升級為 calibrated 需要什麼量測 —— GPU 軌輸入>
REQUIRED_OUTPUTS: <all_points.csv 等六項的路徑>
BASELINE: <make test 輸出>
EVIDENCE_UNCHANGED: <make verify-evidence 輸出>
FILES_CHANGED / CLAIMS_ADDED / CLAIMS_STILL_FORBIDDEN
NEXT / OWNER_DECISION_NEEDED
```
