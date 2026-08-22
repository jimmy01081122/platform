# PCIe extension probe -- TRACK_GPU_PREP-style CPU prep note

Standalone note (per task instruction: `experiments/specs/gpu_measurement_contract_v1.yaml`
is being actively edited by a concurrent session and was not touched; the
argv/params documented in the contract's own `exact_argv` style live here and
in the new probe module's docstring instead, for a supervisor to fold into
the contract by hand).

## What this closes

Two existing GPU probes on the canonical RTX PRO 6000 Blackwell (96 GB) box
both sweep transfer *concurrency*, and both are same-direction only:

* **V2-GAP-A** (`multistream_aggregate_probe.py` / `aggregate_backend.py`): N
  independent objects, one direction at a time. Measured exact serialization
  (`sum_per_object_ms / max_per_object_ms == (N+1)/2`) at every N in {2,4,8}
  and every size tested.
* **The frozen harness's `copy_streams` S sweep** (`gpu_run_package_v2`): one
  object split across S streams, one direction at a time. Measured a
  small-transfer-only penalty: 3.65x at 65536 B, 2.10x at 1048576 B, ~1.00x by
  22020096/44040192/88080384 B.

Neither axis measured **bidirectional** concurrency (H2D racing D2H -- the
actual physical shape of MoE weight offload: fetching new expert weights over
H2D while evicting old KV/experts over D2H), neither densified the completely
unsampled decade between the frozen harness's 1048576 B and 22020096 B
points where the S-axis penalty is known to fall from 2.10x to ~1.00x, and
the frozen harness's smallest measured size is 65536 B even though the
contract calls for a 4096 B point that was never run. Separately, every
existing GPU-attempt environment record captures GPU name/uuid/memory/
driver/compute_cap via `nvidia-smi` but never PCIe link generation/width, so
no existing run record can be checked against the link the box actually
negotiated.

This CPU-only prep session adds one new probe covering all four gaps. No
GPU access was used or attempted; no evidence/ file was touched; nothing
outside the new files listed below was modified.

## Files added (all new; nothing existing was edited)

```
measurement/probes/pcie_extension_backend.py
measurement/probes/pcie_extension_probe.py
measurement/parsers/pcie_extension_parser.py
tests/test_gpu_prep_pcie_extension.py
tests/fixtures/gpu_prep/pcie_extension_pass.json
tests/fixtures/gpu_prep/pcie_extension_fail_nonphysical_bidirectional.json
tests/fixtures/gpu_prep/pcie_extension_fail_impossible_bandwidth.json
docs/status/PCIE_EXTENSION_PROBE_NOTE.md   (this file)
```

Architecture mirrors `multistream_aggregate_probe.py` / `aggregate_backend.py`
/ `multistream_aggregate_parser.py` exactly: `--backend` selects
`mock_pcie_extension` (CPU smoke test, stamps
`evidence = "cpu_smoke_test_not_measurement"`) or `gpu` (real CUDA, imports
torch lazily, raises `BackendError` and never substitutes mock data if CUDA
is unavailable); the parser hard-raises (never clamps/tolerates) on any
physically-impossible relationship; point/record IDs are deterministic
(sha256-based, same `deterministic_id`-style scheme used in
`measurement/gpu_run_package_v2/scripts/benchmark.py`).

## The four axes -> one probe, `--axis` selectable

`pcie_extension_probe.py` covers all four requested axes. Axes 2 and 3 share
one CLI axis (`stream_split`) because they are the identical measurement
primitive (one buffer chunked across `copy_streams` streams, chunking math
copied verbatim from `gpu_run_package_v2/scripts/benchmark.py` lines
~310-333) differing only in which byte sizes are swept; each cell carries a
`gap_label` recording which gap motivated that point
(`declared_never_measured_4096` / `crossover_densification` /
`custom_sweep_point`).

### Axis 1 -- bidirectional concurrent transfer

Two independent pinned-host + device buffer pairs; one H2D copy on its own
stream and one D2H copy on its own stream, both issued from a common CUDA
start event. Records `h2d_ms`, `d2h_ms`, `joint_completion_ms` (wait-all,
derived as `max(h2d_ms, d2h_ms)` from the two already-recorded per-leg
completion events -- NOT a separately recorded coordinator event; see
"Instrumentation discipline" below, corrected after supervisor review) plus
self-contained unidirectional baselines (`h2d_baseline_ms`, `d2h_baseline_ms`)
measured in the same run, and two ratios (`joint_over_max_ratio`,
`joint_over_sum_ratio`) reported without asserting which is "correct" --
true dual-engine overlap and a shared single engine are both physically
valid outcomes; this axis exists to find out which this GPU does.

```
python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis bidirectional --bidirectional-object-bytes 65536,2097152,352321536 --repeats 5 --out runs/<run_id>/pcie_extension_bidirectional.json   # TRACK_GPU only
```

### Axis 2 -- S-axis crossover densification

Same single-object-split-across-`copy_streams` primitive as the frozen
harness, at 5 new log-spaced byte sizes strictly between the frozen grid's
1048576 and 22020096: **2097152 / 3145728 / 5242880 / 8388608 / 13631488**
bytes (2, 3, 5, 8, 13 MiB -- consecutive ratios 2.0, 1.5, 1.67, 1.6, 1.625,
1.615, i.e. a Fibonacci-in-MiB progression, chosen because it lands on clean,
memorable byte counts while staying geometrically spaced), at
`copy_streams` in {1,2,4}, both directions.

```
python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis stream_split --stream-split-bytes 2097152,3145728,5242880,8388608,13631488 --copy-streams 1,2,4 --direction h2d,d2h --repeats 5 --out runs/<run_id>/pcie_extension_crossover.json   # TRACK_GPU only
```

### Axis 3 -- the declared-but-never-measured 4096-byte cell

Same primitive, same `copy_streams` in {1,2,4}, both directions, at exactly
the one point (4096 B) already named in the frozen contract's byte grid but
never actually run.

```
python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis stream_split --stream-split-bytes 4096 --copy-streams 1,2,4 --direction h2d,d2h --repeats 5 --out runs/<run_id>/pcie_extension_4096cell.json   # TRACK_GPU only
```

### Axis 4 -- PCIe link capability capture

`nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max --format=csv,noheader`,
issued through an injectable `command_runner` seam
(`pcie_extension_backend.capture_pcie_link_info`), captured **unconditionally
on every invocation of the probe regardless of `--axis`** and embedded under
`pcie_link_environment` in the output JSON, so every other axis's numbers in
the same file are interpretable against the link the box actually
negotiated. Never fabricates a reading: on nvidia-smi failure/absence it
returns `status: "unavailable"` with a reason, never a substituted number.
Standalone fast-capture invocation (no transfers):

```
python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis pcie_link --repeats 5 --out runs/<run_id>/pcie_extension_linkcap.json   # TRACK_GPU only
```

### All four axes in one dispatch

```
python3 measurement/probes/pcie_extension_probe.py --backend gpu --axis all --bidirectional-object-bytes 65536,2097152,352321536 --stream-split-bytes 4096,2097152,3145728,5242880,8388608,13631488 --copy-streams 1,2,4 --direction h2d,d2h --repeats 5 --out runs/<run_id>/pcie_extension.json   # TRACK_GPU only
```

### CPU smoke test (covers all four axes; this is what was actually run in this session)

```
python3 measurement/probes/pcie_extension_probe.py --backend mock_pcie_extension --axis all --bidirectional-object-bytes 65536,2097152,352321536 --stream-split-bytes 4096,2097152,3145728,5242880,8388608,13631488 --copy-streams 1,2,4 --direction h2d,d2h --repeats 5 --out runs/<run_id>/pcie_extension_smoke.json
```

## Instrumentation discipline (bidirectional + stream-split primitives)

**Corrected after supervisor review; read this section over the code's git
history, not instead of it.** The task that produced this module described
`aggregate_backend.py`'s current wait-all timing pattern as having been
corrected under a `DECISION_LOG` entry "P-022". That entry, and the fix it
documents, genuinely exist -- but only as an **uncommitted** edit on the live
main checkout at the time this worktree was created. A worktree is
initialized from a commit, so it could not see either the entry or the fix;
this repo's HEAD (`81baf2f`) still carries `aggregate_backend.py`'s
**pre-fix** revision, which records a distinct coordinator event after
waiting on every per-leg completion and times elapsed-to-that-event. The
first version of this module (correctly, given what it could see) read that
pre-fix code as its template and replicated the pattern it was written to
fix: `_measure_bidirectional_once` recorded a separate `joint_completion`
event after `coordinator.wait_event()`-ing both legs, then took
`common_start.elapsed_time(joint_completion)` as the joint value.

That pattern is wrong regardless of which commit it's copied from. Recording
a CUDA event is itself a queued stream operation with its own nonzero,
variable dispatch latency; waiting on every leg *before* recording a
separate joint event stops that event from firing too early, but does
nothing to stop its own recording overhead from being folded into the
elapsed-time delta. On real hardware this is exactly what produced P-022's
originally-observed failure (an N=1 V2-GAP-A aggregate exceeding its own
single serialized transfer -- impossible unless the "aggregate" measurement
itself was adding time that wasn't transfer time). P-022's fix, and this
module's corrected primitive, both derive the joint/wait-all value as
`max()` over the per-leg completion events that were already recorded for
the unidirectional measurements -- no additional event is ever recorded.
`_measure_bidirectional_once` and `measure_bidirectional_cell` in
`pcie_extension_backend.py` were edited in place to this discipline
post-review; `_measure_stream_split_once` (single object, single completion
event, no multi-leg wait-all) was never affected -- there is nothing to wait
for except itself.

`tests/test_gpu_prep_pcie_extension.py::test_gpu_backend_bidirectional_waits_on_both_legs_before_joint_event`
still passes unchanged after the fix, and that fact is itself worth flagging
rather than treating as reassurance: its fake-CUDA-clock harness assigns each
stream a fixed, deterministic `transfer_ms` and never models an event's own
recording overhead as a distinct quantity, so `max(h2d_ms, d2h_ms)` and
"elapsed-to-a-separately-recorded-event" are indistinguishable inside that
fake -- both simplify to "the slower leg's fixed transfer_ms" with no
overhead term to disagree about. The test correctly verifies ordering logic
(the joint value tracks the slower leg, not the faster one, not a value from
before both waits landed); it was never capable of catching this class of
bug, on either side of the fix, because the bug is a real-hardware timing
artifact with no counterpart in a deterministic fake clock. That was equally
true of whatever unit tests existed before P-022's real-hardware GPU attempt
caught the original instance. No local test run -- before or after this
fix -- can substitute for that; the only real check is a GPU attempt whose
strict parser is watching the same envelope this module's parser enforces.

## Two other things worth flagging

* **`legacy_component_point_recovery.py`** (cited in the task as an example
  of this repo's `deterministic_id` house style) also does not exist in this
  worktree's snapshot. Per the task's own fallback instruction, the
  deterministic-ID scheme actually used here
  (`pcie_extension_probe.deterministic_point_id`) instead copies the
  `deterministic_id(prefix, *parts)` pattern found in
  `measurement/gpu_run_package_v2/scripts/benchmark.py` (canonical-JSON the
  parts, sha256, truncate to 24 hex chars, prefix) -- a pattern that does
  exist and was read directly.
* **This dev machine is not actually GPU-blind.** It is WSL2, and
  `/usr/lib/wsl/lib/nvidia-smi` exposes the Windows host's local passthrough
  GPU (an NVIDIA GeForce RTX 3050, unrelated to the canonical remote RTX PRO
  6000 Blackwell measurement box). `capture_pcie_link_info()` therefore
  genuinely exercises its `status: "ok"` branch on this host (observed:
  gen.current=4, gen.max=4, width.current=8, width.max=16) rather than only
  the `"unavailable"` branch -- which is a stronger real-world exercise of
  the parsing path than a pure stub would give, and required no code change.
  `torch` is still not installed here, so `--backend gpu`'s CUDA transfer
  measurement path still correctly raises `BackendError` and was never
  exercised against real hardware, consistent with this being CPU-only prep
  work. A local `.venv` symlink to the main checkout's `.venv`
  (`/home/a/platform/.venv`, which already has pytest/PyYAML/jsonschema
  installed and no torch) was created inside this worktree purely so
  `.venv/bin/python`/`make test-py` resolve per this repo's convention; it is
  untracked, gitignored in spirit (though a bare symlink named `.venv` isn't
  matched by the `.venv/`-with-trailing-slash gitignore pattern, so `git
  status` does list it), and carries no repo content of its own.

## Verification

Baseline (`make test-py`, before any new files): `185 passed` (tests/) + `36`
+ `16` + `43` + `141 passed, 1 skipped` (the four `explorations/` suites) --
`421 passed, 1 skipped` total, 0 failed.

After adding the files above: `make test-py` ->

```
tests/                                        215 passed   (+30, all new)
explorations/moe_cycle_simulator/tests         36 passed   (unchanged)
explorations/moe_cycle_simulator/phase1/tests  16 passed   (unchanged)
explorations/moe_cycle_simulator/phase2/tests  43 passed   (unchanged)
explorations/moe_cycle_simulator/phase7/tests 141 passed, 1 skipped, 48 subtests (unchanged)
```

Zero new failures; zero regressions. The 30 new tests
(`tests/test_gpu_prep_pcie_extension.py`) cover: the mock CPU smoke test
end-to-end (`--axis all`, 3 bidirectional cells, 36 stream-split cells);
`--axis` selection (`bidirectional` / `stream_split` / `pcie_link`, the last
producing zero transfer cells by design but still capturing
`pcie_link_environment`); the physical envelope holding across a mock sweep;
the real backend refusing without CUDA; the fake-CUDA-clock ordering test
described above for both the bidirectional and stream-split primitives;
`gap_label_of` classification; `deterministic_point_id` determinism and
input-sensitivity; the `capture_pcie_link_info` argv-capture unit tests
(exact argv issued, missing-binary handling, nonzero-returncode handling,
malformed-row handling, and one live run of the real `subprocess.run`
default on this host); and parser acceptance of the pass fixture plus
rejection (hard raise) of: a non-physical bidirectional joint completion, a
stream-split latency faster than any plausible PCIe link, a joint completion
below the baseline "fastest theoretical bound" floor, a bad `chunk_bytes`
accounting, a tampered `pcie_link_environment.argv`, a missing `"ok"`-status
link field, an unknown `axis` label, a sample-count mismatch, and an unknown
`gap_label`.

Command used throughout: `.venv/bin/python` (via `make test-py`, which sets
`PYTHON ?= .venv/bin/python`), never bare `python3`.

## Forbidden-path check

```
git status --porcelain | grep -E "vllm_backend|vllm_runtime_adapter|inserving_dispatch_probe|long_context_kv_probe|serv_p0_25_arrival_driver|target4_phase2|component_shape_probe|dequant_weight_bytes_probe|sort_permute_probe|dispatch_parser|longctx_kv_parser|component_shape_parser|dequant_weight_bytes_parser|sort_permute_parser|target4_phase2_parser_common|model_identity_manifest|run_gpu_attempt|pull_gpu_attempt|gpu_measurement_contract_v1|evidence/|gpu_run_package_v2"
```

-> no output (clean). `git status --porcelain` shows only 8 new, untracked
files (the 7 listed above plus this note) and the untracked `.venv` symlink;
zero modifications (`M`) to any existing tracked file.

## What this session did NOT do (by design)

No SSH, no remote host, no GPU instance start/stop/touch, no `evidence/`
access, no edits to `experiments/specs/gpu_measurement_contract_v1.yaml` or
any other forbidden path, no reading of session guides outside
`docs/session_guides/TRACK_GPU_MEASUREMENT.md`, no merge/push. This is
CPU-only preparation; a supervising session should review this worktree's
diff and decide how/when to merge and fold the argv above into the frozen
contract.
