from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentTool,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value


TOOL_SELECTION_SCHEMA_VERSION = "agent-tool-selection.v1"

TOOL_DISCOVERY_INSTRUCTIONS = """\
Select the smallest set of optional WAJE tools that may be needed for the current user turn.
Use only tool names from the supplied catalog. Select no optional tool when the request can be
answered directly from ordinary conversation context. Select artifact tools for persisted-result
explanation, BI analysis tools only when new business-data evidence or a material revision is
needed, and the capability catalog tool when the user asks what analysis is available. Do not
infer permissions or invent tools. Return only the typed selection output.
"""


class AgentToolDiscoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DynamicToolSelectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    selected_tools: list[str] = Field(alias="selectedTools", default_factory=list)

    @field_validator("selected_tools")
    @classmethod
    def validate_selected_tools(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("agent_tool_selection_name_invalid")
        if len(values) != len(set(values)):
            raise ValueError("agent_tool_selection_name_duplicate")
        return values


class AgentToolSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["agent-tool-selection.v1"] = Field(
        alias="schemaVersion",
        default=TOOL_SELECTION_SCHEMA_VERSION,
    )
    catalog_digest: str = Field(alias="catalogDigest", min_length=1)
    input_digest: str = Field(alias="inputDigest", min_length=1)
    selected_tools: list[str] = Field(alias="selectedTools")
    selection_digest: str = Field(alias="selectionDigest", min_length=1)

    @field_validator("selected_tools")
    @classmethod
    def validate_selected_tools(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("agent_tool_selection_name_invalid")
        if values != sorted(set(values)):
            raise ValueError("agent_tool_selection_names_not_canonical")
        return values

    @model_validator(mode="after")
    def validate_digest(self) -> "AgentToolSelection":
        if self.schema_version != TOOL_SELECTION_SCHEMA_VERSION:
            raise ValueError("agent_tool_selection_schema_invalid")
        expected = _selection_digest(
            catalog_digest=self.catalog_digest,
            input_digest=self.input_digest,
            selected_tools=self.selected_tools,
        )
        if self.selection_digest != expected:
            raise ValueError("agent_tool_selection_digest_invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        catalog_digest: str,
        input_digest: str,
        selected_tools: Sequence[str],
    ) -> "AgentToolSelection":
        normalized = sorted(set(selected_tools))
        return cls(
            catalogDigest=catalog_digest,
            inputDigest=input_digest,
            selectedTools=normalized,
            selectionDigest=_selection_digest(
                catalog_digest=catalog_digest,
                input_digest=input_digest,
                selected_tools=normalized,
            ),
        )

    def to_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ToolSelectionAdapter(Protocol):
    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult: ...


class ToolSelectionGenerator(Protocol):
    async def select(
        self,
        *,
        user_message: str,
        tool_catalog: Sequence[Mapping[str, Any]],
        permission_scope: Mapping[str, Any],
    ) -> DynamicToolSelectionOutput: ...


class WajeToolSelectionGenerator:
    def __init__(self, adapter: ToolSelectionAdapter) -> None:
        self._adapter = adapter

    async def select(
        self,
        *,
        user_message: str,
        tool_catalog: Sequence[Mapping[str, Any]],
        permission_scope: Mapping[str, Any],
    ) -> DynamicToolSelectionOutput:
        payload = {
            "userMessage": user_message,
            "optionalToolCatalog": [dict(item) for item in tool_catalog],
            "permissionScope": canonical_value(permission_scope),
        }
        input_digest = canonical_digest(payload)
        result = await self._adapter.run(
            WajeAgentRunRequest(
                run_id=f"tool-selection-run-{input_digest[:24]}",
                agent_name="WAJE Dynamic Tool Discovery",
                instructions=TOOL_DISCOVERY_INSTRUCTIONS,
                input_text=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                output_type=DynamicToolSelectionOutput,
                max_turns=1,
                trace_metadata={
                    "waje_tool_catalog_digest": canonical_digest(tool_catalog),
                    "waje_tool_selection_input_digest": input_digest,
                },
            )
        )
        return DynamicToolSelectionOutput.model_validate(result.final_output)


@dataclass(frozen=True)
class ResolvedAgentTools:
    tools: tuple[WajeAgentTool, ...]
    selection: AgentToolSelection


class DynamicAgentToolResolver:
    def __init__(
        self,
        *,
        generator: ToolSelectionGenerator,
        mandatory_tool_names: Sequence[str],
        max_optional_tools: int = 4,
    ) -> None:
        mandatory = tuple(dict.fromkeys(mandatory_tool_names))
        if (
            not mandatory
            or any(not name or name != name.strip() for name in mandatory)
            or isinstance(max_optional_tools, bool)
            or max_optional_tools < 1
        ):
            raise ValueError("agent_tool_resolver_config_invalid")
        self._generator = generator
        self._mandatory_tool_names = frozenset(mandatory)
        self._max_optional_tools = max_optional_tools

    async def resolve(
        self,
        *,
        user_message: str,
        candidate_tools: Sequence[WajeAgentTool],
        permission_scope: Mapping[str, Any],
    ) -> ResolvedAgentTools:
        catalog, by_name = _catalog(candidate_tools)
        missing_mandatory = self._mandatory_tool_names - set(by_name)
        if missing_mandatory:
            raise AgentToolDiscoveryError("agent_tool_mandatory_missing")
        optional_catalog = tuple(
            item for item in catalog if item["name"] not in self._mandatory_tool_names
        )
        output = await self._generator.select(
            user_message=user_message,
            tool_catalog=optional_catalog,
            permission_scope=permission_scope,
        )
        optional_names = set(output.selected_tools)
        known_optional = {str(item["name"]) for item in optional_catalog}
        if not optional_names.issubset(known_optional):
            raise AgentToolDiscoveryError("agent_tool_selection_unknown")
        if len(optional_names) > self._max_optional_tools:
            raise AgentToolDiscoveryError("agent_tool_selection_limit_exceeded")
        selected_names = optional_names | self._mandatory_tool_names
        catalog_digest = canonical_digest(catalog)
        input_digest = _input_digest(user_message, permission_scope)
        selection = AgentToolSelection.create(
            catalog_digest=catalog_digest,
            input_digest=input_digest,
            selected_tools=selected_names,
        )
        return ResolvedAgentTools(
            tools=tuple(tool for tool in candidate_tools if tool.name in selected_names),
            selection=selection,
        )

    def replay(
        self,
        *,
        user_message: str,
        candidate_tools: Sequence[WajeAgentTool],
        permission_scope: Mapping[str, Any],
        selection_payload: Mapping[str, Any],
    ) -> ResolvedAgentTools:
        try:
            selection = AgentToolSelection.model_validate(selection_payload)
        except Exception as exc:
            raise AgentToolDiscoveryError(
                "agent_tool_selection_payload_invalid"
            ) from exc
        catalog, by_name = _catalog(candidate_tools)
        if selection.catalog_digest != canonical_digest(catalog):
            raise AgentToolDiscoveryError("agent_tool_selection_catalog_conflict")
        if selection.input_digest != _input_digest(user_message, permission_scope):
            raise AgentToolDiscoveryError("agent_tool_selection_input_conflict")
        selected_names = set(selection.selected_tools)
        if not self._mandatory_tool_names.issubset(selected_names):
            raise AgentToolDiscoveryError("agent_tool_selection_mandatory_missing")
        if not selected_names.issubset(set(by_name)):
            raise AgentToolDiscoveryError("agent_tool_selection_unknown")
        if (
            len(selected_names - self._mandatory_tool_names)
            > self._max_optional_tools
        ):
            raise AgentToolDiscoveryError("agent_tool_selection_limit_exceeded")
        return ResolvedAgentTools(
            tools=tuple(tool for tool in candidate_tools if tool.name in selected_names),
            selection=selection,
        )


def _catalog(
    tools: Sequence[WajeAgentTool],
) -> tuple[tuple[dict[str, Any], ...], dict[str, WajeAgentTool]]:
    by_name = {tool.name: tool for tool in tools}
    if len(by_name) != len(tools):
        raise AgentToolDiscoveryError("agent_tool_catalog_name_duplicate")
    catalog = tuple(
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_model.model_json_schema(),
            "executionMode": tool.execution_mode,
        }
        for tool in sorted(tools, key=lambda item: item.name)
    )
    return catalog, by_name


def _input_digest(
    user_message: str,
    permission_scope: Mapping[str, Any],
) -> str:
    return canonical_digest(
        {
            "user_message": user_message,
            "permission_scope": canonical_value(permission_scope),
        }
    )


def _selection_digest(
    *,
    catalog_digest: str,
    input_digest: str,
    selected_tools: Sequence[str],
) -> str:
    return canonical_digest(
        {
            "schema_version": TOOL_SELECTION_SCHEMA_VERSION,
            "catalog_digest": catalog_digest,
            "input_digest": input_digest,
            "selected_tools": sorted(set(selected_tools)),
        }
    )


__all__ = (
    "AgentToolSelection",
    "AgentToolDiscoveryError",
    "DynamicAgentToolResolver",
    "DynamicToolSelectionOutput",
    "ResolvedAgentTools",
    "TOOL_DISCOVERY_INSTRUCTIONS",
    "TOOL_SELECTION_SCHEMA_VERSION",
    "ToolSelectionGenerator",
    "WajeToolSelectionGenerator",
)
