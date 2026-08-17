# Trace Acquisition Runbook

## 1. 目的與硬限制

本 runbook 以 hardware-major 執行：在目前 GPU 上完成所有 planned model、
fallback、P0–P6 狀態、audit 與 package 後，才換下一張 GPU。

目前 package 具備：

- offline dry-run、suite freeze 與 frozen capture matrix；
- exact-profile online preflight；
- CUDA microbenchmark smoke/experiment；
- candidate-only compatibility plan；
- tiny-random Qwen2MoE M0 executable benchmark；
- benchmark ingest、content-addressed raw、canonical conversion、workload
  expansion 與 simulation；
- session audit/package/verify。

RTX 3050 現存 M0 vertical slice 的 P0/P1/P2/P3/P5 是真實量測；P4 因
`ncu` unavailable 為 unsupported，P6 optional not run。它仍不具備 M1–M3
正式模型、多 repetitions、正式統計或品質結論。

D-062 已停止全部後續 paid GPU execution 並移除 H100 platform holdout。
所有 paid profiles 目前 `execution_enabled=false`；collector adapters 缺失時
capture plan 為 NO-GO，不能產生假 complete。

## 2. Session 隔離

```bash
cd /path/to/gpu_run_package_v2
export GPU_PERSIST_ROOT=/persistent/edgehetero-v2-20260718/results
export GPU_PROFILE=rtx_pro_6000_blackwell_workstation_96gb
export EXPERIMENT_ID=rtx-pro-6000-calibration
export SESSION_ROOT=/persistent/edgehetero-v2-20260718/sessions/pro6000-blackwell-96gb
./run.sh --help
```

`SESSION_ROOT` 必須是全新的 v2 路徑。不得指向、複製、hard-link、合併或覆寫
舊 RTX PRO 6000 v1 session。此 package ID 是
`edgehetero-benchmark-driven-m0-20260718`；不得沿用舊文件中的
`edgehetero-rtxpro6000-h100-20260718` 或未知 v1 identity。

## 3. Offline dry-run 與 capture plan

```bash
./run.sh --dry-run
./run.sh --freeze-suite
./run.sh --capture-matrix configs/capture_matrices/m0_rtx3050_vertical_v1.json --output /persistent/m0-capture-plan.json
```

判讀：

- dry-run 只做 package 靜態驗證，並刻意 `--skip-checksums`；不代表 package
  `checksums.txt` 已更新或通過。
- freeze-suite 對目前 v1.4.0 做可重現重建與 byte/JSON 比對，不覆寫
  v1.2/v1.3 frozen revision。
- capture-matrix 只輸出 plan，並拒絕覆寫既有 output。
- collector adapter 未實作時仍寫出 blocked plan 供稽核，但狀態為
  `NO-GO`、`execution_allowed=false` 並退出 20。
- deprecated `--capture-plan --session-root PATH [--gpu-profile ID]
  [--trace-profile minimal|standard|maximal]` 是 compatibility alias；profile
  參數會被 frozen matrix 忽略，且只建立 `CAPTURE_PLAN.json`。
- model registry 中 null revision 會以 unresolved 方式進 plan。正式 weight
  acquisition 前必須釘選 immutable revision；不得把 unresolved plan 當可執行。

## 4. Online preflight

```bash
./preflight.sh --gpu-profile "$GPU_PROFILE" \
  --persist-root "$SESSION_ROOT" \
  --capability-output "$SESSION_ROOT/TRACE_CAPABILITY_MATRIX.json" \
  --gpu-uuid "$GPU_UUID" --pci-bus-id "$GPU_PCI_BUS_ID" \
  --provider-metadata "$GPU_PROVIDER_METADATA" \
  --provider-metadata-sha256 "$GPU_PROVIDER_METADATA_SHA256" \
  --storage-estimate "$GPU_STORAGE_ESTIMATE" \
  --capture-matrix "$FROZEN_EXECUTION_MATRIX"
```

Go 必須先滿足 profile `enabled=true` 與 `execution_enabled=true`，再滿足
exact selected UUID/PCI bus pair、normalized `reject_skus`、exact SKU regex/VRAM
容差、由 hash 綁定 provider metadata artifact 提供的 form factor、
MIG/topology、compute capability、PCIe generation/width、CUDA-enabled
PyTorch、至少 20% free VRAM、writable persistent storage。`nsys`/`ncu`
缺失或 counter permission 不足會形成 degraded capability；若 mandatory
collector 無法留下 unsupported/permission-denied manifest，則 no-go。

此外必須以 bounded representative capture 補齊每模型/pass 的 runtime、
raw/compressed bytes、temporary peak、write bandwidth、compression ratio、
packaging time 與 perturbation risk。reservation 不是量測：

- S1：50 GiB reservation / 40 GiB hard minimum，RTX 3050。
- S2：200/160 GiB，3090/4090/5090/PRO6000 與 optional GPU。
- S3：500/400 GiB，H100 PCIe/SXM5。

所有 numeric estimates、temporary working 與 package reserve 必須非 null
且大於 0，並以 state/pass 完整覆蓋 frozen capture matrix；runtime 加
package reserve 不得超過 120 分鐘。preflight 會執行 fsync write probe；量測寫速須至少達 estimated
peak 的 1.20 倍，free space 須同時滿足 hard minimum 與 temporary/package gate。

## 4.1 第二決策層 review gate

任何 paid GPU 命令前先執行：

```bash
python3 scripts/review_gate.py \
  --gpu-profile "$GPU_PROFILE" \
  --matrix "$FROZEN_EXECUTION_MATRIX" \
  --approval "$GPU_EXECUTION_APPROVAL"
```

Approval 需綁定正式 `superseding_decision_id`、package `checksums.txt`
SHA-256、matrix SHA-256 與 selected profile，並由三個不同 reviewer identities
分別擔任 `architecture_system`、`model_benchmark`、`trace_statistics`，
且含 `blockers=[]`、正數 budget、
deadline、approved/expires timestamps。D-062 目前未被 supersede，因此 review
gate 必須退出 20；approval 本身不能覆蓋 D-062。唯一例外是 RTX 3050
`--benchmark-smoke` 的 non-paid local pipeline/correctness smoke。

## 5. Executable M0 benchmark smoke

```bash
export GPU_PERSIST_ROOT=/persistent/edgehetero-m0
export GPU_UUID=GPU-EXACT-UUID
export GPU_PCI_BUS_ID=00000000:01:00.0
export GPU_PROVIDER_METADATA=/persistent/provider-gpu-metadata.json
export GPU_PROVIDER_METADATA_SHA256=<lowercase-sha256>
export GPU_STORAGE_ESTIMATE=/persistent/edgehetero-m0/storage-estimate.yaml
./run.sh --benchmark-smoke --output /persistent/edgehetero-m0/m0-smoke --device cuda
```

此命令會執行 RTX 3050 preflight，再跑 pinned tiny Qwen2MoE correctness
pipeline。現存 evidence：

- P0/P1/P2/P3/P5 measured，跨 pass 10 requests 輸出一致；
- 8 measured samples、1 repetition；
- P4 unsupported，理由是 isolated runtime 沒有 Nsight Compute executable；
- P5 23 telemetry samples、1.329 秒，低於 5 秒 minimum；
- P6 optional_not_run。

因此成功只證明 tiny-random M0 pipeline/correctness。Latency 只能標
`vertical smoke`；品質、泛化、正式 TPOT/throughput 與 M1–M3 均未成立。

## 6. Compatibility plan / all-models

```bash
./run.sh --all-models --gpu-profile "$GPU_PROFILE" --session-root "$SESSION_ROOT"
```

預期行為是寫出 `COMPATIBILITY_PLAN.json`、印出
`DEGRADED PLAN ONLY; M0-M3 were not executed` 並退出 10。此退出碼在這個
入口表示「候選計畫已產生，但沒有 model runner」，與 verify 的
`accepted_incomplete` 退出 10 是不同語境。

逐模型正式確認仍需：

1. weight-size estimate；
2. runtime-overhead estimate；
3. allocation smoke；
4. short-context generation smoke；
5. profiler compatibility smoke。

失敗必須保留 C（capacity boundary）、exact config、failure log 與 fallback；
不得從矩陣刪除。M3 只能 L/T。

## 7. Ingest 與 P0–P6 狀態

```bash
./run.sh --ingest-session --source /persistent/edgehetero-m0/m0-smoke --session-root /persistent/edgehetero-m0/m0-session --archive /persistent/edgehetero-m0/m0-provenance.tar.gz
```

Ingest 會建立 session identity、raw inventory、P0–P6 pass manifests、
canonical traces、40 benchmark records、audit report 與 archive。現存 package：

- `artifacts/m0_provenance_package/INGEST_REPORT.json`
- audit/verify：`complete`，findings 0
- archive SHA-256：
  `2a58c33abacacdbd87d8318dc370c5e400607b53407be52c3a3950c24231193d`
- session bytes：160,340,515；archive bytes：41,204,003

`complete` 僅表示這個 M0 provenance session 滿足其 required-pass 契約；
P4/P6 是 conditional optional。它不表示 P5 已達正式統計長度，也不表示
M1–M3 或 benchmark release 完成。

所有 pass 使用固定 model revision、workload、seed、input、backend/config，
以共同 identity/hash 對齊。P0 與 instrumented P1/P5 必須分離，P4/P6
限代表窗口。不得拿 P1/P5 latency 替代 P0 clean latency。

### Native raw first

資料生命週期固定為：

```text
native raw output
  -> immutable content-addressed raw archive
  -> versioned parser/converter
  -> canonical trace
  -> derived tables/summary
```

不得原地修改 raw、只留 CSV/截圖/平均值、用 canonical 取代 native output，
或在沒有 converter version/provenance 時覆寫輸出。unsupported、
permission_denied、not_applicable、failed 也必須為每 planned repetition
建立真實 manifest。

## 8. Trace audit

Ingest session 完成後執行：

```bash
./run.sh --trace-audit --session-root "$SESSION_ROOT"
```

Audit 檢查 core artifacts/hashes、P0–P6 status/repetitions、native raw inventory、
converter provenance、clock/identity alignment、checksums 與 packaging space。
只執行 `run.sh --help` 列出的命令。

## 9. Package results

Audit 退出 0，或有有效 approval 且退出 10 時，才可封裝：

```bash
./run.sh --package-results --session-root "$SESSION_ROOT"
```

封裝器會再次 audit/verify、產生 `RESULT_PACKAGE_MANIFEST.json`、session-local
`checksums.sha256`、`.tar.gz` 與 sidecar `.sha256`，並解壓後複驗。
這不會更新 source package 根目錄的 `checksums.txt`。

## 10. Verify package

以實際 archive 路徑取代範例：

```bash
export RESULT_ARCHIVE=/persistent/edgehetero-v2-20260718/sessions/pro6000-blackwell-96gb-YYYYMMDDTHHMMSSZ.tar.gz
./run.sh --verify-package "$RESULT_ARCHIVE"
```

- 0：complete。
- 10：approved incomplete；必須 `accepted_incomplete: true` 且具有
  `approved_by`、有效 `approved_utc`、`reason`。
- 20：failed。

退出 10 不得寫成 complete。P0、identity、environment/workload/configuration、
checksums、native raw 等 non-waivable 問題仍會失敗，不能由 approval 覆蓋。

## 11. 安全停止

1. 以 monotonic elapsed time 計時，6300 秒（105 分鐘）後不再 dispatch
   新工作，並保留 900 秒（15 分鐘）audit/package reserve；wall clock 不足以取代。
2. 若需中止，等待目前 atomic write/repetition 邊界，以一次 `Ctrl-C` 停止。
3. 保存 stdout/stderr、exit code、capability/compatibility/completeness report、
   failure logs 與未截斷 native raw。
4. capability 或 release gate no-go 時，不把 optional/unsupported pass 改寫成
   measured；保存 plan、preflight 與 failure evidence。
5. benchmark ingest 完成時，依序 audit、package、verify。
6. 確認 archive 與 `.sha256` 位於持久化儲存且可下載。
7. 需要保留磁碟時使用平台 stop；只有確認不需保留時才 terminate。

## 12. 各硬體套用方式

目前不得對 paid GPU 套用此流程。H100 PCIe/SXM5 profiles 只作歷史保留，
不在 active/required/holdout 路線；optional profiles 亦維持 disabled。
只有新決策正式 supersede D-062 後，才可依
`GPU_EXPERIMENT_PLAN_V2.md` 重新審查角色、fallback、storage 與 gate。
