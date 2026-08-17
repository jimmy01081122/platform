#!/usr/bin/env bash
set -uo pipefail

RUNNER=/vault/flow/moe_simulator_phase7/real_campaign/code/gpu_campaign_runner.py
MODEL=/vault/flow/moe_simulator_phase7/models/mistralai__Mixtral-8x7B-Instruct-v0.1__eba92302__bf16_safetensors
RUN_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/runs
BATCH_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/batches
FIXTURE_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/fixtures/natural-v1-20260811T1530Z
BATCH_ID=${1:?usage: run_natural_matrix.sh BATCH_ID}
ADOPTED_W0_RUN=${2:?usage: run_natural_matrix.sh BATCH_ID ADOPTED_W0_RUN}
BATCH_DIR="$BATCH_ROOT/$BATCH_ID"
TOTAL_RUNS=15
COMPLETED_RUNS=1

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

if [[ ! -f "$ADOPTED_W0_RUN/status.json" ]] || ! grep -q '"status": "PASS"' "$ADOPTED_W0_RUN/status.json"; then
  echo "Adopted W0 CLEAN run is not PASS: $ADOPTED_W0_RUN" >&2
  exit 3
fi

/usr/bin/python3 - "$BATCH_DIR/manifest.json" "$BATCH_ID" "$FIXTURE_ROOT" "$RUNNER" "$ADOPTED_W0_RUN" <<'PY'
import datetime
import hashlib
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
fixture_root = pathlib.Path(sys.argv[3])
runner = pathlib.Path(sys.argv[4])
fixture_manifest = fixture_root / "manifest.json"
plan = [
    ["W0-CLEAN", "W0", "CLEAN", "prompt", "W0_arithmetic_reasoning.txt", 256, 1, 3, "adopted"],
    ["W0-ROUTING", "W0", "ROUTING", "prompt", "W0_arithmetic_reasoning.txt", 256, 0, 1, "execute"],
    ["W0-TELEMETRY", "W0", "TELEMETRY", "prompt", "W0_arithmetic_reasoning.txt", 256, 0, 1, "execute"],
    ["W1-CLEAN", "W1", "CLEAN", "prompt", "W1_long_context_retrieval.txt", 256, 1, 3, "execute"],
    ["W1-ROUTING", "W1", "ROUTING", "prompt", "W1_long_context_retrieval.txt", 256, 0, 1, "execute"],
    ["W1-TELEMETRY", "W1", "TELEMETRY", "prompt", "W1_long_context_retrieval.txt", 256, 0, 1, "execute"],
    ["W1-KERNEL-PROFILE", "W1", "KERNEL_PROFILE", "prompt", "W1_long_context_retrieval.txt", 256, 0, 1, "execute"],
    ["W1-MEMORY-PROFILE", "W1", "MEMORY_PROFILE", "prompt", "W1_long_context_retrieval.txt", 256, 0, 1, "execute"],
    ["W2-CLEAN", "W2", "CLEAN", "prompt", "W2_code_generation.txt", 1024, 1, 3, "execute"],
    ["W2-ROUTING", "W2", "ROUTING", "prompt", "W2_code_generation.txt", 1024, 0, 1, "execute"],
    ["W2-TELEMETRY", "W2", "TELEMETRY", "prompt", "W2_code_generation.txt", 1024, 0, 1, "execute"],
    ["W2-KERNEL-PROFILE", "W2", "KERNEL_PROFILE", "prompt", "W2_code_generation.txt", 1024, 0, 1, "execute"],
    ["W2-MEMORY-PROFILE", "W2", "MEMORY_PROFILE", "prompt", "W2_code_generation.txt", 1024, 0, 1, "execute"],
    ["W3-CLEAN", "W3", "CLEAN", "plan", "W3_request_plan.json", None, 0, 0, "execute"],
    ["W3-TELEMETRY", "W3", "TELEMETRY", "plan", "W3_request_plan.json", None, 0, 0, "execute"],
]
payload = {
    "schema_version": "phase7-natural-batch-v1",
    "batch_id": sys.argv[2],
    "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "fixture_root": str(fixture_root),
    "fixture_manifest_sha256": hashlib.sha256(fixture_manifest.read_bytes()).hexdigest(),
    "runner_path": str(runner),
    "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
    "adopted_w0_run": sys.argv[5],
    "execution_plan": plan,
    "sampling_contract": {"temperature": 0.0, "top_p": 1.0, "seed": 0, "ignore_eos": True},
    "scope": "project-authored natural workloads; not public benchmark correctness evaluation",
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

printf 'completed_at_utc\texperiment_id\tworkload_id\truntime_class\trun_dir\tstatus\tprovenance\n' > "$BATCH_DIR/progress.tsv"
adopted_finished=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["finished_at_utc"])' "$ADOPTED_W0_RUN/status.json")
printf '%s\tW0-CLEAN\tW0\tCLEAN\t%s\tPASS\tadopted_prebatch_gate\n' "$adopted_finished" "$ADOPTED_W0_RUN" >> "$BATCH_DIR/progress.tsv"
cp /vault/flow/moe_simulator_phase7/real_campaign/W0-CLEAN.console.log "$BATCH_DIR/consoles/W0-CLEAN.console.log"
write_status WAITING_FOR_MONITOR "" "Waiting for passive residency monitor readiness marker"

for attempt in $(seq 1 240); do
  [[ -f "$BATCH_DIR/monitor_ready" ]] && break
  sleep 0.5
done
if [[ ! -f "$BATCH_DIR/monitor_ready" ]]; then
  write_status FAIL "" "Passive monitor did not become ready within 120 seconds"
  exit 4
fi

run_experiment() {
  local experiment_id=$1
  local workload_id=$2
  local runtime_class=$3
  local input_kind=$4
  local source_path=$5
  local output_tokens=$6
  local warmup_count=$7
  local measured_count=$8
  local timeout_seconds=$9
  local console="$BATCH_DIR/consoles/${experiment_id}.console.log"
  local logical_id="${experiment_id,,}"
  local command_rc
  local run_dir
  local terminal_status=FAIL

  write_status RUNNING "$experiment_id" "Executing frozen natural workload"
  command=(
    timeout --signal=TERM --kill-after=60s "$timeout_seconds" /usr/bin/python3 -u "$RUNNER"
    --model-path "$MODEL"
    --run-root "$RUN_ROOT"
    --experiment-id "$experiment_id"
    --runtime-class "$runtime_class"
    --logical-request-id "$logical_id"
  )
  if [[ "$input_kind" == prompt ]]; then
    command+=(
      --prompt-file "$source_path"
      --output-tokens "$output_tokens"
      --warmup-count "$warmup_count"
      --measured-count "$measured_count"
    )
  else
    command+=(--request-plan "$source_path")
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
  printf '%s\t%s\t%s\t%s\t%s\t%s\texecuted\n' "$completed_at" "$experiment_id" "$workload_id" "$runtime_class" "$run_dir" "$terminal_status" >> "$BATCH_DIR/progress.tsv"
  if [[ "$terminal_status" != PASS ]]; then
    write_status FAIL "$experiment_id" "runner_rc=$command_rc run_dir=$run_dir"
    exit 1
  fi
  COMPLETED_RUNS=$((COMPLETED_RUNS + 1))
}

run_experiment W0-ROUTING W0 ROUTING prompt "$FIXTURE_ROOT/W0_arithmetic_reasoning.txt" 256 0 1 1800
run_experiment W0-TELEMETRY W0 TELEMETRY prompt "$FIXTURE_ROOT/W0_arithmetic_reasoning.txt" 256 0 1 1800
run_experiment W1-CLEAN W1 CLEAN prompt "$FIXTURE_ROOT/W1_long_context_retrieval.txt" 256 1 3 2400
run_experiment W1-ROUTING W1 ROUTING prompt "$FIXTURE_ROOT/W1_long_context_retrieval.txt" 256 0 1 1800
run_experiment W1-TELEMETRY W1 TELEMETRY prompt "$FIXTURE_ROOT/W1_long_context_retrieval.txt" 256 0 1 1800
run_experiment W1-KERNEL-PROFILE W1 KERNEL_PROFILE prompt "$FIXTURE_ROOT/W1_long_context_retrieval.txt" 256 0 1 2400
run_experiment W1-MEMORY-PROFILE W1 MEMORY_PROFILE prompt "$FIXTURE_ROOT/W1_long_context_retrieval.txt" 256 0 1 1800
run_experiment W2-CLEAN W2 CLEAN prompt "$FIXTURE_ROOT/W2_code_generation.txt" 1024 1 3 3600
run_experiment W2-ROUTING W2 ROUTING prompt "$FIXTURE_ROOT/W2_code_generation.txt" 1024 0 1 2400
run_experiment W2-TELEMETRY W2 TELEMETRY prompt "$FIXTURE_ROOT/W2_code_generation.txt" 1024 0 1 2400
run_experiment W2-KERNEL-PROFILE W2 KERNEL_PROFILE prompt "$FIXTURE_ROOT/W2_code_generation.txt" 1024 0 1 3600
run_experiment W2-MEMORY-PROFILE W2 MEMORY_PROFILE prompt "$FIXTURE_ROOT/W2_code_generation.txt" 1024 0 1 2400
run_experiment W3-CLEAN W3 CLEAN plan "$FIXTURE_ROOT/W3_request_plan.json" 0 0 0 7200
run_experiment W3-TELEMETRY W3 TELEMETRY plan "$FIXTURE_ROOT/W3_request_plan.json" 0 0 0 7200

write_status PASS "" "All frozen natural workload runs passed; local backup pending"
