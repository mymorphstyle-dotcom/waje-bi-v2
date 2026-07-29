"""Canonical JSON and immutable JSON helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any


type JsonScalar = str | int | float | bool | None
type FrozenJson = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


def freeze_json(value: Any) -> FrozenJson:
    """Recursively copy JSON-compatible data into immutable containers."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen = {
            str(key): freeze_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
        if len(frozen) != len(value):
            raise ValueError("JSON object keys must remain unique when converted to strings")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError("value is not JSON-compatible: {!r}".format(type(value)))


def to_jsonable(value: Any) -> Any:
    """Convert domain objects and frozen JSON into canonical JSON-compatible data."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): to_jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError("cannot serialize value of type {!r}".format(type(value)))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("{} must be a lowercase SHA-256 hex digest".format(field_name))


def require_nonempty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError("{} must be non-empty".format(field_name))


def require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("{} must be timezone-aware".format(field_name))
