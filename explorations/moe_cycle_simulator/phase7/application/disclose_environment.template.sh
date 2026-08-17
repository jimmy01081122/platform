#!/usr/bin/env bash
set -euo pipefail

if [[ "${MOE_PHASE7_D0_UNLOCK:-}" != "OWNER_DELEGATED_EXACT_D0_COMMAND" ]]; then
  echo "HARD-STOP: missing exact D0 disclosure unlock" >&2
  exit 64
fi

if [[ "$#" -ne 2 ]]; then
  echo "usage: bash disclose_environment.template.sh APPLICATION_DIR FRESH_LOCAL_EVIDENCE_ROOT" >&2
  exit 64
fi

application_dir="$(realpath "$1")"
evidence_root="$2"
exec timeout --signal=TERM --kill-after=60s 240 \
  python3 "${application_dir}/executor/disclosure_driver.py" \
    --application-dir "${application_dir}" \
    --evidence-root "${evidence_root}"
