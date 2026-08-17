#!/usr/bin/env bash
set -uo pipefail

RUNNER=/vault/flow/moe_simulator_phase7/real_campaign/code/gpu_campaign_runner_patch_v3.py
MODEL=/vault/flow/moe_simulator_phase7/models/mistralai__Mixtral-8x7B-Instruct-v0.1__eba92302__bf16_safetensors
RUN_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/runs
BATCH_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/batches
FIXTURE_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/fixtures/gpu-kernel-v1
BATCH_ID=${1:?usage: run_k_profile_matrix.sh BATCH_ID}
BATCH_DIR="$BATCH_ROOT/$BATCH_ID"
TOTAL_RUNS=12
COMPLETED_RUNS=0

mkdir -p "$BATCH_DIR/consoles"
if [[ -e "$BATCH_DIR/status.json" ]]; then
  echo "Refusing to overwrite existing batch: $BATCH_DIR" >&2
  exit 2
fi
printf '%s\n' "$$" > "$BATCH_DIR/batch.pid"

write_status() {
  local status=$1
  local current=${2:-}
  local message=${3:-}
  /usr/bin/python3 - "$BATCH_DIR/status.json" "$status" "$COMPLETED_RUNS" "$TOTAL_RUNS" "$current" "$message" <<'PY'
import datetime
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = {
    "status": sys.argv[2],
    "completed_runs": int(sys.argv[3]),
    "total_runs": int(sys.argv[4]),
    "current_experiment": sys.argv[5] or None,
    "message": sys.argv[6] or None,
    "updated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
tmp = path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY
}

/usr/bin/python3 - "$BATCH_DIR/manifest.json" "$BATCH_ID" "$FIXTURE_ROOT" "$RUNNER" <<'PY'
import datetime
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
fixture_root = pathlib.Path(sys.argv[3])
rows = [
    ["K0-F0", "F0", "prompt", "F0_canary.txt", 32, 96, "fit", "formal"],
    ["K1-P0-128", "P0-128", "input", None, 32, 96, "fit", "formal"],
    ["K2-P0-8192", "P0-8192", "input", None, 32, 96, "fit", "formal"],
    ["K3-P1-28672", "P1-28672", "input", None, 32, 144, "held-out", "formal"],
    ["K4-DEC0-512", "DEC0-512", "input", None, 512, 96, "fit", "formal"],
    ["K5-DEC1-1024", "DEC1-1024", "input", None, 1024, 96, "held-out", "formal"],
]
plan = []
for row in rows:
    experiment, workload, kind, fixture, output_tokens, max_iterations, fit_role, role = row
    # One generated token can be emitted without a second worker execute_context
    # marker in this vLLM runtime.  Use two tokens so the bounded canary still
    # exercises a separable prefill/decode trace; the formal row lengths remain
    # frozen below.
    plan.append([f"{experiment}-CANARY", workload, kind, fixture, 2, max_iterations, fit_role, "bounded_profiler_canary"])
    plan.append([experiment, workload, kind, fixture, output_tokens, max_iterations, fit_role, role])
payload = {
    "schema_version": "phase7-kernel-profile-batch-v1",
    "batch_id": sys.argv[2],
    "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "fixture_root": str(fixture_root),
    "runner_path": sys.argv[4],
    "sampling_mode": "FORCED_LENGTH_CONTROLLED",
    "instrumentation": "KERNEL_PROFILE / vllm.EngineCore worker torch.profiler",
    "execution_plan": plan,
    "fit_held_out_lock": {row[0]: row[6] for row in rows},
    "canary_rule": "two bounded output tokens per target shape to require a separable prefill/decode marker; canary excluded from fit/held-out",
    "scope": "end-to-end GPU model profiler evidence; not formal dataset benchmark",
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

printf 'completed_at_utc\texperiment_id\tworkload_id\trun_dir\tstatus\tfit_role\trow_role\n' > "$BATCH_DIR/progress.tsv"

run_one() {
  local experiment_id=$1
  local workload_id=$2
  local input_kind=$3
  local fixture=$4
  local output_tokens=$5
  local max_iterations=$6
  local fit_role=$7
  local row_role=$8
  local console="$BATCH_DIR/consoles/${experiment_id}.console.log"
  local logical_id="${experiment_id,,}"
  local command_rc
  local run_dir
  local terminal_status=FAIL

  write_status RUNNING "$experiment_id" "Executing frozen K profiler row"
  command=(
    timeout --signal=TERM --kill-after=60s 3600 /usr/bin/python3 -u "$RUNNER"
    --model-path "$MODEL"
    --run-root "$RUN_ROOT"
    --experiment-id "$experiment_id"
    --runtime-class KERNEL_PROFILE
    --logical-request-id "$logical_id"
    --output-tokens "$output_tokens"
    --sampling-mode FORCED_LENGTH_CONTROLLED
    --profiler-max-iterations "$max_iterations"
    --warmup-count 0
    --measured-count 1
  )
  if [[ "$input_kind" == "prompt" ]]; then
    command+=(--prompt-file "$FIXTURE_ROOT/$fixture")
  else
    command+=(--input-tokens "${workload_id#*-}")
  fi

  if "${command[@]}" > "$console" 2>&1; then
    command_rc=0
  else
    command_rc=$?
  fi
  run_dir=$(find "$RUN_ROOT" -mindepth 1 -maxdepth 1 -type d -name "*__${experiment_id}" -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)
  if [[ $command_rc -eq 0 && -n "$run_dir" && -f "$run_dir/status.json" ]] && grep -q '"status": "PASS"' "$run_dir/status.json"; then
    terminal_status=PASS
  fi
  completed_at=$(/usr/bin/python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())')
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$completed_at" "$experiment_id" "$workload_id" "$run_dir" "$terminal_status" "$fit_role" "$row_role" >> "$BATCH_DIR/progress.tsv"
  if [[ "$terminal_status" != PASS ]]; then
    write_status FAIL "$experiment_id" "runner_rc=$command_rc run_dir=$run_dir"
    exit 1
  fi
  COMPLETED_RUNS=$((COMPLETED_RUNS + 1))
}

run_one K0-F0-CANARY F0 prompt F0_canary.txt 2 96 fit bounded_profiler_canary
run_one K0-F0 F0 prompt F0_canary.txt 32 96 fit formal
run_one K1-P0-128-CANARY P0-128 input "" 2 96 fit bounded_profiler_canary
run_one K1-P0-128 P0-128 input "" 32 96 fit formal
run_one K2-P0-8192-CANARY P0-8192 input "" 2 96 fit bounded_profiler_canary
run_one K2-P0-8192 P0-8192 input "" 32 96 fit formal
run_one K3-P1-28672-CANARY P1-28672 input "" 2 144 held-out bounded_profiler_canary
run_one K3-P1-28672 P1-28672 input "" 32 144 held-out formal
run_one K4-DEC0-512-CANARY DEC0-512 input "" 2 96 fit bounded_profiler_canary
run_one K4-DEC0-512 DEC0-512 input "" 512 96 fit formal
run_one K5-DEC1-1024-CANARY DEC1-1024 input "" 2 96 held-out bounded_profiler_canary
run_one K5-DEC1-1024 DEC1-1024 input "" 1024 96 held-out formal

write_status PASS "" "All K0-K5 canaries and formal profiler rows passed runner gates; audit pending"
