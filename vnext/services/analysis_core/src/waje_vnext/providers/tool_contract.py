"""Strict provider-facing tool schemas derived from typed action payloads."""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import (
    Any,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from waje_vnext.domain.actions import ACTION_PAYLOAD_TYPES, ActionKind
from waje_vnext.domain.canonical import FrozenJson


TOOL_PREFIX = "submit_"


def action_tools(
    allowed_actions: tuple[ActionKind, ...],
    *,
    controller_bound_fields: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    builder = _ToolSchemaBuilder(
        controller_bound_fields=controller_bound_fields
    )
    tools: list[dict[str, object]] = []
    for kind in allowed_actions:
        payload_type = ACTION_PAYLOAD_TYPES[kind]
        builder.schema_for(payload_type)
        parameters = dict(builder.definitions[payload_type.__name__])
        parameters["$defs"] = builder.definitions
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": TOOL_PREFIX + kind.value,
                    "description": (
                        "Submit exactly one typed {} business action."
                    ).format(kind.value),
                    "strict": True,
                    "parameters": parameters,
                },
            }
        )
    return tools


def strict_record_tool(
    *,
    name: str,
    description: str,
    record_type: type[object],
) -> dict[str, object]:
    builder = _ToolSchemaBuilder()
    builder.schema_for(record_type)
    parameters = dict(builder.definitions[record_type.__name__])
    parameters["$defs"] = builder.definitions
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": parameters,
        },
    }


def action_kind_for_tool(name: str) -> ActionKind:
    if not name.startswith(TOOL_PREFIX):
        raise ValueError("unknown provider tool")
    try:
        return ActionKind(name[len(TOOL_PREFIX) :])
    except ValueError as error:
        raise ValueError("unknown provider tool") from error


class _ToolSchemaBuilder:
    def __init__(
        self,
        *,
        controller_bound_fields: frozenset[str] = frozenset(),
    ) -> None:
        self.definitions: dict[str, object] = {}
        self._building: set[type[object]] = set()
        self._controller_bound_fields = controller_bound_fields

    def schema_for(self, expected_type: object) -> dict[str, object]:
        if isinstance(expected_type, TypeAliasType):
            return self.schema_for(expected_type.__value__)
        origin = get_origin(expected_type)
        arguments = get_args(expected_type)
        if expected_type is FrozenJson or expected_type in (Any, object):
            return {
                "type": "string",
                "description": "Canonical JSON-encoded value.",
            }
        if origin in (types.UnionType, Union):
            return {
                "anyOf": [
                    self.schema_for(candidate)
                    for candidate in arguments
                ]
            }
        if origin is tuple:
            if len(arguments) == 2 and arguments[1] is Ellipsis:
                return {
                    "type": "array",
                    "items": self.schema_for(arguments[0]),
                }
            return {
                "type": "array",
                "prefixItems": [
                    self.schema_for(candidate)
                    for candidate in arguments
                ],
                "minItems": len(arguments),
                "maxItems": len(arguments),
            }
        if origin in (dict, Mapping):
            if arguments[1] is FrozenJson:
                return {
                    "type": "string",
                    "description": (
                        "Canonical JSON encoding of an object. The "
                        "controller validates its decoded contents."
                    ),
                }
            raise TypeError(
                "open provider mappings must use JSON-string transport"
            )
        if expected_type is type(None):
            return {"type": "null"}
        if expected_type is str:
            return {"type": "string"}
        if expected_type is bool:
            return {"type": "boolean"}
        if expected_type is int:
            return {"type": "integer"}
        if expected_type is float:
            return {"type": "number"}
        if expected_type is datetime:
            return {"type": "string", "format": "date-time"}
        if expected_type is date:
            return {"type": "string", "format": "date"}
        if (
            isinstance(expected_type, type)
            and issubclass(expected_type, Enum)
        ):
            return {
                "type": "string",
                "enum": [member.value for member in expected_type],
            }
        if (
            isinstance(expected_type, type)
            and is_dataclass(expected_type)
        ):
            return self._ref(expected_type)
        raise TypeError(
            "unsupported provider schema type: {!r}".format(expected_type)
        )

    def _ref(self, record_type: type[object]) -> dict[str, str]:
        name = record_type.__name__
        if name not in self.definitions:
            self._build_dataclass(record_type)
        return {"$ref": "#/$defs/{}".format(name)}

    def _build_dataclass(self, record_type: type[object]) -> None:
        if record_type in self._building:
            return
        self._building.add(record_type)
        name = record_type.__name__
        self.definitions[name] = {}
        type_hints = get_type_hints(record_type)
        provider_fields = tuple(
            field
            for field in fields(record_type)
            if field.name not in self._controller_bound_fields
        )
        properties = {
            field.name: self.schema_for(type_hints[field.name])
            for field in provider_fields
        }
        self.definitions[name] = {
            "type": "object",
            "additionalProperties": False,
            "required": [field.name for field in provider_fields],
            "properties": properties,
        }
        self._building.remove(record_type)
