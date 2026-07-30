"""Generic HTTPS Chat Completions adapter with provider-layer retry."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from waje_vnext.domain.actions import AgentActionProposal
from waje_vnext.domain.action_codec import (
    ActionProposalDecodeError,
    decode_agent_action_proposal,
)
from waje_vnext.domain.canonical import to_jsonable
from waje_vnext.domain.controller import PrimaryAgentRequest

from .base import (
    ProviderConfigurationError,
    ProviderContractError,
    ProviderPermanentError,
    ProviderTransientError,
)


ENV_PREFIX = "WAJE_VNEXT_LLM_"


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
        return cls(
            provider_name=source.get(ENV_PREFIX + "PROVIDER", "").strip(),
            base_url=source.get(ENV_PREFIX + "BASE_URL", "").strip(),
            api_key=source.get(ENV_PREFIX + "API_KEY", "").strip(),
            model=source.get(ENV_PREFIX + "MODEL", "").strip(),
            max_attempts=attempts,
            timeout_seconds=timeout,
        )

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


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
            data=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
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
    def __init__(
        self,
        settings: ChatCompletionsProviderSettings,
        *,
        transport: ChatTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport or UrllibChatTransport()

    def propose(self, request: PrimaryAgentRequest) -> AgentActionProposal:
        messages = [
            {
                "role": "system",
                "content": _SYSTEM_INSTRUCTION,
            },
            {
                "role": "user",
                "content": json.dumps(
                    to_jsonable(request),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        ]
        payload = {
            "model": self._settings.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        last_error: ProviderTransientError | ProviderContractError | None = None
        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                response = self._transport.post_json(
                    url=self._settings.endpoint,
                    headers={
                        "Authorization": "Bearer {}".format(
                            self._settings.api_key
                        ),
                        "Content-Type": "application/json",
                    },
                    payload=payload,
                    timeout_seconds=self._settings.timeout_seconds,
                )
                try:
                    return _decode_chat_response(response)
                except ProviderContractError as error:
                    last_error = error
                    if attempt == self._settings.max_attempts:
                        break
                    messages = messages + [
                        {
                            "role": "system",
                            "content": _CONTRACT_REPAIR_INSTRUCTION,
                        }
                    ]
                    payload = {**payload, "messages": messages}
            except ProviderTransientError as error:
                last_error = error
                if attempt == self._settings.max_attempts:
                    break
            time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        assert last_error is not None
        if isinstance(last_error, ProviderContractError):
            raise ProviderPermanentError(
                "provider repeatedly violated the typed action contract"
            ) from last_error
        raise last_error


def _decode_chat_response(
    response: Mapping[str, Any],
) -> AgentActionProposal:
    try:
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise TypeError
        message = choices[0]["message"]
        content = message["content"]
        if not isinstance(content, str):
            raise TypeError
        decoded = json.loads(content)
        if not isinstance(decoded, Mapping):
            raise TypeError
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ProviderContractError(
            "provider response does not contain one typed JSON proposal"
        ) from error
    try:
        return decode_agent_action_proposal(decoded)
    except ActionProposalDecodeError as error:
        raise ProviderContractError(
            "provider proposal violates the typed action contract"
        ) from error


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


_SYSTEM_INSTRUCTION = """
You are the Primary Business Analysis Agent. Return exactly one JSON object with
two fields: kind and payload. kind must be one of the allowed_actions in the
request. Use the current AnalysisFrame, WorkPlan, evidence, decisions, reviewer
objections, and recent business events in context_packet. Measurement changes
must use revise_frame. Derive the estimand, population, time scope, comparison
groups, exposure variable, normalization, diagnostics, sensitivities, material
alternatives, falsification, reversal, success, and stop conditions from the
business question and inspected semantic contracts. Do not assume fixed date
windows or a fixed denominator. Treat user premises as hypotheses until probes
support them. Each frame requirement must be covered by a WorkPlan task.
When a recent event agent_result already contains the requested semantic
contracts, use those contracts and do not repeat inspect_semantics.
Investigation-task changes use revise_plan. Probe or sensitivity results that
change population, comparison, exposure, normalization, or estimand require a
new revise_frame action; technical retries keep the current revisions. ask_user
is reserved for a material business choice that cannot be resolved from
semantics or low-cost probes, and needs two or three business options, a
recommended option, and allow_freeform=true. Reasonable choices among
estimands, comparison groups, exposure adjustments, and sensitivities belong
to the Primary Agent: create a provisional Frame and investigate them instead
of asking the user. Ask only when an unavailable business policy, objective, or
definition would materially change acceptance and cannot be tested. propose_answer contains claim-level
evidence IDs and applicability boundaries. Do not emit hidden reasoning, SQL,
markdown fences, prompts, verifier internals, or system IDs.

Use these exact payload shapes and include every listed field:

revise_frame: {
  "revision_reason": string,
  "estimand": string,
  "population": string,
  "time_scope": string,
  "observation_unit": string,
  "primary_estimator": {
    "quantity": string,
    "aggregation": "sum"|"mean"|"ratio"|"rate"|"difference"|"model_based"|"other",
    "numerator": string,
    "denominator": string,
    "exposure_adjustment": "none"|"per_exposure_unit"|"model_adjusted"|"stratified"|"design_equalized"|"other"
  },
  "comparison": {
    "mode": "absolute"|"between_groups"|"within_unit"|"counterfactual",
    "groups": [{
      "group_id": string,
      "label": string,
      "role": "focal"|"reference",
      "membership_rule": string
    }],
    "contrast": string
  },
  "exposure": {
    "variable": string,
    "unit": string,
    "balance_assumption": "unknown"|"expected_equal"|"expected_unequal",
    "sensitivity_adjustments": ["none"|"per_exposure_unit"|"model_adjusted"|"stratified"|"design_equalized"|"other"],
    "normalization_strategy": string,
    "diagnostic_requirement_id": string,
    "sensitivity_requirement_id": string
  },
  "measurement_rationale": string,
  "assumptions": [string],
  "alternatives": [{
    "alternative_id": string,
    "statement": string,
    "requirement_id": string
  }],
  "requirements": [{
    "requirement_id": string,
    "kind": "semantic"|"coverage"|"exposure"|"sensitivity"|"alternative"|"falsification",
    "question": string,
    "success_condition": string,
    "failure_consequence": string
  }],
  "falsification_conditions": [string],
  "reversal_conditions": [string],
  "success_conditions": [string],
  "stop_conditions": [string],
  "decision_record_ids": [string],
  "semantic_contract_refs": [string]
}

revise_plan: {
  "revision_reason": string,
  "tasks": [{
    "task_id": string,
    "business_purpose": string,
    "capability_intent": string,
    "target_claim_ids": [string],
    "requirement_ids": [string],
    "depends_on_task_ids": [string],
    "success_conditions": [string],
    "stop_conditions": [string]
  }]
}

inspect_semantics: {"question": string, "contract_refs": [string]}
run_probe: {"task_id": string, "probe_kind": string, "parameters": object}
call_capability: {
  "task_id": string, "capability_name": string, "parameters": object
}
run_sensitivity: {
  "task_id": string, "variant_label": string, "parameters": object
}
record_interpretation: {
  "evidence_record_ids": [string], "interpretation": string
}
ask_user: {
  "question": string,
  "options": [{"option_id": string, "label": string, "impact": string}],
  "recommended_option_id": string,
  "allow_freeform": true
}
propose_answer: {
  "claims": [{
    "claim_id": string,
    "statement": string,
    "applicability": string,
    "evidence_record_ids": [string],
    "boundary_ref": string|null,
    "limitations": [string]
  }],
  "narrative_markdown": string
}
stop: {"reason": string, "terminal_state": "stopped"|"closed"}

Frame reference invariants:
- exposure.diagnostic_requirement_id references one requirement with kind "exposure"
- exposure.sensitivity_requirement_id references one requirement with kind "sensitivity"
- every alternative.requirement_id references one requirement with kind "alternative"
- when exposure balance is unknown or expected_unequal and the primary estimator
  uses exposure_adjustment "none", sensitivity_adjustments includes at least one
  non-"none" mode
""".strip()


_CONTRACT_REPAIR_INSTRUCTION = """
Your previous response was rejected by the typed action decoder. Return one JSON
object with exactly two top-level fields: "kind" and "payload". Do not use an
"action" wrapper, aliases, comments, markdown, or extra fields. Use one allowed
action and the exact payload shape from the system instruction.
""".strip()
