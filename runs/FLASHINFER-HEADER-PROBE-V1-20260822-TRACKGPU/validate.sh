#!/usr/bin/env bash
set -u

validation_root=/workspace/track_gpu/header_fix_validation/20260822
probe_source="${validation_root}/header_probe.cu"
venv_include=/workspace/venvs/track_gpu_vllm_0_23_py310/lib/python3.10/site-packages/nvidia/cu13/include
overlay_include=/workspace/track_gpu/cuda_compat/nvrtc-13.0.88/include
nvcc_bin=/usr/local/cuda/bin/nvcc

run_case() {
  case_name=$1
  shift
  case_dir="${validation_root}/${case_name}"
  mkdir -p "${case_dir}"
  printf '%q ' "$@" >"${case_dir}/exact_argv.txt"
  printf '\n' >>"${case_dir}/exact_argv.txt"
  "$@" >"${case_dir}/stdout.log" 2>"${case_dir}/stderr.log"
  case_rc=$?
  printf '%s\n' "${case_rc}" >"${case_dir}/returncode.txt"
  return 0
}

"${nvcc_bin}" --version >"${validation_root}/nvcc_version.txt"
sha256sum "${probe_source}" >"${validation_root}/source_sha256.txt"
readlink -f "${overlay_include}/nvrtc.h" >"${validation_root}/overlay_target.txt"
sha256sum "${overlay_include}/nvrtc.h" >"${validation_root}/overlay_sha256.txt"

run_case no_nvrtc_header \
  env -u CPATH -u CPLUS_INCLUDE_PATH -u C_INCLUDE_PATH \
  "${nvcc_bin}" -std=c++17 -gencode=arch=compute_120f,code=sm_120f \
  -c "${probe_source}" -o "${validation_root}/no_nvrtc_header/probe.o"

run_case broad_venv_cpath \
  env CPATH="${venv_include}" \
  "${nvcc_bin}" -std=c++17 -gencode=arch=compute_120f,code=sm_120f \
  -c "${probe_source}" -o "${validation_root}/broad_venv_cpath/probe.o"

run_case narrow_nvrtc_overlay \
  env CPATH="${overlay_include}" \
  "${nvcc_bin}" -std=c++17 -gencode=arch=compute_120f,code=sm_120f \
  -c "${probe_source}" -o "${validation_root}/narrow_nvrtc_overlay/probe.o"

no_header_rc=$(cat "${validation_root}/no_nvrtc_header/returncode.txt")
broad_cpath_rc=$(cat "${validation_root}/broad_venv_cpath/returncode.txt")
overlay_rc=$(cat "${validation_root}/narrow_nvrtc_overlay/returncode.txt")
printf 'no_nvrtc_header\t%s\nbroad_venv_cpath\t%s\nnarrow_nvrtc_overlay\t%s\n' \
  "${no_header_rc}" "${broad_cpath_rc}" "${overlay_rc}" \
  >"${validation_root}/results.tsv"

if [[ "${no_header_rc}" -eq 0 || "${broad_cpath_rc}" -eq 0 || "${overlay_rc}" -ne 0 ]]; then
  exit 1
fi
