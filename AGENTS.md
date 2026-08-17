# AGENTS.md

## 1. 任務

你的任務是持續擴充並驗證一套 CPU–GPU edge accelerator exploration flow。目標是建立可執行、可校正、可比較、可重複的工程閉環，而不是證明預先指定的架構一定有效。

## 2. Agent 自主權

允許主動執行下列工作：

- 新增或替換 simulator、profiler、compiler、firmware、RTL、formal、synthesis、P&R、power 或 DSE 工具。
- 建立新的演算法、微架構、記憶體模型、排程策略與 baseline。
- 在 `explorations/` 建立並行分支，不必等待單一方向完全結束。
- 依實驗結果調整研究問題、參數範圍、模型精度與驗證深度。
- 使用 Docker、Podman、Nix、Conda、venv 或原生建置，但必須記錄版本與建置方式。
- 在有合理理由時跳過昂貴工具，先以較低成本模型取得方向性證據。

不要求優先使用任何特定工具。工具選擇以能否回答問題、輸入輸出契約是否清楚、是否可重現為準。

## 3. 不可繞過的工程契約

Agent 不得：

1. 修改 `data/raw/` 原始資料；只能產生新的 canonical 或 derived dataset。
2. 以未註冊來源的頻寬、延遲、時脈、功耗、expert size 或 service time 產生結論。
3. 只比較 proposed 與明顯過弱的 baseline。
4. 讓 simulator、software、firmware、RTL 各自使用不同演算法語意，卻仍宣稱跨層一致。
5. 以控制 FSM、MMIO register 或不可搬移資料的 scaffold 取代 full datapath。
6. 以 Yosys cell count 直接宣稱實體面積、功耗或產品可行性。
7. 覆寫失敗結果、只保留最佳 run，或手動修改 generated metrics。
8. 將模型估計寫成實機量測，或將 RTL pass 寫成端到端效能已驗收。

## 4. 證據優先，不限制探索

每個探索可以自由選擇路徑，但必須產生可稽核的 evidence chain：

```text
Problem observation
-> workload and parameter provenance
-> executable baseline
-> proposed algorithm
-> calibrated model or simulator
-> break-even surface
-> RTL transaction equivalence
-> physical feasibility or explicit stop boundary
```

流程不是固定瀑布。若新資料推翻假設，可以回到前一階段重建模型；若某一層不是問題來源，可以記錄後跳過該硬體方向。

## 5. 每次工作必須維護

- `docs/status/CURRENT_STATUS.md`
- `docs/status/AGENT_HANDOFF.md`
- `docs/status/DECISION_LOG.md`
- `docs/status/ASSUMPTION_REGISTER.md`
- `docs/status/VALIDATION_MATRIX.md`
- 對應 `experiments/specs/<experiment_id>.yaml`
- 對應 `runs/<run_id>/manifest.json`

每次交回時列出：

- 新增、修改、刪除的檔案。
- 執行命令與返回碼。
- 通過、失敗、未執行的檢查。
- 新增的假設與其來源。
- 下一個最高資訊增益的實驗。

## 6. 實驗輸出規則

每個 run 目錄至少包含：

```text
manifest.json
resolved_config.yaml
logs/command.log
logs/stdout.log
logs/stderr.log
metrics.json
artifacts/
environment/tool_versions.json
```

失敗 run 也必須保留 manifest、log 與 failure classification。

## 7. 分層語意

- `S1` trace/characterization：證明工作負載與問題存在。
- `S2` reference/simulator：證明模型可執行、可校正、可做 counterfactual。
- `S3` algorithm：證明演算法相對合理 baseline 的效果與邊界。
- `S4` break-even：證明 software、firmware、hardware 分工何時成立。
- `S5` RTL：證明最小完整 datapath 的功能、backpressure 與 transaction semantics。
- `S6` physical/DSE：證明參數、面積、頻率、功耗與效能的 trade-off。
- `S7` closure：用固定 experiment pack 從頭重現主張。

不得跨層誇大證據。

## 8. 平台規則

所有實驗都必須指定一個 platform profile。禁止用 `edge` 一詞替代具體記憶體與互連模型。

- Discrete profile 必須明確定義 host DRAM、GPU VRAM、device link、DMA/copy 資源與資料 ownership。
- Integrated profile 必須明確定義共享記憶體、一致性模式、cache ownership、共享頻寬與 CPU/GPU contention。

## 9. 既有 HOP 資產

HOP 的 task descriptor、dependency scoreboard、ready queue、event engine、data-location table、DMA descriptor 與 MMIO 對齊方法可以重用，但必須：

- 保留原始來源與 revision。
- 先建立 semantic mapping。
- 以本專案 workload 重跑。
- 不沿用舊效能數字作為新平台結論。

## 10. 何時應停止某條探索

只有在以下情況停止或暫停：

- 問題在可接受的 profiling 精度下不存在。
- proposed 不優於合理 baseline，且敏感度分析沒有可行區域。
- software/firmware 已足夠，不需要硬體 fast path。
- full datapath 成本抵銷系統收益。
- 關鍵參數無法取得、校正或以範圍方式處理。

停止一條探索不是專案失敗；必須把負面結果轉成 reusable boundary condition。
