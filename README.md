# MoE Inference Processor 共同設計基底平台

這個 repo 的產出**不是模擬器本身**，而是回答一個共同設計問題：

> 要加速 MoE inference，系統需要什麼架構與規格的客製化 processor？哪一項功能值得做成硬體、規格下限是多少、什麼條件下不值得做？

模擬器是量具。驗收標準是「break-even 面與規格需求可追溯到實機量測，敢拿去指導 RTL」。

**先讀 [`PLATFORM_FLOW_SPECIFICATION.md`](PLATFORM_FLOW_SPECIFICATION.md)** — 那是本 repo 的根規格，任何實作、量測與結論都必須可回溯到它的某一節。

---

## 目前狀態

```text
Stage 0（遷移與基線）  完成
Stage A（可信基底）    未開始
Stage B（共設計能力）  未開始
Stage C（共設計結論）  未開始

calibrated pass   : 尚未取得（既有 q1 四項 MAPE gate 全數失敗，保留為證據）
accelerator claim : 無
GPU 量測           : 581 MB 已在 evidence/，新量測未開始
```

**目前沒有任何 calibrated、break-even 或 accelerator 主張。**

---

## 研究鏈與三個待補的接點

```text
真實 MoE 執行與 trace
  -> canonical IR（九類）          [接點 1：量測 → IR，目前只有 mock adapter]
  -> cycle-resolved simulator      [接點 2：IR → 引擎，目前無 loader]
  -> measurement-calibrated surrogate  [接點 3：模型形式錯誤，MAPE 61–304%]
  -> routing / dispatch / transfer / residency / KV 的 DSE
  -> sensitivity、ablation、break-even
  -> support processor 規格需求
  -> RTL 介面與 handoff 規格
```

兩端的資產都已成熟（見下），斷的是中間三個接點。修補順序見規格書 Stage A。

---

## 目錄

| 路徑 | 內容 |
|---|---|
| `PLATFORM_FLOW_SPECIFICATION.md` | 根規格（16 節） |
| `governance/` | charter、evidence levels、capability registry、**lineage**（來源與 checksum） |
| `evidence/` | **不可變量測證據，581 MB / 4423 檔，唯讀** |
| `explorations/moe_cycle_simulator/` | phase1–2 = 九類 Canonical IR；phase3–6 = C++ cycle-resolved 引擎；phase7 = GPU campaign runner 與 hooks |
| `src/edgeflow/` | trace 轉換、routing 正規化、residency 模型、calibrated backend、multifidelity dispatcher |
| `measurement/gpu_run_package_v2/` | collectors（P0/P1/P2/P3/P5_BASIC）、schemas、scheduler、trace capture plan |
| `hardware/` | rtl · firmware · formal · syn · sta · verification · mem |
| `accelerator/` | 候選處理器參數化模型、六動詞 ABI、掛載點 A1–A6（**待建**） |
| `calibration/` | 模型形式、fit/held-out、sealed holdout 協定（**待建**） |
| `dse/` | co-design DSE（**待建**） |
| `configs/` `schemas/` `scripts/` `experiments/` `tests/` `docs/` `data/` `runs/` | 支援檔 |

---

## 環境與測試

```bash
make venv          # 建立 .venv 並安裝釘選版本（jsonschema 4.24.0 / pyarrow 20.0.0）
make test          # Python 317 項 + C++ CTest 14 項
make test-py       # 只跑 Python
make test-cpp      # 建置 phase3-6 並跑 CTest
make verify-evidence   # 對 4423 個證據檔驗 SHA-256
make seal-evidence     # clone 後重新套用 evidence/ 唯讀
```

兩個環境變數由 Makefile 自動設定，手動執行時必須帶上：

```bash
export PYTHONPATH=$PWD:$PWD/src
export PYTHONDONTWRITEBYTECODE=1   # 不可省略，見下
```

`PYTHONDONTWRITEBYTECODE` 是必要的：`phase7_d0_r4` 的治理測試會斷言 application package 的**精確檔案集合**，Python 自動產生的 `__pycache__` 會直接讓 19 項測試失敗。

### 基線（2026-08-17 遷移後實測）

```text
tests/                                    96 passed
explorations/moe_cycle_simulator/tests    36 passed
  phase1/tests                            16 passed
  phase2/tests                            43 passed
  phase7/tests                           126 passed + 48 subtests
C++ CTest  phase3 2/2 · phase4 3/3 · phase5 4/4 · phase6 5/5

合計 317 Python 測試 + 14 CTest，0 失敗
```

---

## 證據

`evidence/` 是**不可變**的。規格書 §14 明文禁止修改其中任何 raw 資料；目錄在本機設為唯讀，git 不保留權限位元，因此 clone 後要跑 `make seal-evidence`。

| 群組 | 內容 | 大小 |
|---|---|---|
| `evidence/phase7/` | 47 個 GPU campaigns（2026-08-13~14）：OFF-E-PR0–PR4 含 15 點 expert 容量掃描、OFF-W0–W3、SWAP-K0–K5、expert catalog | 124 MB |
| `evidence/measurement_backups/` | 18 個備份：SERV-P0-25 serving 錨點、controlled matrix、K0–K11 profiles、W0–W3、transfer 與 component 微基準、資格認證 cells | 447 MB |
| `evidence/gpu_measurements/` | q0 fitted parameters 與 q1 validation report（**四項 gate 全失敗，保留為證據**） | 9.5 MB |

量測 domain（目前主力，**非永久假設** — GPU 型號可更換，見規格書 §3.2）：

```text
GPU      NVIDIA RTX PRO 6000 Workstation Edition 96 GB
Model    mistralai/Mixtral-8x7B-Instruct-v0.1  rev eba92302a2861cdc0098cc54bc9f17cb2c47eb61
Runtime  vLLM 0.23.0 · BF16/BF16 · TP/PP/EP = 1/1/1
```

---

## 來源與 lineage

本 repo 由凍結的來源 workspace 遷移而來，來源保持唯讀作為 evidence-of-record：

```text
source repo    git@github.com:jimmy01081122/dis.git
source commit  e804b1633a376f63d57aeba60e7fd15068181ea4
migration      2026-08-17，18 GB -> 610 MB
verification   4423/4423 檔 checksum 一致，差異 0
```

完整對照、排除清單與理由見 [`governance/lineage.yaml`](governance/lineage.yaml)。

---

## 不可違反的約束（摘要，完整見規格書 §14）

1. 不修改 `evidence/` 內任何 raw 資料。
2. 保留失敗結果——既有 q1 的 `fail` 不得刪改。
3. Sealed held-out **只能開封一次**；舊資料一律只做 FIT。
4. GPU 型號、VRAM、鏈路頻寬為 `PlatformIR` 參數，禁止寫死；不得跨平台套用 calibration。
5. 每個 component 必標 fidelity label；結論不得跨層誇大。
6. 不得把模型估計寫成實機量測，或把 RTL pass 寫成端到端效能驗收。
7. 不得以完成率百分比宣稱進度。
