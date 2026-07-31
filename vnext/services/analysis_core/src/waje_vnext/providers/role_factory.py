"""Fail-closed provider profiles for the three Gate 3 model roles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from waje_vnext.domain.runtime_amendment import ModelExecutionRole

from .chat_completions import (
    ENV_PREFIX,
    ChatCompletionsProvider,
    ChatCompletionsProviderSettings,
    ChatTransport,
    _float_value,
    _nonnegative_int,
    _positive_float,
    _positive_int,
)


@dataclass(frozen=True, slots=True)
class ModelRoleProfile:
    profile_ref: str
    execution_role: ModelExecutionRole
    model_ref: str
    thinking: str


@dataclass(frozen=True, slots=True)
class ModelProviderRoleSet:
    primary: ChatCompletionsProvider
    runtime_reviewer: ChatCompletionsProvider
    evaluation_reviewer: ChatCompletionsProvider

    @property
    def binding(self) -> ChatCompletionsProvider:
        return self.primary


SELECTED_GATE3_ROLE_PROFILES = (
    ModelRoleProfile(
        profile_ref="PRIMARY-DEEPSEEK-PRO-THINK-V1",
        execution_role=(
            ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        ),
        model_ref="deepseek-v4-pro",
        thinking="enabled",
    ),
    ModelRoleProfile(
        profile_ref="RUNTIME-REVIEWER-DEEPSEEK-PRO-NOTHINK-V1",
        execution_role=ModelExecutionRole.RUNTIME_REVIEWER,
        model_ref="deepseek-v4-pro",
        thinking="disabled",
    ),
    ModelRoleProfile(
        profile_ref="EVALUATOR-DEEPSEEK-FLASH-THINK-V1",
        execution_role=ModelExecutionRole.EVALUATION_REVIEWER,
        model_ref="deepseek-v4-flash",
        thinking="enabled",
    ),
)


def build_selected_gate3_role_providers(
    environment: Mapping[str, str],
    *,
    transport: ChatTransport | None = None,
) -> ModelProviderRoleSet:
    provider_ref = environment.get(ENV_PREFIX + "PROVIDER", "").strip()
    base_url = environment.get(ENV_PREFIX + "BASE_URL", "").strip()
    api_key = environment.get(ENV_PREFIX + "API_KEY", "").strip()
    timeout_raw = environment.get(
        ENV_PREFIX + "TIMEOUT_SECONDS",
        "",
    ).strip()
    attempts_raw = environment.get(
        ENV_PREFIX + "MAX_ATTEMPTS",
        "",
    ).strip()
    temperature_raw = environment.get(
        ENV_PREFIX + "TEMPERATURE",
        "1.0",
    ).strip()
    top_p_raw = environment.get(
        ENV_PREFIX + "TOP_P",
        "1.0",
    ).strip()
    seed_raw = environment.get(ENV_PREFIX + "SEED", "").strip()
    timeout = None if not timeout_raw else _positive_float(timeout_raw)
    attempts = 3 if not attempts_raw else _positive_int(attempts_raw)
    temperature = _float_value(temperature_raw, "temperature")
    top_p = _float_value(top_p_raw, "top_p")
    seed = None if not seed_raw else _nonnegative_int(seed_raw)

    providers: dict[ModelExecutionRole, ChatCompletionsProvider] = {}
    for profile in SELECTED_GATE3_ROLE_PROFILES:
        settings = ChatCompletionsProviderSettings(
            provider_name=provider_ref,
            base_url=base_url,
            api_key=api_key,
            model=profile.model_ref,
            thinking=profile.thinking,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            max_attempts=attempts,
            timeout_seconds=timeout,
        )
        providers[profile.execution_role] = ChatCompletionsProvider(
            settings,
            transport=transport,
        )
    identities = {
        provider.configuration_identity(role).configuration_sha256
        for role, provider in providers.items()
    }
    if len(identities) != len(SELECTED_GATE3_ROLE_PROFILES):
        raise ValueError("model role configurations must be independent")
    return ModelProviderRoleSet(
        primary=providers[
            ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        ],
        runtime_reviewer=providers[ModelExecutionRole.RUNTIME_REVIEWER],
        evaluation_reviewer=providers[
            ModelExecutionRole.EVALUATION_REVIEWER
        ],
    )
