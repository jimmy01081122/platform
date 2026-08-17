#!/usr/bin/env bash
set -euo pipefail

if [[ "${MOE_PHASE7_MATERIALIZATION_UNLOCK:-}" != \
  "OWNER_APPROVED_EXACT_MATERIALIZATION_COMMAND" ]]; then
  echo "HARD-STOP: missing exact materialization approval" >&2
  exit 64
fi

if [[ "$#" -ne 2 ]]; then
  echo "usage: bash materialize_m0.template.sh APPLICATION_DIR FRESH_EVIDENCE_ROOT" >&2
  exit 64
fi

application_dir="$(realpath "$1")"
evidence_root="$2"
exec timeout --signal=TERM --kill-after=600s 4800 \
  python3 "${application_dir}/executor/materialization_driver.py" \
    --application-dir "${application_dir}" \
    --evidence-root "${evidence_root}"
