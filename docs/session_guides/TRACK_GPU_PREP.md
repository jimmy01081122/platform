# GPU 量測前置準備軌（純 CPU）

**本軌不碰 GPU。** 目的是讓 GPU 窗口開啟時，每一項量測的 contract 已凍結、程式已寫好、parser 已測過——窗口內只剩執行。

---

## 0. 啟動 prompt（直接貼上）

```text
你是 /home/a/platform 專案 GPU 量測前置準備軌的執行者。

工作區：/home/a/platform

本 session 的單一目標：把 GPU 量測從「到時候再想」變成「貼上就跑」。
產出是凍結的量測 contract + 已在 CPU 上 smoke test 過的探針程式 + parser/validator。
本軌純 CPU，不需要 GPU endpoint。

開始前完整讀取：
  docs/session_guides/TRACK_GPU_PREP.md          ← 本軌指引
  PLATFORM_FLOW_SPECIFICATION.md                  ← 根規格，特別是 §7 sealed held-out、§9 GPU 量測流程
  governance/stage_ledger.yaml                    ← 狀態真相來源與量測優先序
  calibration/fits/v2/measurement_gaps.json       ← A1 產出的 6 項缺口，是優先序第 4 項的規格

三條紅線：
  1. 不修改 evidence/ 內任何檔案。
  2. 不執行任何 GPU、SSH、server、vLLM 或 serving 指令——本軌是純 CPU 準備。
  3. 進入檢查任一項不符即停止並回報。

本軌特有的硬規則：
  4. 不讀 TRACK_GPU_MEASUREMENT.md 以外的其他階段指引。本軌與該份是同一條軌的兩半。
  5. 探針的輸出欄位若依賴 A2 的 IR 評估點 schema，標為 PREP-2 並等 A2 完成，
     不得自行猜一組欄位——GAP-4 正是這樣產生的。

第一個動作：執行指引第 2 節的進入檢查。
```

---

## 1. 這個 session 的單一目標

**讓 GPU 窗口內不需要做任何設計決定。**

判準很直接：把本軌的產出交給一個完全不知道專案背景的人，他能不能照著跑完量測？不能，就是還沒做完。

---

## 2. 進入檢查

```bash
cd /home/a/platform

make verify-evidence          # 預期：evidence integrity: OK (4423 files)
make test                     # 預期：0 failed
make doctor                   # 預期：workspace_contract: pass

# A2 是否完成，決定本軌能做到哪一半
grep -A3 'id: STAGE_A2' governance/stage_ledger.yaml | grep status

# A1 的量測缺口必須存在——它是優先序第 4 項的完整規格
test -f calibration/fits/v2/measurement_gaps.json && echo GAPS_OK
```

`STAGE_A2` 若為 `COMPLETE`，PREP-1 與 PREP-2 都可做。若否，只做 PREP-1，PREP-2 留給 A2 完成後的下一個 session。

---

## 3. 授權邊界

**可以修改／新增**

```text
measurement/probes/             ← 新探針程式（新目錄）
measurement/parsers/            ← 新 parser 與 validator（新目錄）
tests/fixtures/gpu_prep/        ← CPU smoke test 用的假輸出
experiments/specs/<id>.yaml     ← 量測 contract
calibration/sealed/             ← sealed split manifest（A4 步驟 1–2）
docs/status/ 五份
governance/stage_ledger.yaml    ← 只改 TRACK_GPU_PREP 那一列
```

**絕對不可修改**

```text
evidence/**
src/edgeflow/calibrated_backend.py    A1 已凍結的模型形式，本軌不動
measurement/gpu_run_package_v2/       既有量測包；要改行為就新增探針，不改既有的
其他階段在 ledger 中的列
```

**絕對不可執行**：任何 GPU、SSH、遠端 server、vLLM、serving 指令。發現需要真實 GPU 才能決定的事，記錄為 open question 交給 TRACK_GPU，不要猜。

---

## 4. 進入時的事實基線

### 4.1 五項量測的就緒度（本軌開始時的實測結果）

| 序 | 目標 | 既有資產 | 缺什麼 |
|---|---|---|---|
| 1 | 掛載點 A2 —— MoE dispatch 資料搬運 | `benchmark.py:463` 的 `gather_scatter` 探針，evidence 內 56 筆記錄 | 既有探針是**同裝置合成 proxy**（`x.index_select(0,idx).index_select(0,inv)` 作用在 `torch.randn` 上），只給 execute 項，不含 `T_prepare/T_queue/T_sync/T_move`。缺 in-serving 的搬運與控制結構量測 |
| 2 | 掛載點 A6 —— 長上下文 offloaded KV attention | **無** | 全倉庫 grep `max_model_len` / `cpu_offload_gb` / `swap_space` / `kv_offload` **零命中**。runner、config、parser 全部要新寫。**這是最大的準備缺口** |
| 3 | sealed held-out 驗證集 | 協定已寫在 `STAGE_A4_SEALED_HOLDOUT.md` §4.2 與規格 §7.2 | split 未設計、manifest 未產生。**必須在量測之前封存**——已經產生並看過的資料無法誠實封存 |
| 4 | component service model 缺口 | `calibration/fits/v2/measurement_gaps.json` 6 項，每項已寫明要掃什麼 | 掃描點清單與 argv 未寫。**GAP-5 可能不需要 GPU**，見 §5 步驟 4 |
| 5 | SERV-P0-25 tail-CI | 既有 runner 可用 | 只缺 run plan 與時間估計。須從 request 0 重跑完整 10K、新 attempt ID，**不得接續任何既有部分進度** |

**優先序（資訊增益）與就緒度幾乎相反**：第 4、5 項今天就能跑，第 1、2 項要寫最多程式。這正是本軌存在的理由。

### 4.2 GAP-4 是本軌的反面教材

`measurement_gaps.json` 的 GAP-4：component_latency 的評估點根本沒帶 `expert_tokens`，A1 只好用 `source_record_id` join 回原始記錄再解析 case 字串才拿到。原文寫得很清楚：

> Any future evaluation-point generator (A2/A3 IR pipeline) should carry operand-shape features directly in the evaluation point rather than requiring this join.

**這個缺陷的成因是量測 schema 先於消費 schema 凍結。** 本軌若在 A2 完成前自行決定新探針的輸出欄位，就是原樣再犯一次。所以有 PREP-1 / PREP-2 的切分。

---

## 5. 工作步驟

### PREP-1 —— 不依賴 A2，立即可做

#### 步驟 1 — 寫量測 contract 骨架

`experiments/specs/gpu_measurement_contract_v1.yaml`。五項各一節，每節必須有：

```text
target              對應的掛載點或缺口 ID
independent_vars    自變量與掃描範圍（含為什麼是這個範圍）
sample_size         每格樣本數與依據
repeats             重複次數與依據
stop_condition      什麼情況下算跑完
failure_condition   什麼情況下算失敗、要不要重試
output_fields       產出欄位（PREP-2 才能定案的標為 PENDING_A2）
exact_argv          可直接貼上執行的完整指令
time_estimate       單格耗時 × 格數，含 warmup 與模型載入
```

`time_estimate` 不是形式欄位。沒有它就無法判斷窗口夠不夠，也就無法排序——這正是「避免 GPU 閒置」的核心。

#### 步驟 2 — 長上下文 runner（優先序 2，最大缺口）

從零寫。至少要決定並寫死：

```text
序列長度掃描點      需要跨越 KV 從「放得下 VRAM」到「必須 offload」的轉折
                    Mixtral 每 token KV 128 KiB（實測 block 2,097,152 B / 16 tokens），
                    96 GB VRAM 的轉折點可由此估算——把估算寫進 contract，量測時驗證
offload 機制        用哪個路徑、參數怎麼給
記錄項              TTFT、per-token latency、KV 搬運位元組與時機、offload 命中/未命中
失敗模式            OOM 時的行為；不得為了跑完而縮短長度或改門檻（那是 owner 決定）
```

**寫完必須在 CPU 上跑通**：用極小模型或 mock backend 走完整條路徑，證明參數解析、輸出格式、錯誤處理都對。GPU 窗口不是拿來 debug 參數名稱的。

#### 步驟 3 — In-serving dispatch 儀測（優先序 1）

既有 `gather_scatter` 探針是同裝置合成 proxy，不需要重寫；要補的是**系統層的搬運與控制結構**：每次 dispatch 移動多少位元組、granularity 多大、多久一次、伴隨多少控制決策。

同樣要 CPU smoke test。

#### 步驟 4 — 先用讀程式碼解 GAP-5（可能省下一整輪 GPU）

GAP-5 說 `moe_replay` 的 `cpu_calls` 與 gpu_service 探針的 `expert_tokens` 之間有約 8 倍正規化落差，並列出兩條解法：

> needs either explicit documentation of the cpu_calls<->launch-granularity mapping from the measurement harness, or a dedicated moe_replay-style probe

**第一條是純讀程式碼的工作。** 線索已定位在 `benchmark.py:440-441`：

```python
route = base_route.repeat(concurrency)
expert_tokens = max(1, route.numel())
```

`expert_tokens` 是 base route 長度乘上 concurrency；而 moe_replay 側是 `tokens / cpu_calls`。把兩邊的 launch granularity 定義追出來寫成文件，若能對上，GAP-5 就不必花 GPU 時間。**先試這條，試不通再排量測。**

#### 步驟 5 — 設計 sealed split 並封存（A4 步驟 1–2，優先序 3）

`STAGE_A4_SEALED_HOLDOUT.md` §5 步驟 1–2 已寫明做法，且該指引 §3 已預期無 endpoint 時「只能做到設計 split 並封存」。

**這一步必須在量測之前完成，理由是協定本身**：held-out 的效力來自「擬合時看不到它」。如果先量測、事後才劃分，劃分者已經看過全部資料，封存就只是形式。

產出 `calibration/sealed/<id>_manifest.json`：split 定義 + 逐筆 SHA-256 + 封存時間。封存後不得變動。

#### 步驟 6 — parser 與 validator

每一項量測的輸出都要有對應的 parser，且**在 fixture 上測過**。fixture 放 `tests/fixtures/gpu_prep/`，內容是手寫的假輸出，含至少一個正常案例與一個失敗案例。

判準：parser 吃到形狀不對的輸入時要**明確報錯**，不可靜默略過。這是有前例的——`hf_sample_download.py` 的 WARN 只進 stderr 且 exit 0，導致 Kimi 的 `professional_law` 被靜默跳過，逐格核對才發現（見 `EXTERNAL_CORPUS_AUDIT_20260818.md`）。

### PREP-2 —— 需要 A2 完成

#### 步驟 7 — 用 A2 的 IR 評估點 schema 定案輸出欄位

把步驟 1 中標為 `PENDING_A2` 的 `output_fields` 全部填實，逐項對照 A2 產出的評估點 schema。

**驗收方式**：新探針的輸出必須能**不經 join** 直接生成 IR 評估點。做得到就代表 GAP-4 這一類缺陷不會重演；做不到就回頭改探針，不要留給下游 join。

---

## 6. 驗收條件

| 項目 | 判準 |
|---|---|
| contract 齊備 | 五項各有完整一節，`time_estimate` 全部有值 |
| 長上下文 runner 可跑 | CPU 上完整跑通一次，輸出通過自己的 validator |
| dispatch 儀測可跑 | 同上 |
| GAP-5 已嘗試 | 有結論文件：對上了（附推導）或對不上（附已排除的可能與需要的量測） |
| sealed manifest 存在 | `calibration/sealed/` 下含 split 定義與逐筆 SHA-256，封存時間已記錄 |
| parser 全部有 fixture | 每個 parser 至少一個正常 + 一個失敗 fixture，失敗案例確實報錯 |
| 窗口計畫可判斷 | 依 `time_estimate` 算出各優先序的總時數，標明哪些能在一個窗口內完成 |
| 基線未退步 | `make test` → 0 failed；`make verify-evidence` → 4423/4423 |

**最後一項的補充**：新增的探針與 parser 應該有自己的測試，`make test` 的通過數會上升。上升是預期的，下降或有失敗則不通過。

---

## 7. Claim boundary

**進入時已成立**：`evidence/` 逐檔一致；A1 的 FIT 側殘差與 6 項缺口（`calibration/fits/v2/`）；§4.1 表列的既有資產盤點。

**本軌可新增**

- 量測 contract 的內容（設計，不是結果）。
- 探針程式在 **CPU fixture 上**的行為正確性。
- GAP-5 的 harness 語意結論（若由讀程式碼解出，須附推導與程式碼位置）。

**仍然禁止**

- 任何 GPU 效能主張——本軌沒有跑過 GPU。
- 把 CPU smoke test 通過說成量測可行性已驗證。smoke test 證明的是「參數與格式對」，不是「這個量測在真實 GPU 上跑得出有意義的數字」。
- 任何 calibrated、break-even、accelerator、長上下文主張。
- 用估算的 `time_estimate` 當作實測耗時。

---

## 8. 失敗處理與必須詢問 owner 的條件

**可自主做的**：探針、parser、fixture、contract 的一切設計與實作；GAP-5 的程式碼追查。

**必須停下並詢問 owner**

- 長上下文的掃描範圍需要超出 96 GB VRAM 能承受的設定，只能靠改 workload 或降門檻才跑得起來；
- 依 `time_estimate` 算出的總時數遠超過可用窗口，需要 owner 裁決取捨；
- sealed split 的劃分方式會影響哪些既有資料被排除在擬合之外；
- 發現既有 evidence 必須修改才能支撐新量測（**這一項本身就代表方案錯了**）。

---

## 9. 完工交付

1. 產出清單。
2. `experiments/specs/gpu_measurement_contract_v1.yaml`。
3. `measurement/probes/`、`measurement/parsers/`、`tests/fixtures/gpu_prep/`。
4. `calibration/sealed/<id>_manifest.json`。
5. GAP-5 結論文件。
6. 窗口計畫（各優先序總時數與可行組合）。
7. 更新 `governance/stage_ledger.yaml` 的 `TRACK_GPU_PREP` 列。
8. 更新五份 status。
9. commit + push。

### 交接記錄格式

```text
TRACK: GPU_PREP
STATUS: COMPLETE | PARTIAL_PREP1_ONLY | BLOCKED
PHASE: PREP-1 | PREP-1+PREP-2
CONTRACT: <五項各自的完成度>
LONG_CONTEXT_RUNNER: <路徑；CPU smoke test 結果>
DISPATCH_PROBE: <路徑；CPU smoke test 結果>
GAP5_RESOLUTION: RESOLVED_BY_CODE | NEEDS_MEASUREMENT | <結論摘要>
SEALED_MANIFEST: <路徑；split 定義摘要；封存時間>
PARSERS: <各 parser 與其 fixture>
WINDOW_PLAN: <各優先序總時數；一個窗口內可完成的組合>
PENDING_A2_FIELDS: <仍未定案的輸出欄位；A2 未完成時必填>
BASELINE: <make test 輸出>
EVIDENCE_VERIFY: <make verify-evidence 輸出>
CLAIMS_ADDED: <設計與 CPU 行為，不含任何 GPU 主張>
CLAIMS_STILL_FORBIDDEN: 任何 GPU 效能 / calibrated / break-even / accelerator / 長上下文主張
NEXT: <TRACK_GPU 可否開始；不可則列出還缺什麼>
OWNER_DECISION_NEEDED: <若有>
```
