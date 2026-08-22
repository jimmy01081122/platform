# TRACK_GPU GPU 執行環境 — 還原步驟文件

```text
用途    : 從零(或從被 recycle/destroy 清空的執行個體)重建 TRACK_GPU 可量測的 GPU 環境
authored: 2026-08-23
基準    : 2026-08-22 首個 GPU 窗口實際捕獲的環境(canary HEADERFIX-PYINC PASS)
事實來源: runs/MECH-G0-...-CANARY-HEADERFIX-PYINC-20260822T114227Z-TRACKGPU/environment/tool_versions.json
          runs/FLASHINFER-HEADER-FIX-20260822T105500Z-TRACKGPU/(nvrtc overlay recipe + checksums)
          experiments/specs/gpu_measurement_contract_v1.yaml (frozen_platform_facts)
          docs/status/DECISION_LOG.md P-019 / P-020 / P-025
狀態    : 撰寫時 server 不可用;本文件純由本機已保存的證據整理,未連線 server 驗證
```

> **這份文件回答一個問題:如果執行個體沒了,怎麼把「能產出可與既有 evidence 合併的量測」的環境一步步裝回來。**
> 正常情況下 **`stop` 會完整保留以下全部**,不需要重跑本文件——只有在發生非預期的 recycle/destroy、
> 或要在一台全新機器上重建時才需要。**能 `stop` 就絕不 `destroy`**(見 §1)。

---

## 0. 先確認:是否真的需要還原

`stop`/`start`(vast.ai)**保留整個 container 檔案系統**;`recycle`/`destroy` **清空一切**。
`start` 回來後先檢查下列路徑是否都在,**任何一個不見了才需要走本文件**;若只是連線字串變了、
內容都在,直接用即可,不要重裝(重裝 = 多花數小時 + 重新下載 87 GiB 權重):

```
/workspace/venvs/track_gpu_vllm_0_23_py310/        # Python venv(vLLM 0.23.0 + torch cu130 + FlashInfer)
/workspace/models/mixtral-8x7b-instruct-v0.1/      # 19 shard safetensors,約 87 GiB
/workspace/track_gpu/cuda_compat/nvrtc-13.0.88/    # FlashInfer 用的 nvrtc header/lib overlay(§4,關鍵)
/workspace/track_gpu/code/                          # 專案程式碼(每次窗口都要重新同步到最新,見 §6)
```

快速檢查指令(SSH 進去後):
```bash
ls -d /workspace/venvs/track_gpu_vllm_0_23_py310 \
      /workspace/models/mixtral-8x7b-instruct-v0.1 \
      /workspace/track_gpu/cuda_compat/nvrtc-13.0.88 \
  && ls /workspace/models/mixtral-8x7b-instruct-v0.1/*.safetensors | wc -l   # 應為 19
```
**若發現該在的東西不見了 → 這可能代表發生過 recycle/destroy。先回報 owner,再決定是否重建;
不要靜默地開始重新下載。**

---

## 1. 硬規則(還原過程同樣適用)

- **絕不 `destroy` / `recycle`**。省費用一律 `stop`。`workspace_is_volume=false`,無 host volume,
  destroy 會連同 87 GiB 權重 + CUDA stack + venv + 未拉回的 raw 一起清空。
- **HF token 是機密**:只放環境變數(`HF_TOKEN`),**不得**寫進任何 manifest / log / commit / 本文件。
- **紅線:不得為繞過編譯/執行失敗而切換或停用 backend**。現行為 FlashInfer CUTLASS MoE;
  換 kernel = 量到不同的東西、資料不可與既有 evidence 合併。§4 的 overlay 是「讓 FlashInfer 編出
  原本該編的 Blackwell kernel」,不是換 backend。
- raw 落 server 後**盡快拉回本機**(`measurement/pull_gpu_attempt.sh`)——無持久儲存。

---

## 2. Canonical domain(必須對上,否則資料報廢)

來自 `experiments/specs/gpu_measurement_contract_v1.yaml` 的 `frozen_platform_facts`,
與 2026-08-22 實測捕獲值:

| 項目 | canonical 目標值 | 2026-08-22 實測捕獲 | 對版 |
|---|---|---|---|
| GPU | RTX PRO 6000 Blackwell WS 96 GB (sm_120 / compute_cap 12.0) | `NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 97887 MiB, compute_cap 12.0` | ✅ |
| Model | mistralai/Mixtral-8x7B-Instruct-v0.1 | 同 | ✅ |
| Model revision | `eba92302a2861cdc0098cc54bc9f17cb2c47eb61` | 同 | ✅ |
| Runtime | vLLM 0.23.0 | `0.23.0` | ✅ |
| torch | 2.11.0+cu130 | `2.11.0+cu130` | ✅ |
| Python | 3.10 | `3.10.12` | ✅ |
| transformers | — | `4.57.6` | (記錄用) |
| FlashInfer | (evidence 無記錄) | `0.6.12` | ⚠️ 無從對版,只能沿用 |
| Weights / KV dtype | BF16 / BF16 | BF16 | ✅ |
| TP / PP / EP | 1 / 1 / 1 | 1/1/1 | ✅ |
| gpu_memory_utilization | 0.97 | 0.97 | ✅ |
| max_model_len (canonical) | 32768 | 32768 | ✅ |
| **driver** | evidence 為 **595.71.05** | **580.95.05** | ⚠️ **不同,須記錄** |
| **CUDA runtime** | evidence 為 **13.2** | **13.0**(nvcc V13.0.88) | ⚠️ **不同,須記錄** |

**⚠️ 已知且已接受的差異(P-019 已記錄)**:driver 580.95.05(evidence 595.71.05)、CUDA 13.0
(evidence 13.2)。非必然 domain 不符(向前相容),**但必須寫入每個 run manifest 並在回報中明示**,
不得靜默視為相同。**若還原後出現這兩項之外的新差異,停止 dispatch 並回報。**

KV 常數(供 target_2 推算,不是要對版):`kv_bytes_per_token=131072`、`kv_block_bytes=2097152`、
`kv_block_tokens=16`、`expert_object_bytes=352321536`。

---

## 3. 執行個體佈局(路徑約定)

還原時把每一塊放回原位,後面所有指令與環境變數都假設這個佈局:

```
/usr/local/cuda -> cuda-13.0                        # 系統 CUDA toolkit(nvcc V13.0.88)
/usr/include/python3.10/Python.h                    # 系統 python3.10-dev(§4 的 PYINC 修正需要)
/workspace/venvs/track_gpu_vllm_0_23_py310/         # 專用 venv(§5)
/workspace/models/mixtral-8x7b-instruct-v0.1/       # 權重(§7)
/workspace/track_gpu/cuda_compat/nvrtc-13.0.88/     # nvrtc overlay(§4)
  ├─ include/nvrtc.h    sha256 9e4415b5ff5a2c58ec2b1a02eefcc743187c94ce48b00743c68092efe8c0f86e
  └─ lib/libnvrtc.so.13
/workspace/track_gpu/code/                          # 專案程式碼(§6)
/workspace/runs/                                    # attempt 產出(拉回本機後可清)
/workspace/.hf_home/                                # HF_HOME 快取
```

---

## 4. 【最關鍵且最難重建】FlashInfer Blackwell JIT 的 nvrtc overlay

**問題**:系統 nvcc 13.0.88 的 header 定義 `__cudaLaunch(fun, isTileKernel)`(兩個參數),
FlashInfer 0.6.12 wheel 內附的 header 期望 13.2 的 `__cudaLaunch(fun)`(一個參數)。直接編會噴
`error: macro "__cudaLaunch" passed 2 arguments, but takes just 1`,FlashInfer JIT 死在編譯期,
vLLM 連 KV allocation 都到不了。

**解法(P-019 裁決 5 條件放行的窄範圍 overlay,不是換 toolchain、不是換 backend)**:
保留系統 CUDA 13.0 toolchain,只用一份 **13.2 的 nvrtc header** 覆蓋 include 路徑。實測比較
(`runs/FLASHINFER-HEADER-FIX-.../results.tsv`):`narrow_nvrtc_overlay` rc=0(成功),
`broad_venv_cpath`(整個 venv 的 cu13 include 全上)rc=1(失敗)——**要窄,不要廣**。

### 4.1 建立 overlay 目錄

```bash
mkdir -p /workspace/track_gpu/cuda_compat/nvrtc-13.0.88/include
mkdir -p /workspace/track_gpu/cuda_compat/nvrtc-13.0.88/lib
```

overlay 的 `nvrtc.h` 來源是 venv 內 FlashInfer 隨附的 cu13 nvrtc header
(`.../site-packages/nvidia/cu13/include/nvrtc.h` 這一系),放進 overlay 後應滿足:
```
sha256(/workspace/track_gpu/cuda_compat/nvrtc-13.0.88/include/nvrtc.h)
  == 9e4415b5ff5a2c58ec2b1a02eefcc743187c94ce48b00743c68092efe8c0f86e
```
`lib/libnvrtc.so.13` 與系統 `/usr/local/cuda-13.0/lib64/libnvrtc.so.13` 內容一致
(實測兩者 sha256 皆 `a49e67e8e74590f1e98de55c39c6287efd3f59e3c3797464d7bbe0fe01349b11`),
可直接由系統路徑複製或連結。

> **如果 venv 是照 §5 全新裝的**,`nvidia/cu13` 的 header 版本可能與當時不同。以「能讓下面 §4.3 的
> 驗證編譯 rc=0」為準;若 sha256 對不上但編譯 rc=0,記錄新 sha256 並回報(header 版本漂移是可接受
> 的,只要 canary 仍 PASS 且 backend markers 不變);若編譯 fail,**停下回報,不要改用 broad 版或
> 換 backend**。

### 4.2 第二個坑:`Python.h`(PYINC 修正)

overlay 解掉 nvrtc 後會冒出 `Python.h: No such file or directory`——FlashInfer 的編譯需要
`/usr/include/python3.10/Python.h`(`python3.10-dev`,通常系統已裝,只是不在 include 路徑上)。
修法是把它加進 `CPATH`(見 §8 的 launch env),**不需要重裝 python3.10-dev**。確認存在:
```bash
ls /usr/include/python3.10/Python.h   # 不存在才需要: apt-get install -y python3.10-dev
```

### 4.3 驗證 overlay(單檔編譯,不需 GPU)

用實測成功的窄 overlay recipe 編一個 FlashInfer 原始檔,rc=0 即代表 overlay 生效
(以下取自 `runs/FLASHINFER-HEADER-FIX-.../narrow_nvrtc_overlay/exact_argv.txt`,路徑依實際 venv 調整):
```bash
env CPATH=/workspace/track_gpu/cuda_compat/nvrtc-13.0.88/include \
  /usr/local/cuda/bin/nvcc \
  -DPy_LIMITED_API=0x03090000 -D_GLIBCXX_USE_CXX11_ABI=1 \
  -I<venv>/lib/python3.10/site-packages/flashinfer/data/csrc/nv_internal \
  -I<venv>/lib/python3.10/site-packages/flashinfer/data/csrc/nv_internal/include \
  -isystem /usr/include/python3.10 -isystem /usr/local/cuda/include \
  --compiler-options=-fPIC --expt-relaxed-constexpr -std=c++17 \
  -DCOMPILE_BLACKWELL_TMA_GEMMS -DCOMPILE_BLACKWELL_SM120_TMA_GROUPED_GEMMS \
  -DUSING_OSS_CUTLASS_MOE_GEMM \
  -gencode=arch=compute_120f,code=sm_120f \
  -c <venv>/lib/python3.10/site-packages/flashinfer/data/csrc/nv_internal/cpp/common/memoryUtils.cu \
  -o /tmp/overlay_probe.o
echo "rc=$?"    # 需為 0
```
(完整旗標見上述 `exact_argv.txt`;此處為關鍵子集。真正的 kernel 編譯由 vLLM/FlashInfer JIT 在
canary 時自動觸發,這只是提前確認 overlay 通了。)

**真正的驗收是 §9 的 guard canary PASS**,不是這個單檔編譯。

---

## 5. Python venv 與釘選套件

```bash
python3.10 -m venv /workspace/venvs/track_gpu_vllm_0_23_py310
source /workspace/venvs/track_gpu_vllm_0_23_py310/bin/activate
pip install --upgrade pip
# 釘選版本(對上 §2):torch 必須是 cu130 build
pip install torch==2.11.0+cu130 --index-url <對應 cu130 的 index>
pip install vllm==0.23.0
pip install transformers==4.57.6
# FlashInfer 0.6.12(vLLM 0.23 相依,通常隨 vLLM 一併帶入;若無則明確裝)
python -c "import flashinfer; print(flashinfer.__version__)"   # 應為 0.6.12
```

裝完立即驗版本 + 一個最小 CUDA tensor(P-019 前實作 session 的良好習慣):
```bash
/workspace/venvs/track_gpu_vllm_0_23_py310/bin/python - <<'PY'
import torch, vllm, transformers
print("torch", torch.__version__)            # 2.11.0+cu130
print("vllm", vllm.__version__)              # 0.23.0
print("transformers", transformers.__version__)  # 4.57.6
print("cuda_ok", torch.cuda.is_available(), torch.zeros(4, device="cuda").sum().item())
PY
```
**任一版本對不上 §2 → 停下回報,不要「用最接近的版本湊」**(跨版本套校準是紅線)。

---

## 6. 專案程式碼同步

`/workspace/track_gpu/code/` 每次窗口都要同步到本機最新分支(不是還原專屬步驟,但列在這裡以免遺漏)。
本機分支 `stage-b2-accelerator-model`。同步後在 remote 上跑 TRACK_GPU 相關測試確認全綠:
```bash
cd /workspace/track_gpu
python3 -m pytest tests/test_gpu_prep.py tests/test_vllm_backend.py \
  tests/test_vllm_runtime_adapter.py tests/test_gpu_prep_pcie_extension.py \
  tests/test_target4_phase2_probes.py tests/test_serv_p0_25_arrival_driver.py \
  tests/test_model_identity_manifest.py -q
```

> 注意:`serv_p0_25_arrival_driver.py` 包裝的是
> `explorations/moe_cycle_simulator/phase7/real_run/serving_burst_runner.py`(P-021),
> 同步時確保該 runner 也在。

---

## 7. 模型權重(87 GiB,已知有一個會浪費數小時的坑)

**⚠️ P-019 發現 3 的坑**:`--exclude *.st` **不會**排除 `consolidated.*.pt`(那是 vLLM 不吃的
~93 GB PyTorch 整合權重)。正確做法是明確排除 `consolidated*`,只抓 19 個 sharded safetensors:

```bash
export HF_HOME=/workspace/.hf_home
export HF_TOKEN=<機密,只放環境變數,不寫進任何檔案>
hf download mistralai/Mixtral-8x7B-Instruct-v0.1 \
  --revision eba92302a2861cdc0098cc54bc9f17cb2c47eb61 \
  --local-dir /workspace/models/mixtral-8x7b-instruct-v0.1 \
  --exclude "consolidated*"
```
- **只用單一 `--exclude`**:同時給 `--include` 會讓 hf CLI 警告 "Ignoring --include..." 並抓錯
  (P-019 首次 relaunch 就栽在這)。
- 完成後確認:`ls /workspace/models/.../*.safetensors | wc -l` 應為 **19**,總量約 **87 GiB**。
- revision 必須是 `eba92302…`(見 §2)。

下載完成後產生 model identity manifest(每個 attempt 都會引用):
```bash
cd /workspace/track_gpu
python3 measurement/model_identity_manifest.py \
  --model-path /workspace/models/mixtral-8x7b-instruct-v0.1 \
  --model-id mistralai/Mixtral-8x7B-Instruct-v0.1 \
  --revision eba92302a2861cdc0098cc54bc9f17cb2c47eb61 \
  --output /workspace/track_gpu/model_identity.json
```

---

## 8. 每個 GPU dispatch 都要帶的 launch 環境(少了會重現 FlashInfer blocker)

這是 2026-08-22 canary PASS 時的**確切** env prefix(來自該 attempt 的 manifest `exact_argv`):
```bash
env -u VLLM_ARGS -u VLLM_MODEL -u VLLM_TEST_ENDPOINT -u LD_LIBRARY_PATH \
  CUDA_HOME=/usr/local/cuda \
  CUDA_PATH=/usr/local/cuda \
  CUDACXX=/usr/local/cuda/bin/nvcc \
  PATH=/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  CPATH=/workspace/track_gpu/cuda_compat/nvrtc-13.0.88/include:/usr/include/python3.10 \
  LIBRARY_PATH=/workspace/track_gpu/cuda_compat/nvrtc-13.0.88/lib \
  /workspace/venvs/track_gpu_vllm_0_23_py310/bin/python  <script...>
```

逐項理由:
- `CPATH` = **nvrtc overlay include(§4.1)** + **`/usr/include/python3.10`(§4.2 Python.h)**。
  兩段缺一不可——這是「HEADERFIX-PYINC」名字的由來。
- `LIBRARY_PATH` = nvrtc overlay lib。
- `-u LD_LIBRARY_PATH`:清掉機器預設可能干擾的動態連結路徑。
- `-u VLLM_ARGS -u VLLM_MODEL -u VLLM_TEST_ENDPOINT`:**這台機器的預設 env 帶了不相關的
  Qwen serving 設定**(實測 `VLLM_ARGS=--max-num-seqs 8 ... VLLM_MODEL=Qwen/Qwen3.5-9B`),
  不清掉會污染我方 engine 建構。**還原到新機器時務必確認有沒有類似的預設 env 要一併 `-u`。**
- **target_2 額外**要 `env -u VLLM_USE_SIMPLE_KV_OFFLOAD`(P-020:維持未設 → `OffloadingConnector`,
  而非 `SimpleCPUOffloadConnector`)。

---

## 9. 驗收(還原完成的定義)

依序,全部通過才算環境還原成功:

1. **§2 domain preflight** 全部對上(driver/CUDA 兩項已知差異除外,其餘須完全一致)。
2. **§5 版本 + CUDA tensor** 檢查通過。
3. **§4.3 overlay 單檔編譯** rc=0。
4. **§6 remote pytest** 全綠。
5. **guard canary PASS**(最終且唯一的權威驗收):用 §8 的 env prefix 跑一次 canary
   (沿用 `MECH-G0-...-CANARY-HEADERFIX-PYINC-...` 那次 PASS 的 exact argv,只換新 attempt ID),
   rc=0,且 vLLM 啟動 log 的 backend markers 為
   **FLASH_ATTN attention + FlashInfer CUTLASS MoE + KernelConfig(enable_flashinfer_autotune)**
   (P-019:`runtime_variant.template.json` 的 `backend_evidence_contract` 要求)。
   **markers 不對 = backend 漂移 = 停下回報**,不得當成環境已還原。

canary 一 PASS,環境即回到 2026-08-22 首個 GPU 窗口的可量測狀態,可接續
`docs/status/GPU_WINDOW_EXECUTION_PLAN_gputw_v1.md` 與最新的執行序列。

---

## 10. 還原後第一件事

**確認 GPU 上沒有非本 session 的 compute process**(這台機器預設可能跑著別的 serving):
```bash
nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv,noheader
```
有輸出 → **不 kill、不動它**,回報 owner 等裁決(`run_gpu_attempt.py` 也會 fail-closed 拒絕在
有 foreign process 時執行)。無輸出才可開始 dispatch。

---

## 附:關鍵 checksum 對照(還原後可比對)

```
nvrtc overlay header   /workspace/track_gpu/cuda_compat/nvrtc-13.0.88/include/nvrtc.h
                       9e4415b5ff5a2c58ec2b1a02eefcc743187c94ce48b00743c68092efe8c0f86e
libnvrtc.so.13         a49e67e8e74590f1e98de55c39c6287efd3f59e3c3797464d7bbe0fe01349b11
                       (系統 /usr/local/cuda-13.0/lib64/ 與 overlay 一致)
nvcc                   release 13.0, V13.0.88 (Built Aug_20_2025)
```
model identity 的完整 87 GiB content hash 由 §7 的 `model_identity_manifest.py` 產生,
存於 `/workspace/track_gpu/model_identity.json`,以該檔為權威。
