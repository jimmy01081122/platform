#!/usr/bin/env bash
set -euo pipefail

label="${1:?capacity label required}"
case "$label" in
  025|050|075|080|085|090|095|099|0375|0625|0825|0875|0925|097|100) ;;
  *) echo "invalid OFF-E-PR3 capacity label: $label" >&2; exit 64 ;;
esac

campaign=/vault/flow/moe_simulator_phase7/real_campaign
attempt="$campaign/attempts/OFF-E-PR3-CAP-${label}-V1-MASTER"
code="$campaign/code"
routing="$campaign/attempts/OFF-E-PR0-V1-MASTER/points/REPLAY/runner_runs/20260814T024504Z__OFF-E-PR0-REPLAY-V1-MASTER/routing/off-e-pr0-REPLAY__measured-01.npy"
model=/vault/flow/moe_simulator_phase7/models/mistralai__Mixtral-8x7B-Instruct-v0.1__eba92302__bf16_safetensors

test ! -e "$attempt"
mkdir -p "$attempt/runner_runs" "$attempt/off_e_pr3_trace"
if nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits | grep -Eq '[0-9]'; then
  echo COMPUTE_CONFLICT
  exit 41
fi
if ps -eo pid=,args= | grep -E '[v]llm[[:space:]].*serve|[a]pi_server|[s]erving_burst_runner'; then
  echo SERVING_CONFLICT
  exit 42
fi
nvidia-smi --query-gpu=uuid,name,memory.used,memory.free,utilization.gpu --format=csv,noheader,nounits > "$attempt/dispatch_preflight.txt"
printf '%s\n' 'session_guard=PASS' 'serving_conflict=NONE' 'compute_conflict=NONE' 'filler_workload=FORBIDDEN' >> "$attempt/dispatch_preflight.txt"
cp "$code/off_e_pr3_capacity_contract_v1.json" "$attempt/off_e_pr3_capacity_contract_v1.json"

PYTHONPATH="$code/off_e_pr3_hook" \
OFF_E_PR3_HOOK_MODE=REPLAY \
OFF_E_PR3_CAPACITY_LABEL="$label" \
OFF_E_PR3_TRACE_DIR="$attempt/off_e_pr3_trace" \
OFF_E_PR3_ROUTING_NPY="$routing" \
python3 "$code/gpu_campaign_runner.py" \
  --model-path "$model" \
  --run-root "$attempt/runner_runs" \
  --experiment-id "OFF-E-PR3-CAP-${label}-V1-MASTER" \
  --runtime-class ROUTING \
  --input-tokens 128 \
  --output-tokens 32 \
  --sampling-mode FORCED_LENGTH_CONTROLLED \
  --logical-request-id "off-e-pr3-cap-${label}" \
  --warmup-count 0 \
  --measured-count 1 \
  --max-num-seqs 1 \
  --max-num-batched-tokens 256 \
  --gpu-memory-utilization 0.97 \
  --cpu-offload-gb 0.0 \
  --offload-backend auto \
  --expected-vllm-version 0.23.0 \
  --profiler-max-iterations 96 \
  > "$attempt/stdout.log" 2> "$attempt/stderr.log"

nvidia-smi --query-gpu=uuid,memory.used,memory.free,utilization.gpu --format=csv,noheader,nounits > "$attempt/terminal_gpu.txt"
find "$attempt" -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > "$attempt/SHA256SUMS"
python3 -c 'import json,pathlib,sys; d=json.loads((pathlib.Path(sys.argv[1])/"off_e_pr3_trace/capacity_replay.json").read_text()); print(json.dumps({k:d[k] for k in ("canonical_experiment_id","capacity_objects","demand_load_count","hit_count","immutable_discard_count","h2d_bytes","dependency_gate","total_h2d_cuda_elapsed_ms")},sort_keys=True))' "$attempt"
