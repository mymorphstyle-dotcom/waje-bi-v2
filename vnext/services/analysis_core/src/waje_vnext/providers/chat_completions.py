"""Generic HTTPS Chat Completions adapter with provider-layer retry."""

from __future__ import annotations

import json
import hashlib
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import local
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse
from pathlib import Path

from waje_vnext.domain.actions import ActionKind, AgentActionProposal
from waje_vnext.domain.action_codec import (
    ActionProposalDecodeError,
    decode_agent_action_proposal,
)
from waje_vnext.domain.canonical import (
    canonical_json_bytes,
    content_sha256,
    to_jsonable,
)
from waje_vnext.domain.controller import PrimaryAgentRequest
from waje_vnext.domain.runtime_amendment import (
    FrameReviewProposal,
    FrameReviewRequest,
    MessageBindingRequest,
    MessageImpactProposal,
    ModelConfigurationIdentity,
    ModelExecutionRole,
    ModelInputViewKind,
    ModelRequestArtifact,
)
from waje_vnext.domain.typed_decode import decode_typed_dataclass

from .base import (
    ProviderConfigurationError,
    ProviderAttemptTrace,
    ProviderPermanentError,
    PreparedModelInvocation,
    ProviderTransientError,
)
from .tool_contract import (
    action_kind_for_tool,
    action_tools,
    strict_record_tool,
)


ENV_PREFIX = "WAJE_VNEXT_LLM_"
PROTOCOL_REF = "openai-compatible-chat-completions.v1"
ADAPTER_RELEASE_REF = "waje-vnext://providers/chat-completions.v1"
DELIVERY_POLICY_REF = "waje-vnext://providers/retry-policy.v1"
_ADAPTER_RELEASE_SHA256 = hashlib.sha256(
    Path(__file__).read_bytes()
).hexdigest()


class ChatTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float | None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ChatCompletionsProviderSettings:
    provider_name: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    thinking: str = "disabled"
    temperature: float = 1.0
    top_p: float = 1.0
    seed: int | None = None
    max_attempts: int = 3
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("provider_name", "base_url", "api_key", "model"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ProviderConfigurationError(
                    "{} must be configured".format(name)
                )
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ProviderConfigurationError(
                "base_url must be an absolute HTTPS URL"
            )
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ProviderConfigurationError(
                "base_url cannot contain credentials, query, or fragment"
            )
        if self.max_attempts < 1:
            raise ProviderConfigurationError("max_attempts must be positive")
        if self.thinking not in {"enabled", "disabled"}:
            raise ProviderConfigurationError(
                "thinking must be enabled or disabled"
            )
        if not 0 <= self.temperature <= 2:
            raise ProviderConfigurationError(
                "temperature must be between 0 and 2"
            )
        if not 0 < self.top_p <= 1:
            raise ProviderConfigurationError(
                "top_p must be greater than 0 and at most 1"
            )
        if self.seed is not None and self.seed < 0:
            raise ProviderConfigurationError(
                "seed must be non-negative when configured"
            )
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ProviderConfigurationError(
                "timeout_seconds must be positive when configured"
            )

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "ChatCompletionsProviderSettings":
        source = os.environ if environment is None else environment
        timeout_raw = source.get(ENV_PREFIX + "TIMEOUT_SECONDS", "").strip()
        timeout = None
        if timeout_raw:
            parsed_timeout = _positive_float(timeout_raw)
            timeout = parsed_timeout
        attempts_raw = source.get(ENV_PREFIX + "MAX_ATTEMPTS", "").strip()
        attempts = 3 if not attempts_raw else _positive_int(attempts_raw)
        temperature_raw = source.get(
            ENV_PREFIX + "TEMPERATURE",
            "1.0",
        ).strip()
        top_p_raw = source.get(ENV_PREFIX + "TOP_P", "1.0").strip()
        seed_raw = source.get(ENV_PREFIX + "SEED", "").strip()
        return cls(
            provider_name=source.get(ENV_PREFIX + "PROVIDER", "").strip(),
            base_url=source.get(ENV_PREFIX + "BASE_URL", "").strip(),
            api_key=source.get(ENV_PREFIX + "API_KEY", "").strip(),
            model=source.get(ENV_PREFIX + "MODEL", "").strip(),
            thinking=(
                source.get(ENV_PREFIX + "THINKING", "disabled").strip()
                or "disabled"
            ),
            temperature=_float_value(
                temperature_raw,
                "temperature",
            ),
            top_p=_float_value(top_p_raw, "top_p"),
            seed=None if not seed_raw else _nonnegative_int(seed_raw),
            max_attempts=attempts,
            timeout_seconds=timeout,
        )

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass(frozen=True, slots=True)
class CompiledChatInvocation:
    execution_role: ModelExecutionRole
    input_view_kind: ModelInputViewKind
    input_view_ref: str
    input_view_sha256: str
    prompt_bundle_ref: str
    system_instruction: str
    tool_bundle_ref: str
    tools: list[dict[str, object]]
    payload: dict[str, object]
    decoder_release_ref: str


def compile_trusted_chat_invocation(
    *,
    logical_job_kind: str,
    request: object,
    configuration: ModelConfigurationIdentity,
) -> CompiledChatInvocation:
    if logical_job_kind == "primary_agent" and isinstance(
        request,
        PrimaryAgentRequest,
    ):
        execution_role = ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        input_view_kind = ModelInputViewKind.AGENT_WORLD_VIEW
        input_view_ref = request.context_packet.packet_id
        input_view_sha256 = request.context_packet.content_sha256
        prompt_bundle_ref = (
            "waje-vnext://prompts/primary-business-analysis-agent.v1"
        )
        system_instruction = _SYSTEM_INSTRUCTION
        tool_bundle_ref = "waje-vnext://tools/primary-agent-actions.v3"
        tools = action_tools(
            request.allowed_actions,
            controller_bound_fields=frozenset(
                {"question_revision_id"}
            ),
        )
        tool_choice: object = "required"
        decoder_release_ref = (
            "waje-vnext://decoders/agent-action-proposal.v3"
        )
    elif logical_job_kind == "message_binding" and isinstance(
        request,
        MessageBindingRequest,
    ):
        execution_role = ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        input_view_kind = ModelInputViewKind.MESSAGE_BINDING_VIEW
        input_view_ref = request.message_id
        input_view_sha256 = content_sha256(request)
        prompt_bundle_ref = "waje-vnext://prompts/message-binding.v1"
        system_instruction = _BINDING_SYSTEM_INSTRUCTION
        tool_bundle_ref = "waje-vnext://tools/message-binding.v1"
        tools = _binding_tools()
        tool_choice = {
            "type": "function",
            "function": {"name": "submit_message_impact"},
        }
        decoder_release_ref = "waje-vnext://decoders/message-impact.v1"
    elif logical_job_kind == "measurement_reviewer" and isinstance(
        request,
        FrameReviewRequest,
    ):
        execution_role = ModelExecutionRole.RUNTIME_REVIEWER
        input_view_kind = ModelInputViewKind.MEASUREMENT_REVIEW_VIEW
        input_view_ref = request.frame_candidate.frame_candidate_id
        input_view_sha256 = content_sha256(request)
        prompt_bundle_ref = "waje-vnext://prompts/measurement-reviewer.v1"
        system_instruction = _REVIEWER_SYSTEM_INSTRUCTION
        tool_bundle_ref = "waje-vnext://tools/measurement-review.v1"
        tools = _review_tools()
        tool_choice = {
            "type": "function",
            "function": {"name": "submit_measurement_review"},
        }
        decoder_release_ref = (
            "waje-vnext://decoders/measurement-review.v1"
        )
    else:
        raise ProviderConfigurationError(
            "unsupported logical job kind or typed request"
        )
    if configuration.execution_role is not execution_role:
        raise ProviderConfigurationError(
            "model configuration role differs from invocation contract"
        )
    payload: dict[str, object] = {
        "model": configuration.model_ref,
        "thinking": {"type": configuration.thinking},
        "temperature": configuration.stable_parameters["temperature"],
        "top_p": configuration.stable_parameters["top_p"],
        "messages": [
            {"role": "system", "content": system_instruction},
            {
                "role": "user",
                "content": json.dumps(
                    to_jsonable(request),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        ],
        "tools": tools,
        "tool_choice": tool_choice,
        "parallel_tool_calls": configuration.stable_parameters[
            "parallel_tool_calls"
        ],
    }
    if "seed" in configuration.stable_parameters:
        payload["seed"] = configuration.stable_parameters["seed"]
    return CompiledChatInvocation(
        execution_role=execution_role,
        input_view_kind=input_view_kind,
        input_view_ref=input_view_ref,
        input_view_sha256=input_view_sha256,
        prompt_bundle_ref=prompt_bundle_ref,
        system_instruction=system_instruction,
        tool_bundle_ref=tool_bundle_ref,
        tools=tools,
        payload=payload,
        decoder_release_ref=decoder_release_ref,
    )


class UrllibChatTransport:
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float | None,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=canonical_json_bytes(payload),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds,
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if error.code in {408, 409, 425, 429} or error.code >= 500:
                raise ProviderTransientError(
                    "provider returned retryable HTTP {}".format(error.code)
                ) from error
            raise ProviderPermanentError(
                "provider returned HTTP {}: {}".format(
                    error.code,
                    body[:200],
                )
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ProviderTransientError(
                "provider transport failed"
            ) from error
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as error:
            raise ProviderPermanentError(
                "provider response is not JSON"
            ) from error
        if not isinstance(decoded, Mapping):
            raise ProviderPermanentError(
                "provider response must be a JSON object"
            )
        return decoded


class ChatCompletionsProvider:
    supports_durable_attempt_observer = True

    def __init__(
        self,
        settings: ChatCompletionsProviderSettings,
        *,
        transport: ChatTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport or UrllibChatTransport()
        self._trace_local = local()

    @property
    def provider_ref(self) -> str:
        return self._settings.provider_name

    @property
    def model_ref(self) -> str:
        return self._settings.model

    @property
    def configuration_ref(self) -> str:
        return self.configuration_identity(
            ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        ).configuration_sha256

    def configuration_identity(
        self,
        execution_role: ModelExecutionRole,
    ) -> ModelConfigurationIdentity:
        stable_parameters: dict[str, object] = {
            "temperature": self._settings.temperature,
            "top_p": self._settings.top_p,
            "tool_choice_policy": "contract_selected",
            "parallel_tool_calls": False,
        }
        if self._settings.seed is not None:
            stable_parameters["seed"] = self._settings.seed
        return ModelConfigurationIdentity.build(
            execution_role=execution_role,
            provider_ref=self._settings.provider_name,
            endpoint_ref=self._settings.endpoint,
            protocol_ref=PROTOCOL_REF,
            adapter_release_ref=ADAPTER_RELEASE_REF,
            adapter_release_sha256=_ADAPTER_RELEASE_SHA256,
            model_ref=self._settings.model,
            thinking=self._settings.thinking,
            stable_parameters=stable_parameters,
            delivery_policy_ref=DELIVERY_POLICY_REF,
            max_attempts=self._settings.max_attempts,
            timeout_seconds=self._settings.timeout_seconds,
        )

    def describe_invocation(
        self,
        *,
        logical_model_job_id: str,
        logical_job_kind: str,
        request: object,
        typed_request_contract_ref: str,
        output_contract_ref: str,
        created_at: datetime,
    ) -> PreparedModelInvocation:
        (
            execution_role,
            input_view_kind,
            input_view_ref,
            input_view_sha256,
            prompt_bundle_ref,
            system_instruction,
            tool_bundle_ref,
            tools,
            payload,
            decoder_release_ref,
        ) = self._invocation_material(
            logical_job_kind=logical_job_kind,
            request=request,
        )
        prompt_bundle_sha256 = content_sha256(
            {
                "messages": (
                    {
                        "role": "system",
                        "content": system_instruction,
                    },
                )
            }
        )
        tool_bundle_sha256 = content_sha256(tools)
        output_contract_sha256 = content_sha256(
            {
                "output_contract_ref": output_contract_ref,
                "tool_bundle_sha256": tool_bundle_sha256,
                "decoder_release_ref": decoder_release_ref,
                "decoder_release_sha256": _ADAPTER_RELEASE_SHA256,
            }
        )
        artifact = ModelRequestArtifact(
            model_request_artifact_id=(
                "model-request:{}".format(logical_model_job_id)
            ),
            logical_model_job_id=logical_model_job_id,
            execution_role=execution_role,
            logical_job_kind=logical_job_kind,
            input_view_kind=input_view_kind,
            input_view_ref=input_view_ref,
            input_view_sha256=input_view_sha256,
            typed_request_contract_ref=typed_request_contract_ref,
            typed_request_sha256=content_sha256(request),
            prompt_bundle_ref=prompt_bundle_ref,
            prompt_bundle_sha256=prompt_bundle_sha256,
            tool_bundle_ref=tool_bundle_ref,
            tool_bundle_sha256=tool_bundle_sha256,
            output_contract_ref=output_contract_ref,
            output_contract_sha256=output_contract_sha256,
            decoder_release_ref=decoder_release_ref,
            decoder_release_sha256=_ADAPTER_RELEASE_SHA256,
            provider_request_body=payload,
            provider_request_sha256=content_sha256(payload),
            created_at=created_at,
        )
        return PreparedModelInvocation(
            configuration_identity=self.configuration_identity(
                execution_role
            ),
            request_artifact=artifact,
        )

    def take_last_attempt_trace(
        self,
    ) -> tuple[ProviderAttemptTrace, ...]:
        attempts = getattr(self._trace_local, "attempts", ())
        self._trace_local.attempts = ()
        return attempts

    def install_attempt_observer(self, observer) -> None:
        if getattr(self._trace_local, "attempt_observer", None) is not None:
            raise ProviderConfigurationError(
                "provider attempt observer is already installed"
            )
        self._trace_local.attempt_observer = observer

    def clear_attempt_observer(self) -> None:
        self._trace_local.attempt_observer = None

    def propose(self, request: PrimaryAgentRequest) -> AgentActionProposal:
        payload = self._primary_payload(request)
        return self._invoke(
            payload,
            lambda response: _decode_action_tool_response(
                response,
                request.allowed_actions,
                request.context_packet.accepted_question_revision_id,
            ),
        )

    def review(self, request: FrameReviewRequest) -> FrameReviewProposal:
        payload = self._review_payload(request)
        return self._invoke(payload, _decode_review_tool_response)

    def bind_message(
        self,
        request: MessageBindingRequest,
    ) -> MessageImpactProposal:
        payload = self._binding_payload(request)
        return self._invoke(payload, _decode_binding_tool_response)

    def _invocation_material(
        self,
        *,
        logical_job_kind: str,
        request: object,
    ):
        if logical_job_kind in {"primary_agent", "message_binding"}:
            execution_role = (
                ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
            )
        elif logical_job_kind == "measurement_reviewer":
            execution_role = ModelExecutionRole.RUNTIME_REVIEWER
        else:
            raise ProviderConfigurationError(
                "unsupported logical job kind or typed request"
            )
        material = compile_trusted_chat_invocation(
            logical_job_kind=logical_job_kind,
            request=request,
            configuration=self.configuration_identity(execution_role),
        )
        return (
            material.execution_role,
            material.input_view_kind,
            material.input_view_ref,
            material.input_view_sha256,
            material.prompt_bundle_ref,
            material.system_instruction,
            material.tool_bundle_ref,
            material.tools,
            material.payload,
            material.decoder_release_ref,
        )

    def _primary_payload(
        self,
        request: PrimaryAgentRequest,
    ) -> dict[str, object]:
        return compile_trusted_chat_invocation(
            logical_job_kind="primary_agent",
            request=request,
            configuration=self.configuration_identity(
                ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
            ),
        ).payload

    def _review_payload(
        self,
        request: FrameReviewRequest,
    ) -> dict[str, object]:
        return compile_trusted_chat_invocation(
            logical_job_kind="measurement_reviewer",
            request=request,
            configuration=self.configuration_identity(
                ModelExecutionRole.RUNTIME_REVIEWER
            ),
        ).payload

    def _binding_payload(
        self,
        request: MessageBindingRequest,
    ) -> dict[str, object]:
        return compile_trusted_chat_invocation(
            logical_job_kind="message_binding",
            request=request,
            configuration=self.configuration_identity(
                ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
            ),
        ).payload

    def _invoke(self, payload, decoder):
        last_error: ProviderTransientError | None = None
        attempts: list[ProviderAttemptTrace] = []
        observer = getattr(
            self._trace_local,
            "attempt_observer",
            None,
        )
        attempt_numbers = tuple(
            range(1, self._settings.max_attempts + 1)
        )
        durable_attempt_plan = (
            None
            if observer is None
            else getattr(observer, "attempt_numbers", None)
        )
        if durable_attempt_plan is not None:
            attempt_numbers = tuple(
                durable_attempt_plan(self._settings.max_attempts)
            )
        dispatch_url = self._settings.endpoint
        dispatch_timeout = self._settings.timeout_seconds
        durable_dispatch_parameters = (
            None
            if observer is None
            else getattr(observer, "dispatch_parameters", None)
        )
        if durable_dispatch_parameters is not None:
            dispatch_url, dispatch_timeout = durable_dispatch_parameters()
        for attempt in attempt_numbers:
            idempotency_key = (
                "provider-request:{}:{}".format(
                    content_sha256(payload),
                    attempt,
                )
                if observer is None
                else observer.before_attempt(attempt, payload)
            )
            try:
                response = self._transport.post_json(
                    url=dispatch_url,
                    headers={
                        "Authorization": "Bearer {}".format(
                            self._settings.api_key
                        ),
                        "Content-Type": "application/json",
                        "Idempotency-Key": idempotency_key,
                    },
                    payload=payload,
                    timeout_seconds=dispatch_timeout,
                )
                try:
                    decoded = decoder(response)
                except ProviderPermanentError:
                    trace = _attempt_trace(
                        response=response,
                        disposition="terminal_failure",
                        output_sha256=None,
                    )
                    attempts.append(trace)
                    if observer is not None:
                        observer.after_attempt(attempt, trace)
                    self._trace_local.attempts = tuple(attempts)
                    raise
                output_sha = content_sha256(decoded)
                trace = _attempt_trace(
                    response=response,
                    disposition="succeeded",
                    output_sha256=output_sha,
                )
                attempts.append(trace)
                if observer is not None:
                    observer.after_success(attempt, trace, decoded)
                self._trace_local.attempts = tuple(attempts)
                return decoded
            except ProviderTransientError as error:
                last_error = error
                trace = ProviderAttemptTrace(
                    disposition="retryable_failure",
                    provider_response_id=None,
                    output_sha256=None,
                    finish_reason=None,
                    usage_payload={},
                    completed_at=datetime.now(tz=UTC),
                )
                attempts.append(trace)
                if observer is not None:
                    observer.after_attempt(attempt, trace)
                if attempt == attempt_numbers[-1]:
                    break
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        self._trace_local.attempts = tuple(attempts)
        assert last_error is not None
        raise last_error


def _attempt_trace(
    *,
    response: Mapping[str, Any],
    disposition: str,
    output_sha256: str | None,
) -> ProviderAttemptTrace:
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id.strip():
        response_id = (
            None
            if output_sha256 is None
            else "transport-response:{}".format(output_sha256[:24])
        )
    finish_reason = None
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        candidate = choices[0]
        if isinstance(candidate, Mapping):
            raw_finish = candidate.get("finish_reason")
            if isinstance(raw_finish, str) and raw_finish.strip():
                finish_reason = raw_finish
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}
    return ProviderAttemptTrace(
        disposition=disposition,
        provider_response_id=response_id,
        output_sha256=output_sha256,
        finish_reason=finish_reason,
        usage_payload=dict(usage),
        completed_at=datetime.now(tz=UTC),
    )


def _decode_action_tool_response(
    response: Mapping[str, Any],
    allowed_actions: tuple,
    accepted_question_revision_id: str | None,
) -> AgentActionProposal:
    tool_name, decoded = _decode_single_tool_call(response)
    try:
        kind = action_kind_for_tool(tool_name)
    except ValueError as error:
        raise ProviderPermanentError(
            "provider selected an unknown action tool"
        ) from error
    if kind not in allowed_actions:
        raise ProviderPermanentError(
            "provider selected an action outside allowed_actions"
        )
    _reject_provider_system_identifiers(decoded)
    if kind is ActionKind.REVISE_FRAME:
        if accepted_question_revision_id is None:
            raise ProviderPermanentError(
                "revise_frame requires an accepted question authority"
            )
        decoded = _bind_revise_frame_question_authority(
            decoded,
            accepted_question_revision_id,
        )
    try:
        return decode_agent_action_proposal(
            {"kind": kind.value, "payload": decoded}
        )
    except ActionProposalDecodeError as error:
        raise ProviderPermanentError(
            "provider proposal violates the typed action contract"
        ) from error


def _decode_review_tool_response(
    response: Mapping[str, Any],
) -> FrameReviewProposal:
    tool_name, decoded = _decode_single_tool_call(response)
    if tool_name != "submit_measurement_review":
        raise ProviderPermanentError(
            "provider selected an unknown Reviewer tool"
        )
    _reject_provider_system_identifiers(decoded)
    try:
        return decode_typed_dataclass(FrameReviewProposal, decoded)
    except (TypeError, ValueError, KeyError) as error:
        raise ProviderPermanentError(
            "Reviewer output violates its typed contract"
        ) from error


def _decode_binding_tool_response(
    response: Mapping[str, Any],
) -> MessageImpactProposal:
    tool_name, decoded = _decode_single_tool_call(response)
    if tool_name != "submit_message_impact":
        raise ProviderPermanentError(
            "provider selected an unknown semantic-binding tool"
        )
    _reject_provider_system_identifiers(decoded)
    try:
        return decode_typed_dataclass(MessageImpactProposal, decoded)
    except (TypeError, ValueError, KeyError) as error:
        raise ProviderPermanentError(
            "message impact violates its typed contract"
        ) from error


def _review_tools() -> list[dict[str, object]]:
    return [
        strict_record_tool(
            name="submit_measurement_review",
            description=(
                "Submit one structured objection-only measurement review."
            ),
            record_type=FrameReviewProposal,
        )
    ]


def _binding_tools() -> list[dict[str, object]]:
    return [
        strict_record_tool(
            name="submit_message_impact",
            description="Bind one user message to typed business semantics.",
            record_type=MessageImpactProposal,
        )
    ]


def _decode_single_tool_call(
    response: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]]:
    try:
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise TypeError
        message = choices[0]["message"]
        tool_calls = message["tool_calls"]
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise TypeError
        function = tool_calls[0]["function"]
        name = function["name"]
        arguments = function["arguments"]
        if not isinstance(name, str) or not isinstance(arguments, str):
            raise TypeError
        decoded = json.loads(arguments)
        if not isinstance(decoded, Mapping):
            raise TypeError
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ProviderPermanentError(
            "provider response must contain exactly one typed tool call"
        ) from error
    return name, decoded


_PROVIDER_FORBIDDEN_ID_FIELDS = {
    "answer_version_id",
    "case_id",
    "checkpoint_id",
    "event_id",
    "frame_revision_id",
    "idempotency_key",
    "operation_id",
    "outbox_message_id",
    "plan_revision_id",
    "question_revision_id",
    "run_id",
}

_CANONICAL_JSON_TRANSPORT_FIELDS = {
    "recommended_interpretation_json",
    "value_json",
}


def _reject_provider_system_identifiers(value: object) -> None:
    if isinstance(value, Mapping):
        forbidden = _PROVIDER_FORBIDDEN_ID_FIELDS.intersection(value)
        if forbidden:
            raise ProviderPermanentError(
                "provider output contains controller-owned identifiers: {}"
                .format(",".join(sorted(forbidden)))
            )
        for key, item in value.items():
            if (
                key in _CANONICAL_JSON_TRANSPORT_FIELDS
                and isinstance(item, str)
            ):
                try:
                    nested = json.loads(item)
                except json.JSONDecodeError:
                    nested = None
                if isinstance(nested, (Mapping, list)):
                    _reject_provider_system_identifiers(nested)
            _reject_provider_system_identifiers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_provider_system_identifiers(item)


def _bind_revise_frame_question_authority(
    payload: Mapping[str, Any],
    question_revision_id: str,
) -> Mapping[str, Any]:
    decoded = dict(payload)
    measurement_design = decoded.get("measurement_design")
    if not isinstance(measurement_design, Mapping):
        raise ProviderPermanentError(
            "revise_frame requires a measurement design"
        )
    question_grounding = measurement_design.get("question_grounding")
    if not isinstance(question_grounding, Mapping):
        raise ProviderPermanentError(
            "measurement design requires question grounding"
        )
    bound_grounding = dict(question_grounding)
    bound_grounding["question_revision_id"] = question_revision_id
    bound_design = dict(measurement_design)
    bound_design["question_grounding"] = bound_grounding
    decoded["measurement_design"] = bound_design
    decoded["question_revision_id"] = question_revision_id
    return decoded


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ProviderConfigurationError(
            "max attempts must be an integer"
        ) from error
    if parsed < 1:
        raise ProviderConfigurationError("max attempts must be positive")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ProviderConfigurationError(
            "timeout must be numeric"
        ) from error
    if parsed <= 0:
        raise ProviderConfigurationError("timeout must be positive")
    return parsed


def _float_value(value: str, field_name: str) -> float:
    try:
        return float(value)
    except ValueError as error:
        raise ProviderConfigurationError(
            "{} must be numeric".format(field_name)
        ) from error


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ProviderConfigurationError("seed must be an integer") from error
    if parsed < 0:
        raise ProviderConfigurationError("seed must be non-negative")
    return parsed


_SYSTEM_INSTRUCTION = """
You are the Primary Business Analysis Agent. Call exactly one of the provided
typed action tools. Use the current AnalysisFrame, WorkPlan, evidence, decisions, reviewer
objections, and recent business events in context_packet. Measurement changes
must use revise_frame. Investigation-task changes use revise_plan. Technical
retries keep the current revisions. ask_user needs two or three business
options, a recommended option, and allow_freeform=true. propose_answer contains
claim-level evidence IDs and applicability boundaries. Do not emit hidden
reasoning, SQL, markdown fences, prompts, verifier internals, or system IDs.
""".strip()


_REVIEWER_SYSTEM_INSTRUCTION = """
You are an independent measurement-design Reviewer. Inspect estimand identity,
population, observation unit, time semantics, window/calendar rules, exposure,
comparison, alternatives, falsification, reversal, and stop conditions. Call
the review tool exactly once. Return structured objections and a disposition.
You may accept a coherent design or request revision/block it. You do not
produce a parallel business answer, SQL, hidden reasoning, or system IDs.
""".strip()


_BINDING_SYSTEM_INSTRUCTION = """
You bind open business language to typed semantics. Determine whether the new
message changes the question or measurement frame, challenges an existing
conclusion, adds context, or asks to stop. Ground every assertion and ambiguity
in exact codepoint spans from message_content. Low-risk inferences may be
accepted. A material ambiguity that changes business meaning must request one
user decision with two or three business options and a recommendation. Encode
assertion values and recommended interpretations as canonical JSON strings.
Call the binding tool once. Do not emit system IDs, SQL, hidden reasoning, or a
business answer.
""".strip()
