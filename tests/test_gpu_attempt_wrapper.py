"""Runtime-identity guards for the TRACK_GPU attempt wrapper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from measurement import run_gpu_attempt


def test_environment_snapshot_records_cuda_assembly_and_hidden_connector(
    monkeypatch, tmp_path
):
    cuda_home = tmp_path / "cuda"
    (cuda_home / "include").mkdir(parents=True)
    (cuda_home / "include" / "nvrtc.h").write_text("/* test */\n")
    monkeypatch.setenv("CUDA_HOME", str(cuda_home))
    monkeypatch.setenv("CPATH", "/overlay/include:/usr/include/python3.10")
    monkeypatch.delenv("VLLM_USE_SIMPLE_KV_OFFLOAD", raising=False)
    monkeypatch.setattr(run_gpu_attempt.shutil, "which", lambda name: "/tool/bin/nvcc")
    monkeypatch.setattr(
        run_gpu_attempt,
        "command_output",
        lambda argv: {"argv": argv, "returncode": 0, "stdout": "test", "stderr": ""},
    )

    identity = run_gpu_attempt.environment_snapshot()

    selected = identity["selected_environment"]
    assert selected["CUDA_HOME"] == str(cuda_home)
    assert selected["CPATH"] == "/overlay/include:/usr/include/python3.10"
    assert selected["VLLM_USE_SIMPLE_KV_OFFLOAD"] is None
    toolkit = identity["cuda_toolkit"]
    assert toolkit["CUDA_HOME"] == str(cuda_home)
    expected_nvcc = str(cuda_home / "bin" / "nvcc")
    assert toolkit["nvcc_path"] == expected_nvcc
    assert toolkit["nvcc_version"]["argv"] == [expected_nvcc, "--version"]
    assert toolkit["cuda_home_nvrtc_header_exists"] is True
    assert toolkit["nvcc_resolution_source"] == "CUDA_HOME/bin/nvcc"


def test_environment_snapshot_prefers_cudacxx(monkeypatch, tmp_path):
    nvcc = tmp_path / "cuda" / "bin" / "nvcc"
    monkeypatch.setenv("CUDA_HOME", str(tmp_path / "other-cuda"))
    monkeypatch.setenv("CUDACXX", str(nvcc))
    monkeypatch.setattr(
        run_gpu_attempt,
        "command_output",
        lambda argv: {"argv": argv, "returncode": 0, "stdout": "test", "stderr": ""},
    )
    identity = run_gpu_attempt.environment_snapshot()
    assert identity["cuda_toolkit"]["nvcc_path"] == str(nvcc)
    assert identity["cuda_toolkit"]["nvcc_resolution_source"] == "CUDACXX"


def test_declared_output_must_be_inside_attempt(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    assert run_gpu_attempt._declared_output(
        ["probe", "--out", str(attempt / "raw.json")], attempt
    ) == (attempt / "raw.json").resolve()
    with pytest.raises(run_gpu_attempt.AttemptContractError, match="inside"):
        run_gpu_attempt._declared_output(
            ["probe", "--out", str(tmp_path / "outside.json")], attempt
        )


def test_declared_model_path_is_absolute_and_unique(tmp_path):
    model = (tmp_path / "model").resolve()
    assert run_gpu_attempt._declared_model_path(
        ["probe", "--model-path", str(model)]
    ) == model
    assert run_gpu_attempt._declared_model_path(["probe"]) is None
    with pytest.raises(run_gpu_attempt.AttemptContractError, match="absolute"):
        run_gpu_attempt._declared_model_path(
            ["probe", "--model-path", "relative/model"]
        )


def test_probe_output_requires_identity_and_raw_timings(tmp_path):
    output = tmp_path / "result.json"
    output.write_text(
        json.dumps({"runtime_identity": {}, "cells": []}) + "\n",
        encoding="utf-8",
    )
    assert run_gpu_attempt._validate_probe_output(output)["cells"] == []
    output.write_text(json.dumps({"runtime_identity": {}}) + "\n", encoding="utf-8")
    with pytest.raises(run_gpu_attempt.AttemptContractError, match="raw timing"):
        run_gpu_attempt._validate_probe_output(output)


def test_preflight_refuses_foreign_compute_process():
    with pytest.raises(
        run_gpu_attempt.AttemptContractError, match="compute application"
    ) as caught:
        run_gpu_attempt._preflight_compute_inventory(
            {"nvidia_compute_apps": {"returncode": 0, "stdout": "14721, python, 1 MiB"}}
        )
    assert caught.value.classification == "FOREIGN_GPU_PROCESS_PRESENT"
