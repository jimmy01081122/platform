# AGENT_HANDOFF

最近一次 session 的交接記錄。**每個 session 完工時把新記錄加在最上面**，不刪除舊記錄。

跨 session 的權威狀態是 `governance/stage_ledger.yaml`；本文件是人類可讀的敘述補充。**衝突時以 ledger 與實際重跑的指令為準**，不以本文件的敘述為準。

---

## 2026-08-18 · Stage 0 收尾與 session 指引系統

**Session 目標** 補完 Stage 0 遺留缺口，建立跨 session 的交接契約。

**新增**

```text
governance/stage_ledger.yaml
docs/PHASE_NAMING_MAP.md
docs/status/{CURRENT_STATUS,AGENT_HANDOFF,DECISION_LOG,ASSUMPTION_REGISTER,VALIDATION_MATRIX}.md
docs/session_guides/README.md
docs/session_guides/STAGE_{A1,A2,A3,A4,B1,B2,C1,C2}_*.md
docs/session_guides/TRACK_GPU_MEASUREMENT.md
```

**移動** `governance/{charter,evidence_levels,capability_registry}.yaml` → `project/`

**修改** `PLATFORM_FLOW_SPECIFICATION.md`（§0 權威來源路徑、§12 改為實際結構、新增 §13.1 每階段開新 session）、`README.md`

**執行的檢查**

| 指令 | 結果 |
|---|---|
| `make doctor` | `workspace_contract: pass`（修復前為 `missing_required_paths: project/charter.yaml`） |
| `make verify-evidence` | 4423/4423 通過 |
| `make test` | 317 Python + 14 CTest，0 失敗 |

**修正的問題**

`make doctor` 先前失敗：遷移時把 `project/*.yaml` 移到 `governance/`，但 `scripts/projectctl.py:15` 仍要求 `project/charter.yaml`。實測 4 個檔案引用 `project/`、僅 1 個引用 `governance/`，因此還原路徑並改該份文件（決策 P-008）。

**新增假設** 無新的 `measured` 假設。新增 4 條 `assumed`（PA-101–PA-104），全部標明須由 A1/A3/A4 驗證，不得作為結論。

**仍然禁止的主張** 任何 calibrated、break-even、accelerator 主張；任何長上下文或高並發效能主張。

**下一個最高資訊增益的動作** Stage A1：修四項模型形式缺陷並重擬合。這是純 CPU 工作，重擬合所需的 component 與 transfer 微基準已在 `evidence/measurement_backups/`。**動手前必須先寫 `experiments/specs/cal_model_form_repair_v1.yaml` 事前登記**（規格 §7.2）。

---

## 2026-08-17 · Stage 0 遷移

**Session 目標** 從凍結的來源 workspace 建立 `/home/a/platform`。

**結果** 18 GB → 610 MB；4423 個證據檔逐檔 checksum 驗證一致（差異 0）；317 Python + 14 CTest 全綠；commit `01e54ce` 已推送。

**排除項** 439 MB 第三方 HF 語料、16.7 GB vendored 環境與模型權重、`governance/history` 遞迴快照、生成的 netlist 與工具 log。理由與完整對照見 `governance/lineage.yaml`。

**解決的既有失敗** 來源 repo 有 45 個測試失敗，根因僅是依賴釘選未安裝（jsonschema 3.2.0 vs 需要 4.24.0）加上 `governance/history` 遞迴快照打壞 pytest collection。裝上釘選版本並排除快照後全部消失。

**遷移中造成又還原的兩個回歸**

1. 把 `explorations/moe_cycle_simulator` 改名為 `simulator/` — 119 個非證據檔引用該路徑，含 governance JSON（其雜湊是竄改偵測機制），另有 2 個 evidence 檔記錄該路徑。已還原（決策 P-002）。
2. 把 `src/edgeflow` 攤平為 `edgeflow` — repo root 解析多跳一層變成 `/home/a`。已還原（決策 P-003）。

**發現的必要條件** `PYTHONDONTWRITEBYTECODE=1` 不可省略：`phase7_d0_r4` 的治理測試斷言 application package 的精確檔案集合，自動產生的 `__pycache__` 會讓 19 項測試失敗。已寫入 Makefile。

**遺留缺口（已於 2026-08-18 補完）** `make doctor` 失敗；`docs/status/` 五份文件不存在。
