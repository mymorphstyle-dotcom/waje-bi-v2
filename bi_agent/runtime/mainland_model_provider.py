from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
from time import monotonic
from typing import Any, AsyncIterator, Literal, Mapping, Optional
from urllib.parse import urlparse

import httpx
from agents import ModelSettings
from agents.models.interface import Model, ModelProvider
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI, OpenAIError

from bi_agent.runtime.llm_client import (
    LLMConfigurationError,
    LLMProviderError,
    _llm_provider_error_from_openai,
    _parse_timeout_seconds,
)


REQUIRED_PROVIDER_CAPABILITIES = (
    "text_generation",
    "function_calling",
    "structured_output",
    "streaming_text",
    "streaming_tool_calls",
    "typed_error_mapping",
)

_DEEPSEEK_MODEL_CAPABILITY_DEFAULTS: Mapping[str, tuple[int, int, str]] = {
    "deepseek-v4-flash": (1_000_000, 8_192, "enabled"),
    "deepseek-v4-pro": (1_000_000, 8_192, "enabled"),
}


@dataclass(frozen=True)
class MainlandModelCapabilities:
    text_generation: bool
    function_calling: bool
    structured_output: bool
    streaming_text: bool
    streaming_tool_calls: bool
    typed_error_mapping: bool
    context_window_tokens: int
    max_output_tokens: int
    thinking: bool
    streaming_tool_call_mode: str = "native"
    structured_output_mode: str = "json_object_with_waje_schema"
    deterministic_tool_choice_thinking: Literal["inherit", "disabled"] = "inherit"

    def __post_init__(self) -> None:
        for value, code in (
            (self.context_window_tokens, "provider_context_window_invalid"),
            (self.max_output_tokens, "provider_max_output_tokens_invalid"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LLMConfigurationError(code)
        if self.max_output_tokens >= self.context_window_tokens:
            raise LLMConfigurationError("provider_token_limits_invalid")
        if self.streaming_tool_call_mode not in {"native", "buffered"}:
            raise LLMConfigurationError("provider_streaming_tool_mode_invalid")
        if self.structured_output_mode not in {
            "json_schema",
            "json_object_with_waje_schema",
        }:
            raise LLMConfigurationError("provider_structured_output_mode_invalid")
        if self.deterministic_tool_choice_thinking not in {"inherit", "disabled"}:
            raise LLMConfigurationError(
                "provider_tool_choice_thinking_mode_invalid"
            )

    def missing_required(self) -> tuple[str, ...]:
        return tuple(
            name for name in REQUIRED_PROVIDER_CAPABILITIES if not getattr(self, name)
        )


@dataclass(frozen=True)
class MainlandModelSettings:
    max_output_tokens: int
    thinking: str
    temperature: float | None = None
    top_p: float | None = None
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens < 1
        ):
            raise LLMConfigurationError("provider_request_max_output_tokens_invalid")
        if self.thinking not in {"enabled", "disabled"}:
            raise LLMConfigurationError("provider_thinking_setting_invalid")
        for value, code in (
            (self.temperature, "provider_temperature_invalid"),
            (self.top_p, "provider_top_p_invalid"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise LLMConfigurationError(code)


@dataclass(frozen=True)
class MainlandProviderConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    model_settings: MainlandModelSettings
    capabilities: MainlandModelCapabilities
    timeout_seconds: float | None = None
    max_attempts: int = 3
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: float = 30.0

    def __post_init__(self) -> None:
        _validate_provider_identity(self.provider)
        _validate_mainland_base_url(self.base_url)
        if not self.api_key.strip():
            raise LLMConfigurationError("missing_llm_api_key")
        if not self.model.strip():
            raise LLMConfigurationError("missing_llm_model")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise LLMConfigurationError("invalid_llm_max_attempts")
        if (
            isinstance(self.circuit_failure_threshold, bool)
            or not isinstance(self.circuit_failure_threshold, int)
            or self.circuit_failure_threshold < 1
        ):
            raise LLMConfigurationError("provider_circuit_threshold_invalid")
        if self.circuit_recovery_seconds <= 0:
            raise LLMConfigurationError("provider_circuit_recovery_invalid")
        if self.model_settings.max_output_tokens > self.capabilities.max_output_tokens:
            raise LLMConfigurationError("provider_request_output_limit_exceeded")
        if self.model_settings.thinking == "enabled" and not self.capabilities.thinking:
            raise LLMConfigurationError("provider_thinking_capability_missing")

    @classmethod
    def deepseek_from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "MainlandProviderConfig":
        env = os.environ if environ is None else environ
        provider = env.get("WAJE_LLM_PROVIDER", "").strip()
        base_url = env.get("WAJE_LLM_BASE_URL", "").strip()
        model = env.get("WAJE_LLM_MODEL", "").strip()
        api_key = (
            env.get("WAJE_LLM_API_KEY") or env.get("DEEPSEEK_API_KEY") or ""
        ).strip()
        _validate_provider_identity(provider)
        parsed = urlparse(base_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if hostname != "deepseek.com" and not hostname.endswith(".deepseek.com"):
            raise LLMConfigurationError("deepseek_base_url_invalid")
        capability_defaults = _DEEPSEEK_MODEL_CAPABILITY_DEFAULTS.get(model)
        if capability_defaults is None:
            context_window_tokens = _required_positive_int(
                env,
                "WAJE_LLM_CONTEXT_WINDOW_TOKENS",
            )
            max_output_tokens = _required_positive_int(
                env,
                "WAJE_LLM_MAX_OUTPUT_TOKENS",
            )
            thinking = env.get("WAJE_LLM_THINKING", "").strip()
        else:
            default_context, default_output, default_thinking = capability_defaults
            context_window_tokens = _optional_positive_int(
                env,
                "WAJE_LLM_CONTEXT_WINDOW_TOKENS",
                default=default_context,
            )
            max_output_tokens = _optional_positive_int(
                env,
                "WAJE_LLM_MAX_OUTPUT_TOKENS",
                default=default_output,
            )
            thinking = (
                env.get("WAJE_LLM_THINKING", "").strip() or default_thinking
            )
        return cls(
            provider="deepseek",
            base_url=base_url,
            api_key=api_key,
            model=model,
            model_settings=MainlandModelSettings(
                max_output_tokens=max_output_tokens,
                thinking=thinking,
                temperature=0.0,
            ),
            capabilities=MainlandModelCapabilities(
                text_generation=True,
                function_calling=True,
                structured_output=True,
                streaming_text=True,
                streaming_tool_calls=True,
                typed_error_mapping=True,
                context_window_tokens=context_window_tokens,
                max_output_tokens=max_output_tokens,
                thinking=True,
                deterministic_tool_choice_thinking="disabled",
            ),
            timeout_seconds=_parse_timeout_seconds(
                env.get("WAJE_LLM_TIMEOUT_SECONDS")
            ),
            max_attempts=_optional_positive_int(
                env,
                "WAJE_LLM_MAX_ATTEMPTS",
                default=3,
            ),
        )


class ProviderCapabilityError(LLMConfigurationError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("provider_capability_missing:" + ",".join(missing))


class OutboundTargetGuard:
    def __init__(self, base_url: str) -> None:
        _validate_mainland_base_url(base_url)
        parsed = urlparse(base_url)
        self._scheme = parsed.scheme.lower()
        self._hostname = (parsed.hostname or "").lower().rstrip(".")
        self._port = parsed.port

    def assert_allowed(self, url: str | httpx.URL) -> None:
        parsed = urlparse(str(url))
        target = (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower().rstrip("."),
            parsed.port,
        )
        if target != (self._scheme, self._hostname, self._port):
            raise LLMConfigurationError("provider_outbound_target_rejected")
        _reject_openai_host(target[1])

    async def on_request(self, request: httpx.Request) -> None:
        self.assert_allowed(request.url)


class MainlandModelProvider(ModelProvider):
    """Explicit Chat Completions-only model provider for mainland inference."""

    transport = "openai_compatible_chat_completions"

    def __init__(
        self,
        config: MainlandProviderConfig,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        missing = config.capabilities.missing_required()
        if missing:
            raise ProviderCapabilityError(missing)
        self.config = config
        self.outbound_guard = OutboundTargetGuard(config.base_url)
        self._http_client = httpx.AsyncClient(
            transport=http_transport,
            follow_redirects=False,
            event_hooks={"request": [self.outbound_guard.on_request]},
        )
        self._openai_client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=config.max_attempts - 1,
            http_client=self._http_client,
        )
        delegate = OpenAIChatCompletionsModel(
            model=config.model,
            openai_client=self._openai_client,
        )
        self._circuit = _ProviderCircuit(
            failure_threshold=config.circuit_failure_threshold,
            recovery_seconds=config.circuit_recovery_seconds,
        )
        self._observation = _ProviderObservation()
        self._model = _TypedMainlandChatCompletionsModel(
            delegate,
            self._circuit,
            self._observation,
        )

    @classmethod
    def deepseek_from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> "MainlandModelProvider":
        return cls(
            MainlandProviderConfig.deepseek_from_env(environ),
            http_transport=http_transport,
        )

    def get_model(self, model_name: str | None) -> Model:
        if not model_name:
            raise LLMConfigurationError("sdk_default_model_forbidden")
        if model_name != self.config.model:
            raise LLMConfigurationError("unconfigured_mainland_model")
        return self._model

    def sdk_model_settings(
        self,
        *,
        structured_output: bool,
        initial_tool_choice: str = "auto",
    ) -> ModelSettings:
        configured = self.config.model_settings
        extra_body = dict(configured.extra_body)
        thinking = configured.thinking
        if (
            initial_tool_choice != "auto"
            and self.config.capabilities.deterministic_tool_choice_thinking
            == "disabled"
        ):
            thinking = "disabled"
        if _is_deepseek_endpoint(self.config.base_url):
            extra_body["thinking"] = {"type": thinking}
        if structured_output:
            mode = self.config.capabilities.structured_output_mode
            if mode == "json_object_with_waje_schema":
                extra_body["response_format"] = {"type": "json_object"}
        return ModelSettings(
            temperature=configured.temperature,
            top_p=configured.top_p,
            max_tokens=configured.max_output_tokens,
            extra_body=extra_body,
        )

    @property
    def thinking_observed(self) -> bool:
        return self._observation.thinking_observed

    def reset_probe_observations(self) -> None:
        self._observation.reset()

    async def close(self) -> None:
        await self._openai_client.close()


class _TypedMainlandChatCompletionsModel(Model):
    def __init__(
        self,
        delegate: OpenAIChatCompletionsModel,
        circuit: "_ProviderCircuit",
        observation: "_ProviderObservation",
    ) -> None:
        self._delegate = delegate
        self._circuit = circuit
        self._observation = observation

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        _reject_provider_managed_state(kwargs)
        self._circuit.before_request()
        try:
            response = await self._delegate.get_response(*args, **kwargs)
        except OpenAIError as exc:
            typed = _llm_provider_error_from_openai(exc)
            self._circuit.record_failure(typed)
            raise typed from exc
        else:
            self._circuit.record_success()
            if any(
                getattr(item, "type", "") == "reasoning"
                for item in getattr(response, "output", ())
            ):
                self._observation.record_thinking()
            return response

    def stream_response(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        async def guarded() -> AsyncIterator[Any]:
            _reject_provider_managed_state(kwargs)
            self._circuit.before_request()
            try:
                async for event in self._delegate.stream_response(*args, **kwargs):
                    if getattr(event, "type", "") in {
                        "response.reasoning_summary_text.delta",
                        "response.reasoning_text.delta",
                    }:
                        self._observation.record_thinking()
                    yield event
            except OpenAIError as exc:
                typed = _llm_provider_error_from_openai(exc)
                self._circuit.record_failure(typed)
                raise typed from exc
            else:
                self._circuit.record_success()

        return guarded()


class _ProviderCircuit:
    def __init__(self, *, failure_threshold: int, recovery_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def before_request(self) -> None:
        with self._lock:
            if self._open_until > monotonic():
                raise LLMProviderError(
                    kind="provider_unavailable",
                    retryability="retryable",
                    error_code="provider_circuit_open",
                )
            if self._open_until:
                self._open_until = 0.0
                self._failures = 0

    def record_failure(self, error: LLMProviderError) -> None:
        if error.retryability != "retryable":
            return
        with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._open_until = monotonic() + self._recovery_seconds

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0


class _ProviderObservation:
    def __init__(self) -> None:
        self._thinking_observed = False
        self._lock = threading.Lock()

    @property
    def thinking_observed(self) -> bool:
        with self._lock:
            return self._thinking_observed

    def record_thinking(self) -> None:
        with self._lock:
            self._thinking_observed = True

    def reset(self) -> None:
        with self._lock:
            self._thinking_observed = False


def _reject_provider_managed_state(kwargs: Mapping[str, Any]) -> None:
    if kwargs.get("previous_response_id") is not None:
        raise LLMConfigurationError("provider_previous_response_id_forbidden")
    if kwargs.get("conversation_id") is not None:
        raise LLMConfigurationError("provider_conversation_id_forbidden")
    if kwargs.get("prompt") is not None:
        raise LLMConfigurationError("provider_hosted_prompt_forbidden")


def _validate_provider_identity(provider: str) -> None:
    normalized = provider.strip().lower()
    if not normalized:
        raise LLMConfigurationError("missing_llm_provider")
    if normalized in {"openai", "openai_default", "sdk_default"}:
        raise LLMConfigurationError("openai_model_provider_forbidden")


def _validate_mainland_base_url(base_url: str) -> None:
    if not base_url.strip():
        raise LLMConfigurationError("missing_llm_base_url")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LLMConfigurationError("invalid_llm_base_url")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LLMConfigurationError("invalid_llm_base_url")
    _reject_openai_host((parsed.hostname or "").lower().rstrip("."))


def _reject_openai_host(hostname: str) -> None:
    if hostname == "openai.com" or hostname.endswith(".openai.com"):
        raise LLMConfigurationError("openai_endpoint_forbidden")


def _is_deepseek_endpoint(base_url: str) -> bool:
    hostname = (urlparse(base_url).hostname or "").lower().rstrip(".")
    return hostname == "deepseek.com" or hostname.endswith(".deepseek.com")


def _required_positive_int(env: Mapping[str, str], name: str) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        raise LLMConfigurationError(f"missing_{name.lower()}")
    return _positive_int(raw, f"invalid_{name.lower()}")


def _optional_positive_int(
    env: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    raw = env.get(name, "").strip()
    return default if not raw else _positive_int(raw, f"invalid_{name.lower()}")


def _positive_int(raw: str, code: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise LLMConfigurationError(code) from exc
    if value < 1:
        raise LLMConfigurationError(code)
    return value
