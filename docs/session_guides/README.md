# Session 作業指引

每個階段在**獨立 session** 執行，避免上下文汙染與長對話造成的判斷劣化。

因此冷啟動的 session 沒有前一階段的記憶。這帶出一個必須正面處理的問題：**敘述性的交接報告會過期或寫錯。** 舊 repo 就出過這個問題——`execution_ledger.json` 的 `active_gpu_guard` 仍指向已失聯的 attempt，`next_gpu_unit_after_inflight` 停在 guard 執行前的舊值，接手者需要額外一輪才能釐清真實狀態。

所以本系統的交接不靠敘述，靠**可執行的檢查**。

---

## 怎麼開一個 session

1. 從下表找到要執行的階段。
2. 開新 session，貼上該份指引第 0 節的**啟動 prompt**。
3. session 自己會去讀指引、跑進入檢查、執行、完工交付。

啟動 prompt 刻意寫得很短——它不重述指引內容，只指路。重述會造成兩份來源不一致。

---

## 階段一覽

| 階段 | 指引 | 需要 GPU | 深度 | 前置 |
|---|---|---|---|---|
| **A1** calibration 模型形式修復 | [`STAGE_A1_CALIBRATION_MODEL_FORM.md`](STAGE_A1_CALIBRATION_MODEL_FORM.md) | 否 | 可執行 | Stage 0 |
| **A2** measured → 九類 IR | [`STAGE_A2_MEASURED_TO_IR.md`](STAGE_A2_MEASURED_TO_IR.md) | 否 | 可執行 | Stage 0 |
| **A3** IR → 引擎 loader | [`STAGE_A3_IR_TO_ENGINE.md`](STAGE_A3_IR_TO_ENGINE.md) | 否 | 可執行 | A2 |
| **A4** sealed held-out 驗證 | [`STAGE_A4_SEALED_HOLDOUT.md`](STAGE_A4_SEALED_HOLDOUT.md) | **是** | 可執行 | A1 + A3 |
| **B1** KV + continuous batching | [`STAGE_B1_KV_BATCHING.md`](STAGE_B1_KV_BATCHING.md) | 否 | 規則為主 | A3 |
| **B2** 參數化候選處理器 | [`STAGE_B2_ACCELERATOR_MODEL.md`](STAGE_B2_ACCELERATOR_MODEL.md) | 否 | 規則為主 | A3 |
| **C1** co-design DSE + break-even | [`STAGE_C1_CODESIGN_DSE.md`](STAGE_C1_CODESIGN_DSE.md) | 否 | 規則為主 | A4 + B2 |
| **C2** HW0 需求 + LM18 handoff | [`STAGE_C2_HW0_RTL_HANDOFF.md`](STAGE_C2_HW0_RTL_HANDOFF.md) | 否 | 規則為主 | C1 |
| **GPU 軌** 實機量測 | [`TRACK_GPU_MEASUREMENT.md`](TRACK_GPU_MEASUREMENT.md) | **是** | 可執行 | Stage 0，可並行 |

**A1 與 A2 沒有相依關係，可由兩個 session 並行執行。** B1 與 B2 同理。

**深度說明**：「可執行」代表指引含具體檔案路徑、已驗證的數字與逐步做法，可直接動手。「規則為主」代表目標、約束、驗收與交接格式已固定，但實作細節依賴尚未存在的前階段結果，標為待補——這比硬寫出日後要大改的細節誠實。

---

## 三條跨 session 硬規則

**1. 進入檢查是指令，不是敘述。**

每份指引第 2 節列出具體指令與預期輸出。任一不符即停止並回報，不得「看起來差不多就繼續」。前一個 session 的報告只是參考；`governance/stage_ledger.yaml` 加上**實際重跑的指令**才是依據。

**2. 只讀本階段的指引。**

不要讀其他階段的指引。理由是避免提前套用後期階段的假設，或把後期尚未成立的結論當成已成立。需要背景時讀根規格 `PLATFORM_FLOW_SPECIFICATION.md`，那是唯一的權威來源。

**3. 狀態改為 `COMPLETE` 前，`verification` 每一條都必須實際執行並貼上實際輸出。**

只有該階段的 session 可以修改 ledger 中自己那一列，不得改動或刪除其他階段的列。

---

## 每份指引的固定結構

```text
0. 啟動 prompt（可直接貼上）
1. 這個 session 的單一目標
2. 進入檢查（指令 + 預期輸出）
3. 授權邊界（可改什麼 / 絕對不可改什麼）
4. 進入時的事實基線
5. 工作步驟
6. 驗收條件
7. Claim boundary（已成立 / 可新增 / 仍禁止）
8. 失敗處理與必須詢問 owner 的條件
9. 完工交付
```

第 7 節是雙向鎖：同時寫明「進入時哪些主張已成立」與「本階段結束後**仍然**禁止的主張」，避免冷啟動 session 從殘留印象推論。

---

## 必讀的共同文件

| 文件 | 用途 |
|---|---|
| `PLATFORM_FLOW_SPECIFICATION.md` | 根規格。權威來源，指引引用其節號 |
| `governance/stage_ledger.yaml` | 跨 session 的單一狀態真相來源 |
| `docs/PHASE_NAMING_MAP.md` | 五套並存命名的對照。**第一次接手務必讀**，否則極易誤判進度 |
| `AGENTS.md` | 不可繞過的工程契約 |
| `docs/status/` | 五份狀態文件，每階段結束更新 |

---

## 環境

```bash
cd /home/a/platform
make venv          # 只需一次；建立 .venv 並安裝釘選版本
make test          # 317 Python + 14 CTest
make verify-evidence
make doctor
```

手動執行時必須帶兩個環境變數（Makefile 已自動設定）：

```bash
export PYTHONPATH=$PWD:$PWD/src
export PYTHONDONTWRITEBYTECODE=1
```

`PYTHONDONTWRITEBYTECODE` 不可省略：`phase7_d0_r4` 的治理測試斷言 application package 的**精確檔案集合**，Python 自動產生的 `__pycache__` 會讓 19 項測試失敗。
