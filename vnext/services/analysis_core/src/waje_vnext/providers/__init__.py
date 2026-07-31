"""Primary Agent provider adapters."""

from .base import (
    PrimaryAgentProvider,
    ProviderConfigurationError,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
)
from .chat_completions import (
    ChatCompletionsProvider,
    ChatCompletionsProviderSettings,
    ChatTransport,
    UrllibChatTransport,
)
from waje_vnext.domain.action_codec import decode_agent_action_proposal
from .fake import ScriptedPrimaryAgentProvider
from .role_factory import (
    ModelProviderRoleSet,
    ModelRoleProfile,
    SELECTED_GATE3_ROLE_PROFILES,
    build_selected_gate3_role_providers,
)

__all__ = [
    "ChatCompletionsProvider",
    "ChatCompletionsProviderSettings",
    "ChatTransport",
    "PrimaryAgentProvider",
    "ModelProviderRoleSet",
    "ModelRoleProfile",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderPermanentError",
    "ProviderTransientError",
    "ScriptedPrimaryAgentProvider",
    "SELECTED_GATE3_ROLE_PROFILES",
    "UrllibChatTransport",
    "decode_agent_action_proposal",
    "build_selected_gate3_role_providers",
]
