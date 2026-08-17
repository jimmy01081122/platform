#!/usr/bin/env bash
set -uo pipefail

RUNNER=/vault/flow/moe_simulator_phase7/real_campaign/code/gpu_campaign_runner.py
MODEL=/vault/flow/moe_simulator_phase7/models/mistralai__Mixtral-8x7B-Instruct-v0.1__eba92302__bf16_safetensors
RUN_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/runs
BATCH_ROOT=/vault/flow/moe_simulator_phase7/real_campaign/batches
BATCH_ID=${1:?usage: run_controlled_matrix.sh BATCH_ID}
BATCH_DIR="$BATCH_ROOT/$BATCH_ID"

POINTS=(
  "P0-128|P0|fit|128|32"
  "P0-2048|P0|fit|2048|32"
  "P0-8192|P0|fit|8192|32"
  "P0-16384|P0|fit|16384|32"
  "P1-512|P1|held-out|512|32"
  "P1-4096|P1|held-out|4096|32"
  "P1-12288|P1|held-out|12288|32"
  "P1-28672|P1|held-out|28672|32"
  "DEC0-512x32|DEC0|fit|512|32"
  "DEC0-512x128|DEC0|fit|512|128"
  "DEC0-512x512|DEC0|fit|512|512"
  "DEC1-512x64|DEC1|held-out|512|64"
  "DEC1-512x256|DEC1|held-out|512|256"
  "DEC1-512x1024|DEC1|held-out|512|1024"
  "PX0-512x32|PX0|interaction|512|32"
  "PX0-512x256|PX0|interaction|512|256"
  "PX0-512x1024|PX0|interaction|512|1024"
  "PX0-4096x32|PX0|interaction|4096|32"
  "PX0-4096x256|PX0|interaction|4096|256"
  "PX0-4096x1024|PX0|interaction|4096|1024"
  "PX0-16384x32|PX0|interaction|16384|32"
  "PX0-16384x256|PX0|interaction|16384|256"
  "PX0-16384x1024|PX0|interaction|16384|1024"
  "PX0-28672x32|PX0|interaction|28672|32"
  "PX0-28672x256|PX0|interaction|28672|256"
  "PX0-28672x1024|PX0|interaction|28672|1024"
  "CE0-28672x4096|CE0|capacity-edge|28672|4096"
  "CE1-30720x2048|CE1|capacity-edge|30720|2048"
  "CE2-31744x1024|CE2|capacity-edge|31744|1024"
  "CE3-32256x512|CE3|capacity-edge|32256|512"
)
MODES=(CLEAN ROUTING TELEMETRY)
TOTAL_RUNS=$((${#POINTS[@]} * ${#MODES[@]}))
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

/usr/bin/python3 - "$BATCH_DIR/manifest.json" "$BATCH_ID" "${POINTS[@]}" <<'PY'
import datetime
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
points = []
for row in sys.argv[3:]:
    point_id, set_id, fit_role, input_tokens, output_tokens = row.split("|")
    points.append({
        "point_id": point_id,
        "set_id": set_id,
        "fit_role": fit_role,
        "input_tokens": int(input_tokens),
        "forced_output_tokens": int(output_tokens),
    })
payload = {
    "schema_version": "phase7-controlled-batch-v1",
    "batch_id": sys.argv[2],
    "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "points": points,
    "execution_per_point": {
        "CLEAN": {"warmup_count": 1, "measured_count": 3},
        "ROUTING": {"warmup_count": 0, "measured_count": 1},
        "TELEMETRY": {"warmup_count": 0, "measured_count": 1},
    },
    "preregistered_decisions": [
        "held-out points remain excluded from surrogate fitting",
        "routing is collected for capacity-edge points because section 7 applies the routing run to each controlled point",
        "telemetry is collected for every controlled point to satisfy timing, memory, power, and clock evidence without replacing clean timing",
    ],
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

printf 'completed_at_utc\texperiment_id\truntime_class\trun_dir\tstatus\n' > "$BATCH_DIR/progress.tsv"
write_status RUNNING "initializing" "Controlled matrix started"

for point_row in "${POINTS[@]}"; do
  IFS='|' read -r point_id set_id fit_role input_tokens output_tokens <<< "$point_row"
  for runtime_class in "${MODES[@]}"; do
    runtime_slug=${runtime_class,,}
    experiment_id="CTRL-${point_id}-${runtime_slug}"
    logical_id="${point_id}-${runtime_slug}"
    console="$BATCH_DIR/consoles/${experiment_id}.console.log"
    write_status RUNNING "$experiment_id" "Executing controlled point"

    warmup_count=0
    measured_count=1
    if [[ "$runtime_class" == CLEAN ]]; then
      warmup_count=1
      measured_count=3
    fi

    if timeout --signal=TERM --kill-after=60s 1800s /usr/bin/python3 -u "$RUNNER" \
      --model-path "$MODEL" \
      --run-root "$RUN_ROOT" \
      --experiment-id "$experiment_id" \
      --runtime-class "$runtime_class" \
      --input-tokens "$input_tokens" \
      --output-tokens "$output_tokens" \
      --logical-request-id "$logical_id" \
      --warmup-count "$warmup_count" \
      --measured-count "$measured_count" > "$console" 2>&1; then
      command_rc=0
    else
      command_rc=$?
    fi

    run_dir=$(find "$RUN_ROOT" -mindepth 1 -maxdepth 1 -type d -name "*__${experiment_id}" -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)
    terminal_status=FAIL
    if [[ $command_rc -eq 0 && -n "$run_dir" && -f "$run_dir/status.json" ]] && grep -q '"status": "PASS"' "$run_dir/status.json"; then
      terminal_status=PASS
    fi
    completed_at=$(/usr/bin/python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())')
    printf '%s\t%s\t%s\t%s\t%s\n' "$completed_at" "$experiment_id" "$runtime_class" "$run_dir" "$terminal_status" >> "$BATCH_DIR/progress.tsv"

    if [[ "$terminal_status" != PASS ]]; then
      write_status FAIL "$experiment_id" "runner_rc=$command_rc run_dir=$run_dir"
      exit 1
    fi
    COMPLETED_RUNS=$((COMPLETED_RUNS + 1))
  done
done

write_status PASS "" "All controlled clean/routing/telemetry runs passed; local backup pending"
/usr/bin/python3 - "$BATCH_DIR" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
rows = []
for path in sorted(root.rglob("*")):
    if path.is_file() and path.name != "SHA256SUMS":
        rows.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root)}")
(root / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")
PY
