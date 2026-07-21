from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

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
            )
        )
        if tool_calls != [{"marker": "WAJE_TOOL_PROBE_OK"}]:
            raise ProviderCapabilityError(("function_calling",))
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

        checks = {
            "text_generation": True,
            "function_calling": True,
            "structured_output": True,
            "streaming_text": True,
            "streaming_tool_calls": True,
            "context_window_limit": capabilities.context_window_tokens > 0,
            "output_limit": (
                0
                < self._provider.config.model_settings.max_output_tokens
                <= capabilities.max_output_tokens
            ),
            "thinking": (
                capabilities.thinking and self._provider.thinking_observed
                if self._provider.config.model_settings.thinking == "enabled"
                else True
            ),
            "typed_error_mapping": capabilities.typed_error_mapping,
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
        )


def _require_marker(output: Any, marker: str, capability: str) -> None:
    if not isinstance(output, str) or marker not in output:
        raise ProviderCapabilityError((capability,))
