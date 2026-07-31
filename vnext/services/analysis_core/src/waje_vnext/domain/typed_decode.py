"""Strict recursive decoder for frozen typed domain records."""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints


class TypedDecodeError(ValueError):
    """Raised when JSON-like input cannot satisfy a typed domain contract."""


def decode_typed_dataclass[T](
    expected_type: type[T],
    value: Mapping[str, Any],
) -> T:
    if not is_dataclass(expected_type):
        raise TypeError("expected_type must be a dataclass type")
    decoded = _decode_value(expected_type, value, expected_type.__name__)
    if not isinstance(decoded, expected_type):
        raise TypedDecodeError("decoded value has an unexpected type")
    return decoded


def _decode_value(
    expected_type: object,
    value: object,
    path: str,
) -> object:
    origin = get_origin(expected_type)
    arguments = get_args(expected_type)

    if origin in (types.UnionType, Union):
        if value is None and type(None) in arguments:
            return None
        errors: list[Exception] = []
        for candidate in arguments:
            if candidate is type(None):
                continue
            try:
                return _decode_value(candidate, value, path)
            except (TypeError, ValueError) as error:
                errors.append(error)
        raise TypedDecodeError(
            f"{path} does not satisfy any allowed type"
        ) from (errors[-1] if errors else None)

    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise TypedDecodeError(f"{path} must be an array")
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(
                _decode_value(arguments[0], item, f"{path}[{index}]")
                for index, item in enumerate(value)
            )
        if len(value) != len(arguments):
            raise TypedDecodeError(f"{path} has the wrong tuple length")
        return tuple(
            _decode_value(item_type, item, f"{path}[{index}]")
            for index, (item_type, item) in enumerate(
                zip(arguments, value, strict=True)
            )
        )

    if origin in (dict, Mapping):
        if not isinstance(value, Mapping):
            raise TypedDecodeError(f"{path} must be an object")
        key_type, item_type = arguments
        return {
            _decode_value(key_type, key, f"{path}.<key>"): _decode_value(
                item_type,
                item,
                f"{path}.{key}",
            )
            for key, item in value.items()
        }

    if expected_type is Any:
        return value
    if expected_type is datetime:
        if not isinstance(value, str):
            raise TypedDecodeError(f"{path} must be an ISO datetime")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if expected_type is date:
        if not isinstance(value, str):
            raise TypedDecodeError(f"{path} must be an ISO date")
        return date.fromisoformat(value)
    if (
        isinstance(expected_type, type)
        and issubclass(expected_type, Enum)
    ):
        return expected_type(value)
    if isinstance(expected_type, type) and is_dataclass(expected_type):
        if not isinstance(value, Mapping):
            raise TypedDecodeError(f"{path} must be an object")
        type_hints = get_type_hints(expected_type)
        declared = {field.name: field for field in fields(expected_type)}
        unknown = set(value) - set(declared)
        missing = {
            name
            for name, field in declared.items()
            if name not in value
            and field.default is MISSING
            and field.default_factory is MISSING
        }
        if unknown or missing:
            raise TypedDecodeError(
                f"{path} has unknown={sorted(unknown)} "
                f"missing={sorted(missing)}"
            )
        kwargs = {
            name: _decode_value(
                type_hints[name],
                value[name],
                f"{path}.{name}",
            )
            for name in declared
            if name in value
        }
        return expected_type(**kwargs)
    if expected_type is str:
        if not isinstance(value, str):
            raise TypedDecodeError(f"{path} must be a string")
        return value
    if expected_type is bool:
        if not isinstance(value, bool):
            raise TypedDecodeError(f"{path} must be a boolean")
        return value
    if expected_type is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypedDecodeError(f"{path} must be an integer")
        return value
    if expected_type is float:
        raise TypedDecodeError(
            f"{path} cannot use a binary floating-point contract"
        )
    if value is None:
        raise TypedDecodeError(f"{path} cannot be null")
    if isinstance(expected_type, type) and isinstance(value, expected_type):
        return value
    raise TypedDecodeError(f"{path} uses an unsupported typed contract")
