# Switch Colab trace provenance

## Scope and access policy

`switch_colab_trace_readonly_v1` registers an external Switch-Transformer routing corpus without copying it into this repository. `SWITCH_COLAB_TRACE_ROOT` aliases the default root `/home/a/prototype/trace_data`; the default inventory is `/home/a/prototype/data/manifests/TRACE_INVENTORY.csv`. Both external trees are strictly read-only. The audit tool opens files only for metadata, schema, byte-count, and optional checksum verification and produces JSON on stdout.

The registry is intentionally `complete: false`. It is an integrity and semantic index, not a claim that collection provenance is complete.

## Inventory anchors

- 1,527 files, 11,159,735,788 bytes.
- 168 successful run metadata records.
- Models: `google/switch-base-{8,16,32}`.
- Benchmarks: HumanEval, MBPP, WikiText, arXiv summarization, CNN/DailyMail, and GSM8K.
- `TRACE_INVENTORY.csv` SHA-256: `9046f970ebf58459a11cf2aca1d1189e4dac9f08f3fcdd23b93448fe2d8f91eb`.
- Content-set SHA-256: `2378bd769156e8884008bd7f42a40b1572b9d2e6312c3f8886a7e03e37ee2b6e`.

The content-set digest is computed after sorting inventory rows by `relative_path`. Each row contributes UTF-8 `relative_path`, NUL, decimal `bytes`, NUL, lowercase file SHA-256, and LF. This excludes inventory formatting and mtime while binding path, size, and content identity.

The pre-existing `/home/a/prototype/data/manifests/trace_audit_summary.json` reports zero schema/hash errors and no raw modification for all 1,527 files. Its companion checksum list hashes to `a8d79300e2f1b329b8f734cc6a657d528af2adc6b96f8dc7f21fb7aee1cdd22a`. This change reran the quick audit, not the 11 GB full rehash.

## Trace semantics

`router_token_trace.csv` captures per-token router values. `top1_expert` is the router argmax and `top1_prob` its normalized probability. `top2_expert` and `top2_prob` are runner-up diagnostics; they do not mean that a second expert was selected or executed under Switch top-1 dispatch.

Decoder rows are teacher-forced using `teacher_forced_shift_right_from_input_ids_for_tracing_only`. They are unsuitable as evidence for autoregressive generation, TPOT, KV-cache evolution, or output quality.

`hardware_event_trace.csv` is a derived logical expansion into `route`, `dispatch`, `expert_compute`, and `gather` labels. It has no timestamps, durations, profiler counters, bandwidth, power, or device timing. It is not measured hardware evidence.

The load traces provide per-sample and per-batch assigned-token, capacity, and overflow observations. `run_metadata.json` and `router_diagnostics.json` provide metadata and diagnostics rather than performance measurements.

## Candidate benchmark mapping

- T1: `math_reasoning/gsm8k`, routing-shape candidate only. No frozen-suite sample/hash join or exact-answer evidence exists.
- T3: `coding/humaneval`, routing-shape candidate only. No frozen-suite sample/hash join or code-correctness evidence exists.
- T6: `long_text/{arxiv_summarization,cnn_dailymail}`, long-prefill candidate only. The traces stop at input length 256 and do not cover the formal 512–8192 context buckets.

These mappings are candidates, not identity claims between the external samples and the frozen benchmark suite.

## Missing provenance

The registry enumerates all currently known absent fields, including collector repository/commit and script hash, exact command, image digest, host/GPU/software identity, checkpoint/tokenizer/dataset revisions, split and row IDs, input hashes, preprocessing revision, random seeds, collection protocol, Colab notebook revision, chain of custody, licenses, retry history, quality results, and physical profiler provenance. Until those gaps are closed, the corpus must remain incomplete and cannot establish end-to-end performance or quality claims.

## Reproduction

Quick mode validates the registry schema, inventory digest and content-set digest, row counts, total bytes, path safety, all external file sizes, run metadata, model/benchmark sets, and inventory-to-trace schema relations:

```bash
python3 scripts/audit_external_switch_traces.py --mode quick
```

Full mode additionally recomputes every external file SHA-256 and reads approximately 11 GB:

```bash
python3 scripts/audit_external_switch_traces.py --mode full
```

Alternate read-only mounts are supported without changing the registry:

```bash
python3 scripts/audit_external_switch_traces.py \
  --root /read-only/trace_data \
  --inventory /read-only/manifests/TRACE_INVENTORY.csv \
  --mode quick
```
