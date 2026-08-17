# Decision Record V2

## DR-001：已整合 V2 套件與舊 PRO 6000 v1 完全隔離

- 狀態：Accepted。
- 決策：本 package ID 固定為
  `edgehetero-benchmark-driven-m0-20260718`，與
  `package_manifest.json` 一致；每張 GPU 建立新的 v2
  `GPU_PERSIST_ROOT`、`SESSION_ROOT` 與 `session_id`。禁止讀寫合併舊 RTX
  PRO 6000 v1 的 raw traces、manifests、checksums、environment 或 results。
- 理由：跨 package/session 混合會破壞 provenance、hash identity、blind
  split 與校正可信度。
- 後果：v1 只能作外部歷史證據，若日後比較須先建立明確的 cross-package
  comparison protocol，不可直接匯入本 session。

## DR-002：量測狀態改為有限範圍 RTX 3050 M0 evidence

- 狀態：Superseded by measured M0 evidence。
- 決策：承認 `artifacts/m0_benchmark_smoke/` 中 RTX 3050
  P0/P1/P2/P3/P5 為 measured M0 vertical smoke，並承認 ingest 後 40 筆
  benchmark records；禁止擴張為品質或正式效能。
- 理由：native GPU artifacts、pass manifests、raw inventory 與 provenance
  archive 已存在，繼續寫 `no hardware results included` 已不正確。
- 後果：latency 只標 vertical smoke；P4 unsupported、P5 insufficient、
  P6 optional_not_run、M1–M3/正式統計未完成必須同時揭露。

## DR-003：精確 SKU 優先於產品家族名稱

- 狀態：Accepted。
- 決策：PRO 6000 必須是
  `NVIDIA RTX PRO 6000 Blackwell Workstation Edition 96GB`；H100 必須明選
  PCIe 80GB 或 SXM5 80GB；A100/V100 必須分 VRAM SKU。MIG、form factor
  與 link topology 必須量測。
- 理由：同產品家族的 VRAM、compute、interconnect、MIG 與 thermal envelope
  不同，不能視為相同硬體。
- 後果：generic 名稱 unresolved 時 preflight no-go；PCIe/SXM、MIG/full GPU、
  16/32GB 或 40/80GB 結果分開呈現。

## DR-004：採 hardware-major scheduling

- 狀態：Superseded for active execution by D-062；以下僅保留歷史 scheduling
  contract。
- 決策：在單一 GPU 上依 M0→M1→M2→M3 完成 candidate/fallback、P0–P6
  status、audit、package、verify，再切換硬體。H100 holdout 最後且 frozen。
- 理由：減少遠端環境漂移與漏採，並在 instance 仍可用時補救缺失。
- 後果：付費 session 使用 120 分鐘模板，105 分鐘硬停止新實驗並保留封裝時間。

## DR-005：F/Q 一律先視為 candidate

- 狀態：Accepted。
- 決策：F/Q 需通過 weight-size、runtime-overhead、allocation、
  short-context generation、profiler compatibility 五項檢查才可 confirmed。
  失敗保留 capacity boundary；M3 禁止 F/Q/O。
- 理由：理論 weight bytes 不含 runtime、KV cache、workspace、fragmentation、
  backend/kernel 與 profiler compatibility。
- 後果：tiny-random M0 fixture 已完成 compatibility remap 與 exact tensor copy
  verification，但不能推廣為正式 checkpoint F/Q；其餘 model revisions、
  checkpoint layout 與 fused-kernel contract 未解析。

## DR-006：Maximal 是分離 multi-pass，不是 profiler 疊加

- 狀態：Accepted。
- 決策：P0 clean baseline 不啟用高開銷 profiler；P1–P6 在固定 workload、
  seed、input/config 下分離採集。P4/P6 限代表 kernel/layer/window。
- 理由：同 pass 疊加 profiler 會造成 perturbation，無法把結果當 clean baseline。
- 後果：跨 pass 以 identity、hash、request/token ID、NVTX、kernel sequence
  對齊，不直接相減絕對 profiler timestamp。

## DR-007：Native raw first

- 狀態：Accepted。
- 決策：native raw → immutable content-addressed archive → versioned converter
  → canonical trace → derived summary。
- 理由：只留 canonical/summary 會失去重新解析、工具交叉驗證與完整性證據。
- 後果：禁止刪除或原地修改 raw；unsupported/permission denied 也需 manifest
  與完整錯誤。

## DR-008：儲存級別是 reservation，不是量測

- 狀態：Accepted。
- 決策：RTX 3050 預設 S1 50/40 GiB；3090/4090/5090/PRO6000 與 optional
  GPU 預設 S2 200/160 GiB；H100 預設 S3 500/400 GiB。
- 理由：現有 M0 package 已有 empirical size，但不足以估計正式 M1–M3 或
  maximal capture。
- 後果：正式 run 前仍須 bounded sample 估算每模型/pass 的 raw/compressed
  bytes、temporary peak、write bandwidth 與 package time；null 即 no-go。

## DR-009：比較限制由 identity 決定

- 狀態：Accepted。
- 決策：純 GPU speed comparison 必須相同 model/revision、precision、
  quantization、checkpoint layout、backend、workload hash。
- 理由：跨 precision 或格式差異同時改變 kernel/runtime，不能歸因於 GPU。
- 後果：跨 precision 只能標 precision/runtime ablation；RTX 3050 不可比例
  外推；H100 不可作唯一 edge representative；3090 NVLink 不可由單卡推定；
  4090 不得使用 NVLink profile。

## DR-010：Benchmark-driven M0 orchestration 已實作，正式 release 仍 no-go

- 狀態：Partially implemented。
- 決策：`--freeze-suite`、`--capture-matrix`、`--benchmark-smoke`、
  `--ingest-session`、`--canonicalize-m0`、`--expand-workload` 與
  `--simulate-workload` 是目前正式可用入口。`--all-models` 仍為
  plan-only，退出 10。
- 證據：`run.sh --help`；`artifacts/m0_benchmark_smoke/`；
  `artifacts/m0_provenance_package/INGEST_REPORT.json`。
- 後果：可宣稱 RTX 3050 tiny-random M0 vertical slice 與 provenance
  完整；不可宣稱正式 repetitions/statistics、品質或 M1–M3 acquisition。

## DR-011：Accepted incomplete 使用退出碼 10

- 狀態：Accepted。
- 決策：package verify 退出 0=complete、10=approved incomplete、20=failed。
  退出 10 需 `accepted_incomplete: true` 及 `approved_by`、有效
  `approved_utc`、`reason`。
- 理由：硬體/權限可能合理缺少可 waiver 項目，但不能靜默忽略。
- 後果：退出 10 不得改稱 complete；P0、identity、core artifacts、checksums、
  native raw 等 non-waivable 問題仍為 failed。另需注意 all-models 的退出 10
  是 plan-only 語義，不是 accepted incomplete。

## DR-012：不更新根 package checksums.txt

- 狀態：Accepted for the pre-integration documentation task。
- 決策：本次只修改指定文件，不更新根目錄 `checksums.txt`。
- 理由：任務明確禁止更新；因此文件變更後根 checksum inventory 預期不一致。
- 後果：靜態驗證只能使用 `./run.sh --dry-run` 的 skip-checksums 路徑並明確
  報告限制。不得聲稱 `sha256sum -c checksums.txt` 或完整 package integrity
  已通過。Result session 內由 packager 產生的 `checksums.sha256` 是不同檔案，
  不受此決策禁止。

## DR-013：T0–T8 錯位以 v1.2.0 分類修正並重凍結

- 狀態：Accepted。
- 決策：早期 native ID 中 GSM8K 的 T0、MMLU 的 T1 標記視為 taxonomy
  錯位；v1.2.0 正式定義 GSM8K=T1、MMLU=T2，依相同 raw sample hash 建立
  mapping 與新 sample ID，重凍結 suite。
- 證據：`artifacts/m0_benchmark_smoke/suite_class_mapping_v1.2.0.json`、
  `configs/test_suites/frozen/v1.2.0/inventory.json`。
- 理由：任務語義必須與 T0–T8 suite contract 一致。
- 後果：native GPU artifacts 不變且不需重跑；修正屬 post-execution
  classification，不改變量測值，也不提升 evidence tier。

## DR-014：M0 completeness 不等於 benchmark release

- 狀態：Accepted。
- 決策：`TRACE_COMPLETENESS_REPORT.status=complete` 只表示該 session 的
  required passes、hash、raw/canonical 與 provenance 契約完整。
- 證據：required passes 為 P0/P1/P2/P3/P5；P4/P6 是 conditional optional；
  archive SHA-256
  `1771738622386ec57262c85abc0bb1419d0775ab01534d5e8c39c3962e70d3a4`。
- 後果：P5 1.329 秒不足、1 repetition、品質 0、M1–M3 未執行仍阻擋正式
  release；不能因 audit/verify complete 而省略這些限制。

## DR-015：Measured、canonical 與 simulator estimate 分層

- 狀態：Accepted。
- 決策：native RTX 3050 capture 是 measured；canonical trace 是 measured
  raw 的 versioned conversion；expanded workload、route analysis 與 system
  simulation 是 derived/estimate。
- 理由：格式轉換保留量測來源，但 workload expansion 與 service simulation
  引入模型化假設。
- 後果：simulator latency 不得標成 RTX 3050 measured latency；P1/P5
  instrumented latency 也不得替代 P0 clean latency。

## DR-016：D-062 停止後續付費 GPU 執行

- 狀態：Accepted。
- 決策：兩輪 RTX PRO 6000 後四項 Q1 MAPE gate 全數失敗，依 D-062
  停止所有後續 paid GPU execution，並將 H100 從 active、required 與
  holdout roadmap 移除。H100 profiles 僅歷史保留且
  `enabled=false, optional=true, disabled_reason=D-062`。
- 執行契約：所有 paid profiles `execution_enabled=false`。新的正式決策
  supersede D-062 後，仍須通過第二決策層 approval；approval 綁定 package
  checksums hash、matrix hash、reviewers、空 blockers、budget、deadline 與
  expiry。RTX 3050 local pipeline smoke 是唯一 non-paid 例外。
- 後果：collector 缺失時維持 NO-GO，不實作假 collector、不產生假 complete。
  未來 runner 以 monotonic 6300 秒停止新 dispatch，保留 900 秒 audit/package。

## DR-017：D-063 封存 NO-GO governance baseline

- 狀態：Accepted。
- 決策：D-063 封存 suite v1.4、外部 historical M0 evidence v1.2、MAPE/quality
  formal gate 與 D-062 paid NO-GO 基線，不構成 D-062 的 superseding decision。
- 核准契約：任何未來 paid execution approval 必須綁定實際
  `superseding_decision_id`，並由三個不同人分別擔任
  `architecture_system`、`model_benchmark`、`trace_statistics`。D-062
  `superseded_by: null` 時固定退出 20。
- Preflight 契約：RTX PRO 6000 名稱採完整 exact regex；form factor 只能取自
  有 SHA-256 綁定的 provider metadata artifact。storage estimate 必須覆蓋
  frozen capture matrix 每個 state/pass，runtime 加 package reserve 不得超過
  120 分鐘。
- 限制：目前沒有 runner，monotonic deadline 仍是 blocker；不得將 capture
  plan 中的 dispatch contract 誤稱為已實作的 deadline enforcement。

## 未解決事項與解除 no-go 條件

1. 釘選所有 model/tokenizer/checkpoint immutable revisions。
2. 完成 F/Q checkpoint layout、quantization 與 fused-kernel gates。
3. 對 M1–M3 實作並執行 allocation/generation/profiler smoke。
4. 增加正式 repetitions、統計、P5 ≥5 秒與品質 evaluator。
5. 在有 `ncu` 的隔離環境執行 P4，或持續保留 unsupported capability
   evidence；P6 依實驗需求決定是否執行。
6. 完成跨硬體 validation，且不得由 RTX 3050 比例外推。
7. 在付費 session 前完成 per-model/per-pass 儲存與時間估算。
8. 任何 paid GPU 重啟前，先以新決策正式 supersede D-062，再通過第二決策層
   review gate；H100 不會因原歷史 holdout 定義自動恢復。
