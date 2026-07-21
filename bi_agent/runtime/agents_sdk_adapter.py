from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Mapping, Sequence

from agents import Agent, FunctionTool, RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError
from pydantic import BaseModel, ValidationError

from bi_agent.runtime.agent_sdk_contracts import (
    AgentSdkAdapterError,
    AgentTraceSink,
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentStreamEvent,
    WajeAgentTool,
)
from bi_agent.runtime.agents_sdk_trace import (
    install_waje_trace_processor,
    waje_sdk_trace_id,
)
from bi_agent.runtime.llm_client import LLMProviderError
from bi_agent.runtime.mainland_model_provider import MainlandModelProvider


class WajeAgentsSdkAdapter:
    """The only boundary allowed to translate WAJE contracts into SDK types."""

    def __init__(
        self,
        *,
        provider: MainlandModelProvider,
        trace_sink: AgentTraceSink,
    ) -> None:
        self._provider = provider
        self._trace_processor = install_waje_trace_processor(trace_sink)

    async def run(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        agent, run_config = self._build_sdk_run(request)
        try:
            result = await Runner.run(
                agent,
                request.input_text,
                max_turns=request.max_turns,
                run_config=run_config,
                previous_response_id=None,
                conversation_id=None,
                session=None,
            )
        except LLMProviderError:
            raise
        except MaxTurnsExceeded as exc:
            raise AgentSdkAdapterError(
                "agent_model_turn_limit_exceeded",
                retryability="not_retryable",
            ) from exc
        except (ModelBehaviorError, ValidationError) as exc:
            raise AgentSdkAdapterError("agent_output_contract_invalid") from exc
        except UserError as exc:
            raise AgentSdkAdapterError("agents_sdk_contract_invalid") from exc
        return _project_result(request, result)

    def run_sync(self, request: WajeAgentRunRequest) -> WajeAgentRunResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(request))
        raise AgentSdkAdapterError("agent_sync_run_inside_event_loop_forbidden")

    async def run_streamed(
        self,
        request: WajeAgentRunRequest,
    ) -> WajeAgentRunResult:
        agent, run_config = self._build_sdk_run(request)
        stream = Runner.run_streamed(
            agent,
            request.input_text,
            max_turns=request.max_turns,
            run_config=run_config,
            previous_response_id=None,
            conversation_id=None,
            session=None,
        )
        projected_events: list[WajeAgentStreamEvent] = []
        try:
            async for event in stream.stream_events():
                projected = _project_stream_event(event)
                if projected is not None:
                    projected_events.append(projected)
        except LLMProviderError:
            raise
        except MaxTurnsExceeded as exc:
            raise AgentSdkAdapterError(
                "agent_model_turn_limit_exceeded",
                retryability="not_retryable",
            ) from exc
        except (ModelBehaviorError, ValidationError) as exc:
            raise AgentSdkAdapterError("agent_output_contract_invalid") from exc
        except UserError as exc:
            raise AgentSdkAdapterError("agents_sdk_contract_invalid") from exc
        return _project_result(
            request,
            stream,
            stream_events=projected_events,
        )

    def _build_sdk_run(self, request: WajeAgentRunRequest) -> tuple[Any, RunConfig]:
        tools = [_to_sdk_tool(tool) for tool in request.tools]
        model_name = self._provider.config.model
        settings = self._provider.sdk_model_settings(
            structured_output=request.output_type is not None,
        )
        instructions = request.instructions
        if request.output_type is not None:
            schema_json = json.dumps(
                request.output_type.model_json_schema(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            instructions = (
                f"{instructions.rstrip()}\n"
                "Return the final output as JSON matching this exact JSON Schema: "
                f"{schema_json}"
            )
        agent = Agent(
            name=request.agent_name,
            instructions=instructions,
            model=model_name,
            model_settings=settings,
            tools=tools,
            output_type=request.output_type,
        )
        metadata = {
            **dict(request.trace_metadata),
            "waje_run_id": request.run_id,
            "provider": self._provider.config.provider,
            "model": model_name,
            "transport": self._provider.transport,
        }
        run_config = RunConfig(
            model=model_name,
            model_provider=self._provider,
            model_settings=settings,
            tracing_disabled=False,
            trace_include_sensitive_data=True,
            workflow_name="WAJE Agent Runtime",
            trace_id=waje_sdk_trace_id(request.run_id),
            group_id=request.run_id,
            trace_metadata=metadata,
        )
        return agent, run_config


def _to_sdk_tool(tool: WajeAgentTool) -> FunctionTool:
    async def invoke(_context: Any, arguments_json: str) -> Any:
        try:
            parsed = tool.input_model.model_validate_json(arguments_json)
        except ValidationError as exc:
            raise AgentSdkAdapterError("agent_tool_arguments_invalid") from exc
        value = tool.handler(parsed.model_dump(mode="json"))
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, Mapping):
            return dict(value)
        return value

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema=tool.input_model.model_json_schema(),
        on_invoke_tool=invoke,
        # WAJE validates every argument with the Pydantic input model. Provider-side
        # strict JSON Schema remains a capability distinction across mainland vendors.
        strict_json_schema=False,
    )


def _project_result(
    request: WajeAgentRunRequest,
    result: Any,
    *,
    stream_events: Sequence[WajeAgentStreamEvent] = (),
) -> WajeAgentRunResult:
    final_output = result.final_output
    if isinstance(final_output, BaseModel):
        projected_output: str | Mapping[str, Any] = final_output.model_dump(mode="json")
    elif isinstance(final_output, Mapping):
        projected_output = dict(final_output)
    elif isinstance(final_output, str):
        projected_output = final_output
    else:
        raise AgentSdkAdapterError("agent_final_output_type_invalid")

    usage_object = getattr(getattr(result, "context_wrapper", None), "usage", None)
    usage: dict[str, int] = {}
    for source, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
        ("requests", "requests"),
    ):
        value = getattr(usage_object, source, None)
        if isinstance(value, int) and not isinstance(value, bool):
            usage[target] = value
    return WajeAgentRunResult(
        run_id=request.run_id,
        final_output=projected_output,
        usage=usage,
        model_turns=len(getattr(result, "raw_responses", ()) or ()),
        stream_events=tuple(stream_events),
    )


def _project_stream_event(event: Any) -> WajeAgentStreamEvent | None:
    event_type = getattr(event, "type", "")
    if event_type == "raw_response_event":
        data = getattr(event, "data", None)
        raw_type = getattr(data, "type", "")
        if raw_type == "response.output_text.delta":
            return WajeAgentStreamEvent(
                kind="model_text_delta",
                delta=str(getattr(data, "delta", "")),
            )
        if raw_type == "response.function_call_arguments.delta":
            return WajeAgentStreamEvent(
                kind="tool_call_delta",
                delta=str(getattr(data, "delta", "")),
            )
        # Reasoning deltas stay in server-side SDK trace and never enter stream
        # projections consumed by customer surfaces.
        return None
    if event_type == "run_item_stream_event":
        item = getattr(event, "item", None)
        item_type = getattr(item, "type", "")
        if item_type == "tool_call_item":
            raw_item = getattr(item, "raw_item", None)
            return WajeAgentStreamEvent(
                kind="tool_called",
                tool_name=str(getattr(raw_item, "name", "")),
            )
        if item_type == "tool_call_output_item":
            return WajeAgentStreamEvent(kind="tool_output")
    return None
