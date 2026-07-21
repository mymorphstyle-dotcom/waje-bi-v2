from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import json
from typing import Any, Mapping, Sequence

from agents import Agent, FunctionTool, RunConfig, Runner
from agents.agent import ToolsToFinalOutputResult
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError
from pydantic import BaseModel, ValidationError

from bi_agent.runtime.agent_sdk_contracts import (
    AgentToolResult,
    AgentSessionError,
    AgentSdkAdapterError,
    AgentTraceSink,
    WajeAgentRunRequest,
    WajeAgentRunResult,
    WajeAgentStreamEvent,
    WajeAgentTool,
    WajeAgentToolCall,
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
                session=request.session,
            )
        except AgentSessionError as exc:
            raise AgentSdkAdapterError(
                "agent_session_persistence_failed",
                retryability="retryable",
            ) from exc
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
        projected = _project_result(request, result)
        _validate_tool_choice_result(request, projected)
        return projected

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
            session=request.session,
        )
        projected_events: list[WajeAgentStreamEvent] = []
        try:
            async for event in stream.stream_events():
                projected = _project_stream_event(event)
                if projected is not None:
                    projected_events.append(projected)
        except AgentSessionError as exc:
            raise AgentSdkAdapterError(
                "agent_session_persistence_failed",
                retryability="retryable",
            ) from exc
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
        projected = _project_result(
            request,
            stream,
            stream_events=projected_events,
        )
        _validate_tool_choice_result(request, projected)
        return projected

    def _build_sdk_run(self, request: WajeAgentRunRequest) -> tuple[Any, RunConfig]:
        tools = [
            _to_sdk_tool(tool, event_sink=request.event_sink) for tool in request.tools
        ]
        suspending_tool_names = [
            tool.name
            for tool in request.tools
            if tool.execution_mode == "suspend_turn"
        ]
        model_name = self._provider.config.model
        settings = self._provider.sdk_model_settings(
            structured_output=request.output_type is not None,
            initial_tool_choice=request.initial_tool_choice,
        )
        settings = replace(settings, tool_choice=request.initial_tool_choice)
        if suspending_tool_names:
            settings = replace(settings, parallel_tool_calls=False)
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
        suspending_tool_names_set = frozenset(suspending_tool_names)
        required_suspending_tool = request.required_tool_name in (
            suspending_tool_names_set
        )
        agent = Agent(
            name=request.agent_name,
            instructions=instructions,
            model=model_name,
            model_settings=settings,
            tools=tools,
            output_type=request.output_type,
            tool_use_behavior=(
                _successful_suspension_behavior(suspending_tool_names_set)
                if suspending_tool_names
                else "run_llm_again"
            ),
            reset_tool_choice=not required_suspending_tool,
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


def _to_sdk_tool(tool: WajeAgentTool, *, event_sink: Any = None) -> FunctionTool:
    async def invoke(context: Any, arguments_json: str) -> Any:
        call_id = str(getattr(context, "tool_call_id", "") or "")
        if not call_id:
            raise AgentSdkAdapterError("agent_tool_call_id_missing")
        try:
            parsed = tool.input_model.model_validate_json(arguments_json)
        except ValidationError as exc:
            if tool.execution_mode == "suspend_turn":
                return await _contract_correction_output(
                    tool=tool,
                    event_sink=event_sink,
                    call_id=call_id,
                    arguments={"contract_error": "agent_tool_arguments_invalid"},
                    error=exc,
                )
            raise AgentSdkAdapterError("agent_tool_arguments_invalid") from exc
        arguments = parsed.model_dump(mode="json")
        if event_sink is not None:
            await event_sink.record_tool_call(
                tool_name=tool.name,
                call_id=call_id,
                arguments=arguments,
            )
        try:
            value = tool.handler(arguments)
            if inspect.isawaitable(value):
                value = await value
        except (ValidationError, ValueError, TypeError) as exc:
            if tool.execution_mode == "suspend_turn":
                return await _contract_correction_output(
                    tool=tool,
                    event_sink=event_sink,
                    call_id=call_id,
                    arguments=arguments,
                    error=exc,
                    call_already_recorded=True,
                )
            if event_sink is not None:
                await event_sink.record_tool_result(
                    tool_name=tool.name,
                    call_id=call_id,
                    result={"error_type": type(exc).__name__},
                    succeeded=False,
                )
            raise
        except Exception as exc:
            if event_sink is not None:
                await event_sink.record_tool_result(
                    tool_name=tool.name,
                    call_id=call_id,
                    result={"error_type": type(exc).__name__},
                    succeeded=False,
                )
            raise
        if event_sink is not None:
            await event_sink.record_tool_result(
                tool_name=tool.name,
                call_id=call_id,
                result=(
                    value.model_dump(mode="json", by_alias=True)
                    if isinstance(value, BaseModel)
                    else dict(value)
                    if isinstance(value, Mapping)
                    else value
                ),
                succeeded=not (
                    isinstance(value, AgentToolResult) and value.status == "failed"
                ),
            )
        if isinstance(value, BaseModel):
            return _sdk_tool_output(value.model_dump(mode="json", by_alias=True))
        if isinstance(value, Mapping):
            return _sdk_tool_output(dict(value))
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


async def _contract_correction_output(
    *,
    tool: WajeAgentTool,
    event_sink: Any,
    call_id: str,
    arguments: Mapping[str, Any],
    error: Exception,
    call_already_recorded: bool = False,
) -> str:
    error_code = _contract_error_code(error)
    if event_sink is not None and not call_already_recorded:
        await event_sink.record_tool_call(
            tool_name=tool.name,
            call_id=call_id,
            arguments=arguments,
        )
    correction = {
        "schemaVersion": "waje-tool-contract-error.v1",
        "status": "failed",
        "errorCode": error_code,
        "retryability": "correct_arguments",
        "instruction": _contract_correction_instruction(error_code),
    }
    if event_sink is not None:
        await event_sink.record_tool_result(
            tool_name=tool.name,
            call_id=call_id,
            result=correction,
            succeeded=False,
        )
    return _sdk_tool_output(correction)


def _contract_error_code(error: Exception) -> str:
    candidates: list[str] = []
    if isinstance(error, ValidationError):
        for detail in error.errors():
            context = detail.get("ctx")
            if isinstance(context, Mapping):
                candidates.append(str(context.get("error") or ""))
    candidates.append(str(error))
    for candidate in candidates:
        normalized = candidate.removeprefix("Value error, ").strip()
        if normalized and all(
            character.islower()
            or character.isdigit()
            or character == "_"
            for character in normalized
        ):
            return normalized
    return "agent_tool_arguments_contract_invalid"


def _contract_correction_instruction(error_code: str) -> str:
    if error_code == "agent_interaction_customer_language_mismatch":
        return (
            "Rewrite materialDecision, every option label, and every option description "
            "in the required customer language. materialDecision must be a natural-language "
            "customer prompt, never an enum name or technical identifier. Then call the same "
            "required tool again."
        )
    if error_code == "pending_action_question_shape_invalid":
        return (
            "Provide two or three distinct customer-readable options and set recommended=true "
            "on exactly one option. Then call the same required tool again."
        )
    return (
        "Correct the arguments to satisfy the supplied tool schema and invariants, "
        "then call the same required tool again."
    )


def _successful_suspension_behavior(
    suspending_tool_names: frozenset[str],
) -> Any:
    async def behavior(_context: Any, results: list[Any]) -> ToolsToFinalOutputResult:
        for result in results:
            tool_name = str(getattr(getattr(result, "tool", None), "name", "") or "")
            if tool_name not in suspending_tool_names:
                continue
            output = getattr(result, "output", None)
            if _is_successful_suspension_output(output):
                return ToolsToFinalOutputResult(
                    is_final_output=True,
                    final_output=output,
                )
        return ToolsToFinalOutputResult(is_final_output=False)

    return behavior


def _is_successful_suspension_output(output: Any) -> bool:
    value = output
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return False
    if not isinstance(value, Mapping):
        return False
    status = value.get("status")
    if status == "needs_input":
        return True
    nested = value.get("output")
    return (
        status == "succeeded"
        and isinstance(nested, Mapping)
        and (nested.get("taskState") or nested.get("task_state")) == "queued"
    )


def _sdk_tool_output(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise AgentSdkAdapterError("agent_tool_result_not_json_serializable") from exc


def _project_result(
    request: WajeAgentRunRequest,
    result: Any,
    *,
    stream_events: Sequence[WajeAgentStreamEvent] = (),
) -> WajeAgentRunResult:
    final_output = result.final_output
    if isinstance(final_output, BaseModel):
        projected_output: str | Mapping[str, Any] = final_output.model_dump(
            mode="json",
            by_alias=True,
        )
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
        tool_calls=_project_tool_calls(result),
    )


def _project_tool_calls(result: Any) -> tuple[WajeAgentToolCall, ...]:
    projected: list[WajeAgentToolCall] = []
    for item in getattr(result, "new_items", ()) or ():
        if getattr(item, "type", "") != "tool_call_item":
            continue
        raw_item = getattr(item, "raw_item", None)
        tool_name = str(getattr(raw_item, "name", "") or "")
        call_id = str(
            getattr(raw_item, "call_id", "") or getattr(raw_item, "id", "") or ""
        )
        if tool_name and call_id:
            projected.append(WajeAgentToolCall(tool_name=tool_name, call_id=call_id))
    return tuple(projected)


def _validate_tool_choice_result(
    request: WajeAgentRunRequest,
    result: WajeAgentRunResult,
) -> None:
    if request.initial_tool_choice == "none" and result.tool_calls:
        raise AgentSdkAdapterError("agent_forbidden_tool_call")
    if request.required_tool_name is None:
        return
    if not result.tool_calls:
        raise AgentSdkAdapterError("agent_required_tool_call_missing")
    if result.tool_calls[0].tool_name != request.required_tool_name:
        raise AgentSdkAdapterError("agent_required_tool_call_mismatch")


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
