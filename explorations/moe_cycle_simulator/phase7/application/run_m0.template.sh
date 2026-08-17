#!/usr/bin/env bash
set -euo pipefail

if [[ "${MOE_PHASE7_EXECUTION_UNLOCK:-}" != "OWNER_APPROVED_EXACT_M0_COMMAND" ]]; then
  echo "HARD-STOP: this draft cannot execute without owner exact-command approval" >&2
  exit 64
fi

if [[ "$#" -ne 2 ]]; then
  echo "usage: bash run_m0.template.sh APPLICATION_DIR EXISTING_OUTPUT_ROOT" >&2
  exit 64
fi

application_dir="$(realpath "$1")"
output_root="$(realpath "$2")"
exec timeout --signal=TERM --kill-after=1200s 13200 \
  python3 "${application_dir}/executor/driver.py" \
    --application-dir "${application_dir}" \
    --output-root "${output_root}"
