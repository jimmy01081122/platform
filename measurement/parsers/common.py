"""Shared strict-validation helpers for GPU-prep parsers.

Every helper raises on violation. None of them warn-and-continue. This is the
deliberate opposite of a silent skip: a shape the parser did not expect is an
error, not a log line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


class ParseError(ValueError):
    """Input could not be read or is not the expected top-level shape."""


class ValidationError(ValueError):
    """Input parsed but violates a structural or semantic invariant."""


def load_json(path: str | Path) -> Any:
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as exc:
        raise ParseError(f"cannot read {p}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"{p} is not valid JSON: {exc}") from exc


def require_mapping(obj: Any, where: str) -> dict:
    if not isinstance(obj, dict):
        raise ValidationError(f"{where}: expected object, got {type(obj).__name__}")
    return obj


def require_list(obj: Any, where: str) -> list:
    if not isinstance(obj, list):
        raise ValidationError(f"{where}: expected list, got {type(obj).__name__}")
    return obj


def require_key(obj: dict, key: str, where: str) -> Any:
    if key not in obj:
        raise ValidationError(f"{where}: missing required key {key!r}")
    return obj[key]


def require_type(value: Any, types: type | tuple[type, ...], where: str) -> Any:
    if isinstance(value, bool) and types is not bool and bool not in _as_tuple(types):
        # bool is a subclass of int; reject it where a number is expected.
        raise ValidationError(f"{where}: expected {_names(types)}, got bool")
    if not isinstance(value, types):
        raise ValidationError(f"{where}: expected {_names(types)}, got {type(value).__name__}")
    return value


def require_positive_int(value: Any, where: str) -> int:
    require_type(value, int, where)
    if value <= 0:
        raise ValidationError(f"{where}: expected positive int, got {value}")
    return value


def require_nonneg_int(value: Any, where: str) -> int:
    require_type(value, int, where)
    if value < 0:
        raise ValidationError(f"{where}: expected non-negative int, got {value}")
    return value


def require_equal(value: Any, expected: Any, where: str) -> Any:
    if value != expected:
        raise ValidationError(f"{where}: expected {expected!r}, got {value!r}")
    return value


def require_in(value: Any, allowed: Iterable[Any], where: str) -> Any:
    allowed = list(allowed)
    if value not in allowed:
        raise ValidationError(f"{where}: {value!r} not in {allowed!r}")
    return value


def _as_tuple(types: type | tuple[type, ...]) -> tuple[type, ...]:
    return types if isinstance(types, tuple) else (types,)


def _names(types: type | tuple[type, ...]) -> str:
    return "/".join(t.__name__ for t in _as_tuple(types))
