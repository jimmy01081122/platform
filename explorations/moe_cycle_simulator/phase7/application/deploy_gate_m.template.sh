#!/usr/bin/env bash
set -euo pipefail

if [[ "${MOE_PHASE7_DEPLOYMENT_UNLOCK:-}" != \
  "OWNER_APPROVED_EXACT_GATE_M_DEPLOYMENT_COMMAND" ]]; then
  echo "HARD-STOP: missing exact Gate M deployment approval" >&2
  exit 64
fi

if [[ "$#" -ne 3 ]]; then
  echo "usage: bash deploy_gate_m.template.sh APPLICATION_DIR EXTERNAL_APPROVAL FRESH_LOCAL_EVIDENCE_ROOT" >&2
  exit 64
fi

application_dir="$(realpath "$1")"
approval="$(realpath "$2")"
evidence_root="$3"
exec timeout --signal=TERM --kill-after=600s 4800 \
  python3 "${application_dir}/executor/deployment_controller.py" \
    --application-dir "${application_dir}" \
    --approval "${approval}" \
    --evidence-root "${evidence_root}"
