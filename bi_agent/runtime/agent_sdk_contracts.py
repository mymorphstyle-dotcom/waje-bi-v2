from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field


ToolHandler = Callable[
    [Mapping[str, Any]],
    Union[Any, Awaitable[Any]],
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
