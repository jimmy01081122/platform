#!/usr/bin/env bash
set -euo pipefail

if [[ "${MOE_PHASE7_EXECUTION_UNLOCK:-}" != "OWNER_APPROVED_EXACT_M0_COMMAND" ]]; then
  echo "HARD-STOP: missing exact M0 execution unlock" >&2
  exit 64
fi

if [[ "$#" -ne 2 ]]; then
  echo "usage: bash preflight_m0.template.sh APPLICATION_DIR OUTPUT_JSON" >&2
  exit 64
fi

application_dir="$(realpath "$1")"
output_json="$2"
python3 "${application_dir}/validate_application.py" \
  --mode execution-ready \
  --application-dir "${application_dir}"
exec python3 "${application_dir}/executor/preflight.py" \
  --mode execution \
  --application-dir "${application_dir}" \
  --output "${output_json}"
