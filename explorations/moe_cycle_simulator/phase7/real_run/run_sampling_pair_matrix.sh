#!/usr/bin/env bash
set -uo pipefail

RUNNER=/vault/flow/moe_simulator_phase7/real_campaign/code/gpu_campaign_runner_patch_v3.py
MODEL=/vault/flow/moe_simulator_phase7/models/mistralai__Mixtral-8x7B-Instruct-v0.1__eba92302__bf16_safetensors
RUN_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/runs
BATCH_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/batches
FIXTURE_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/fixtures/natural-v1-20260811T1530Z
BATCH_ID=${1:?usage: run_sampling_pair_matrix.sh BATCH_ID}
BATCH_DIR="$BATCH_ROOT/$BATCH_ID"
TOTAL_RUNS=9
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
plan = []
for workload, fixture, cap in (
    ("W0", "W0_arithmetic_reasoning.txt", 256),
    ("W1", "W1_long_context_retrieval.txt", 256),
    ("W2", "W2_code_generation.txt", 1024),
):
    pair_id = f"SMP1-{workload}-PAIR-V1"
    plan.extend(
        [
            [f"{pair_id}-CLEAN", workload, "CLEAN", fixture, cap, "clean", pair_id],
            [f"{pair_id}-ROUTING", workload, "ROUTING", fixture, cap, "instrumented", pair_id],
            [f"{pair_id}-TELEMETRY", workload, "TELEMETRY", fixture, cap, "instrumented", pair_id],
        ]
    )
payload = {
    "schema_version": "phase7-sampling-pair-batch-v1",
    "batch_id": sys.argv[2],
    "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "fixture_root": str(fixture_root),
    "fixture_manifest": str(fixture_root / "manifest.json"),
    "runner_path": sys.argv[4],
    "execution_plan": plan,
    "sampling_mode": "NATURAL_EOS_CAPPED",
    "sampling_contract": {
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 0,
        "seed": 0,
        "ignore_eos": False,
        "min_tokens": None,
        "max_tokens": "per-row cap",
    },
    "pair_gate": "same input IDs, SamplingParams, output IDs, output count, finish reason",
    "scope": "project-authored natural fixtures; not a formal public benchmark",
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

printf 'completed_at_utc\texperiment_id\tworkload_id\truntime_class\trun_dir\tstatus\tpair_id\tpair_role\n' > "$BATCH_DIR/progress.tsv"

run_experiment() {
  local experiment_id=$1
  local workload_id=$2
  local runtime_class=$3
  local fixture=$4
  local output_cap=$5
  local pair_role=$6
  local pair_id=$7
  local console="$BATCH_DIR/consoles/${experiment_id}.console.log"
  local logical_id="${experiment_id,,}"
  local command_rc
  local run_dir
  local terminal_status=FAIL

  write_status RUNNING "$experiment_id" "Executing frozen SMP1/SMP2 paired workload"
  command=(
    timeout --signal=TERM --kill-after=60s 2400 /usr/bin/python3 -u "$RUNNER"
    --model-path "$MODEL"
    --run-root "$RUN_ROOT"
    --experiment-id "$experiment_id"
    --runtime-class "$runtime_class"
    --logical-request-id "$logical_id"
    --prompt-file "$FIXTURE_ROOT/$fixture"
    --output-tokens "$output_cap"
    --sampling-mode NATURAL_EOS_CAPPED
    --sampling-pair-id "$pair_id"
    --sampling-pair-role "$pair_role"
    --warmup-count 0
    --measured-count 1
  )

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
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$completed_at" "$experiment_id" "$workload_id" "$runtime_class" "$run_dir" "$terminal_status" "$pair_id" "$pair_role" >> "$BATCH_DIR/progress.tsv"
  if [[ "$terminal_status" != PASS ]]; then
    write_status FAIL "$experiment_id" "runner_rc=$command_rc run_dir=$run_dir"
    exit 1
  fi
  COMPLETED_RUNS=$((COMPLETED_RUNS + 1))
}

run_experiment SMP1-W0-PAIR-V1-CLEAN W0 CLEAN W0_arithmetic_reasoning.txt 256 clean SMP1-W0-PAIR-V1
run_experiment SMP1-W0-PAIR-V1-ROUTING W0 ROUTING W0_arithmetic_reasoning.txt 256 instrumented SMP1-W0-PAIR-V1
run_experiment SMP1-W0-PAIR-V1-TELEMETRY W0 TELEMETRY W0_arithmetic_reasoning.txt 256 instrumented SMP1-W0-PAIR-V1
run_experiment SMP1-W1-PAIR-V1-CLEAN W1 CLEAN W1_long_context_retrieval.txt 256 clean SMP1-W1-PAIR-V1
run_experiment SMP1-W1-PAIR-V1-ROUTING W1 ROUTING W1_long_context_retrieval.txt 256 instrumented SMP1-W1-PAIR-V1
run_experiment SMP1-W1-PAIR-V1-TELEMETRY W1 TELEMETRY W1_long_context_retrieval.txt 256 instrumented SMP1-W1-PAIR-V1
run_experiment SMP1-W2-PAIR-V1-CLEAN W2 CLEAN W2_code_generation.txt 1024 clean SMP1-W2-PAIR-V1
run_experiment SMP1-W2-PAIR-V1-ROUTING W2 ROUTING W2_code_generation.txt 1024 instrumented SMP1-W2-PAIR-V1
run_experiment SMP1-W2-PAIR-V1-TELEMETRY W2 TELEMETRY W2_code_generation.txt 1024 instrumented SMP1-W2-PAIR-V1

write_status PASS "" "All frozen SMP1/SMP2 pairs passed runner gates; pair audit pending"
