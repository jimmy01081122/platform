# GPU V2 執行前檢查

本清單是正式付費 session 的 go/no-go gate。任何 mandatory 項目未通過時，
不得用 CPU fallback、空 manifest、candidate plan 或舊 session 資料補成 pass。
依 D-062，目前正式付費 execution 為 NO-GO；只有 RTX 3050 non-paid local
pipeline smoke 有明確例外。

## 1. 隔離與證據狀態

- [ ] 操作目錄是獨立的 `<package-directory>`，未與既有量測目錄混用。
- [ ] `package_id` 是 `edgehetero-benchmark-driven-m0-20260718`，與
      `package_manifest.json` 一致。
- [ ] 已閱讀既有 RTX 3050 M0 證據：
      `artifacts/m0_benchmark_smoke/` 與
      `artifacts/m0_provenance_package/`。
- [ ] 未把 tiny-random M0 vertical smoke 寫成品質、泛化或正式效能；
      未把 canonical/simulator estimate 寫成 measured GPU latency。
- [ ] `GPU_PERSIST_ROOT` 與 `SESSION_ROOT` 是全新的 v2 專用持久化路徑。
- [ ] 舊 RTX PRO 6000 v1 session 未被掛載成輸出、未複製進 v2、未合併
      raw traces/manifest/checksum/results，且 v1 不會被 repo 內已整合的
      `gpu_run_package_v2` 覆寫。
- [ ] `SESSION_ROOT` 尚無 `SESSION_MANIFEST.json`；capture plan 不會覆寫既有 session。

## 2. GPU identity 與平台

### G2.5 local RTX 3050 application 的額外前置 gate

- [ ] 目前 target 是全新的 S4-R5 commit/tree/annotated tag；未修改或重新解讀
      S4-R4 與 G3-R4 immutable evidence。
- [ ] Source package 完整驗證、inventory/checksum closure 與 extracted clean-room
      均通過，且 qualification GPU cells 仍為 0。
- [ ] 三角色在完全相同的 commit/tree/tag/package ledger 上皆為 `GO` 且
      `blockers=[]`；另有獨立 `gpt-5.6-sol: GO`。
- [ ] Owner approval 綁定 application runner 產生的完整 exact argv，包含
      loader `--inhibit-cache`、`-I -S -B -X utf8`、`LC_ALL=C` 與 `LANG=C`。
- [ ] Granite snapshot 是 payload contract 的精確九檔 regular-file set，並位於
      approval 綁定的 exact path；不得臨時下載、補檔或接受額外 cache file。
- [ ] 已指定 session 外獨立保存 external seal anchor SHA-256 的方法。Session
      結束後，`status`／`audit` 必須以 `--seal-anchor-sha256` 傳回該 trusted hash；
      缺少 trusted hash 時只能 fail-closed。
- [ ] 未授權 `resume`、`retry-failed`、G3-R5、C1-B、paid GPU 或 archive release。

- [ ] 使用下列精確 profile ID 之一：
  - `rtx3050_6gb`
  - `rtx3090_24gb`
  - `rtx4090_24gb`
  - `rtx5090_32gb`
  - `rtx_pro_6000_blackwell_workstation_96gb`
  - optional：`a6000_48gb`、`a100_40gb`、`a100_80gb`、
    `v100_16gb`、`v100_32gb`
- [ ] H100 PCIe/SXM5 只作歷史 profile 保存，均為 disabled/optional，
      不在 active、required 或 holdout 排程。
- [ ] selected profile 必須同時 `enabled=true` 且
      `execution_enabled=true`；所有 paid profiles 目前因 D-062 為 false。
- [ ] product name、VRAM、PCI bus ID 與 form factor 符合 selected profile。
- [ ] H100 PCIe/SXM5 二選一且 MIG capability、mode、instance profile 已記錄；
      MIG partition 未標為 full H100。
- [ ] 3090 的 NVLink 只有在至少兩張卡且 preflight 實測 link 時才另立 profile。
- [ ] 4090 未建立 NVLink profile。
- [ ] topology、PCIe/NVLink state 來自量測，不是由型號推定。

## 3. Offline package 與規劃

```bash
./run.sh --help
./run.sh --dry-run
./run.sh --freeze-suite
./run.sh --capture-matrix configs/capture_matrices/m0_rtx3050_vertical_v1.json --output "$CAPTURE_PLAN"
```

- [ ] `--dry-run` 通過，且目前封存的 `checksums.txt` coverage/hash 完整。
- [ ] v1.4.0 frozen inventory 為 810 samples，manifest SHA-256 為
      `4de9eda6a8eabb5e49c897563033e2fa9d9a8b62db7b81790bb9a4c871f5621e`。
- [ ] capture plan 顯示 planned only，未宣稱 hardware pass/capture complete。
- [ ] 任一 collector adapter 缺失時 capture plan 為
      `NO-GO`、`execution_allowed=false` 並退出 20，未產生假 complete。
- [ ] model ID、checkpoint revision、tokenizer revision 尚未解析者維持 unresolved；
      正式下載前已釘選 immutable revision。
- [ ] 所有 F/Q 仍標為 candidate，沒有由理論 weight size 直接改成 confirmed。
- [ ] 已為每模型/每 pass 填寫 runtime、raw/compressed size、temporary space、
      sustained/peak write bandwidth 與 perturbation risk；不得保留 null 後正式開跑。

## 4. 儲存 go/no-go

- [ ] 選定 storage class：RTX 3050 預設 S1；3090/4090/5090/PRO6000 與
      optional GPU 預設 S2；H100 預設 S3。
- [ ] S0/S1/S2/S3 的規劃保留量分別為 10/50/200/500 GiB，hard minimum
      分別為 8/40/160/400 GiB；這些是 reservation，不是 trace size 量測。
- [ ] bounded smoke 已量測 raw bytes、duration、compression ratio 與 packaging
      throughput，且 105–120 分鐘 audit/package window 足夠。
- [ ] estimate 每一數值欄位皆為非 null 且大於 0；temporary working 與
      package reserve 均為 additive capacity gate。
- [ ] estimate 以 `state_id`/`pass_id` 完整且不重複覆蓋 frozen capture matrix
      全部 state；runtime 總和加 `package_reserve_minutes` 不超過 120 分鐘。
- [ ] preflight 實際執行 fsync write probe，量測寫速至少為 estimated peak
      的 1.20 倍，且 free space 同時通過 hard minimum 與 temporary/package gate。
- [ ] archive destination 為持久化儲存；不需要刪除 P0、P1 native timeline、
      P2 lossless routing、model/workload/environment manifests 或 failure logs 才能容納。

## 5. Online preflight

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

- [ ] `GPU_PERSIST_ROOT` 已顯式設定至持久化路徑。
- [ ] `nvidia-smi`、CUDA-enabled PyTorch、driver/runtime 可用。
- [ ] exact selected UUID 與 PCI bus pair 在列舉的所有 GPU 中只匹配一張；
      未默認使用第一張 GPU。
- [ ] exact SKU、`reject_skus`、VRAM 容差及最低 20% free VRAM gate 通過。
- [ ] form factor 來自 hash 驗證通過的 provider metadata artifact，不接受 CLI
      自稱；MIG mode/instance、topology dump、compute capability 與
      PCIe generation/width 全部符合 selected profile。
- [ ] `nsys`、`ncu` 與 counter permission 已記錄；degraded capability 有明確理由。
- [ ] mandatory collector 即使 unsupported/permission denied，也能建立真實 failure
      manifest；否則 no-go。

## 6. 第二決策層 review gate

```bash
python3 scripts/review_gate.py \
  --gpu-profile "$GPU_PROFILE" \
  --matrix "$FROZEN_EXECUTION_MATRIX" \
  --approval "$GPU_EXECUTION_APPROVAL"
```

- [ ] approval 綁定目前 `checksums.txt` 檔案 SHA-256、matrix SHA-256 與 profile。
- [ ] 三位不同 reviewer identities 分別擔任 `architecture_system`、
      `model_benchmark`、`trace_statistics`；`blockers=[]`、正數 budget、
      deadline、approved/expires timestamps 均有效。
- [ ] approval 的 `superseding_decision_id` 綁定正式 supersede D-062 的決策。
- [ ] `configs/gpu_execution_review.yaml` 已記錄 superseding decision。
      現在 D-062 `superseded_by: null`，因此 paid execution 必須 hard fail。
- [ ] RTX 3050 exception 僅限 `--benchmark-smoke` 的 local pipeline/correctness，
      不涵蓋 legacy smoke、正式 acquisition 或效能 release。

## 7. Benchmark smoke 與 compatibility

```bash
./run.sh --benchmark-smoke --output "$BENCHMARK_OUTPUT" --device cuda
./run.sh --all-models --gpu-profile "$GPU_PROFILE" --session-root "$SESSION_ROOT"
```

- [ ] CUDA benchmark 前已顯式設定 `GPU_PERSIST_ROOT`，且 output 不存在。
- [ ] 已理解 benchmark 是 tiny-random M0 pipeline/correctness vertical smoke。
- [ ] 已確認 P0/P1/P2/P3/P5 有 measured artifacts；P4 是 `ncu`
      unavailable/unsupported；P6 是 optional_not_run。
- [ ] 已確認 P5 duration 至少 5 秒；現存 artifact 只有 1.329 秒，因此不符合
      正式 telemetry statistics。
- [ ] 已規劃正式 repetitions、統計與品質 gate；現存 1 repetition 不足。
- [ ] 已理解第二個命令預期退出 10，只建立 `COMPATIBILITY_PLAN.json`，
      不代表 M0–M3 已執行或 accepted incomplete。
- [ ] M1–M3 的 allocation、generation、profiler compatibility 仍須實測。
- [ ] 失敗的 F/Q/O 候選保留 capacity boundary、精確設定與 failure log，
      未從矩陣靜默刪除。

## 8. Provenance 與正式 release gate

```bash
./run.sh --ingest-session --source "$BENCHMARK_OUTPUT" --session-root "$SESSION_ROOT" --archive "$RESULT_ARCHIVE"
./run.sh --canonicalize-m0 "$BENCHMARK_OUTPUT" "$CANONICAL_ROOT"
./run.sh --expand-workload "$CANONICAL_ROOT/m0_moe_routing.json" "$WORKLOAD_JSON"
./run.sh --simulate-workload "$WORKLOAD_JSON" "$SIMULATION_JSON"
```

- [ ] Native raw、canonical、derived/simulator output 分開保存並正確標記。
- [ ] 40 measured records、pass manifests、raw inventory、converter provenance
      與 archive hash 可追溯。
- [ ] 未把 P1/P5 instrumented latency 與 P0 clean latency 直接比較。
- [ ] 未把 `TRACE_COMPLETENESS_REPORT.status=complete` 擴張解讀為正式
      benchmark release；它只表示該 M0 provenance session 的契約完整。
- [ ] 正式 release 維持 no-go，直到 repetitions/statistics、P5 ≥5 秒、
      M1–M3 與品質/跨硬體 validation 完成。

## 9. 關閉 instance 前

collector 已實作且真實資料完整時才執行：

```bash
./run.sh --trace-audit --session-root "$SESSION_ROOT"
./run.sh --package-results --session-root "$SESSION_ROOT"
./run.sh --verify-package "$RESULT_ARCHIVE"
```

- [ ] 每個 model/pass 有 complete 或明確 failure status；native raw 先保存且不可變。
- [ ] archive 可解壓、schema/checksum 可驗證，archive 與 `.sha256` 已在持久化儲存。
- [ ] verify 退出 0 才是 complete；退出 10 是有 approval 的
      `accepted_incomplete`；退出 20 是 failed。
- [ ] Paid/capture-matrix acquisition runner 尚未實作，因此該流程的 monotonic
      elapsed deadline 維持 blocker；不得將 plan contract 當成 enforcement。
      這不涵蓋已獨立實作的 G2.5 qualification application runner。未來 paid
      runner 必須在 105 分鐘後停止新 dispatch，並保留 15 分鐘
      audit/package reserve。
