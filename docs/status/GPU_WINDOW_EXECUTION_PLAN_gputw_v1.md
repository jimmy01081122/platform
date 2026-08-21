# GPU 窗口執行排程文件 — gputw.ai × TRACK_GPU (v1)

```text
authored_by : SESSION_ORCHESTRATOR
authored_on : 2026-08-19
revised     : 2026-08-20（v1.1：/vault 持久儲存見 §2.1；v1.2：OD-4 條件性 target V2-GAP-A 於區塊 A，見 §3；v1.3：PREP-3 已凍結 V2-GAP-A（統籌 CONFIRMED）＋ HF 下載兩個已知問題與 token HARD STOP，見 §2.2）
kind        : 排程 runbook（設計/排程文件，非量測執行）
authority   : 統籌 session 授權可寫 docs/status/；本文件不修改 stages:、原始碼、evidence
executed_by : 獨立 TRACK_GPU session（docs/session_guides/TRACK_GPU_MEASUREMENT.md）
```

## Context

統籌 session 上一輪覆核把唯一未解的資源閘標為 **OD-1：無可用 GPU endpoint**。A1 closure
（V2-GAP-B/C 量測）、STAGE_A4（sealed held-out）、TRACK_GPU 三者全部 gated 在它上，也就是
C1/C2 的 calibrated / break-even 的唯一路徑。owner 現以 **gputw.ai** 開機提供這個 endpoint。

gputw 目錄有本專案 canonical domain 的**同型卡**（RTX PRO 6000 WS 96GB，Blackwell，NT$34/hr），
因此量測結果**有機會**直接餵校準、不需另開 platform profile —— 前提是 driver/runtime 也對得上。

**前置已就緒**：`TRACK_GPU_PREP` COMPLETE（PREP-1+PREP-2）；contract 凍結
（`experiments/specs/gpu_measurement_contract_v1.yaml`，FROZEN_PREP2）；五項探針 CPU smoke 過、
輸出欄位對 A2 IR schema 有效；sealed split 已封存
（`calibration/sealed/holdout_split_v1_manifest.json`）。**GPU 窗口只剩執行，不需任何設計決定。**

**owner 裁決（2026-08-19）**：
1. 量測範圍 = **全量程**：exclusive 區塊 1+2+4（~100min）＋ P5 tail-CI 另排（2.65h arrival-bound）。
2. domain 不符處理 = **中止並回報等換機**（TRACK_GPU 指引 §9 預設），確保新資料可與既有 evidence 合併。

> 這是 owner 與 TRACK_GPU session 的啟動 runbook；統籌 session **不跑任何 GPU 量測**。
> 實際 dispatch 貼上 `docs/session_guides/TRACK_GPU_MEASUREMENT.md` §0 啟動 prompt，另開 session 執行。

---

## 1. 軟體環境與映像檔

**鐵律：重現凍結 contract 的 domain，不用「最新版」範本漂移。** 跨平台/跨版本套用校準是紅線
禁止（TRACK_GPU §8）；環境一偏離，資料即報廢（GAP-4 類）。canonical domain（contract §env）：

```
GPU      NVIDIA RTX PRO 6000 Workstation Edition 96 GB（Blackwell / sm_120）
Model    mistralai/Mixtral-8x7B-Instruct-v0.1  rev eba92302a2861cdc0098cc54bc9f17cb2c47eb61
Runtime  vLLM 0.23.0
Weights/KV  BF16 / BF16    TP/PP/EP 1/1/1
Python   3.10（與本地 .venv 3.10.12 一致）
```

**映像檔選法**（gputw 首頁列有「環境範本」但未公開明細，須進 console 確認）：

| gputw 範本情況 | 做法 |
|---|---|
| 有 **CUDA 12.4+ / Ubuntu 22.04 base**（Blackwell 需 driver ≥ 550） | 選它，於乾淨 venv 裝**釘選版** `vllm==0.23.0` + 對應 torch，**不用**範本預裝的 vLLM |
| 只有「vLLM latest / PyTorch latest」整包 | **不直接用**——版本漂移掉 domain；取其 CUDA base，其餘自裝釘選版 |
| 無合適 base | 選純 CUDA 12.4+ runtime image，自建環境 |

理由：預裝範本圖方便，但 vLLM 版本 ≠ 0.23.0 時，量到的 dispatch / KV-offload 行為就跟既有
evidence 不同 domain、不能合併。寧可 `pip install vllm==0.23.0`。

> **租機前先確認範本 CUDA 版本**：若 base 能跑 Blackwell 但裝不上 vLLM 0.23.0 這個確切版本，
> 就會落入 owner 選的「中止回報」分支——先在 console 看清 CUDA/driver 再租，避免白付開機費。

---

## 2. 機規需求（量測可行性，不只 GPU 型號）

全量程含長上下文（P2），租機時一併確認：

- **系統 RAM ≥ 128 GiB**：P2 到 1M token 時 KV 被強制 offload 到 host（contract §target_2：
  128 GiB > 96 GB VRAM，頂端必然 offload；每 token KV 128 KiB）。RAM 不足會在跨過 offload
  邊界前 OOM——OOM 本身是可回報結果，但過早 OOM 會使 P2 拿不到 knee。
- **`/vault` 空間 ≥ 150 GB**：Mixtral-8x7B BF16 權重 ~90 GB **放 /vault，不放本機磁碟**（見 §2.1）。
- **HF token 先備妥**：權重下載前需向 owner 取得（匿名限流），屬 HARD STOP（見 §2.2）。
- **SSH 直連**：gputw pod ready 後可直連；探針為 argv 驅動，SSH 進去即可跑。
- **獨占 GPU**：P1/P2/P4 是敏感微基準（root spec §9.3），彼此與 filler 皆不可並跑。

### 2.1 `/vault` 持久儲存（官方提供，跨執行個體保留）

`/vault` 是掛在每台 GPU 執行個體上的網路儲存，**重啟或刪除執行個體都不會消失**。因此：

- **模型權重下載到 `/vault`，只下載一次**。Mixtral-8x7B BF16 ~90 GB；放 /vault 後，之後
  換機、分兩個窗口、或日後重測都**不必重抓**——這正是「避免重新測量時要重新下載」的關鍵。
- **HF cache 指向 /vault**：開機後 `export HF_HOME=/vault/hf`（或 `HF_HUB_CACHE=/vault/hf/hub`），
  讓 `from_pretrained` / vLLM 載入自動落在 /vault；已存在就直接命中、不重抓。
- **量測 raw 先落 /vault**：每個 attempt 的 raw（含失敗者）先寫到 `/vault/runs/<run_id>/`，
  **再**拉回 workspace 納入 evidence（§4 步驟9）。這樣即使執行個體在拉回前被刪，raw 也不遺失。
- **權重 revision 對版**：pin rev `eba92302…`；命中 /vault 既有權重時仍要驗 checksum/revision
  一致（§4 步驟5），避免用到別的 revision 而悄悄漂移 domain。
- **⚠️ 餘額警告**：官方規則——帳戶餘額 ≤ NT$0 時 /vault 內容**保留 30 天後刪除**。長期停用前
  務必儲值或把權重/raw 另備，否則 90 GB 權重會被清掉、下次重測又要重抓。

### 2.2 Hugging Face 下載的兩個已知問題（owner 實務經驗，必讀）

權重來源是 **Hugging Face Hub**（`huggingface.co`，模型 ID `mistralai/Mixtral-8x7B-Instruct-v0.1`；
`HF_HOME`/`HF_HUB_CACHE` 即 `huggingface_hub` 的原生快取變數）。以下兩點在窗口內踩到會直接
燒掉計費時間：

1. **🛑 匿名下載會被限流——開始下載前必須停下來跟 owner 要 HF token。**
   無 `HF_TOKEN` 的匿名請求會遭 rate limit，90 GB 會拖到不可接受或中途失敗。
   **這是一個 HARD STOP**：TRACK_GPU session 在執行任何權重下載前，先向 owner 索取 token
   （`export HF_TOKEN=<token>` 或 `huggingface-cli login`），**拿到才開始下載**。
   不要用匿名硬拉、也不要自行找鏡像繞過。
   > token 屬機密：只放進執行個體的環境變數，**不得**寫進 run manifest、log、commit 或
   > 任何 evidence 產物。

2. **只拉 sharded safetensors，不要連 `.st` 一起下載。**
   該 repo 同時存在 sharded 權重與額外的 `.st` 檔；全抓會多下載一份等量資料、浪費頻寬與
   /vault 空間。下載時以 allow/ignore pattern 限定，例如：
   ```bash
   huggingface-cli download mistralai/Mixtral-8x7B-Instruct-v0.1 \
     --revision eba92302a2861cdc0098cc54bc9f17cb2c47eb61 \
     --local-dir /vault/hf/mixtral-8x7b-instruct-v0.1 \
     --exclude "*.st"
   ```
   （或 `snapshot_download(..., ignore_patterns=["*.st"])`。）下載後確認取得的是
   `model-0000X-of-0001Y.safetensors` 分片 + `model.safetensors.index.json`。

---

## 3. 窗口排程（owner 選：全量程）

依 `experiments/specs/gpu_measurement_window_plan_v1.md`。**分兩個獨立區塊，不得重疊**
（P5 的 serving 負載會擾動 1/2/4 的時序）：

### 區塊 A — exclusive 微基準（單一 ~2h 窗口，實跑 ~100–115min），順序 P4 → **V2-GAP-A** → P1 → P2

| 序 | 目標 | Est. | 探針 / argv（contract） |
|---|---|---|---|
| P4 | component/PCIe 缺口 + V2-GAP-C sort/permute | 15min | `measurement/gpu_run_package_v2` benchmark.py（新 argv per gap，見 TRACK_GPU）；GAP-5 已由程式碼解、不需 GPU |
| **V2-GAP-A** | **多物件並發搬運掃描（PCIe two-regime S>1）** | **+10~15min** | **條件性：僅在 PREP-3 已凍結時執行**（probe+parser+contract 段落，OD-4）。P4 同類 transfer 微基準、共用 setup。FIT-side，不得混 held-out |
| P1 | A2 in-serving dispatch 搬運 | 25min | `python3 measurement/probes/inserving_dispatch_probe.py --backend <gpu> --concurrency 1,2,4,8 --steps 128 --out runs/<run_id>/dispatch.json` |
| P2 | A6 長上下文 / KV offload | 60min（**HIGH 不確定**） | `python3 measurement/probes/long_context_kv_probe.py --backend <gpu> --seq-lens 4096,16384,65536,131072,262144,524288,1048576 --out runs/<run_id>/longctx.json` |

- **V2-GAP-A 條件（OD-4 硬護欄）**：只有 **PREP-3 在窗口開前完成凍結** 才納入本窗，排在 P4 之後。
  **若 endpoint 早於 PREP-3 凍結出現 → 跳過 V2-GAP-A，照 P4/P1/P2 跑，延到區塊 B 尾段或第二窗；
  絕不在窗口內臨時設計**（GAP-4 成因）。V2-GAP-A 補上 A1 PCIe 模型的 S>1（目前 UNSUPPORTED），
  直接餵 C1 concurrent-transfer break-even。
- **P2 中途驗估**：先跑 2–3 個 seq_len，若 1M 點將爆窗口就即時 re-plan（窗口計畫明載）。
- **P4 順帶量到 sealed held-out cells**（target_3 assignment）＋ PCIe sweep 的 held-out 點。
- 2h 窗口留 ~20min 給 preflight / guard canary / 每 attempt raw 保存；加 V2-GAP-A 後實跑 ~115min，
  若 P2 爆估，V2-GAP-A 讓位到區塊 B。

### 區塊 B — P5 tail-CI（另一個 ~3h 窗口）
- SERV-P0-25 從 request 0 重跑完整 10K，**新 attempt ID，不接續任何既有部分進度**。
- 10,000 req @ Poisson 1.0472 rps ≈ **2.65h wall-clock，與 concurrency 無關**（arrival-bound）。
- `parser_check: python3 measurement/parsers/serving_tail_parser.py <serving_result.json>`。

---

## 4. 開機到量測流程（TRACK_GPU session 執行；對映指引節次）

> 這是 TRACK_GPU session 會走的步驟，**不是統籌 session 做的**。

1. **本機進入檢查（§2.1）**：`make verify-evidence`（4423 OK）/ `make test`（0 failed）/
   `make doctor`（pass）；確認 `CONTRACT_OK`。
2. **開機 + SSH + 對版（§2.3 read-only preflight）**：`nvidia-smi` 記下 GPU SKU / UUID / VRAM /
   driver / CUDA / runtime，寫入 run 的 environment。裝 `vllm==0.23.0` + torch + Python 3.10。
   **設定 `export HF_HOME=/vault/hf`**（見 §2.1），讓權重載入落在持久 /vault。
3. **Domain 判定（驗收 #1）**：實測 vs canonical domain。
   **不符 → 依 owner 裁決：停止 dispatch、回報 `BLOCKED_OTHER` 等換機**（§9；本次不走獨立 profile）。
4. **Session-local guard canary（§6 步驟3）**：新 attempt ID 跑最小 guard 確認 host/runtime。
5. **拉模型到 /vault（只一次）**：先檢查 `/vault/hf` 是否已有 Mixtral-8x7B-Instruct-v0.1
   rev `eba92302…` BF16；**已存在就跳過下載**，僅驗 checksum/revision 一致。
   缺才下載——**下載前 HARD STOP：先向 owner 索取 HF token**（匿名會限流，§2.2 第 1 點），
   且**下載時排除 `.st`、只拉 sharded safetensors**（§2.2 第 2 點）。
   權重 identity 與 checksum 寫入 run（**token 不得寫入任何產物**）。
   之後換機/重測命中 /vault 不再重抓（§2.1）。
6. **區塊 A 量測**：P4 → P1 → P2（§6 步驟4），每 attempt 保存 exact argv / 環境 / 模型 identity /
   輸入 fixture / 輸出 token IDs+finish reason / 原始時序 / telemetry / stdout+stderr / 失敗分類
   （§6 步驟5，**失敗 attempt 一樣保存**）。
7. **區塊 B 量測**：P5 於獨立窗口（不與 A 重疊）。
8. **Non-interference（§5 硬規則）**：發現非本 session 的 GPU/serving process → 不 signal、
   不 attach、不改 config、回報等 owner。**禁 kill/pkill/killall**。
9. **納入 evidence（§6 步驟6）**：raw 先寫 `/vault/runs/<run_id>/`（含失敗者，防執行個體被刪前
   遺失，§2.1）→ 拉回 workspace 獨立 namespace → checksum 驗證 + 本機備份 → 納入 `evidence/` →
   更新 `governance/lineage/EVIDENCE_SHA256SUMS` → `make seal-evidence` 恢復唯讀。

---

## 5. Held-out 隔離（硬性，跨 session）

- sealed held-out cells（`holdout_split_v1_manifest.json`）**只量測、只由 STAGE_A4 開封一次評分**。
  TRACK_GPU **不得**在量測期間拿模型對 held-out cells 評分。
- **V2-GAP-B/C 是 FIT-side**，不得與 sealed held-out 混用（ledger A1 note）。

---

## 6. 成本、驗收與交接

**成本估算**（NT$34/hr）：
- 區塊 A：2h 窗口 ≈ **NT$68**（實跑 ~100min，留 margin）。
- 區塊 B：3h 窗口 ≈ **NT$102**（2.65h arrival-bound + 開關機）。
- 合計 ≈ **NT$170**。**權重放 /vault 後只下載一次**（§2.1）：A、B 即使分兩次開機、或日後重測，
  都不再重抓 ~90 GB，省下重複的下載時間與計費。第一次下載計一次即可。

**驗收（TRACK_GPU §7）**：domain 相符（或已中止回報）／未干擾他人 process／每 attempt raw 完整含
失敗者／checksum 前後一致／`EVIDENCE_SHA256SUMS` 更新且 `make verify-evidence` 通過／
`make seal-evidence` 已跑／`make test` 0 failed。

**交接**：TRACK_GPU session 依指引 §10 的 `TRACK: GPU` 格式回報；統籌 session 下一輪覆核時
**獨立重跑其 verification** 才認定完工（不採信敘述性回報）。

**回饋下游**：P1/P2/P4 raw → A1 closure（V2-GAP-B/C）與 A2 dispatch break-even 輸入；
sealed held-out 量測 → STAGE_A4 開封評分。這是解 OD-1 後關鍵路徑重新流動的起點。
