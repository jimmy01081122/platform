#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_PYTHON="${BENCHMARK_PYTHON:-python3}"
PROFILE=""
OFFLINE=0
PERSIST_ROOT="${GPU_PERSIST_ROOT:-$ROOT/results}"
CAPABILITY_OUTPUT=""
GPU_UUID="${GPU_UUID:-}"
PCI_BUS_ID="${GPU_PCI_BUS_ID:-}"
PROVIDER_METADATA="${GPU_PROVIDER_METADATA:-}"
PROVIDER_METADATA_SHA256="${GPU_PROVIDER_METADATA_SHA256:-}"
STORAGE_ESTIMATE="${GPU_STORAGE_ESTIMATE:-}"
CAPTURE_MATRIX="${GPU_CAPTURE_MATRIX:-}"
usage() {
  echo "usage: $0 --gpu-profile ID [--offline] [--persist-root PATH] [--capability-output PATH] [--gpu-uuid UUID --pci-bus-id BUS --provider-metadata PATH --provider-metadata-sha256 SHA256 --storage-estimate PATH --capture-matrix PATH]"
}
while (($#)); do
  case "$1" in
    --gpu-profile) [[ $# -ge 2 ]] || { echo "FAIL: --gpu-profile needs ID" >&2; exit 2; }; PROFILE="$2"; shift 2 ;;
    --offline) OFFLINE=1; shift ;;
    --persist-root) [[ $# -ge 2 ]] || { echo "FAIL: --persist-root needs PATH" >&2; exit 2; }; PERSIST_ROOT="$2"; shift 2 ;;
    --capability-output) [[ $# -ge 2 ]] || { echo "FAIL: --capability-output needs PATH" >&2; exit 2; }; CAPABILITY_OUTPUT="$2"; shift 2 ;;
    --gpu-uuid) [[ $# -ge 2 ]] || { echo "FAIL: --gpu-uuid needs UUID" >&2; exit 2; }; GPU_UUID="$2"; shift 2 ;;
    --pci-bus-id) [[ $# -ge 2 ]] || { echo "FAIL: --pci-bus-id needs BUS" >&2; exit 2; }; PCI_BUS_ID="$2"; shift 2 ;;
    --provider-metadata) [[ $# -ge 2 ]] || { echo "FAIL: --provider-metadata needs PATH" >&2; exit 2; }; PROVIDER_METADATA="$2"; shift 2 ;;
    --provider-metadata-sha256) [[ $# -ge 2 ]] || { echo "FAIL: --provider-metadata-sha256 needs SHA256" >&2; exit 2; }; PROVIDER_METADATA_SHA256="$2"; shift 2 ;;
    --storage-estimate) [[ $# -ge 2 ]] || { echo "FAIL: --storage-estimate needs PATH" >&2; exit 2; }; STORAGE_ESTIMATE="$2"; shift 2 ;;
    --capture-matrix) [[ $# -ge 2 ]] || { echo "FAIL: --capture-matrix needs PATH" >&2; exit 2; }; CAPTURE_MATRIX="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$PROFILE" ]] || { echo "FAIL: --gpu-profile is required" >&2; exit 2; }
[[ -n "$CAPABILITY_OUTPUT" ]] || CAPABILITY_OUTPUT="$PERSIST_ROOT/TRACE_CAPABILITY_MATRIX.json"
command -v "$BENCHMARK_PYTHON" >/dev/null || {
  echo "FAIL: BENCHMARK_PYTHON unavailable: $BENCHMARK_PYTHON" >&2
  exit 2
}
"$BENCHMARK_PYTHON" - "$ROOT" "$PROFILE" "$OFFLINE" "$PERSIST_ROOT" "$CAPABILITY_OUTPUT" "$GPU_UUID" "$PCI_BUS_ID" "$PROVIDER_METADATA" "$PROVIDER_METADATA_SHA256" "$STORAGE_ESTIMATE" "$CAPTURE_MATRIX" <<'PY'
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

(
    root, profile_id, offline_s, persist_s, output_s, selected_uuid,
    selected_bus, provider_metadata_s, provider_metadata_sha256,
    storage_estimate_s, capture_matrix_s,
) = sys.argv[1:]
offline = offline_s == "1"
persist = Path(persist_s).expanduser().resolve()
output = Path(output_s).expanduser().resolve()
try:
    import yaml
except Exception as exc:
    raise SystemExit(f"FAIL: PyYAML unavailable: {exc}")
config = yaml.safe_load((Path(root) / "configs/gpu_profiles.yaml").read_text())
profiles = config.get("profiles", {})
if profile_id not in profiles:
    raise SystemExit(f"FAIL: unknown GPU profile {profile_id}")
profile = profiles[profile_id]
capabilities = {}
hard_failures = []
degraded = []
selected_form_factor = None
provider_metadata = None
provider_metadata_path = None
provider_metadata_digest = None
if profile.get("enabled") is not True:
    hard_failures.append(
        f"profile is disabled: {profile_id} ({profile.get('disabled_reason', 'no reason')})"
    )
if profile.get("execution_enabled") is not True:
    hard_failures.append(
        f"profile execution is not allowed: {profile_id} "
        f"({profile.get('disabled_reason', 'no reason')})"
    )

def command_capability(name):
    path = shutil.which(name)
    capabilities[name] = {
        "status": "available" if path else "unavailable",
        "path": path,
        "required_online": name in {"nvidia-smi"},
    }
    return path

nvidia_smi = command_capability("nvidia-smi")
nsys = command_capability("nsys")
ncu = command_capability("ncu")
for optional in ("nsys", "ncu"):
    if not capabilities[optional]["path"]:
        degraded.append(f"{optional} unavailable")

torch_info = {"status": "unavailable"}
try:
    import torch
    torch_info = {
        "status": "available",
        "version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "cuda_available": bool(torch.cuda.is_available()),
    }
except Exception as exc:
    torch_info["error"] = str(exc)
capabilities["torch"] = torch_info

storage_config = yaml.safe_load(
    (Path(root) / "configs/storage_budget.yaml").read_text(encoding="utf-8")
)
assignment = storage_config.get("profile_assignment", {}).get(profile_id)
storage = {
    "path": str(persist),
    "persistent_path_explicit": bool(os.environ.get("GPU_PERSIST_ROOT")),
    "storage_class": assignment.get("default_class") if assignment else None,
}
try:
    persist.mkdir(parents=True, exist_ok=True)
    probe = persist / ".preflight-write-probe"
    probe_bytes = int(storage_config["preflight_estimate"]["write_probe"]["bytes"])
    started = time.monotonic()
    with probe.open("wb") as handle:
        handle.write(b"\0" * probe_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    elapsed = max(time.monotonic() - started, 1e-9)
    measured_write_mib_s = probe_bytes / elapsed / 1048576
    probe.unlink()
    usage = shutil.disk_usage(persist)
    storage.update({
        "writable": True,
        "free_bytes": usage.free,
        "free_gib": usage.free / 1073741824,
        "total_bytes": usage.total,
        "write_probe_bytes": probe_bytes,
        "measured_write_mib_per_second": measured_write_mib_s,
        "write_probe_fsync": True,
    })
except Exception as exc:
    storage.update({"writable": False, "error": str(exc)})
    hard_failures.append("persistent storage is not writable")
if not storage["persistent_path_explicit"] and not offline:
    hard_failures.append("GPU_PERSIST_ROOT must explicitly name persistent storage")

if not offline:
    if not assignment:
        hard_failures.append(f"storage class assignment missing for {profile_id}")
    if not storage_estimate_s:
        hard_failures.append("nonzero per-model/per-pass storage estimate is required")
    if not capture_matrix_s:
        hard_failures.append("frozen capture matrix is required for storage coverage")
    else:
        estimate_path = Path(storage_estimate_s).expanduser().resolve()
        try:
            estimate = yaml.safe_load(estimate_path.read_text(encoding="utf-8"))
            rows = estimate.get("estimates", [])
            required = storage_config["preflight_estimate"]["required_per_model_and_pass"]
            if not isinstance(rows, list) or not rows:
                hard_failures.append("storage estimate requires non-empty estimates")
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    hard_failures.append(f"storage estimate row {index} must be an object")
                    continue
                for field in required:
                    value = row.get(field)
                    if field in {"state_id", "pass_id", "perturbation_risk"}:
                        if not isinstance(value, str) or not value.strip():
                            hard_failures.append(
                                f"storage estimate row {index} has empty {field}"
                            )
                    elif not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                        hard_failures.append(
                            f"storage estimate row {index} requires nonzero {field}"
                        )
            package_reserve = estimate.get("package_reserve_gib")
            if (
                not isinstance(package_reserve, (int, float))
                or isinstance(package_reserve, bool)
                or package_reserve <= 0
            ):
                hard_failures.append("storage estimate requires nonzero package_reserve_gib")
                package_reserve = 0
            package_reserve_minutes = estimate.get("package_reserve_minutes")
            if (
                not isinstance(package_reserve_minutes, (int, float))
                or isinstance(package_reserve_minutes, bool)
                or package_reserve_minutes <= 0
            ):
                hard_failures.append(
                    "storage estimate requires nonzero package_reserve_minutes"
                )
                package_reserve_minutes = 0
            matrix_path = Path(capture_matrix_s).expanduser().resolve()
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
            sys.path.insert(0, root)
            from scripts.capture_orchestrator import build_capture_plan
            plan = build_capture_plan(matrix, matrix_path, Path(root))
            expected_states = {
                (state["state_id"], state["pass_id"]) for state in plan["states"]
            }
            estimated_states = [
                (row.get("state_id"), row.get("pass_id"))
                for row in rows if isinstance(row, dict)
            ]
            estimated_state_set = set(estimated_states)
            if len(estimated_states) != len(estimated_state_set):
                hard_failures.append("storage estimate contains duplicate state/pass rows")
            missing_states = expected_states - estimated_state_set
            extra_states = estimated_state_set - expected_states
            if missing_states:
                hard_failures.append(
                    f"storage estimate lacks {len(missing_states)} capture state/pass rows"
                )
            if extra_states:
                hard_failures.append(
                    f"storage estimate has {len(extra_states)} unknown state/pass rows"
                )
            runtime_minutes = sum(
                float(row.get("expected_runtime_minutes", 0))
                for row in rows if isinstance(row, dict)
            )
            total_session_minutes = runtime_minutes + float(package_reserve_minutes)
            if total_session_minutes > 120:
                hard_failures.append(
                    "estimated runtime plus package reserve exceeds 120 minutes"
                )
            temporary_peak = sum(
                float(row.get("temporary_working_gib", 0))
                for row in rows if isinstance(row, dict)
            )
            peak_write = max(
                [float(row.get("peak_write_mib_per_second", 0)) for row in rows]
                or [0]
            )
            storage_class = storage_config["storage_classes"][assignment["default_class"]]
            hard_minimum = float(storage_class["hard_minimum_free_gib"])
            temporary_ratio = float(
                storage_config["preflight_estimate"]["capacity_gate"][
                    "temporary_headroom_ratio"
                ]
            )
            required_free = max(
                hard_minimum, temporary_peak * temporary_ratio + float(package_reserve)
            )
            write_headroom = float(
                storage_config["preflight_estimate"]["write_probe"][
                    "peak_headroom_ratio"
                ]
            )
            storage.update({
                "estimate_path": str(estimate_path),
                "estimate_row_count": len(rows),
                "temporary_peak_gib": temporary_peak,
                "package_reserve_gib": package_reserve,
                "package_reserve_minutes": package_reserve_minutes,
                "estimated_runtime_minutes": runtime_minutes,
                "estimated_total_session_minutes": total_session_minutes,
                "capture_matrix_path": str(matrix_path),
                "capture_matrix_sha256": hashlib.sha256(
                    matrix_path.read_bytes()
                ).hexdigest(),
                "capture_state_count": len(expected_states),
                "hard_minimum_free_gib": hard_minimum,
                "required_free_gib": required_free,
                "estimated_peak_write_mib_per_second": peak_write,
                "required_measured_write_mib_per_second": peak_write * write_headroom,
            })
            if storage.get("free_gib", 0) < required_free:
                hard_failures.append(
                    f"free storage {storage.get('free_gib', 0):.2f} GiB is below "
                    f"required {required_free:.2f} GiB"
                )
            if storage.get("measured_write_mib_per_second", 0) < peak_write * write_headroom:
                hard_failures.append(
                    "measured persistent write speed lacks required peak headroom"
                )
        except Exception as exc:
            hard_failures.append(f"storage estimate invalid: {exc}")

gpu = {
    "status": "not_checked_offline" if offline else "pending",
    "expected_exact_sku": profile.get("exact_sku"),
    "expected_vram_gib": profile.get("vram_gib"),
    "selected_uuid": selected_uuid or None,
    "selected_pci_bus_id": selected_bus or None,
    "selected_form_factor": selected_form_factor or None,
    "vram_tolerance_ratio": 0.05,
    "free_vram_minimum_ratio": 0.20,
}
counter_permission = {"status": "not_checked_offline" if offline else "unknown"}
if not offline:
    if not selected_uuid:
        hard_failures.append("exact selected GPU UUID is required")
    if not selected_bus:
        hard_failures.append("exact selected PCI bus ID is required")
    required_preflight = set(profile.get("required_preflight", []))
    if not provider_metadata_s:
        hard_failures.append("provider metadata artifact is required for form factor")
    elif not re.fullmatch(r"[0-9a-f]{64}", provider_metadata_sha256):
        hard_failures.append("provider metadata SHA-256 is required")
    else:
        try:
            provider_metadata_path = Path(provider_metadata_s).expanduser().resolve()
            provider_metadata_digest = hashlib.sha256(
                provider_metadata_path.read_bytes()
            ).hexdigest()
            if provider_metadata_digest != provider_metadata_sha256:
                hard_failures.append("provider metadata SHA-256 mismatch")
            provider_metadata = json.loads(
                provider_metadata_path.read_text(encoding="utf-8")
            )
            if provider_metadata.get("schema_version") != "provider-gpu-metadata-v1":
                hard_failures.append("provider metadata schema_version is invalid")
            if provider_metadata.get("gpu_uuid") != selected_uuid:
                hard_failures.append("provider metadata GPU UUID mismatch")
            if str(provider_metadata.get("pci_bus_id", "")).casefold() != selected_bus.casefold():
                hard_failures.append("provider metadata PCI bus ID mismatch")
            selected_form_factor = provider_metadata.get("form_factor")
            if not isinstance(selected_form_factor, str) or not selected_form_factor.strip():
                hard_failures.append("provider metadata form_factor is missing")
        except Exception as exc:
            hard_failures.append(f"provider metadata artifact is invalid: {exc}")
    if (
        profile.get("form_factor") is not None or "form_factor" in required_preflight
    ) and not selected_form_factor:
        hard_failures.append("selected GPU form factor is required")
    if not nvidia_smi:
        hard_failures.append("nvidia-smi missing")
    else:
        query = subprocess.run([
            nvidia_smi,
            "--query-gpu=uuid,name,memory.total,memory.free,pci.bus_id,compute_cap,pcie.link.gen.current,pcie.link.width.current",
            "--format=csv,noheader,nounits",
        ], text=True, capture_output=True)
        if query.returncode or not query.stdout.strip():
            hard_failures.append("nvidia-smi cannot query a GPU")
            gpu["status"] = "failed"
            gpu["error"] = query.stderr.strip()
        else:
            rows = [
                [part.strip() for part in line.split(",")]
                for line in query.stdout.splitlines() if line.strip()
            ]
            gpu["enumerated_gpu_count"] = len(rows)
            gpu["enumerated_gpu_uuids"] = [
                row[0] for row in rows if len(row) == 8
            ]
            selected_rows = [
                row for row in rows
                if len(row) == 8
                and row[0] == selected_uuid
                and row[4].casefold() == selected_bus.casefold()
            ]
            if len(selected_rows) != 1:
                hard_failures.append(
                    "selected UUID/PCI bus pair does not resolve to exactly one GPU"
                )
            else:
                (
                    detected_uuid, name, total_mib, free_mib, pci_bus_id,
                    compute_capability, pcie_generation, pcie_width,
                ) = selected_rows[0]
                total = float(total_mib) / 1024
                free = float(free_mib) / 1024
                gpu.update({
                    "status": "checked",
                    "uuid": detected_uuid,
                    "detected_name": name,
                    "detected_vram_gib": total,
                    "free_vram_gib": free,
                    "free_vram_ratio": free / total if total else 0,
                    "pci_bus_id": pci_bus_id,
                    "compute_capability": float(compute_capability),
                    "pcie_generation": int(pcie_generation),
                    "pcie_width": int(pcie_width),
                    "form_factor": selected_form_factor,
                })
                exact = profile.get("exact_sku")
                regex = profile.get("accepted_name_regex")
                if name != exact and not (regex and re.fullmatch(regex, name)):
                    hard_failures.append(f"exact SKU mismatch: detected {name!r}, expected {exact!r}")
                def normalize_sku(value):
                    return "".join(character for character in value.casefold() if character.isalnum())
                rejected = {
                    normalize_sku(value) for value in profile.get("reject_skus", [])
                }
                if normalize_sku(name) in rejected:
                    hard_failures.append(f"detected SKU is explicitly rejected: {name}")
                expected = float(profile["vram_gib"])
                if abs(total - expected) / expected > gpu["vram_tolerance_ratio"]:
                    hard_failures.append(
                        f"VRAM mismatch: detected {total:.2f} GiB, expected {expected:.2f} GiB"
                    )
                if gpu["free_vram_ratio"] < gpu["free_vram_minimum_ratio"]:
                    hard_failures.append(
                        f"free VRAM ratio {gpu['free_vram_ratio']:.3f} is below 0.20"
                    )
                expected_form = profile.get("form_factor")
                if (
                    expected_form is not None
                    and (
                        not isinstance(selected_form_factor, str)
                        or selected_form_factor.casefold()
                        != str(expected_form).casefold()
                    )
                ):
                    hard_failures.append(
                        f"form factor mismatch: selected {selected_form_factor!r}, "
                        f"expected {expected_form!r}"
                    )
                requirements = profile.get("hardware_requirements", {})
                if float(compute_capability) < float(
                    requirements.get("compute_capability_min", 0)
                ):
                    hard_failures.append("compute capability is below profile minimum")
                if int(pcie_generation) < int(
                    requirements.get("pcie_generation_min", 0)
                ):
                    hard_failures.append("PCIe generation is below profile minimum")
                if int(pcie_width) < int(requirements.get("pcie_width_min", 0)):
                    hard_failures.append("PCIe width is below profile minimum")

            topology = subprocess.run(
                [nvidia_smi, "topo", "-m"], text=True, capture_output=True
            )
            gpu["topology_dump"] = topology.stdout
            if topology.returncode or not topology.stdout.strip():
                hard_failures.append("measured GPU topology dump is required")

            if selected_uuid and (
                "mig_capability" in required_preflight
                or "mig_current_mode" in required_preflight
                or "mig_instance_profile" in required_preflight
            ):
                mig_query = subprocess.run(
                    [nvidia_smi, "-i", selected_uuid, "-q"],
                    text=True,
                    capture_output=True,
                )
                gpu["mig_query"] = mig_query.stdout
                current_match = re.search(
                    r"MIG Mode\s*\n\s*Current\s*:\s*(\S+)",
                    mig_query.stdout,
                    re.I,
                )
                pending_match = re.search(
                    r"MIG Mode\s*\n(?:.*\n)*?\s*Pending\s*:\s*(\S+)",
                    mig_query.stdout,
                    re.I,
                )
                gpu["mig_current_mode"] = (
                    current_match.group(1) if current_match else None
                )
                gpu["mig_pending_mode"] = (
                    pending_match.group(1) if pending_match else None
                )
                gpu["mig_instance_profile"] = None
                if mig_query.returncode or not current_match:
                    hard_failures.append("MIG capability/current mode could not be measured")
                if "mig_instance_profile" in required_preflight:
                    mig_list = subprocess.run(
                        [nvidia_smi, "mig", "-i", selected_uuid, "-lgi"],
                        text=True,
                        capture_output=True,
                    )
                    gpu["mig_instance_query"] = mig_list.stdout
                    if current_match and current_match.group(1).casefold() == "enabled":
                        if mig_list.returncode or not mig_list.stdout.strip():
                            hard_failures.append(
                                "MIG is enabled but instance profile could not be measured"
                            )
                        else:
                            gpu["mig_instance_profile"] = mig_list.stdout
    if torch_info.get("status") != "available" or not torch_info.get("cuda_available"):
        hard_failures.append("CUDA-enabled torch is unavailable")
    if ncu:
        probe = subprocess.run([ncu, "--query-metrics"], text=True, capture_output=True)
        counter_permission = {
            "status": "available" if probe.returncode == 0 else "permission_denied_or_unsupported",
            "returncode": probe.returncode,
            "detail": (probe.stderr or probe.stdout)[-1000:],
        }
        if probe.returncode:
            degraded.append("Nsight Compute counter access unavailable")

status = (
    "failed" if hard_failures
    else "offline_validated" if offline
    else "degraded" if degraded
    else "pass"
)
document = {
    "schema_version": "trace-capability-matrix-v2",
    "status": status,
    "offline": offline,
    "hardware_pass_claimed": False if offline else not hard_failures,
    "gpu_profile_id": profile_id,
    "gpu": gpu,
    "provider_metadata": {
        "path": str(provider_metadata_path) if provider_metadata_path else None,
        "sha256": provider_metadata_digest,
        "provider": (
            provider_metadata.get("provider")
            if isinstance(provider_metadata, dict) else None
        ),
        "form_factor_source": "provider_metadata_artifact",
    },
    "storage": storage,
    "capabilities": capabilities,
    "counter_permission": counter_permission,
    "hard_failures": hard_failures,
    "degraded_reasons": degraded,
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(document, indent=2, sort_keys=True))
if hard_failures:
    raise SystemExit(20)
if degraded and not offline:
    print("preflight: DEGRADED (optional profiler capability missing)", file=sys.stderr)
else:
    print("preflight: OFFLINE VALIDATED (no GPU pass claimed)" if offline else "preflight: PASS")
PY
