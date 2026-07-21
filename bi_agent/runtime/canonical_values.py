from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any, Mapping


def canonical_thaw(value: Any) -> Any:
    """Project frozen runtime values into recursively plain containers."""
    if is_dataclass(value):
        return {
            field.name: canonical_thaw(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: canonical_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(canonical_thaw(item) for item in value)
    if isinstance(value, list):
        return [canonical_thaw(item) for item in value]
    return value


def to_jsonable(value: Any) -> Any:
    """Project runtime values into JSON-compatible containers."""
    if is_dataclass(value):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
