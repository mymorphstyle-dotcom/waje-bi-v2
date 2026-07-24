from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field

from bi_agent.runtime.evidence_authority import canonical_value


ToolHandler = Callable[
    [Mapping[str, Any]],
    Union[Any, Awaitable[Any]],
]
ToolArgumentAuthorityValidator = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    None,
]


class AgentToolResult(BaseModel):
    """SDK-neutral result contract shared by every WAJE function tool."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    status: Literal["succeeded", "limited", "failed", "needs_input"]
    output: dict[str, Any] | None
    artifact_refs: list[str] = Field(alias="artifactRefs", default_factory=list)
    material_refs: list[str] = Field(alias="materialRefs", default_factory=list)
    limitation_refs: list[str] = Field(alias="limitationRefs", default_factory=list)
    retryability: Literal["never", "same_input", "replan_required"]
    customer_summary: str = Field(alias="customerSummary", min_length=1)
    technical_detail_ref: str | None = Field(
        alias="technicalDetailRef",
        default=None,
    )


class WajeAgentSession(Protocol):
    """SDK-neutral persistent history adapter accepted by the SDK boundary."""

    session_id: str

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]: ...

    async def add_items(self, items: list[dict[str, Any]]) -> None: ...

    async def pop_item(self) -> dict[str, Any] | None: ...

    async def clear_session(self) -> None: ...


class WajeAgentEventSink(Protocol):
    async def record_tool_call(
        self,
        *,
        tool_name: str,
        call_id: str,
        arguments: Mapping[str, Any],
    ) -> None: ...

    async def record_tool_result(
        self,
        *,
        tool_name: str,
        call_id: str,
        result: Any,
        succeeded: bool,
    ) -> None: ...


class AgentSessionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WajeAgentTool:
    """WAJE-owned function tool contract exposed to the SDK adapter."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    execution_mode: Literal["continue", "suspend_turn"] = "continue"
    failure_recovery: Literal["none", "customer_summary"] = "none"
    prebinding_policy: Literal["disabled", "read_only"] = "disabled"
    argument_authority_validator: ToolArgumentAuthorityValidator | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("agent_tool_name_invalid")
        if not self.description.strip():
            raise ValueError("agent_tool_description_missing")
        if not isinstance(self.input_model, type) or not issubclass(
            self.input_model,
            BaseModel,
        ):
            raise TypeError("agent_tool_input_model_invalid")
        if self.execution_mode not in {"continue", "suspend_turn"}:
            raise ValueError("agent_tool_execution_mode_invalid")
        if self.failure_recovery not in {"none", "customer_summary"}:
            raise ValueError("agent_tool_failure_recovery_invalid")
        if self.prebinding_policy not in {"disabled", "read_only"}:
            raise ValueError("agent_tool_prebinding_policy_invalid")
        if (
            self.argument_authority_validator is not None
            and not callable(self.argument_authority_validator)
        ):
            raise TypeError("agent_tool_argument_authority_validator_invalid")
        if (
            self.execution_mode == "suspend_turn"
            and self.failure_recovery != "none"
        ):
            raise ValueError("agent_suspending_tool_failure_recovery_invalid")
        if (
            self.prebinding_policy == "read_only"
            and self.execution_mode != "continue"
        ):
            raise ValueError("agent_tool_prebinding_requires_continue")


@dataclass(frozen=True)
class WajePreboundToolCall:
    """SDK-neutral, typed first tool call accepted by WAJE action binding."""

    tool_name: str
    call_id: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        for value, code in (
            (self.tool_name, "agent_prebound_tool_name_invalid"),
            (self.call_id, "agent_prebound_tool_call_id_invalid"),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise ValueError(code)
        normalized = canonical_value(dict(self.arguments))
        if not isinstance(normalized, dict):
            raise ValueError("agent_prebound_tool_arguments_invalid")
        object.__setattr__(self, "arguments", normalized)


@dataclass(frozen=True)
class WajeAgentRunRequest:
    """SDK-neutral input for one in-process model/tool loop."""

    run_id: str
    agent_name: str
    instructions: str
    input_text: str
    tools: Sequence[WajeAgentTool] = ()
    output_type: type[BaseModel] | None = None
    max_turns: int = 10
    trace_metadata: Mapping[str, Any] = field(default_factory=dict)
    session: WajeAgentSession | None = None
    event_sink: WajeAgentEventSink | None = None
    initial_tool_choice: str = "auto"
    required_tool_name: str | None = None
    prebound_tool_call: WajePreboundToolCall | None = None
    thinking_mode: Literal["provider_default", "enabled", "disabled"] = (
        "provider_default"
    )

    def __post_init__(self) -> None:
        for value, code in (
            (self.run_id, "agent_run_id_missing"),
            (self.agent_name, "agent_name_missing"),
            (self.instructions, "agent_instructions_missing"),
            (self.input_text, "agent_input_missing"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(code)
        if isinstance(self.max_turns, bool) or self.max_turns < 1:
            raise ValueError("agent_max_turns_invalid")
        if self.output_type is not None and (
            not isinstance(self.output_type, type)
            or not issubclass(self.output_type, BaseModel)
        ):
            raise TypeError("agent_output_type_invalid")
        tool_names = {tool.name for tool in self.tools}
        if self.initial_tool_choice not in {"auto", "none"} and (
            not self.initial_tool_choice
            or not self.initial_tool_choice.replace("_", "").isalnum()
        ):
            raise ValueError("agent_initial_tool_choice_invalid")
        if (
            self.initial_tool_choice not in {"auto", "none"}
            and self.initial_tool_choice not in tool_names
        ):
            raise ValueError("agent_initial_tool_choice_unknown")
        if self.required_tool_name is not None:
            if self.required_tool_name not in tool_names:
                raise ValueError("agent_required_tool_unknown")
            if self.initial_tool_choice != self.required_tool_name:
                raise ValueError("agent_required_tool_choice_mismatch")
        if self.prebound_tool_call is not None:
            prebound = self.prebound_tool_call
            if (
                self.required_tool_name != prebound.tool_name
                or self.initial_tool_choice != prebound.tool_name
            ):
                raise ValueError("agent_prebound_tool_binding_mismatch")
            tool = next(
                (candidate for candidate in self.tools if candidate.name == prebound.tool_name),
                None,
            )
            if tool is None or tool.prebinding_policy != "read_only":
                raise ValueError("agent_prebound_tool_policy_forbidden")
            try:
                parsed = tool.input_model.model_validate(dict(prebound.arguments))
            except Exception as exc:
                raise ValueError("agent_prebound_tool_arguments_invalid") from exc
            normalized = canonical_value(parsed.model_dump(mode="json"))
            if normalized != dict(prebound.arguments):
                raise ValueError("agent_prebound_tool_arguments_not_canonical")
        if self.thinking_mode not in {
            "provider_default",
            "enabled",
            "disabled",
        }:
            raise ValueError("agent_thinking_mode_invalid")


@dataclass(frozen=True)
class WajeAgentToolCall:
    """SDK-neutral evidence that the Runner emitted one function call."""

    tool_name: str
    call_id: str

    def __post_init__(self) -> None:
        if not self.tool_name or not self.call_id:
            raise ValueError("agent_tool_call_identity_invalid")


@dataclass(frozen=True)
class WajeAgentStreamEvent:
    """Customer-independent stream event projected out of SDK events."""

    kind: str
    delta: str = ""
    tool_name: str = ""


@dataclass(frozen=True)
class WajeAgentRunResult:
    """SDK-neutral terminal result retained by WAJE runtime code."""

    run_id: str
    final_output: str | Mapping[str, Any]
    usage: Mapping[str, int]
    model_turns: int
    stream_events: Sequence[WajeAgentStreamEvent] = ()
    tool_calls: Sequence[WajeAgentToolCall] = ()


class AgentTraceSink(Protocol):
    """Server-side Workbench trace sink; never used by customer projection code."""

    def write_trace_record(self, record: Mapping[str, Any]) -> None: ...


class AgentSdkAdapterError(RuntimeError):
    def __init__(self, code: str, *, retryability: str = "not_retryable") -> None:
        if retryability not in {"retryable", "not_retryable"}:
            raise ValueError("agent_sdk_error_retryability_invalid")
        super().__init__(code)
        self.code = code
        self.retryability = retryability
