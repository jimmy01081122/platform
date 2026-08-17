# 第三方 routing 語料稽核：`core12345/MoE_expert_selection_trace`

```text
日期        : 2026-08-18
資料集      : core12345/MoE_expert_selection_trace
revision   : 27febb7b2d24169560a9f6f83d38eb53f94916cf（稽核時 main 仍為此值，無漂移）
稽核方式    : 全資料集 metadata 掃描（不下載內容）+ 本地已轉換資料逐項核對
結論        : 資料集可用，但僅限 routing 行為；專案先前的使用有 7 項缺陷，其中 6 項已修正
```

本文件是 `evidence/` 之外唯一大型資料來源的完整稽核。所有基於 `w3_*` 的 DSE 結論都建立在這份語料上，因此其效力邊界直接決定那些結論的效力邊界。

---

## 1. 資料集的能力與硬限制

四個生產級 MoE 模型的 expert 選擇 trace。每檔一個 query，外層 list 是 generation step（0 = prefill 全部 prompt token，≥1 = decode 每步一 token），step 內 `{layer_id: [per-token top-k expert ids]}`，dense layer 為 `null`。

**三項硬限制（資料集本身的性質，非專案缺陷）**

| 限制 | 後果 |
|---|---|
| 無 router scores / logits / probabilities | 任何需要 gate 分數的 predictor 不可能實作。只能做 ID 序列預測（persistence / frequency / markov） |
| 無任何時序資料 | 只有 expert ID。所有時間必須由外部 service model 提供，因此本語料的時序價值受 A1／A4 收斂狀況牽制 |
| decode 硬上限 128 tokens | 見缺陷 6 |

專案把 `router_scores` 記為 `null` 是**忠實記錄**，不是缺陷。轉換本身也乾淨：prefill/decode 正確區分、dense layer 正確跳過、`validation_errors` 全部為 0。

---

## 2. 七項缺陷

### 缺陷 1 · 取樣覆蓋 0.06%，三個 benchmark 完全未碰 → 已修正

稽核時實際下載 60 / 99,540 檔。完全未取樣：Chinese-SimpleQA（8,020 檔）、hellaswag（9,999 檔）、HuggingFaceH4/aime_2024（120 檔）。MMLU 的 57 個科目只用了 `abstract_algebra`。

**根因**：`scripts/hf_sample_download.py` 以 `sorted(subjects)[:spd]` 取字母序前 N 個科目。`abstract_algebra` 恰是 MMLU 字母序第一，因此每一輪都只會拿到它，長 prefill 的科目永遠不會被選中。

**修正**：新增可選的 `subjects` 設定欄位以明確指定科目（向後相容，round1/round2 設定不受影響）。已補抓 aime_2024 與 professional_law。

### 缺陷 2 · 專案自訂的收斂門檻，11 個 cell 只有 1 個達標 → 已修正

`convergence.json` 以 bootstrap（500 resamples、seed 12345、L1 tolerance 0.05）定出 **k\* = 14**，但只在 `Qwen/mmlu/abstract_algebra`（n=30）這**一個** cell 測過。其餘 10 個 cell 全部是 n=3，比專案自己的門檻低 **4.7 倍**。

這是最嚴重的一項：**專案建立了自己的充分性標準，然後在 91% 的 cell 上沒有滿足它。**

**修正**：全部 cell 補到 n≥14。現況 **21/21 達標**。

### 缺陷 3 · Kimi-K2 hold-out 宣稱過強 → 已修正

`docs/methodology/LARGE_MOE_CALIBRATION.md` §5 記「H5 hold-out workload validated (Kimi-K2 model hold-out done at W2/W3)」。實際只有 6 檔、2 個 cell、每 cell n=3，而資料集裡有 13,949 檔可用。

**修正**：Kimi 補到 48 檔、4 個 cell、每 cell n≥14。但 **H5 的宣稱仍需重新評估**——hold-out 的效力要等 `w3_*` 分析用新資料重跑後才成立。

### 缺陷 4 · Llama-4-Maverick 結構不同，不宜與其他三個並列 → 已標註，需下游處理

| 模型 | experts | top_k | MoE 層 / 總層 |
|---|---|---|---|
| Llama-4-Maverick | 128 | **1** | **24 / 48** |
| Qwen3-235B | 128 | 8 | 94 / 94 |
| DeepSeek-R1 | 256 | 8 | 58 / 61 |
| Kimi-K2 | 384 | 8 | 60 / 61 |

top_k=1 的 routing 本質上更難預測（其 prefetch retained 為負），且只有半數層是 MoE。放進同一張跨模型表比較時必須明確標註，不能並列後取平均。

### 缺陷 5 · n=3 時跨 benchmark 變異 ≥ 效應量本身 → 資料已補，待重跑

補抓前的 markov1 retained median：

| 模型 | benchmark 間差異 |
|---|---|
| DeepSeek | mmlu 0.3% vs livecodebench 5.3%（17×） |
| Llama | livecodebench −1.3% vs mmlu_ZH_CN +2.5%（**變號**） |
| Kimi | livecodebench 3.7% vs mmlu 11.7%（3×） |

效應量本身只有 0–15%，cell 間變異同量級，而每格 n=3。**這些數字不足以支撐目前引用的精度。**

### 缺陷 6 · 整個 routing 證據基礎限於約 721 tokens → 無法修正，資料集本身缺乏

以 metadata 掃描全部檔案（用已知 60 檔建立 `bytes = a × prefill + b` 換算，四個模型 R² 皆為 1.0000）得到全資料集 prefill 分布：

| path | n | p50 | p90 | max |
|---|---|---|---|---|
| Qwen mmlu/professional_law | 1,534 | 151 | 287 | **593** |
| Kimi mmlu/professional_law | 1,534 | 140 | 262 | 558 |
| DeepSeek mmlu/professional_law | 1,534 | 105 | 241 | 540 |
| Qwen HuggingFaceH4/aime_2024 | 30 | 93 | 176 | 394 |
| Llama hellaswag/test | 9,999 | 210 | 255 | 313 |
| Qwen livecodebench | 479 | 104 | 164 | 224 |
| Qwen Chinese-SimpleQA | 609 | 15 | 25 | 46 |

**全資料集最長 prefill = 593 tokens；decode 硬上限 128 → 單一 query 最長約 721 tokens。距離 1M context 差約 1,386 倍（三個數量級）。**

專案自產的 Mixtral GPU 量測是 159 tokens。因此**本專案全部的 routing 證據，第三方與自產加總，全部限於約 150–721 tokens**。

**這無法靠補抓解決。長上下文 routing 證據只能自己量測。**

部分緩解：`professional_law` 的 prefill 比原本取樣的 `abstract_algebra` 長 4–6 倍，提供資料集容許範圍內唯一的**序列長度敏感度軸**。已納入取樣。

### 缺陷 7 · `dataset_structure.json` 探查不完整，低估 4,421 檔 → 已修正

登記記 Kimi-K2 的 mmlu 有 45 個科目，實際有 **57** 個。缺少的 12 個在字母序上**連續**（`philosophy` … `world_religions`，字母表尾端），且 revision 相同——因此是**探查時的分頁截斷，不是資料集漂移**。

**實際影響**：`round3_long_prefill` 因為 registry 沒有這個科目而**靜默跳過** Kimi 的 `professional_law`；WARN 只進 stderr，程序仍以 exit 0 結束。若非逐 cell 核對，這個缺口不會被發現。

**修正**：以即時 API 重建該節點。`Kimi mmlu` 9,621 → 14,042 檔；`total_json_files` 99,540 → **103,961**。修正記錄寫入 `dataset_structure.json` 的 `corrections` 欄位。

---

## 3. 補抓後的語料狀態

```text
檔數      60 -> 354
位元組    147 MB -> 805 MB   （政策上限 10 GB，佔 7.9%）
cell 達標  1/11 -> 21/21     （門檻 k* = 14）
```

| 模型 | 檔數 | cells |
|---|---|---|
| Qwen3-235B-A22B-FP8 | 120 | livecodebench · mmlu/{abstract_algebra, professional_law} · mmlu_ZH_CN/{abstract_algebra, high_school_mathematics} · aime_2024 |
| Llama-4-Maverick | 93 | 同上 |
| DeepSeek-R1-AWQ | 79 | livecodebench · mmlu/{abstract_algebra, professional_law} · mmlu_ZH_CN/high_school_mathematics · aime_2024 |
| Kimi-K2-Thinking | 62 | livecodebench · mmlu/{abstract_algebra, professional_law} · aime_2024 |

新增的兩條分析軸：**序列長度**（professional_law vs abstract_algebra，prefill 長 4–6 倍）與**工作負載類型**（aime_2024 數學推理，先前完全未涵蓋）。

---

## 4. 這份語料的真正價值

移除缺陷之後，它提供三件自產量測拿不到的東西。

### 4.1 架構規模——最關鍵

| 模型 | experts | top_k | MoE 層 | expert 物件數 | identity 位元 | 查表/token |
|---|---|---|---|---|---|---|
| **Mixtral-8x7B（自產量測）** | 8 | 2 | 32 | **256** | 8 | 64 |
| Llama-4-Maverick | 128 | 1 | 24 | 3,072 | 12 | 24 |
| Qwen3-235B | 128 | 8 | 94 | 12,032 | 14 | 752 |
| DeepSeek-R1 | 256 | 8 | 58 | 14,848 | 14 | 464 |
| Kimi-K2 | 384 | 8 | 60 | **23,040** | 15 | 480 |

**Mixtral 位於這個座標系的最底端，跨度 90 倍。**

直接決定三個 HW0 row：`HW0-RESIDENCY-IDENTITY-WIDTH`（8 vs 15 bit）、`HW0-METADATA-CAPACITY`（256 vs 23,040 entry）、`HW0-METADATA-BANDWIDTH`（64 vs 752 查表/token）。

**若規格只從 Mixtral 推導，將為現代 MoE 中最小的一個定規格。** 既有 STA 顯示 residency engine 在 ME=256 已降到 50.56 MHz——那還只是 Mixtral 的 256 個物件。

### 4.2 causal 不可預測性是硬體無關的上界

「causal predictor 只保留 ≤15% 的 oracle 收益，top_k=1 為負」與硬體速度無關——限制在資訊，不在算力。這釘住了任何 prefetch 加速器的收益天花板，是可重用的 boundary condition。

**但此結論目前基於 n=3 樣本，必須用補抓後的資料重跑才能引用。**

### 4.3 working set 結構讓控制率可外推

實測 `ws_decode_mean` 精確等於 top_k——decode 階段每層每 token 恰好 top_k 個 expert，決定性。因此任意架構的控制負載可直接計算為 `MoE層 × top_k` 次查表/token，不需重新量測。

---

## 5. 對下游階段的影響

| 階段 | 影響 |
|---|---|
| **A1** | 無影響。A1 用的是 `evidence/measurement_backups/` 的 component 與 transfer 微基準，不碰 routing 語料 |
| **A2** | 必須把本文件的限制寫入 IR provenance：無 router scores、無時序、序列長度上限 721 tokens、各 cell 的 n |
| **C1** | **必須用補抓後的語料重跑全部 `w3_*` 分析**。目前 `data/canonical/moe_routing_v1/` 下的所有結果都是 60 檔、多數 cell n=3 的產物 |
| **C2** | 架構規模（§4.1）是 HW0 metadata 相關 row 的主要證據來源；不得只用 Mixtral 定規格 |
| **GPU 軌** | 缺陷 6 使長上下文量測從「資訊增益最高」升級為「**唯一能支撐長上下文論點的來源**」 |

---

## 6. 尚未處理

1. **`w3_*` 分析尚未用新語料重跑。** 目前 `data/canonical/moe_routing_v1/` 的 13 份 JSON 結果仍是舊樣本的產物。在重跑前，所有引用這些數字的地方都必須標註 `n=3 per cell, below own k*=14`。
2. **Chinese-SimpleQA 與 hellaswag 仍未取樣。** 前者 prefill 極短（p50 9–15），對長度軸無貢獻；後者只有 Llama 有。判斷為低優先，但仍是覆蓋缺口。
3. **缺陷 3 的 H5 宣稱尚未重新評估**，需待重跑後決定是否恢復。
