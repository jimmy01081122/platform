# GPU Run Package V2

## 狀態與隔離邊界

- 本 source package 已整合至 repo 的 `gpu_run_package_v2/`；以下路徑均以此
  repo 內目錄為基準，不再使用外部 staging 目錄。
- 本 source package 的 `package_id` 是
  `edgehetero-benchmark-driven-m0-20260718`，與 `package_manifest.json`
  一致。舊文件中的 `edgehetero-rtxpro6000-h100-20260718` 已廢止。RTX 3050
  6GB 上執行的 tiny-random Qwen2MoE M0 vertical smoke 是外部 evidence，
  不內嵌於 source package：P0/P1/P2/P3/P5 有真實 capture；
  P4 因 isolated runtime 找不到 Nsight Compute (`ncu`) 而
  `unsupported`；P6 為 `optional_not_run`。
- M0 共 8 個 measured samples、1 repetition，進入 provenance package 後為
  40 筆 measured benchmark records。P5 僅 1.329 秒，低於 5 秒 minimum。
  這些資料只證明 pipeline/correctness 與 pass capture；tiny-random M0
  不能作品質、泛化或正式效能結論，latency 只能標為 vertical smoke。
- 本整合套件執行時必須使用全新的持久化目錄與 session root。舊 RTX PRO 6000
  v1 session、其 raw traces、manifest、checksum、結果與環境檔不得複製、
  合併、覆寫或作為本 package 的輸出目錄。
- `--dry-run`、`--capture-plan` 與 `--all-models` 都不產生硬體量測。
  `--all-models` 目前只輸出 candidate plan，預期退出碼為 10。
- `--smoke` 與 `--experiment` 目前執行 CUDA microbenchmark/replay，
  不是完整 MoE model runner，也不是 P0–P6 trace collector。
- D-062 已停止所有後續付費 GPU execution，並從 active/required/holdout
  roadmap 移除 H100。所有 paid profiles 的 `execution_enabled=false`；
  在新的正式決策 supersede D-062 前，即使 approval 格式正確也必須 hard fail。
- D-063 封存本 NO-GO governance baseline，但不 supersede D-062；它綁定
  suite v1.4、外部 historical M0 evidence v1.2、三角色 review 與
  MAPE/quality formal gate。
- G2.5-S4-R4 的全新同源審查為三方 `NO-GO/NO-GO/NO-GO`。S4-R5 只做
  prospective CPU-only 修復：live ELF/loader/locale closure、supervisor death
  時的 OS-enforced worker lifetime、parent-authoritative EOS/parser replay、精確
  九檔 Granite payload、session 外部 seal anchor，以及遞迴完整 session
  inventory。S4-R4 與 G3-R4 均維持 immutable failure evidence。
- `qualification start` 必須先解析全新三角色同 hash 紀錄、獨立
  `gpt-5.6-sol` evaluation record 與 owner exact-command approval；目前這些 gate
  尚未完成，qualification GPU cells 為 0，GPU authority 為 `NONE`。
- 本封存的 `release_class` 是 `pipeline_smoke`。以
  `--release-class formal_candidate` 驗證必須因 repetitions/正式 gate 不足而拒絕。
- 目前 P0–P6 collector adapters 未實作完整，capture plan 明確為
  `NO-GO`/`execution_allowed=false`，不得產生假 `complete`。
- `--benchmark-smoke` 已實作 frozen matrix 驅動的 tiny M0 executable
  benchmark；`--capture-matrix` 建立 capture plan，`--ingest-session`
  建立 provenance session/archive。這不表示正式 M1–M3 或多重複統計完成。
- 外部 `artifacts/m0_provenance_package/` 的 audit/verify 為 complete；archive
  SHA-256 是
  `2a58c33abacacdbd87d8318dc370c5e400607b53407be52c3a3950c24231193d`，
  並由同名 `.sha256` sidecar 驗證。
  Canonical traces 是 measured raw 的可追溯轉換；expanded workload 與
  simulator 輸出是 derived/estimate，不是 GPU 實測 latency。

## Source package 與外部 evidence

Source package 的 unit/source 驗證完全自足，不內嵌約 2.67 GB Granite model
payload 或約 300 MB measured artifacts。Package 內含精確九檔 payload contract、
model inventory 與 fail-closed verifier；實際 payload 位於被 source inventory
排除的 `models/snapshots/`。`tests/fixtures/model_snapshot/` 僅供
`model_snapshot_inventory` 的 deterministic unit test，不是模型或量測證據。

未設定 `M0_EVIDENCE_ROOT` 時，9 個 measured-evidence regression tests 會以
`external measured evidence not bundled` 明確標記為 skipped；這只表示
source package 可驗證，不是 measurement pass。formal release 與 paid gate
不得以 skipped tests 取代外部 evidence 驗證。

完整 M0 evidence regression（以下路徑為可攜式 placeholder）：

```bash
PACKAGE_ROOT=/path/to/gpu_run_package_v2
M0_EVIDENCE_ROOT=/path/to/external/m0_benchmark_smoke \
PYTHONPATH="$PACKAGE_ROOT/.benchmark-runtime" \
python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

若使用其他保存位置，`M0_EVIDENCE_ROOT` 必須直接指向含
`run_manifest.json`、`checksums.json` 與 `p0/`、`p1/`、`p2/`、`p3/`、
`p5/` 的 `m0_benchmark_smoke` 目錄。

## G2.5 S4-R5 CPU-only application 邊界

S4-R5 package revision 為
`benchmark-driven-pipeline-smoke-v1.22-g25-s4-r5-application-review-candidate`。
它只能成為新的 frozen review target；不是 GPU approval、Granite
qualification result 或 G3-R5 entry evidence。未取得全新三角色 `GO/GO/GO`、
`gpt-5.6-sol: GO` 與 owner 對完整 argv 的逐字批准前，不得執行
`qualification start`。

正式 argv 由 application runner 產生並綁定，使用 dynamic loader
`--inhibit-cache`、`/usr/bin/python3 -I -S -B -X utf8`、`LC_ALL=C`、`LANG=C`
及 clean environment。Model snapshot 必須是 contract 指定路徑上的精確九個
regular files；缺檔、多檔、目錄、symlink、size 或 SHA-256 drift 都會在 worker
dispatch 前 hard-stop。

若未來 owner 授權的 session 抵達 terminal seal，runner 會在 session 同層的
`.g25_seal_anchors/<session>.json` 以 exclusive create 寫出 anchor，並在 stdout
回報其 SHA-256。檔案 permission 或 exclusive create 本身不能抵抗同一帳號的
事後改寫；可信性來自把該 SHA-256 獨立保存於 session 外，再於 audit 明確傳入：

```bash
./projectctl qualification status \
  --session granite-c1a-g25-qualification-r1-20260719 \
  --seal-anchor-sha256 <externally-retained-sha256>

./projectctl qualification audit \
  --session granite-c1a-g25-qualification-r1-20260719 \
  --seal-anchor-sha256 <externally-retained-sha256>
```

缺少可信 anchor hash、session 內新增任意檔案或目錄、symlink/special file、
inventory drift 或 final-seal drift，都不得得到 qualification PASS。不得以
`resume`、`retry-failed` 或從舊 G3 session 複製 artifact 補救。

## 閱讀順序

1. `PRE_FLIGHT_CHECKLIST.md`
2. `docs/GPU_EXPERIMENT_PLAN_V2.md`
3. `docs/BENCHMARK_SUITE_V1.md`
4. `docs/VALIDATION_MATRIX_BENCHMARK_V1.md`
5. `docs/TRACE_ACQUISITION_RUNBOOK.md`
6. `docs/DECISION_RECORD_V2.md`

設定來源為 `configs/gpu_profiles.yaml`、`configs/model_registry.yaml`、
`configs/model_compatibility.yaml`、`configs/hardware_schedule.yaml`、
`configs/storage_budget.yaml`、`configs/gpu_execution_review.yaml` 與
`TRACE_CAPTURE_PLAN.yaml`。

## GPU 範圍

- 歷史規劃 profile：RTX 3050 6GB、RTX 3090 24GB、RTX 4090 24GB、
  RTX 5090 32GB、精確 SKU `NVIDIA RTX PRO 6000 Blackwell Workstation
  Edition 96GB`。
- Optional：RTX A6000 48GB、A100 40GB、A100 80GB、V100 16GB、
  V100 32GB。
- H100 PCIe/SXM5 profiles 僅保留歷史資料，現在為
  `enabled=false, optional=true, disabled_reason=D-062`，不屬 active、
  required 或 holdout 排程。不接受籠統的 `RTX PRO 6000`、`H100`、
  `A100` 或 `V100` 名稱。H100
  form factor 與 MIG 狀態、3090 NVLink、所有 PCIe/NVLink 拓撲都必須由
  preflight 實測，不得推定。RTX 4090 禁止建立 NVLink profile。
- RTX 3050 只供本地流程與低 VRAM 壓力測試，不可按比例推算其他 GPU。
  H100 的盲測/資料中心上界角色僅是已停用的歷史規劃，不構成 active 要求。

模型分級、逐 GPU M0–M3 candidate/fallback、兩小時順序、P0–P6、儲存級別、
比較限制及 go/no-go 詳見 `docs/GPU_EXPERIMENT_PLAN_V2.md`。所有 F
（full-resident）與 Q（quantized-resident）都只是候選；只有 weight/runtime
estimate、allocation smoke、short-context generation smoke 及 profiler
compatibility smoke 全部通過後才能標成 confirmed。

## 上傳、解壓與環境

在本機從 source package 的父目錄建立獨立上傳檔：

```bash
tar -czf <package-archive>.tar.gz <package-directory>
scp <package-archive>.tar.gz USER@HOST:/persistent/
```

在 GPU instance 建立本 package 專用目錄；不得指向舊 v1 session：

```bash
cd /persistent
tar -xzf <package-archive>.tar.gz
cd <package-directory>
export GPU_PERSIST_ROOT=/persistent/edgehetero-v2-20260718/results
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.lock
chmod +x run.sh preflight.sh collect_environment.sh scripts/*.py
./run.sh --help
```

## Benchmark-driven 可執行流程

以下命令與目前 `run.sh --help` 一致。所有 output/session 路徑應不存在，
避免覆寫既有證據。

### 1. Offline validation 與 suite freeze

```bash
./run.sh --dry-run
./run.sh --freeze-suite
```

`--dry-run` 會驗證目前封存的 source package `checksums.txt`，並明確輸出
`source_package=PASS` 與 `embedded_measurement_evidence=NOT_INCLUDED`。
未提供外部 evidence 時的 explicit skips 不構成 measurement pass。
`--freeze-suite` 會重建並比對 frozen v1.4.0，不改寫既有 v1.2/v1.3
revision。v1.4 的 sample/domain 集合保持不重疊；model/hardware holdout
是獨立軸。hardware holdout 為 `unassigned_pending_future_decision`，不參與
active split。

### 2. 建立 frozen capture plan

```bash
./run.sh --capture-matrix configs/capture_matrices/m0_rtx3050_vertical_v1.json --output /persistent/m0-capture-plan.json
```

相容舊入口仍可建立 plan：

```bash
./run.sh --capture-plan --session-root /persistent/m0-plan --gpu-profile rtx3050_6gb --trace-profile standard
```

`--capture-plan` 是 deprecated compatibility alias；GPU profile/trace profile
會被 frozen matrix 忽略，只產生 `CAPTURE_PLAN.json`，不建立量測 session。
目前 collector adapter 缺失時命令寫出可稽核的 blocked plan 後退出 20；
這是預期 NO-GO，不是 capture complete。

### 3. RTX 3050 M0 benchmark smoke

```bash
export GPU_PERSIST_ROOT=/persistent/edgehetero-m0
export GPU_UUID=GPU-EXACT-UUID
export GPU_PCI_BUS_ID=00000000:01:00.0
export GPU_PROVIDER_METADATA=/persistent/provider-gpu-metadata.json
export GPU_PROVIDER_METADATA_SHA256=<lowercase-sha256>
export GPU_STORAGE_ESTIMATE=/persistent/edgehetero-m0/storage-estimate.yaml
./run.sh --benchmark-smoke --output /persistent/edgehetero-m0/m0-smoke --device cuda
```

此入口會執行 RTX 3050 preflight 與 tiny M0 correctness pipeline。CPU 模式
可用 `--device cpu`，但不得宣稱 GPU measurement。

### 4. Ingest、canonical、expand、simulate

```bash
./run.sh --ingest-session --source /persistent/edgehetero-m0/m0-smoke --session-root /persistent/edgehetero-m0/m0-session --archive /persistent/edgehetero-m0/m0-provenance.tar.gz
./run.sh --canonicalize-m0 /persistent/edgehetero-m0/m0-smoke /persistent/edgehetero-m0/m0-canonical
./run.sh --expand-workload /persistent/edgehetero-m0/m0-canonical/m0_moe_routing.json /persistent/edgehetero-m0/m0-workload.json
./run.sh --simulate-workload /persistent/edgehetero-m0/m0-workload.json /persistent/edgehetero-m0/m0-simulation.json
```

Ingest 會保存 native raw、content-addressed inventory、canonical converter
provenance、pass manifests、audit 與 archive hash。Simulation 是 estimate。

### 5. Review gate 與目前停止狀態

```bash
python3 scripts/review_gate.py \
  --gpu-profile rtx_pro_6000_blackwell_workstation_96gb \
  --matrix /persistent/frozen-paid-matrix.json \
  --approval /persistent/gpu-execution-approval.json
```

Approval 必須通過
`schemas/gpu_execution_approval.schema.json`，綁定 package `checksums.txt`
檔案 SHA-256、frozen matrix SHA-256、profile 與正式
`superseding_decision_id`；三個不同 reviewer identities 必須分別擔任
`architecture_system`、`model_benchmark`、`trace_statistics`，並要求
`blockers=[]`、正數 budget、deadline 與 expires。D-062 的
`superseded_by` 目前為 null，因此上述命令預期退出 20。RTX 3050
`--benchmark-smoke` 是唯一明確的 non-paid local pipeline exception；
不授權 performance/release claim。

### 6. Audit、封裝與驗證既有 session

```bash
./run.sh --trace-audit --session-root /persistent/edgehetero-m0/m0-session
./run.sh --package-results --session-root /persistent/edgehetero-m0/m0-session
./run.sh --verify-package /persistent/edgehetero-m0/m0-provenance.tar.gz
```

驗證退出碼：0 為 complete；10 為有完整 approval metadata 的
`accepted_incomplete`；20 為 failed。退出碼 10 不是完整通過，不得改寫成
complete。P0、identity、核心 manifests、checksums 與 native raw 等
non-waivable 缺失不能用 accepted incomplete 掩蓋。

### 7. 安全停止

一般付費 GPU runner 的 105/120 分鐘治理仍未獲授權。G2.5 本機 qualification
runner 已以 monotonic clock 實作 5790 秒停止新 dispatch、6300 秒結束 execution、
7200 秒 session hard deadline 與 7500 秒外層 timeout；每格 480 秒，先 TERM、
等待 30 秒後必要時 KILL；outer TERM 會先清除獨立 worker process group，避免
orphan GPU work。這個入口仍須 S4-R2 fresh 同源審查、hash-bound
`gpt-5.6-sol: GO` evaluation record與 owner逐字命令核准，不能因程式存在而執行。

付費 GPU runner 未來仍必須以 monotonic clock 在 elapsed 105 分鐘停止新
dispatch，並保留完整 15 分鐘 audit/package reserve，不得只靠可跳動的 wall
clock。
等待當前原子寫入/重複完成後，以一次 `Ctrl-C`
停止；先完成 audit、package、archive verify，並將 archive 與 `.sha256`
複製到持久化位置。確認兩者可下載後，才從平台介面 stop；只有確定無須保留
磁碟時才 terminate。若 capability 或 release gate 不通過，保存
failure/preflight/plan artifacts 後停止，不得用空 manifest、optional status
或 simulator estimate 冒充採集。

## 既有 microbenchmark 入口的證據範圍

在同一 experiment 已有 smoke 結果後，可執行：

```bash
./run.sh --experiment rtx-pro-6000-calibration --gpu-profile rtx_pro_6000_blackwell_workstation_96gb
```

它量測 shape-faithful CUDA microbenchmark 與 replay。`MoE-replay TPOT`
不是 full-model TPOT；`dequant` 是 symmetric-int4 proxy，不是已驗證的
DeepSeek AWQ checkpoint layout/fused kernel。這些結果不能替代完整模型
F/Q/O 執行或 P0–P6 trace。跨 GPU 純速度比較必須匹配 model/revision、
precision、quantization、checkpoint layout、backend 與 workload hash；
跨 precision 只能標為 precision/runtime ablation。

## 失敗回傳

保留並回傳：完整命令與 exit code、stdout/stderr、
`TRACE_CAPABILITY_MATRIX.json`、`COMPATIBILITY_PLAN.json`、
`TRACE_COMPLETENESS_REPORT.json`（若已產生）、environment/topology、
所有 failure logs 與未截斷的 native raw。不得把 dry-run、offline、
CPU fallback、candidate plan 或未受控 Colab 結果標成硬體校正。
