# GPU Experiment Plan V2

## 1. 證據狀態與適用範圍

本計畫對應 package
`edgehetero-benchmark-driven-m0-20260718`，與 manifest 一致。舊
`edgehetero-rtxpro6000-h100-20260718` identity 已廢止。目前已有 RTX 3050 6GB 上
tiny-random Qwen2MoE M0 vertical smoke 與 provenance-complete package。
這只確認 M0 pipeline/correctness 及 P0/P1/P2/P3/P5 capture；不能推廣為
品質、正式效能、其他模型或其他 GPU 的可執行性。舊 RTX PRO 6000 v1
session 是隔離證據，不得併入 repo 內已整合 `gpu_run_package_v2` 的新
session identity、raw archive、manifest 或結果。

D-062 在兩輪 RTX PRO 6000 與四項 Q1 MAPE gate 全數失敗後，停止所有後續
paid GPU execution，並移除 H100 platform holdout。所有 paid profiles
`execution_enabled=false`；H100 profiles 只保留歷史定義，為
`enabled=false, optional=true, disabled_reason=D-062`。目前正式 acquisition
為 NO-GO。

執行忠實度：

- F：checkpoint-native floating-point、單 GPU full-resident。
- Q：經獨立驗證的 quantized checkpoint、單 GPU full-resident。
- O：有實測 CPU/GPU transfer/runtime trace 的完整 offload。
- L：shape-faithful layer/kernel/window replay。
- T：routing trace 加 calibrated service model；不是 full-model execution。
- C：capacity boundary，保留配置、錯誤、allocation/runtime evidence。

除已釘選 tiny-random M0 fixture 外，所有正式 F/Q 都是 candidate。只有 G1 weight-size/runtime-overhead estimate、G2
allocation 與 short-context generation smoke、G3 profiler compatibility
smoke 全部通過，才可標為 confirmed。M3 禁止 F/Q/O，只能使用 L/T。

模型層級：

- M0：Small Switch、OLMoE-1B-7B；correctness/pipeline。
- M1：DeepSeek-MoE 16B、Qwen3-30B-A3B；medium executable MoE。
- M2：Mixtral 8x7B；主要 workstation VRAM/offload pressure model。
- M3：DeepSeek-R1、Qwen3-235B、待凍結 large-MoE holdout 的 trace-derived
  workload；不得要求單 GPU 完整執行。

Tiny-random M0 revision
`f736f270816032b3c721f7422c62dea1381f49d7` 已釘選；正式 M0 模型與
M1–M3 checkpoint revisions 尚未全部釘選，OLMoE 與 holdout identity 仍未解析。
正式 acquisition 前須釘選 immutable revision，否則 no-go。

## 1.1 已完成的 RTX 3050 M0 vertical slice

- Suite：frozen v1.2.0，810 samples；本次選 8 measured samples。
- Model：tiny-random Qwen2MoE；用途為 pipeline/correctness。
- P0/P1/P2/P3/P5：measured；40 筆 benchmark records 已進 provenance package。
- P4：unsupported，isolated runtime 無 `ncu`。
- P5：1.329 秒、23 telemetry samples；低於 5 秒 minimum。
- P6：optional_not_run。
- Repetition：1；`statistical_status=insufficient_for_formal_statistics`。
- Quality：`quality_correct=0`、`quality_valid=0`，不得宣稱品質。
- Latency：只可標 `latency_ms_vertical_smoke`，不得當正式 TPOT/throughput。

## 2. 逐 GPU 計畫

「候選 → fallback」完全依 `configs/gpu_profiles.yaml`；仍須逐模型通過上述
G1–G3，不代表理論上可放入 VRAM 就可跑。

### RTX 3050 6GB

- Profile/SKU：`rtx3050_6gb` /
  `NVIDIA GeForce RTX 3050 6GB`。
- 角色：本地 pipeline、benchmark/collector development、CUDA smoke、低 VRAM 壓力；
  不屬付費兩小時排程。
- M0：F → Q → C。
- M1：Q → O → L → C。
- M2：O → L → T → C。
- M3：T → L → C。
- 儲存：S1 standard，50 GiB reservation、40 GiB hard minimum。
- 比較限制：結果不可比例換算成其他 GPU；FP8 禁止。
- 現況：exact 6GB device 的 M0 vertical slice 已完成；P0/P1/P2/P3/P5
  measured，P4/P6 為能力狀態。任何結果只可標 local development。
- 正式 go/no-go：需補足 repetitions/statistics、P5 ≥5 秒、正式模型與品質
  gate；RTX 3050 結果不得比例外推。

### RTX 3090 24GB

- Profile/SKU：`rtx3090_24gb` / `NVIDIA GeForce RTX 3090 24GB`。
- 角色：consumer edge validation；expert offload/residency、PCIe transfer、
  copy-compute overlap、grouped GEMM。
- M0：F → Q → C。
- M1：Q → O → L → C。
- M2：Q → O → L → T → C。
- M3：T → L → C。
- 儲存：S2 maximal，200 GiB reservation、160 GiB hard minimum。
- 比較限制：FP8 禁止；單卡不得推定 NVLink。只有至少雙卡且 preflight
  實測 NVLink 才可建立獨立 multi-GPU profile。
- Go/no-go：exact SKU、24GB、topology/link、candidate gates 通過才 go；
  不可用 validation 結果重調 calibration。

### RTX 4090 24GB

- Profile/SKU：`rtx4090_24gb` / `NVIDIA GeForce RTX 4090 24GB`。
- 角色：新一代 consumer edge validation；觀察相同 24GB 下，相對 3090 的
  compute、copy engine 與資料搬移瓶頸轉移。
- M0：F → Q → C。
- M1：Q → O → L → C。
- M2：Q → O → L → T → C。
- M3：T → L → C。
- 儲存：S2 maximal，200/160 GiB。
- 比較限制：禁止 NVLink profile；FP8 仍需 checkpoint-layout/backend/kernel
  gate，不能由 GPU 世代直接確認。
- Go/no-go：exact SKU、PCIe topology 及 F/Q gates 通過才 go；任何 NVLink
  假設或跨 precision 純速度聲稱均 no-go。

### RTX 5090 32GB

- Profile/SKU：`rtx5090_32gb` / `NVIDIA GeForce RTX 5090 32GB`。
- 角色：consumer edge validation；新 compute capability 下的 backend
  forward-compatibility、copy/compute balance 與瓶頸轉移。
- M0：F → Q → C。
- M1：Q → F → O → L → C。
- M2：Q → O → L → T → C。
- M3：T → L → C。
- 儲存：S2 maximal，200/160 GiB。
- 比較限制：不得假設 backend/kernel 自動支援新 compute capability；所有
  low-precision path 需獨立 gate。
- Go/no-go：exact 32GB SKU、backend build 與代表 shape profiler smoke
  通過才 go；forward-compatibility 推定為 no-go。

### RTX PRO 6000 Blackwell Workstation Edition 96GB

- Profile/SKU：`rtx_pro_6000_blackwell_workstation_96gb` /
  `NVIDIA RTX PRO 6000 Blackwell Workstation Edition 96GB`。
- 角色：主要 calibration；large-VRAM workstation、expert residency、
  offload/prefetch calibration。
- 明確拒絕：RTX 6000 Ada、Blackwell Server Edition、Blackwell Max-Q
  Workstation Edition，以及任何只寫「RTX PRO 6000」的未解析租用頁面。
- M0：F → Q → C。
- M1：F → Q → O → L → C。
- M2：Q → F → O → L → T → C。
- M3：T → L → C。
- 儲存：S2 maximal，200/160 GiB。
- 比較限制：需保留 calibration whole-query split 與 raw profiler artifacts；
  舊 v1 session 絕不可與此 v2 package 混合。
- Go/no-go：只有 exact 96GB Workstation SKU、全新 v2 session、完整 hashes、
  storage/candidate gates 通過才 go；名稱或世代不明立即 no-go。

### 歷史停用：H100 PCIe 80GB

- Profile/SKU：`h100_pcie_80gb` / `NVIDIA H100 PCIe 80GB`。
- 歷史角色：原規劃 blind holdout；D-062 後不再是 active/required/holdout。
- M0：F → Q → C。
- M1：F → Q → L → C。
- M2：Q → F → L → T → C。
- M3：T → L → C。
- 儲存：S3 maximal detailed，500/400 GiB；P6 只限 approved bounded windows。
- 比較限制：不可作唯一 edge representative；PCIe 與 SXM5 是不同硬體；
  MIG partition 不是 full H100。
- Go/no-go：目前固定 no-go。只有新的正式決策 supersede D-062、重新啟用
  profile 並通過第二決策層 approval 後才可重新評估。

### 歷史停用：H100 SXM5 80GB

- Profile/SKU：`h100_sxm5_80gb` / `NVIDIA H100 SXM5 80GB`。
- 歷史角色：原規劃 blind holdout；D-062 後不再是 active/required/holdout。
- M0：F → Q → C。
- M1：F → Q → L → C。
- M2：Q → F → L → T → C。
- M3：T → L → C。
- 儲存：S3 maximal detailed，500/400 GiB。
- 比較限制：同一次 holdout 只能明選 PCIe 或 SXM5 其中一個 exact profile；
  不可合併成 generic H100。
- Go/no-go：目前固定 no-go。歷史 profile 仍保留 exact form factor/MIG/
  topology 契約，不能據此推定已獲執行授權。

### Optional RTX A6000 48GB

- Profile/SKU：`a6000_48gb` / `NVIDIA RTX A6000 48GB`。
- 角色：professional workstation cross-platform validation。
- M0：F → Q → C；M1：Q → F → O → L → C；M2：Q → O → L → T → C；
  M3：T → L → C。
- 儲存：S2 maximal，200/160 GiB。
- 限制/gate：FP8 禁止；backend/kernel、PCIe/NVLink 需實測。profile 預設
  disabled，只有明確啟用、校正參數已凍結且不影響必要硬體時才 go。

### Optional A100 40GB

- Profile/SKU：`a100_40gb` / `NVIDIA A100 40GB`。
- 角色：datacenter-generation cross-platform validation。
- M0：F → Q → C；M1：Q → F → O → L → C；M2：Q → O → L → T → C；
  M3：T → L → C。
- 儲存：S2 maximal，200/160 GiB。
- 限制/gate：FP8 禁止；40GB、form factor、MIG、PCIe/NVLink 必須明確。
  generic A100 或以 80GB 結果替代皆 no-go。

### Optional A100 80GB

- Profile/SKU：`a100_80gb` / `NVIDIA A100 80GB`。
- 角色：datacenter-generation cross-platform validation。
- M0：F → Q → C；M1：F → Q → L → C；M2：Q → F → L → T → C；
  M3：T → L → C。
- 儲存：S2 maximal，200/160 GiB。
- 限制/gate：FP8 禁止；80GB、form factor、MIG 與 link 必須明確。
  不得與 A100 40GB 聚合成單一硬體結果。

### Optional V100 16GB

- Profile/SKU：`v100_16gb` / `NVIDIA Tesla V100 16GB`。
- 角色：legacy-generation cross-platform validation。
- M0：F → Q → C；M1：O → Q → L → C；M2：L → T → C；
  M3：T → L → C。
- 儲存：S2 maximal，200/160 GiB。
- 限制/gate：BF16/FP8 禁止；quantized fused kernel 必須明確支援 SM70，
  backend 不得 silent cast。16GB/form factor/link 未解析即 no-go。

### Optional V100 32GB

- Profile/SKU：`v100_32gb` / `NVIDIA Tesla V100 32GB`。
- 角色：legacy-generation cross-platform validation。
- M0：F → Q → C；M1：Q → O → L → C；M2：O → L → T → C；
  M3：T → L → C。
- 儲存：S2 maximal，200/160 GiB。
- 限制/gate：同 V100 16GB；32GB 不能與 16GB 合併，SM70 gate 未通過即
  降級 L/T/C 或 no-go。

## 3. Hardware-major 順序與兩小時模板

歷史順序已由 D-062 停止。RTX 3050 只保留 non-paid local pipeline smoke；
PRO 6000、3090、4090、5090、optional GPU 與 H100 均不得 dispatch。H100
已從 active、required 與 holdout 路線移除。

每張付費 GPU 的 120 分鐘上限：

| 分鐘 | 階段 | Gate / 產物 |
|---|---|---|
| 0–10 | acquire/preflight | exact SKU、VRAM、form factor/MIG、topology、profiler capability、persistent storage |
| 10–22 | smoke/compatibility | allocation、generation、profiler smoke；M0–M3 mode 或 C |
| 22–42 | P0 clean | 無高開銷 profiler 的 baseline |
| 42–67 | P2 + P3 | routing semantics；memory/transfer/residency |
| 67–82 | P1 | CPU/GPU timeline/runtime/API |
| 82–97 | P4 | 代表 kernel/shape/window counters |
| 97–105 | P5 + bounded P6 | telemetry；capability-dependent detail/replay |
| 105–120 | audit/package/verify | 不再開始新實驗；持久化 archive + checksum |

同一 GPU 必須完成所有 planned models/fallback、audit、package、verify 後才可
換硬體。若 105 分鐘仍未完成，停止新增 capture，保留明確 incomplete/failure。
未來 runner 必須以 monotonic elapsed time 在 6300 秒停止新 dispatch，並保留
900 秒 audit/package reserve，不能只依 wall clock。

## 4. P0–P6 證據契約

- P0 clean baseline：每次 warmup/repetition、request/token latency、TTFT、
  TPOT、throughput、queue/prefill/decode、stdout/stderr/return code；禁止高開銷 profiler。
- P1 timeline：Nsight Systems native report、portable export、PyTorch/CUPTI
  trace、CUDA API/NVTX/streams/events；原生檔優先。
- P2 routing：request/token/layer、router scores、experts/top-k/weights、
  overflow、dispatch/combine、expert counts；先保存 lossless raw。
- P3 memory/transfer：tensor bytes/storage、alloc/free/reuse、H2D/D2H/D2D/P2P、
  residency/offload/prefetch、OOM/high-water mark、實測 link。
- P4 counters：只選代表 kernel/shape/layer/window，保存 metric set/replay count；
  不得混入 P0 後宣稱 baseline。
- P5 telemetry：固定週期的 power/clock/thermal/utilization/VRAM/PCIe/ECC/Xid
  與 host time series，保存 clock source 與工具版本。
- P6 optional detail：selected cycle trace/kernel/layer replay、generator/simulator
  version/config/log/checkpoint；unsupported 仍須 manifest。

`maximal` 表示 P0–P6 分離的 deterministic multi-pass 與最大化保存，不表示
同 pass 同時開啟所有 profiler。跨 pass 依 `session_id`、`run_group_id`、
model revision、workload/configuration/environment hashes、seed、request/token
IDs、NVTX 與 kernel sequence 對齊；不得直接相減不同 profiler 的絕對 timestamp。

## 5. 比較與 release gate

純 GPU 速度比較必須相同 model ID/revision、precision、quantization method、
checkpoint layout、runtime backend 與 workload hash。不同 precision/checkpoint
格式只能標為 precision/runtime ablation；PCIe/SXM、MIG/full GPU、
NVLink/non-NVLink、不同 VRAM SKU 均須分層呈現。

Release 必須同時滿足：

1. calibration：P0–P5 mandatory artifacts、重複與統計契約通過，raw 與
   derived 可區分。
2. validation：calibration parameters 已凍結，使用 designated split，保留
   exact hardware identity。
3. shutdown：每模型/pass 有 status，native raw/manifests 存在，archive 可
   解壓/schema verify，archive/checksum 已持久化。

目前 tiny-random M0 benchmark runner、capture matrix、ingest、canonicalize、
expand、simulate、audit/package/verify 已可執行，且 RTX 3050 M0 artifact
真實存在。正式 acquisition/release gate 仍是 no-go，原因是 1 repetition、
P5 太短、正式品質未驗證、M1–M3 未執行，且跨硬體 validation 尚未完成。
此外 D-062 尚未被 supersede，第二決策層 approval 不可能解除此 hard fail；
P0–P6 collector adapters 缺失時 capture plan 亦固定 NO-GO，不能產生假 complete。
Canonical trace 是 measured raw 的轉換；expanded workload/system simulation
是 derived/estimate，不得冒充 GPU measured latency。
