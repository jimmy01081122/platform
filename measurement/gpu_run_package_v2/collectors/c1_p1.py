"""P1 timeline collector with an injectable torch-profiler-like backend."""
from __future__ import annotations

from .c1_common import as_mapping, generate, load_runner
from .c1_contract import (
    ArtifactCallback, CollectorRequest, CollectorResult, ModelRunnerLike,
    ProfileBackend, RecordCallback, build_execution_alignment_key,
)


class ProfilerUnavailable(RuntimeError):
    """Profiler could not start or export in this runtime."""


class TorchProfilerBackend:
    """Lazy torch backend; importing this module never requires torch."""

    def available(self) -> tuple[bool, str | None]:
        try:
            import torch
        except (ImportError, OSError) as exc:
            return False, str(exc)
        if not hasattr(torch, "profiler"):
            return False, "installed torch does not expose torch.profiler"
        if not torch.cuda.is_available():
            return False, "CUDA is unavailable to torch"
        return True, None

    def profile(self, operation):
        import tempfile
        from pathlib import Path
        import torch

        try:
            with torch.profiler.profile(
                activities=[
                    torch.profiler.ProfilerActivity.CPU,
                    torch.profiler.ProfilerActivity.CUDA,
                ],
                record_shapes=True,
            ) as profiler:
                output = operation()
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "torch_trace.json"
                profiler.export_chrome_trace(str(path))
                payload = path.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise ProfilerUnavailable(
                f"torch profiler unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        if not payload:
            raise ProfilerUnavailable("torch profiler exported an empty trace")
        return output, [("torch_trace.json", payload)]


def collect(
    runner: ModelRunnerLike,
    request: CollectorRequest,
    backend: ProfileBackend | None = None,
    emit: RecordCallback | None = None,
    save_artifact: ArtifactCallback | None = None,
) -> CollectorResult:
    result = CollectorResult("P1")
    profiler = backend or TorchProfilerBackend()
    available, reason = profiler.available()
    if not available:
        result.status = "unavailable_due_to_environment"
        result.unavailable["torch_profiler"] = reason or "backend unavailable"
        result.add({
            "schema_version": "c1-pass-v1",
            "pass_id": "P1",
            "status": result.status,
            "execution_alignment_key": build_execution_alignment_key(request.execution),
            "unavailable": dict(result.unavailable),
            "artifacts": [],
        }, emit)
        return result

    load_runner(runner)
    tokens = runner.tokenize(request.prompt)
    try:
        output, artifacts = profiler.profile(
            lambda: generate(runner, tokens, request.generation_config)
        )
    except ProfilerUnavailable as exc:
        result.status = "unavailable_due_to_environment"
        result.unavailable["torch_profiler"] = str(exc)
        result.add({
            "schema_version": "c1-pass-v1",
            "pass_id": "P1",
            "status": result.status,
            "execution_alignment_key": build_execution_alignment_key(request.execution),
            "unavailable": dict(result.unavailable),
            "artifacts": [],
        }, emit)
        return result
    output = as_mapping(output)
    saved = []
    for name, payload in artifacts:
        if not payload:
            raise ValueError(f"P1 profiler artifact is empty: {name}")
        saved.append(dict(save_artifact(name, payload)) if save_artifact else {
            "name": name, "bytes": len(payload)
        })
    result.artifacts.extend(saved)
    result.add({
        "schema_version": "c1-pass-v1",
        "pass_id": "P1",
        "status": "complete",
        "execution_alignment_key": build_execution_alignment_key(request.execution),
        "artifacts": saved,
        "output_token_count": len(output.get("output_token_ids", [])),
    }, emit)
    return result
