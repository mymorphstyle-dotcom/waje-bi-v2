from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from bi_agent.runtime.agent_sdk_contracts import (
    WajeAgentRunRequest,
    WajeAgentTool,
)
from bi_agent.runtime.agents_sdk_adapter import WajeAgentsSdkAdapter
from bi_agent.runtime.mainland_model_provider import (
    MainlandModelProvider,
    ProviderCapabilityError,
)
from bi_agent.runtime.llm_client import LLMConfigurationError


class _ProbeToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marker: str


class _ProbeStructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marker: str
    ok: bool


@dataclass(frozen=True)
class ProviderCapabilityProbeResult:
    provider: str
    model: str
    transport: str
    checks: Mapping[str, bool]
    context_window_tokens: int
    max_output_tokens: int
    thinking: bool
    observations: Mapping[str, Any]


class ProviderCapabilityProbe:
    """Live startup probe for the capabilities required by the WAJE agent loop."""

    def __init__(
        self,
        *,
        provider: MainlandModelProvider,
        adapter: WajeAgentsSdkAdapter,
    ) -> None:
        self._provider = provider
        self._adapter = adapter

    async def run(self) -> ProviderCapabilityProbeResult:
        capabilities = self._provider.config.capabilities
        missing = capabilities.missing_required()
        if missing:
            raise ProviderCapabilityError(missing)
        self._provider.reset_probe_observations()

        direct = await self._adapter.run(
            WajeAgentRunRequest(
                run_id="provider-probe:text",
                agent_name="provider_capability_probe",
                instructions=(
                    "Return the exact text WAJE_TEXT_PROBE_OK and no other text."
                ),
                input_text="Run the text generation probe.",
                max_turns=2,
            )
        )
        _require_marker(direct.final_output, "WAJE_TEXT_PROBE_OK", "text_generation")

        structured = await self._adapter.run(
            WajeAgentRunRequest(
                run_id="provider-probe:structured",
                agent_name="provider_capability_probe",
                instructions=(
                    "Return marker WAJE_STRUCTURED_PROBE_OK and ok=true using the "
                    "required final output schema."
                ),
                input_text="Run the structured output probe.",
                output_type=_ProbeStructuredOutput,
                max_turns=2,
            )
        )
        if structured.final_output != {
            "marker": "WAJE_STRUCTURED_PROBE_OK",
            "ok": True,
        }:
            raise ProviderCapabilityError(("structured_output",))

        tool_calls: list[dict[str, Any]] = []

        def echo_tool(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            tool_calls.append(dict(arguments))
            return {"echoed_marker": arguments["marker"]}

        streamed_tool = await self._adapter.run_streamed(
            WajeAgentRunRequest(
                run_id="provider-probe:stream-tool",
                agent_name="provider_capability_probe",
                instructions=(
                    "Call probe_echo once with marker WAJE_TOOL_PROBE_OK. After its "
                    "result, return the exact text WAJE_TOOL_PROBE_OK."
                ),
                input_text="Run the streaming function tool probe.",
                tools=(
                    WajeAgentTool(
                        name="probe_echo",
                        description="Echo the capability probe marker.",
                        input_model=_ProbeToolInput,
                        handler=echo_tool,
                    ),
                ),
                max_turns=4,
                initial_tool_choice="probe_echo",
                required_tool_name="probe_echo",
            )
        )
        if tool_calls != [{"marker": "WAJE_TOOL_PROBE_OK"}]:
            raise ProviderCapabilityError(("function_calling",))
        if [call.tool_name for call in streamed_tool.tool_calls] != ["probe_echo"]:
            raise ProviderCapabilityError(("specific_tool_choice",))
        _require_marker(
            streamed_tool.final_output,
            "WAJE_TOOL_PROBE_OK",
            "function_calling",
        )
        stream_kinds = {event.kind for event in streamed_tool.stream_events}
        if "tool_call_delta" not in stream_kinds:
            if capabilities.streaming_tool_call_mode != "buffered":
                raise ProviderCapabilityError(("streaming_tool_calls",))
            if "tool_called" not in stream_kinds:
                raise ProviderCapabilityError(("streaming_tool_calls",))

        streamed_text = await self._adapter.run_streamed(
            WajeAgentRunRequest(
                run_id="provider-probe:stream-text",
                agent_name="provider_capability_probe",
                instructions=(
                    "Return the exact text WAJE_STREAM_PROBE_OK and no other text."
                ),
                input_text="Run the streaming text probe.",
                max_turns=2,
            )
        )
        text_deltas = "".join(
            event.delta
            for event in streamed_text.stream_events
            if event.kind == "model_text_delta"
        )
        if "WAJE_STREAM_PROBE_OK" not in text_deltas:
            raise ProviderCapabilityError(("streaming_text",))

        observations = dict(self._provider.probe_observations)
        expected = urlparse(self._provider.config.base_url)
        expected_origin = f"{expected.scheme}://{expected.netloc}"
        expected_path = expected.path.rstrip("/") + "/chat/completions"
        mapping = self._provider.typed_error_mapping_observation()
        context_budget_enforced = _context_budget_is_enforced(self._provider)
        checks = {
            "text_generation": True,
            "function_calling": True,
            "specific_tool_choice": True,
            "structured_output": True,
            "streaming_text": True,
            "streaming_tool_calls": True,
            "request_origin": observations.get("origins") == [expected_origin],
            "chat_completions_path": observations.get("paths") == [expected_path],
            "configured_model": observations.get("models")
            == [self._provider.config.model],
            "context_budget_enforcement": context_budget_enforced,
            "output_limit_observed": observations.get(
                "requested_max_output_tokens"
            )
            == [self._provider.config.model_settings.max_output_tokens],
            "thinking": (
                capabilities.thinking and self._provider.thinking_observed
                if self._provider.config.model_settings.thinking == "enabled"
                else True
            ),
            "typed_error_mapping": capabilities.typed_error_mapping
            and mapping
            == {
                400: ("provider_request_rejected", "not_retryable"),
                401: ("provider_authentication_failed", "not_retryable"),
                403: ("provider_permission_denied", "not_retryable"),
                408: ("provider_unavailable", "retryable"),
                409: ("provider_unavailable", "retryable"),
                429: ("provider_rate_limited", "retryable"),
                500: ("provider_unavailable", "retryable"),
            },
        }
        failed = tuple(name for name, passed in checks.items() if not passed)
        if failed:
            raise ProviderCapabilityError(failed)
        return ProviderCapabilityProbeResult(
            provider=self._provider.config.provider,
            model=self._provider.config.model,
            transport=self._provider.transport,
            checks=checks,
            context_window_tokens=capabilities.context_window_tokens,
            max_output_tokens=capabilities.max_output_tokens,
            thinking=capabilities.thinking,
            observations=observations,
        )


def _require_marker(output: Any, marker: str, capability: str) -> None:
    if not isinstance(output, str) or marker not in output:
        raise ProviderCapabilityError((capability,))


def _context_budget_is_enforced(provider: MainlandModelProvider) -> bool:
    configured_output = provider.config.model_settings.max_output_tokens
    context_window = provider.config.capabilities.context_window_tokens
    provider.assert_token_budget(
        estimated_input_tokens=context_window - configured_output,
        requested_output_tokens=configured_output,
    )
    try:
        provider.assert_token_budget(
            estimated_input_tokens=context_window - configured_output + 1,
            requested_output_tokens=configured_output,
        )
    except LLMConfigurationError as exc:
        return str(exc) == "provider_request_context_limit_exceeded"
    return False
