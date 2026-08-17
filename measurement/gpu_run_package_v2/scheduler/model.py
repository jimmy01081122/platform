"""Deterministic scheduler domain models."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


LOGICAL_PASSES = ("P0", "P1", "P2", "P3", "P5_BASIC")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


@dataclass(frozen=True)
class WorkUnit:
    """The indivisible model × sample × repetition × logical-pass unit."""

    model_id: str
    sample_id: str
    repetition: int
    logical_pass: str
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.model_id or not self.sample_id:
            raise ValueError("model_id and sample_id are required")
        if isinstance(self.repetition, bool) or self.repetition < 0:
            raise ValueError("repetition must be a non-negative integer")
        if self.logical_pass not in LOGICAL_PASSES:
            raise ValueError(
                f"logical_pass must be one of {', '.join(LOGICAL_PASSES)}"
            )

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "sample_id": self.sample_id,
            "repetition": self.repetition,
            "logical_pass": self.logical_pass,
        }

    @property
    def work_unit_id(self) -> str:
        return hashlib.sha256(canonical_json(self.identity)).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["work_unit_id"] = self.work_unit_id
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkUnit":
        unit = cls(
            model_id=value["model_id"],
            sample_id=value["sample_id"],
            repetition=value["repetition"],
            logical_pass=value["logical_pass"],
            metadata=value.get("metadata") or {},
        )
        supplied = value.get("work_unit_id")
        if supplied is not None and supplied != unit.work_unit_id:
            raise ValueError("work_unit_id does not match canonical identity")
        return unit


def expand_work_units(
    models: list[str],
    samples: list[str],
    repetitions: int,
    logical_passes: list[str],
) -> list[WorkUnit]:
    if isinstance(repetitions, bool) or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    units = [
        WorkUnit(model, sample, repetition, logical_pass)
        for model in models
        for sample in samples
        for repetition in range(repetitions)
        for logical_pass in logical_passes
    ]
    return sorted(units, key=lambda unit: unit.work_unit_id)
