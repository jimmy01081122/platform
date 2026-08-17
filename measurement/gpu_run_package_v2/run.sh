#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_ROOT="${GPU_PERSIST_ROOT:-$ROOT/results}"
BENCHMARK_PYTHON="${BENCHMARK_PYTHON:-python3}"
DEFAULT_CAPTURE_MATRIX="$ROOT/configs/capture_matrices/m0_rtx3050_vertical_v1.json"

if [[ "${1:-}" == "projectctl" ]]; then
  shift
  exec "$BENCHMARK_PYTHON" "$ROOT/projectctl" "$@"
fi

usage() {
  cat <<'EOF'
usage:
  C1 control plane:
    run.sh projectctl model preflight|smoke ...
    run.sh projectctl run start|status|resume|package|verify ...

  Legacy CUDA microbenchmark:
    run.sh --smoke [--experiment ID] [--gpu-profile ID] --execution-matrix PATH --execution-approval PATH
    run.sh --experiment ID [--gpu-profile ID] --execution-matrix PATH --execution-approval PATH

  Benchmark-driven suite and tiny executable MoE:
    run.sh --freeze-suite
    run.sh --capture-matrix MATRIX --output PLAN
    run.sh --benchmark-smoke [--output PATH] [--device auto|cuda|cpu]
    run.sh --ingest-session --source PATH --session-root PATH [--archive PATH]
    run.sh --canonicalize-m0 SOURCE OUTPUT
    run.sh --expand-workload INPUT OUTPUT
    run.sh --simulate-workload INPUT OUTPUT
    run.sh --score-validation-mape --calibration PATH --validation PATH --output PATH [--zero-policy reject|skip]

  Session/package utilities:
    run.sh --capture-plan --session-root PATH [--gpu-profile ID] [--trace-profile minimal|standard|maximal]
    run.sh --all-models --gpu-profile ID [--session-root PATH]
    run.sh --trace-audit --session-root PATH
    run.sh --package-results --session-root PATH
    run.sh --verify-package TARGET [--release-class pipeline_smoke|formal_candidate|formal_release]
    run.sh --dry-run

Notes:
  --smoke is the legacy CUDA microbenchmark/replay.
  --benchmark-smoke runs the pinned tiny Qwen2MoE M0 correctness pipeline.
  --capture-plan is a deprecated compatibility alias that expands the frozen
  benchmark matrix into <session-root>/CAPTURE_PLAN.json; it does not create a
  session and never claims capture completion.
  BENCHMARK_PYTHON selects the benchmark Python interpreter (default: python3).
  Online GPU preflight requires --provider-metadata plus
  --provider-metadata-sha256; form factor is never accepted as a CLI assertion.
  Paid GPU execution is hard-blocked by D-062 until a recorded superseding decision
  and a valid second-decision-layer approval both exist.
EOF
}

MODE=""
EXPERIMENT="rtx-pro-6000-calibration"
EXPERIMENT_SET=0
GPU_PROFILE=""
TRACE_PROFILE=""
SESSION_ROOT=""
VERIFY_TARGET=""
RELEASE_CLASS="pipeline_smoke"
RELEASE_CLASS_SET=0
MATRIX=""
OUTPUT=""
SOURCE=""
ARCHIVE=""
CALIBRATION=""
VALIDATION=""
ZERO_POLICY="reject"
ZERO_POLICY_SET=0
DEVICE="auto"
GPU_UUID="${GPU_UUID:-}"
PCI_BUS_ID="${GPU_PCI_BUS_ID:-}"
PROVIDER_METADATA="${GPU_PROVIDER_METADATA:-}"
PROVIDER_METADATA_SHA256="${GPU_PROVIDER_METADATA_SHA256:-}"
STORAGE_ESTIMATE="${GPU_STORAGE_ESTIMATE:-}"
PREFLIGHT_CAPTURE_MATRIX="${GPU_CAPTURE_MATRIX:-$DEFAULT_CAPTURE_MATRIX}"
EXECUTION_MATRIX=""
EXECUTION_APPROVAL=""
POSITIONAL=()

set_mode() {
  [[ -z "$MODE" ]] || {
    echo "FAIL: conflicting modes: $MODE and $1" >&2
    exit 2
  }
  MODE="$1"
}

need_value() {
  [[ $# -ge 2 && "$2" != --* ]] || {
    echo "FAIL: $1 needs a value" >&2
    exit 2
  }
}

while (($#)); do
  case "$1" in
    --smoke) set_mode smoke; shift ;;
    --benchmark-smoke) set_mode benchmark-smoke; shift ;;
    --freeze-suite) set_mode freeze-suite; shift ;;
    --capture-matrix)
      set_mode capture-matrix
      need_value "$@"
      MATRIX="$2"
      shift 2
      ;;
    --capture-plan) set_mode capture-plan; shift ;;
    --ingest-session) set_mode ingest-session; shift ;;
    --score-validation-mape) set_mode score-validation-mape; shift ;;
    --canonicalize-m0)
      set_mode canonicalize-m0
      [[ $# -ge 3 && "$2" != --* && "$3" != --* ]] || {
        echo "FAIL: --canonicalize-m0 needs SOURCE OUTPUT" >&2
        exit 2
      }
      POSITIONAL=("$2" "$3")
      shift 3
      ;;
    --expand-workload)
      set_mode expand-workload
      [[ $# -ge 3 && "$2" != --* && "$3" != --* ]] || {
        echo "FAIL: --expand-workload needs INPUT OUTPUT" >&2
        exit 2
      }
      POSITIONAL=("$2" "$3")
      shift 3
      ;;
    --simulate-workload)
      set_mode simulate-workload
      [[ $# -ge 3 && "$2" != --* && "$3" != --* ]] || {
        echo "FAIL: --simulate-workload needs INPUT OUTPUT" >&2
        exit 2
      }
      POSITIONAL=("$2" "$3")
      shift 3
      ;;
    --experiment)
      need_value "$@"
      [[ "$EXPERIMENT_SET" -eq 0 ]] || {
        echo "FAIL: duplicate --experiment" >&2
        exit 2
      }
      [[ -n "$MODE" ]] || MODE=experiment
      [[ "$MODE" == smoke || "$MODE" == experiment ]] || {
        echo "FAIL: --experiment conflicts with $MODE" >&2
        exit 2
      }
      EXPERIMENT="$2"
      EXPERIMENT_SET=1
      shift 2
      ;;
    --all-models) set_mode all-models; shift ;;
    --trace-audit) set_mode trace-audit; shift ;;
    --package-results) set_mode package; shift ;;
    --verify-package)
      set_mode verify
      need_value "$@"
      VERIFY_TARGET="$2"
      shift 2
      ;;
    --release-class)
      need_value "$@"
      [[ "$2" =~ ^(pipeline_smoke|formal_candidate|formal_release)$ ]] || {
        echo "FAIL: --release-class needs pipeline_smoke, formal_candidate, or formal_release" >&2
        exit 2
      }
      RELEASE_CLASS="$2"
      RELEASE_CLASS_SET=1
      shift 2
      ;;
    --dry-run) set_mode dry-run; shift ;;
    --gpu-profile)
      need_value "$@"
      [[ -z "$GPU_PROFILE" ]] || { echo "FAIL: duplicate --gpu-profile" >&2; exit 2; }
      GPU_PROFILE="$2"
      shift 2
      ;;
    --trace-profile)
      need_value "$@"
      [[ "$2" =~ ^(minimal|standard|maximal)$ ]] || {
        echo "FAIL: --trace-profile needs minimal, standard, or maximal" >&2
        exit 2
      }
      [[ -z "$TRACE_PROFILE" ]] || { echo "FAIL: duplicate --trace-profile" >&2; exit 2; }
      TRACE_PROFILE="$2"
      shift 2
      ;;
    --session-root)
      need_value "$@"
      [[ -z "$SESSION_ROOT" ]] || { echo "FAIL: duplicate --session-root" >&2; exit 2; }
      SESSION_ROOT="$2"
      shift 2
      ;;
    --output)
      need_value "$@"
      [[ -z "$OUTPUT" ]] || { echo "FAIL: duplicate --output" >&2; exit 2; }
      OUTPUT="$2"
      shift 2
      ;;
    --source)
      need_value "$@"
      [[ -z "$SOURCE" ]] || { echo "FAIL: duplicate --source" >&2; exit 2; }
      SOURCE="$2"
      shift 2
      ;;
    --archive)
      need_value "$@"
      [[ -z "$ARCHIVE" ]] || { echo "FAIL: duplicate --archive" >&2; exit 2; }
      ARCHIVE="$2"
      shift 2
      ;;
    --calibration) need_value "$@"; CALIBRATION="$2"; shift 2 ;;
    --validation) need_value "$@"; VALIDATION="$2"; shift 2 ;;
    --zero-policy)
      need_value "$@"
      [[ "$2" =~ ^(reject|skip)$ ]] || {
        echo "FAIL: --zero-policy needs reject or skip" >&2
        exit 2
      }
      ZERO_POLICY="$2"
      ZERO_POLICY_SET=1
      shift 2
      ;;
    --device)
      need_value "$@"
      [[ "$2" =~ ^(auto|cuda|cpu)$ ]] || {
        echo "FAIL: --device needs auto, cuda, or cpu" >&2
        exit 2
      }
      DEVICE="$2"
      shift 2
      ;;
    --gpu-uuid) need_value "$@"; GPU_UUID="$2"; shift 2 ;;
    --pci-bus-id) need_value "$@"; PCI_BUS_ID="$2"; shift 2 ;;
    --provider-metadata) need_value "$@"; PROVIDER_METADATA="$2"; shift 2 ;;
    --provider-metadata-sha256) need_value "$@"; PROVIDER_METADATA_SHA256="$2"; shift 2 ;;
    --storage-estimate) need_value "$@"; STORAGE_ESTIMATE="$2"; shift 2 ;;
    --execution-matrix) need_value "$@"; EXECUTION_MATRIX="$2"; shift 2 ;;
    --execution-approval) need_value "$@"; EXECUTION_APPROVAL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "FAIL: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$MODE" ]] || { usage >&2; exit 2; }
if [[ -n "$TRACE_PROFILE" && "$MODE" != capture-plan ]]; then
  echo "FAIL: --trace-profile is only valid with --capture-plan" >&2
  exit 2
fi
if [[ -n "$SESSION_ROOT" && ! "$MODE" =~ ^(capture-plan|all-models|trace-audit|package|ingest-session)$ ]]; then
  echo "FAIL: --session-root is not valid with $MODE" >&2
  exit 2
fi
if [[ -n "$GPU_PROFILE" && ! "$MODE" =~ ^(smoke|experiment|capture-plan|all-models)$ ]]; then
  echo "FAIL: --gpu-profile is not valid with $MODE" >&2
  exit 2
fi
if [[ -n "$OUTPUT" && ! "$MODE" =~ ^(capture-matrix|benchmark-smoke|score-validation-mape)$ ]]; then
  echo "FAIL: --output is not valid with $MODE" >&2
  exit 2
fi
if [[ -n "$CALIBRATION$VALIDATION" && "$MODE" != score-validation-mape ]]; then
  echo "FAIL: --calibration and --validation are only valid with --score-validation-mape" >&2
  exit 2
fi
if [[ "$ZERO_POLICY_SET" -eq 1 && "$MODE" != score-validation-mape ]]; then
  echo "FAIL: --zero-policy is only valid with --score-validation-mape" >&2
  exit 2
fi
if [[ "$DEVICE" != auto && "$MODE" != benchmark-smoke ]]; then
  echo "FAIL: --device is only valid with --benchmark-smoke" >&2
  exit 2
fi
if [[ "$RELEASE_CLASS_SET" -eq 1 && "$MODE" != verify ]]; then
  echo "FAIL: --release-class is only valid with --verify-package" >&2
  exit 2
fi

preflight_online() {
  local profile="$1"
  [[ -n "$GPU_UUID" && -n "$PCI_BUS_ID" && -n "$PROVIDER_METADATA" && -n "$PROVIDER_METADATA_SHA256" && -n "$STORAGE_ESTIMATE" ]] || {
    echo "FAIL: online preflight requires GPU identity, hashed provider metadata, and a complete storage estimate" >&2
    exit 2
  }
  "$ROOT/preflight.sh" --gpu-profile "$profile" \
    --persist-root "$RESULTS_ROOT" \
    --capability-output "$RESULTS_ROOT/${profile}-capability.json" \
    --gpu-uuid "$GPU_UUID" --pci-bus-id "$PCI_BUS_ID" \
    --provider-metadata "$PROVIDER_METADATA" \
    --provider-metadata-sha256 "$PROVIDER_METADATA_SHA256" \
    --storage-estimate "$STORAGE_ESTIMATE" \
    --capture-matrix "$PREFLIGHT_CAPTURE_MATRIX"
}

review_paid_execution() {
  local profile="$1"
  [[ -n "$EXECUTION_MATRIX" && -n "$EXECUTION_APPROVAL" ]] || {
    echo "FAIL: paid GPU execution requires --execution-matrix and --execution-approval" >&2
    exit 20
  }
  "$BENCHMARK_PYTHON" "$ROOT/scripts/review_gate.py" \
    --gpu-profile "$profile" --matrix "$EXECUTION_MATRIX" \
    --approval "$EXECUTION_APPROVAL"
}

case "$MODE" in
  smoke)
    echo "FAIL: S4-R6 permits no non-qualification GPU entrypoint" >&2
    exit 20
    ;;
  experiment)
    echo "FAIL: S4-R6 permits no non-qualification GPU entrypoint" >&2
    exit 20
    ;;
  freeze-suite)
    suite_revision="$("$BENCHMARK_PYTHON" - "$ROOT/configs/test_suites/moe_trace_suite_v1.yaml" <<'PY'
import sys, yaml
print(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["suite_revision"])
PY
)"
    frozen="$ROOT/configs/test_suites/frozen/$suite_revision"
    if [[ -d "$frozen" ]]; then
      temporary="$(mktemp -d)"
      trap 'rm -rf "$temporary"' EXIT
      "$BENCHMARK_PYTHON" "$ROOT/scripts/freeze_benchmark_suite.py" \
        --output-root "$temporary" --revision "$suite_revision" >/dev/null
      cmp "$temporary/$suite_revision/sample_manifest.jsonl" "$frozen/sample_manifest.jsonl"
      "$BENCHMARK_PYTHON" - \
        "$temporary/$suite_revision/inventory.json" "$frozen/inventory.json" <<'PY'
import json, sys
generated = json.load(open(sys.argv[1], encoding="utf-8"))
frozen = json.load(open(sys.argv[2], encoding="utf-8"))
if generated != frozen:
    raise SystemExit("FAIL: frozen suite inventory differs from reproducible generation")
print(json.dumps({
    "status": "already_frozen_verified",
    "suite_revision": frozen["suite_revision"],
    "sample_count": frozen["sample_count"],
    "frozen_manifest_sha256": frozen["frozen_manifest_sha256"],
}, sort_keys=True))
PY
    else
      "$BENCHMARK_PYTHON" "$ROOT/scripts/freeze_benchmark_suite.py"
    fi
    ;;
  capture-matrix)
    [[ -n "$OUTPUT" ]] || { echo "FAIL: --capture-matrix requires --output PLAN" >&2; exit 2; }
    [[ ! -e "$OUTPUT" ]] || { echo "FAIL: refusing to overwrite output: $OUTPUT" >&2; exit 2; }
    "$BENCHMARK_PYTHON" "$ROOT/scripts/capture_orchestrator.py" \
      --matrix "$MATRIX" --output "$OUTPUT"
    ;;
  benchmark-smoke)
    [[ -n "$OUTPUT" ]] || OUTPUT="$ROOT/artifacts/m0_benchmark_smoke_new"
    [[ ! -e "$OUTPUT" ]] || { echo "FAIL: refusing to overwrite output: $OUTPUT" >&2; exit 2; }
    if [[ "$DEVICE" != cpu ]]; then
      echo "FAIL: S4-R6 hard-disables benchmark-smoke GPU execution; use --device cpu" >&2
      exit 20
    else
      echo "benchmark-smoke: CPU correctness mode; no GPU measurement claimed" >&2
    fi
    benchmark_args=(--output "$OUTPUT" --device "$DEVICE")
    if [[ "$DEVICE" == cpu ]]; then
      benchmark_args+=(--run-mode local --local-cpu-fallback)
    fi
    "$BENCHMARK_PYTHON" "$ROOT/scripts/executable_moe_benchmark.py" \
      "${benchmark_args[@]}"
    ;;
  capture-plan)
    [[ -n "$SESSION_ROOT" ]] || { echo "FAIL: --capture-plan requires --session-root" >&2; exit 2; }
    mkdir -p "$SESSION_ROOT"
    OUTPUT="$SESSION_ROOT/CAPTURE_PLAN.json"
    [[ ! -e "$OUTPUT" ]] || { echo "FAIL: refusing to overwrite capture plan: $OUTPUT" >&2; exit 2; }
    echo "DEPRECATED: --capture-plan is a compatibility alias for the frozen benchmark capture matrix." >&2
    [[ -z "$GPU_PROFILE" ]] || echo "DEPRECATED: --gpu-profile is ignored; GPU identity is frozen in the matrix." >&2
    [[ -z "$TRACE_PROFILE" ]] || echo "DEPRECATED: --trace-profile is ignored; P0-P6 are frozen in the matrix." >&2
    "$BENCHMARK_PYTHON" "$ROOT/scripts/capture_orchestrator.py" \
      --matrix "$DEFAULT_CAPTURE_MATRIX" --output "$OUTPUT"
    echo "capture-plan: PLAN ONLY; no session created and no capture completion claimed"
    ;;
  ingest-session)
    [[ -n "$SOURCE" ]] || { echo "FAIL: --ingest-session requires --source PATH" >&2; exit 2; }
    [[ -n "$SESSION_ROOT" ]] || { echo "FAIL: --ingest-session requires --session-root PATH" >&2; exit 2; }
    args=(--source "$SOURCE" --destination "$SESSION_ROOT")
    [[ -z "$ARCHIVE" ]] || args+=(--archive "$ARCHIVE")
    "$BENCHMARK_PYTHON" "$ROOT/scripts/ingest_benchmark_session.py" "${args[@]}"
    ;;
  score-validation-mape)
    [[ -n "$CALIBRATION" ]] || { echo "FAIL: --score-validation-mape requires --calibration PATH" >&2; exit 2; }
    [[ -n "$VALIDATION" ]] || { echo "FAIL: --score-validation-mape requires --validation PATH" >&2; exit 2; }
    [[ -n "$OUTPUT" ]] || { echo "FAIL: --score-validation-mape requires --output PATH" >&2; exit 2; }
    "$BENCHMARK_PYTHON" "$ROOT/scripts/score_validation_mape.py" \
      --calibration "$CALIBRATION" --validation "$VALIDATION" \
      --output "$OUTPUT" --zero-policy "$ZERO_POLICY"
    ;;
  canonicalize-m0)
    source_root="${POSITIONAL[0]}"
    output_root="${POSITIONAL[1]}"
    [[ ! -e "$output_root" ]] || { echo "FAIL: refusing to overwrite output: $output_root" >&2; exit 2; }
    mkdir -p "$output_root"
    "$BENCHMARK_PYTHON" "$ROOT/scripts/canonicalize_trace.py" \
      --m0-root "$source_root" \
      --routing-output "$output_root/m0_moe_routing.json" \
      --benchmark-records-output "$output_root/benchmark_trace_records.jsonl"
    ;;
  expand-workload)
    [[ ! -e "${POSITIONAL[1]}" ]] || { echo "FAIL: refusing to overwrite output: ${POSITIONAL[1]}" >&2; exit 2; }
    "$BENCHMARK_PYTHON" "$ROOT/scripts/workload_expand.py" \
      --m0-routing "${POSITIONAL[0]}" --output "${POSITIONAL[1]}"
    ;;
  simulate-workload)
    [[ ! -e "${POSITIONAL[1]}" ]] || { echo "FAIL: refusing to overwrite output: ${POSITIONAL[1]}" >&2; exit 2; }
    "$BENCHMARK_PYTHON" "$ROOT/scripts/system_simulate.py" \
      --ir "${POSITIONAL[0]}" --output "${POSITIONAL[1]}"
    ;;
  all-models)
    [[ -n "$GPU_PROFILE" ]] || { echo "FAIL: --all-models requires --gpu-profile" >&2; exit 2; }
    PLAN_ARGS=(--root "$ROOT" --gpu-profile "$GPU_PROFILE")
    if [[ -n "$SESSION_ROOT" ]]; then
      mkdir -p "$SESSION_ROOT"
      PLAN_ARGS+=(--output "$SESSION_ROOT/COMPATIBILITY_PLAN.json")
    fi
    set +e
    "$BENCHMARK_PYTHON" "$ROOT/scripts/compatibility_plan.py" "${PLAN_ARGS[@]}"
    code=$?
    set -e
    [[ "$code" -eq 10 ]] || exit "$code"
    echo "all-models: DEGRADED PLAN ONLY; M0-M3 were not executed" >&2
    exit 10
    ;;
  trace-audit)
    [[ -n "$SESSION_ROOT" ]] || { echo "FAIL: --trace-audit requires --session-root" >&2; exit 2; }
    "$BENCHMARK_PYTHON" "$ROOT/scripts/trace_audit.py" --session-root "$SESSION_ROOT"
    ;;
  package)
    [[ -n "$SESSION_ROOT" ]] || { echo "FAIL: --package-results requires --session-root" >&2; exit 2; }
    "$BENCHMARK_PYTHON" "$ROOT/scripts/package_results.py" --session-root "$SESSION_ROOT"
    ;;
  verify)
    "$BENCHMARK_PYTHON" "$ROOT/scripts/trace_package_verify.py" "$VERIFY_TARGET" \
      --release-class "$RELEASE_CLASS"
    ;;
  dry-run)
    "$BENCHMARK_PYTHON" "$ROOT/scripts/validate_package.py" --root "$ROOT"
    ;;
esac
