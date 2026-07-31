from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests.test_gate2_controller import NOW, frame_proposal
from waje_vnext.controller import ScriptedEffectExecutor, WAJEController
from waje_vnext.domain.canonical import to_jsonable
from waje_vnext.domain.controller import ControllerPhase
from waje_vnext.domain.runtime_amendment import (
    FrameReviewDisposition,
    FrameReviewProposal,
    MessageBindingDisposition,
    MessageImpactKind,
    MessageImpactProposal,
    JobDisposition,
    ProviderAttemptDisposition,
    ProposedSemanticAssertion,
    SemanticAssertionKind,
)
from waje_vnext.providers import (
    ChatCompletionsProvider,
    ChatCompletionsProviderSettings,
    ProviderConfigurationError,
    ProviderTransientError,
)
from waje_vnext.storage import InMemoryAuthorityStore


class RecordingTransport:
    def __init__(
        self,
        *,
        case_id: str = "case-provider",
        fail_once: bool = False,
        malformed: bool = False,
    ) -> None:
        self.case_id = case_id
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
        tool_name = payload["tools"][0]["function"]["name"]
        if self.fail_once:
            self.fail_once = False
            raise ProviderTransientError("retryable transport failure")
        if self.malformed and tool_name != "submit_message_impact":
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "submit_revise_frame",
                                        "arguments": (
                                            '{"unexpected":true}'
                                        ),
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        if tool_name == "submit_message_impact":
            request = __import__("json").loads(
                payload["messages"][1]["content"]
            )
            message_content = request["message_content"]
            arguments = to_jsonable(
                MessageImpactProposal(
                    impact_kind=MessageImpactKind.QUESTION_REVISION,
                    disposition=MessageBindingDisposition.ACCEPTED,
                    assertions=(
                        ProposedSemanticAssertion(
                            kind=(
                                SemanticAssertionKind.BUSINESS_CONSTRAINT
                            ),
                            value_json=__import__("json").dumps(
                                {
                                    "business_request": message_content,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            source_start_codepoint=0,
                            source_end_codepoint=len(message_content),
                            material=True,
                        ),
                    ),
                    ambiguities=(),
                    clarification_options=(),
                    recommended_option_id=None,
                )
            )
        elif tool_name == "submit_measurement_review":
            arguments = to_jsonable(
                FrameReviewProposal(
                    disposition=FrameReviewDisposition.ACCEPT,
                    objections=(),
                    review_summary="Measurement design is coherent.",
                )
            )
        else:
            proposal = to_jsonable(frame_proposal(self.case_id))
            arguments = proposal["payload"]
            arguments.pop("question_revision_id")
            arguments["measurement_design"][
                "question_grounding"
            ].pop("question_revision_id")
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": (
                                        tool_name
                                        if tool_name
                                        in {
                                            "submit_message_impact",
                                            "submit_measurement_review",
                                        }
                                        else "submit_revise_frame"
                                    ),
                                    "arguments": __import__("json").dumps(
                                        arguments,
                                        ensure_ascii=False,
                                    ),
                                }
                            }
                        ]
                    }
                }
            ]
        }


def reviewer_provider_for(
    transport: RecordingTransport,
) -> ChatCompletionsProvider:
    return ChatCompletionsProvider(
        ChatCompletionsProviderSettings(
            provider_name="contract-reviewer",
            base_url="https://provider.example/v1",
            api_key="reviewer-secret-value",
            model="measurement-reviewer-model",
            max_attempts=1,
            timeout_seconds=None,
        ),
        transport=transport,
    )


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
        reviewer_provider = reviewer_provider_for(transport)
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=provider,
            reviewer_provider=reviewer_provider,
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

        controller.deliver_pending_message_binding("case-provider")
        controller.advance("case-provider")
        controller.deliver_pending_llm("case-provider")
        controller.deliver_pending_frame_review("case-provider")

        self.assertIsNotNone(
            store.get_case("case-provider").accepted_frame_revision_id
        )
        self.assertEqual(len(transport.calls), 3)
        self.assertIsNone(transport.calls[0]["timeout_seconds"])
        request_payload = transport.calls[1]["payload"]
        self.assertNotIn("max_tokens", request_payload)
        self.assertNotIn("response_format", request_payload)
        self.assertFalse(request_payload["parallel_tool_calls"])
        self.assertEqual(
            request_payload["thinking"],
            {"type": "disabled"},
        )
        self.assertTrue(
            all(
                tool["function"]["strict"]
                for tool in request_payload["tools"]
            )
        )
        revise_frame_tool = next(
            tool
            for tool in request_payload["tools"]
            if tool["function"]["name"] == "submit_revise_frame"
        )
        definitions = revise_frame_tool["function"]["parameters"][
            "$defs"
        ]
        self.assertNotIn(
            "question_revision_id",
            definitions["ReviseFramePayload"]["properties"],
        )
        self.assertNotIn(
            "question_revision_id",
            definitions["QuestionGrounding"]["properties"],
        )
        review_request = json.loads(
            transport.calls[2]["payload"]["messages"][1]["content"]
        )
        self.assertEqual(
            review_request["reviewer_configuration_ref"],
            reviewer_provider.configuration_ref,
        )
        self.assertNotIn("secret-value", repr(settings))

    def test_thinking_mode_is_requested_and_part_of_configuration_identity(
        self,
    ) -> None:
        disabled = ChatCompletionsProviderSettings.from_env(
            {
                "WAJE_VNEXT_LLM_PROVIDER": "deepseek",
                "WAJE_VNEXT_LLM_BASE_URL": "https://provider.example/v1",
                "WAJE_VNEXT_LLM_API_KEY": "secret",
                "WAJE_VNEXT_LLM_MODEL": "deepseek-v4-pro",
                "WAJE_VNEXT_LLM_THINKING": "disabled",
            }
        )
        enabled = ChatCompletionsProviderSettings.from_env(
            {
                "WAJE_VNEXT_LLM_PROVIDER": "deepseek",
                "WAJE_VNEXT_LLM_BASE_URL": "https://provider.example/v1",
                "WAJE_VNEXT_LLM_API_KEY": "secret",
                "WAJE_VNEXT_LLM_MODEL": "deepseek-v4-pro",
                "WAJE_VNEXT_LLM_THINKING": "enabled",
            }
        )
        self.assertNotEqual(
            ChatCompletionsProvider(disabled).configuration_ref,
            ChatCompletionsProvider(enabled).configuration_ref,
        )
        with self.assertRaisesRegex(
            ProviderConfigurationError,
            "thinking must be enabled or disabled",
        ):
            ChatCompletionsProviderSettings(
                provider_name="deepseek",
                base_url="https://provider.example/v1",
                api_key="secret",
                model="deepseek-v4-pro",
                thinking="sometimes",
            )

    def test_transient_retry_is_centralized_in_provider(self) -> None:
        transport = RecordingTransport(
            case_id="case-provider-retry",
            fail_once=True,
        )
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
            reviewer_provider=reviewer_provider_for(transport),
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
            controller.deliver_pending_message_binding(
                "case-provider-retry"
            )
            controller.advance("case-provider-retry")
            controller.deliver_pending_llm("case-provider-retry")

        self.assertEqual(len(transport.calls), 3)
        sleep.assert_called_once()
        jobs = store.list_logical_model_jobs(
            "case-provider-retry"
        )
        self.assertEqual(len(jobs), 2)
        attempts = store.list_provider_attempt_receipts(
            next(
                item.logical_model_job_id
                for item in jobs
                if item.role == "message_binding"
            )
        )
        self.assertEqual(
            tuple(item.disposition for item in attempts),
            (
                ProviderAttemptDisposition.RETRYABLE_FAILURE,
                ProviderAttemptDisposition.SUCCEEDED,
            ),
        )

    def test_adapter_reads_only_vnext_provider_environment(self) -> None:
        with self.assertRaises(ProviderConfigurationError):
            ChatCompletionsProviderSettings.from_env(
                {
                    "OPENAI_API_KEY": "legacy-or-unscoped-key",
                    "OPENAI_MODEL": "legacy-model",
                }
            )

    def test_malformed_typed_output_uses_provider_error_taxonomy(self) -> None:
        transport = RecordingTransport(malformed=True)
        provider = ChatCompletionsProvider(
            ChatCompletionsProviderSettings(
                provider_name="contract-provider",
                base_url="https://provider.example/v1",
                api_key="secret-value",
                model="business-analysis-model",
                max_attempts=1,
            ),
            transport=transport,
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=provider,
            reviewer_provider=reviewer_provider_for(transport),
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
        controller.deliver_pending_message_binding(
            "case-provider-malformed"
        )
        controller.advance("case-provider-malformed")
        job_id = controller.resume(
            "case-provider-malformed"
        ).pending_job_ids[0]
        blocked = controller.deliver_pending_llm(
            "case-provider-malformed"
        )
        self.assertEqual(blocked.phase, ControllerPhase.BLOCKED)
        self.assertEqual(
            store.get_job_disposition(job_id).disposition,
            JobDisposition.TERMINAL_FAILURE,
        )
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
