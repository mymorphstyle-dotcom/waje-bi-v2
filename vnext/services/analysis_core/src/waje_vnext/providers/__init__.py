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

__all__ = [
    "ChatCompletionsProvider",
    "ChatCompletionsProviderSettings",
    "ChatTransport",
    "PrimaryAgentProvider",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderPermanentError",
    "ProviderTransientError",
    "ScriptedPrimaryAgentProvider",
    "UrllibChatTransport",
    "decode_agent_action_proposal",
]
