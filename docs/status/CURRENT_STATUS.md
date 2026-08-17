# CURRENT_STATUS

```text
updated       : 2026-08-18
platform      : /home/a/platform
repo          : git@github.com:jimmy01081122/platform.git
current stage : Stage 0 完成；A1 尚未開始
```

## 一句話狀態

平台已建立、基線全綠、規格與 session 指引系統就緒；**研究鏈的三個斷點都還沒接上，因此沒有任何 calibrated、break-even 或 accelerator 主張。**

## 階段狀態

| 階段 | 狀態 | 說明 |
|---|---|---|
| Stage 0 | **COMPLETE** | 遷移、基線、根規格、session 指引系統 |
| A1 calibration 模型形式修復 | NOT_STARTED | 下一個 session |
| A2 measured → 九類 IR | NOT_STARTED | |
| A3 IR → 引擎 loader | NOT_STARTED | |
| A4 sealed held-out 驗證 | NOT_STARTED | |
| B1 KV + continuous batching | NOT_STARTED | 指引為 RULES_ONLY |
| B2 參數化候選處理器 | NOT_STARTED | 指引為 RULES_ONLY |
| C1 co-design DSE | NOT_STARTED | 指引為 RULES_ONLY |
| C2 HW0 + LM18 handoff | NOT_STARTED | 指引為 RULES_ONLY |
| GPU 量測軌 | NOT_STARTED | 可與 A/B/C 並行 |

權威狀態記錄為 `governance/stage_ledger.yaml`；本文件是人類可讀摘要，衝突時以 ledger 為準。

## 已驗證的基線（2026-08-17 實測）

```text
證據完整性   4423 / 4423 檔 SHA-256 通過（make verify-evidence）
Python 測試  317 通過、0 失敗
             tests 96 · simulator/tests 36 · phase1 16 · phase2 43 · phase7 126(+48 subtests)
C++ CTest    14 通過、0 失敗（phase3 2/2 · phase4 3/3 · phase5 4/4 · phase6 5/5）
workspace    make doctor -> workspace_contract: pass
```

## 研究鏈的三個斷點（全部未修）

1. **量測 → IR**：唯一 adapter 是 `explorations/moe_cycle_simulator/phase7/adapters/vllm_mock_adapter.py`，檔案自述為 mock。已 grep 確認無 code path 從真實 `.npy` routing array 進到 `EventIR`/`RoutingIR`。→ A2
2. **IR → 引擎**：不存在 IR bundle → C++ 引擎的 loader。目前已發表的 DSE 數字出自 `src/edgeflow/residency.py`（265 行解析模型），**不是** C++ 引擎。→ A3
3. **校準**：四項 Q1 MAPE gate 全數失敗（component 304.418%、TPOT 293.936%、PCIe 66.879%、throughput 60.658%；90 點中 17 通過）。根因是模型形式錯誤，非量測品質。→ A1 / A4

## 可用證據

`evidence/` 共 581 MB / 4423 檔，**唯讀**：

| 群組 | 內容 |
|---|---|
| `evidence/phase7/` | 47 個 GPU campaigns（2026-08-13~14）：OFF-E-PR0–PR4 含 15 點 expert 容量掃描、OFF-W0–W3、SWAP-K0–K5、expert catalog、session guards |
| `evidence/measurement_backups/` | 18 個備份：SERV-P0-25 serving 錨點、controlled matrix、K0–K11 profiles、W0–W3、transfer 與 component 微基準、資格認證 cells |
| `evidence/gpu_measurements/` | q0 fitted parameters 與 q1 validation report（四項 gate 全失敗，保留為證據） |

量測 domain（目前主力，非永久假設）：single RTX PRO 6000 96 GB · Mixtral-8x7B-Instruct-v0.1 rev `eba92302…` · vLLM 0.23.0 · BF16/BF16 · TP/PP/EP 1/1/1。

## 已知缺口

- **掛載點 A2（MoE dispatch 資料搬運）與 A6（offloaded KV 上的 attention）完全沒有量測。** 兩者都在 GPU 軌的最高優先序。
- 長上下文與高並發區間沒有量測；目前所有 expert residency 結論都限於單請求、eager、159 tokens、`max_num_seqs=1`。
- `accelerator/`、`calibration/`、`dse/` 目前只有 README 骨架，無實作。
- SWAP-K2 的 KV 事件 `block_size=0`，位元組帳目由 runtime shape/dtype 推導；治理已開放 KV 效能主張，但此資料品質限制不因此消失。

## 下一步

開新 session 執行 A1，指引為 `docs/session_guides/STAGE_A1_CALIBRATION_MODEL_FORM.md`。
