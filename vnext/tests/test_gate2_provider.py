from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.test_gate2_controller import NOW, frame_proposal
from waje_vnext.controller import ScriptedEffectExecutor, WAJEController
from waje_vnext.domain.canonical import to_jsonable
from waje_vnext.providers import (
    ChatCompletionsProvider,
    ChatCompletionsProviderSettings,
    ProviderConfigurationError,
    ProviderTransientError,
    ProviderPermanentError,
)
from waje_vnext.storage import InMemoryAuthorityStore


class RecordingTransport:
    def __init__(
        self,
        *,
        fail_once: bool = False,
        malformed: bool = False,
    ) -> None:
        self.fail_once = fail_once
        self.malformed = malformed
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        *,
        url,
        headers,
        payload,
        timeout_seconds,
    ):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.fail_once:
            self.fail_once = False
            raise ProviderTransientError("retryable transport failure")
        if self.malformed:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"kind":"revise_frame","extra":true}'
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": __import__("json").dumps(
                            to_jsonable(frame_proposal()),
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }


class Gate2ProviderAdapterTest(unittest.TestCase):
    def test_https_adapter_drives_controller_with_no_default_timeout(self) -> None:
        transport = RecordingTransport()
        settings = ChatCompletionsProviderSettings(
            provider_name="contract-provider",
            base_url="https://provider.example/v1",
            api_key="secret-value",
            model="business-analysis-model",
            max_attempts=1,
            timeout_seconds=None,
        )
        provider = ChatCompletionsProvider(
            settings,
            transport=transport,
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="provider-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-provider",
            thread_id="thread-provider",
            run_id="run-provider",
            user_message="定义经营问题的测量口径",
        )

        controller.advance("case-provider")

        self.assertIsNotNone(
            store.get_case("case-provider").accepted_frame_revision_id
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertIsNone(transport.calls[0]["timeout_seconds"])
        request_payload = transport.calls[0]["payload"]
        self.assertNotIn("max_tokens", request_payload)
        self.assertNotIn("secret-value", repr(settings))

    def test_transient_retry_is_centralized_in_provider(self) -> None:
        transport = RecordingTransport(fail_once=True)
        provider = ChatCompletionsProvider(
            ChatCompletionsProviderSettings(
                provider_name="contract-provider",
                base_url="https://provider.example/v1",
                api_key="secret-value",
                model="business-analysis-model",
                max_attempts=2,
            ),
            transport=transport,
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="provider-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-provider-retry",
            thread_id="thread-provider-retry",
            run_id="run-provider-retry",
            user_message="定义经营问题的测量口径",
        )

        with patch(
            "waje_vnext.providers.chat_completions.time.sleep"
        ) as sleep:
            controller.advance("case-provider-retry")

        self.assertEqual(len(transport.calls), 2)
        sleep.assert_called_once()

    def test_adapter_reads_only_vnext_provider_environment(self) -> None:
        with self.assertRaises(ProviderConfigurationError):
            ChatCompletionsProviderSettings.from_env(
                {
                    "OPENAI_API_KEY": "legacy-or-unscoped-key",
                    "OPENAI_MODEL": "legacy-model",
                }
            )

    def test_malformed_typed_output_uses_provider_error_taxonomy(self) -> None:
        provider = ChatCompletionsProvider(
            ChatCompletionsProviderSettings(
                provider_name="contract-provider",
                base_url="https://provider.example/v1",
                api_key="secret-value",
                model="business-analysis-model",
                max_attempts=1,
            ),
            transport=RecordingTransport(malformed=True),
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="provider-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-provider-malformed",
            thread_id="thread-provider-malformed",
            run_id="run-provider-malformed",
            user_message="定义测量",
        )
        with self.assertRaises(ProviderPermanentError):
            controller.advance("case-provider-malformed")
        with self.assertRaisesRegex(
            ProviderConfigurationError,
            "timeout must be positive",
        ):
            ChatCompletionsProviderSettings.from_env(
                {
                    "WAJE_VNEXT_LLM_PROVIDER": "provider",
                    "WAJE_VNEXT_LLM_BASE_URL": "https://provider.example/v1",
                    "WAJE_VNEXT_LLM_API_KEY": "secret",
                    "WAJE_VNEXT_LLM_MODEL": "model",
                    "WAJE_VNEXT_LLM_TIMEOUT_SECONDS": "0",
                }
            )


if __name__ == "__main__":
    unittest.main()
