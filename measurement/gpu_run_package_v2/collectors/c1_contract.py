"""Model-agnostic contracts shared by C1 collectors.

Collectors intentionally depend on this structural protocol and callbacks only.
No model implementation is imported here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

from .trace_contract import (
    EVENT_KEY_FIELDS, EXECUTION_ALIGNMENT_FIELDS, build_event_key,
    build_execution_alignment_key,
)


@runtime_checkable
class ModelRunnerLike(Protocol):
    """Minimum runner surface consumed by collectors."""

    def load_model(self, *, local_files_only: bool = True) -> Any: ...
    def tokenize(self, prompt: str) -> Any: ...
    def generate(self, tokens: Any, request: Any) -> Any: ...
    def collect_quality_result(self, result: Any, sample: Mapping[str, Any]) -> Any: ...
    def collect_runtime_metadata(self) -> Any: ...
    def cleanup(self) -> None: ...


RecordCallback = Callable[[Mapping[str, Any]], None]
ArtifactCallback = Callable[[str, bytes], Mapping[str, Any]]


@dataclass(frozen=True)
class CollectorRequest:
    execution: Mapping[str, Any]
    prompt: str
    generation_config: Mapping[str, Any]
    request_id: str
    sample: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class CollectorResult:
    pass_id: str
    status: str = "complete"
    records: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    def add(self, record: Mapping[str, Any], emit: RecordCallback | None = None) -> None:
        row = dict(record)
        self.records.append(row)
        if emit is not None:
            emit(row)


@runtime_checkable
class RoutingCaptureLike(Protocol):
    def enable_routing_capture(self) -> None: ...
    def disable_routing_capture(self) -> Any: ...


class ProfileBackend(Protocol):
    def available(self) -> tuple[bool, str | None]: ...
    def profile(
        self, operation: Callable[[], Mapping[str, Any]]
    ) -> tuple[Mapping[str, Any], Iterable[tuple[str, bytes]]]: ...


class TelemetryBackend(Protocol):
    def sample(self) -> Mapping[str, Any]: ...
