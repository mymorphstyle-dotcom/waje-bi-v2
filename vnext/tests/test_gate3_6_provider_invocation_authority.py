from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest

from test_gate2_controller import NOW
from test_gate2_provider import RecordingTransport, reviewer_provider_for
from waje_vnext.controller import ScriptedEffectExecutor, WAJEController
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.controller import ControllerPhase
from waje_vnext.domain.runtime_amendment import (
    ModelConfigurationIdentity,
    ModelExecutionRole,
    ModelInputViewKind,
    ProviderAttemptDisposition,
    ProviderAttemptRequest,
)
from waje_vnext.providers import (
    ChatCompletionsProvider,
    ChatCompletionsProviderSettings,
    build_selected_gate3_role_providers,
    ScriptedPrimaryAgentProvider,
)
from waje_vnext.storage import InMemoryAuthorityStore
from waje_vnext.storage.ports import InvalidAuthorityTransition


def chat_provider(
    transport: RecordingTransport,
) -> ChatCompletionsProvider:
    return ChatCompletionsProvider(
        ChatCompletionsProviderSettings(
            provider_name="deepseek",
            base_url="https://provider.example/v1",
            api_key="secret-value",
            model="deepseek-v4-pro",
            thinking="enabled",
            max_attempts=1,
        ),
        transport=transport,
    )


def controller_for(store, provider, transport) -> WAJEController:
    return WAJEController(
        store=store,
        provider=provider,
        reviewer_provider=reviewer_provider_for(transport),
        effect_executor=ScriptedEffectExecutor(()),
        owner_id="g36-provider-worker",
        clock=lambda: NOW,
    )


class _CrashOnceDuringSuccessStore(InMemoryAuthorityStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_success = True

    def commit_provider_attempt_success(self, *, receipt, result):
        if not self.fail_next_success:
            return super().commit_provider_attempt_success(
                receipt=receipt,
                result=result,
            )
        self.fail_next_success = False
        with self.atomic():
            self._provider_attempt_receipts[
                receipt.provider_attempt_receipt_id
            ] = receipt
            self._durable_model_results[
                result.logical_model_job_id
            ] = result
            raise RuntimeError("simulated crash inside success transaction")


class _CrashAfterRetryableReceiptStore(InMemoryAuthorityStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_after_retryable_receipt = True

    def record_provider_attempt_receipt(self, record):
        persisted = super().record_provider_attempt_receipt(record)
        if (
            self.fail_after_retryable_receipt
            and record.disposition
            is ProviderAttemptDisposition.RETRYABLE_FAILURE
        ):
            self.fail_after_retryable_receipt = False
            raise RuntimeError("simulated crash after retryable receipt")
        return persisted


class _CrashAfterAttemptRequestStore(InMemoryAuthorityStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_after_attempt_request = True

    def record_provider_attempt_request(self, record):
        persisted = super().record_provider_attempt_request(record)
        if self.fail_after_attempt_request:
            self.fail_after_attempt_request = False
            raise RuntimeError("simulated crash after attempt request")
        return persisted


class _DriftingBindingProvider(ChatCompletionsProvider):
    def __init__(self, settings, *, transport) -> None:
        super().__init__(settings, transport=transport)
        self._binding_payload_count = 0

    def _binding_payload(self, request):
        self._binding_payload_count += 1
        payload = super()._binding_payload(request)
        payload = copy.deepcopy(payload)
        payload["messages"][0]["content"] += "\nInjected drift."
        return payload


class _OracleLeakingScriptedProvider(ScriptedPrimaryAgentProvider):
    def describe_invocation(self, **kwargs):
        prepared = super().describe_invocation(**kwargs)
        body = {
            **dict(prepared.request_artifact.provider_request_body),
            "hidden_oracle": {"expected_answer": "forbidden"},
        }
        artifact = replace(
            prepared.request_artifact,
            provider_request_body=body,
            provider_request_sha256=content_sha256(body),
        )
        return replace(prepared, request_artifact=artifact)


class _UnboundConfigurationProvider(ChatCompletionsProvider):
    def configuration_identity(self, execution_role):
        identity = super().configuration_identity(execution_role)
        return ModelConfigurationIdentity.build(
            execution_role=identity.execution_role,
            provider_ref=identity.provider_ref,
            endpoint_ref=identity.endpoint_ref,
            protocol_ref=identity.protocol_ref,
            adapter_release_ref=identity.adapter_release_ref,
            adapter_release_sha256=identity.adapter_release_sha256,
            model_ref=identity.model_ref,
            thinking=identity.thinking,
            stable_parameters={
                **dict(identity.stable_parameters),
                "tool_choice_policy": "auto",
                "unapplied_generation_parameter": "claimed-but-not-sent",
            },
            delivery_policy_ref=identity.delivery_policy_ref,
            max_attempts=identity.max_attempts,
            timeout_seconds=identity.timeout_seconds,
        )


class _OraclePromptProvider(ChatCompletionsProvider):
    @staticmethod
    def _with_oracle(payload):
        changed = copy.deepcopy(to_jsonable(payload))
        changed["messages"][0]["content"] += (
            "\nEvaluator oracle: expected answer is forbidden."
        )
        return changed

    def describe_invocation(self, **kwargs):
        prepared = super().describe_invocation(**kwargs)
        body = self._with_oracle(
            prepared.request_artifact.provider_request_body
        )
        prompt_sha256 = content_sha256(
            {"messages": (body["messages"][0],)}
        )
        artifact = replace(
            prepared.request_artifact,
            prompt_bundle_sha256=prompt_sha256,
            provider_request_body=body,
            provider_request_sha256=content_sha256(body),
        )
        return replace(prepared, request_artifact=artifact)

    def _binding_payload(self, request):
        return self._with_oracle(super()._binding_payload(request))


class _ClaimedEndpointProvider(ChatCompletionsProvider):
    def configuration_identity(self, execution_role):
        identity = super().configuration_identity(execution_role)
        return ModelConfigurationIdentity.build(
            execution_role=identity.execution_role,
            provider_ref=identity.provider_ref,
            endpoint_ref="http://169.254.169.254/latest/meta-data",
            protocol_ref=identity.protocol_ref,
            adapter_release_ref=identity.adapter_release_ref,
            adapter_release_sha256=identity.adapter_release_sha256,
            model_ref=identity.model_ref,
            thinking=identity.thinking,
            stable_parameters=identity.stable_parameters,
            delivery_policy_ref=identity.delivery_policy_ref,
            max_attempts=identity.max_attempts,
            timeout_seconds=identity.timeout_seconds,
        )


class Gate36ProviderInvocationAuthorityTest(unittest.TestCase):
    def test_historical_partial_run_trace_cannot_project_later_results(
        self,
    ) -> None:
        transport = RecordingTransport(case_id="case-g36-stale-trace")
        provider = chat_provider(transport)
        store = InMemoryAuthorityStore()
        controller = controller_for(store, provider, transport)
        controller.start(
            case_id="case-g36-stale-trace",
            thread_id="thread-g36-stale-trace",
            run_id="run-g36-stale-trace",
            user_message="分析昨天收入变化",
        )
        partial_manifest = controller.build_run_trace_manifest(
            "case-g36-stale-trace"
        )
        controller.deliver_pending_message_binding(
            "case-g36-stale-trace"
        )
        job = store.list_logical_model_jobs("case-g36-stale-trace")[0]
        with self.assertRaisesRegex(
            ValueError,
            "run trace .* does not match",
        ):
            store.read_model_execution_trace_records(
                job.logical_model_job_id,
                partial_manifest.trace_manifest_id,
            )

    def test_actual_outbound_body_is_the_durable_request_artifact(self) -> None:
        transport = RecordingTransport(case_id="case-g36-request")
        provider = chat_provider(transport)
        store = InMemoryAuthorityStore()
        controller = controller_for(store, provider, transport)
        controller.start(
            case_id="case-g36-request",
            thread_id="thread-g36-request",
            run_id="run-g36-request",
            user_message="分析昨天收入变化",
        )

        controller.deliver_pending_message_binding("case-g36-request")

        job = store.list_logical_model_jobs("case-g36-request")[0]
        artifact = job.model_request_artifact
        actual = transport.calls[0]
        receipt = store.list_provider_attempt_receipts(
            job.logical_model_job_id
        )[0]
        attempt = store.get_provider_attempt_request(
            receipt.provider_attempt_id
        )
        self.assertEqual(
            store.list_provider_attempt_requests(job.logical_model_job_id),
            (attempt,),
        )
        projected_job, requests, receipts, durable_result = (
            store.read_model_execution_records(job.logical_model_job_id)
        )
        self.assertEqual(projected_job, job)
        self.assertEqual(requests, (attempt,))
        self.assertEqual(receipts, (receipt,))
        self.assertIsNotNone(durable_result)
        trace_manifest = controller.build_run_trace_manifest(
            "case-g36-request"
        )
        traced_projection = store.read_model_execution_trace_records(
            job.logical_model_job_id,
            trace_manifest.trace_manifest_id,
        )
        self.assertEqual(
            traced_projection,
            (
                job,
                (attempt,),
                (receipt,),
                durable_result,
                trace_manifest,
            ),
        )
        self.assertEqual(
            artifact.input_view_kind,
            ModelInputViewKind.MESSAGE_BINDING_VIEW,
        )
        self.assertEqual(
            artifact.execution_role,
            ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT,
        )
        self.assertEqual(
            content_sha256(actual["payload"]),
            artifact.provider_request_sha256,
        )
        self.assertEqual(attempt.request_sha256, artifact.provider_request_sha256)
        self.assertEqual(
            actual["headers"]["Idempotency-Key"],
            attempt.provider_idempotency_key,
        )
        user_payload = json.loads(
            actual["payload"]["messages"][1]["content"]
        )
        self.assertEqual(user_payload["message_content"], "分析昨天收入变化")
        self.assertNotIn("evaluator_oracle", json.dumps(user_payload))
        self.assertNotIn("secret-value", repr(job))

    def test_prompt_or_tool_render_drift_is_rejected_before_transport(self) -> None:
        transport = RecordingTransport(case_id="case-g36-drift")
        provider = _DriftingBindingProvider(
            ChatCompletionsProviderSettings(
                provider_name="deepseek",
                base_url="https://provider.example/v1",
                api_key="secret-value",
                model="deepseek-v4-pro",
                thinking="enabled",
                max_attempts=1,
            ),
            transport=transport,
        )
        store = InMemoryAuthorityStore()
        controller = controller_for(store, provider, transport)
        controller.start(
            case_id="case-g36-drift",
            thread_id="thread-g36-drift",
            run_id="run-g36-drift",
            user_message="分析收入",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "concrete adapter release is not registered",
        ):
            controller.deliver_pending_message_binding("case-g36-drift")
        self.assertEqual(transport.calls, [])

    def test_unbound_configuration_parameter_is_rejected(self) -> None:
        transport = RecordingTransport(case_id="case-g36-config-drift")
        provider = _UnboundConfigurationProvider(
            ChatCompletionsProviderSettings(
                provider_name="deepseek",
                base_url="https://provider.example/v1",
                api_key="secret-value",
                model="deepseek-v4-pro",
                thinking="enabled",
                max_attempts=1,
            ),
            transport=transport,
        )
        store = InMemoryAuthorityStore()
        controller = controller_for(store, provider, transport)
        controller.start(
            case_id="case-g36-config-drift",
            thread_id="thread-g36-config-drift",
            run_id="run-g36-config-drift",
            user_message="分析收入",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "concrete adapter release is not registered",
        ):
            controller.deliver_pending_message_binding(
                "case-g36-config-drift"
            )
        self.assertEqual(transport.calls, [])

    def test_provider_cannot_self_author_an_oracle_prompt(self) -> None:
        transport = RecordingTransport(case_id="case-g36-prompt-oracle")
        provider = _OraclePromptProvider(
            ChatCompletionsProviderSettings(
                provider_name="deepseek",
                base_url="https://provider.example/v1",
                api_key="secret-value",
                model="deepseek-v4-pro",
                thinking="enabled",
                max_attempts=1,
            ),
            transport=transport,
        )
        store = InMemoryAuthorityStore()
        controller = controller_for(store, provider, transport)
        controller.start(
            case_id="case-g36-prompt-oracle",
            thread_id="thread-g36-prompt-oracle",
            run_id="run-g36-prompt-oracle",
            user_message="分析收入",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "concrete adapter release is not registered",
        ):
            controller.deliver_pending_message_binding(
                "case-g36-prompt-oracle"
            )
        self.assertEqual(transport.calls, [])

    def test_durable_endpoint_cannot_escape_sealed_settings(self) -> None:
        transport = RecordingTransport(case_id="case-g36-endpoint")
        provider = _ClaimedEndpointProvider(
            ChatCompletionsProviderSettings(
                provider_name="deepseek",
                base_url="https://actual.example/v1",
                api_key="secret-value",
                model="deepseek-v4-pro",
                thinking="enabled",
                max_attempts=1,
            ),
            transport=transport,
        )
        store = InMemoryAuthorityStore()
        controller = controller_for(store, provider, transport)
        controller.start(
            case_id="case-g36-endpoint",
            thread_id="thread-g36-endpoint",
            run_id="run-g36-endpoint",
            user_message="分析收入",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "concrete adapter release is not registered",
        ):
            controller.deliver_pending_message_binding(
                "case-g36-endpoint"
            )
        self.assertEqual(transport.calls, [])

    def test_role_label_alone_does_not_prove_reviewer_independence(self) -> None:
        settings = ChatCompletionsProviderSettings(
            provider_name="deepseek",
            base_url="https://provider.example/v1",
            api_key="secret-value",
            model="deepseek-v4-pro",
            thinking="enabled",
            max_attempts=1,
        )
        primary = ChatCompletionsProvider(settings)
        reviewer = ChatCompletionsProvider(settings)

        with self.assertRaisesRegex(
            ValueError,
            "distinct, auditable configurations",
        ):
            WAJEController(
                store=InMemoryAuthorityStore(),
                provider=primary,
                reviewer_provider=reviewer,
                effect_executor=ScriptedEffectExecutor(()),
                owner_id="g36-role-label-worker",
                clock=lambda: NOW,
            )

    def test_success_commit_crash_rolls_back_and_fences_replay(self) -> None:
        transport = RecordingTransport(case_id="case-g36-atomic")
        provider = chat_provider(transport)
        store = _CrashOnceDuringSuccessStore()
        controller = controller_for(store, provider, transport)
        waiting = controller.start(
            case_id="case-g36-atomic",
            thread_id="thread-g36-atomic",
            run_id="run-g36-atomic",
            user_message="分析收入",
        )
        job_id = waiting.pending_job_ids[0]

        with self.assertRaisesRegex(
            RuntimeError,
            "simulated crash inside success transaction",
        ):
            controller.deliver_pending_message_binding("case-g36-atomic")
        job = store.get_logical_model_job(job_id)
        self.assertEqual(
            store.list_provider_attempt_receipts(job.logical_model_job_id),
            (),
        )
        self.assertIsNone(
            store.get_durable_model_result(job.logical_model_job_id)
        )
        self.assertEqual(
            controller.resume("case-g36-atomic").phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "outcome is unknown",
        ):
            controller.deliver_pending_message_binding(
                "case-g36-atomic"
            )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            store.list_provider_attempt_receipts(
                job.logical_model_job_id
            ),
            (),
        )
        self.assertIsNone(
            store.get_durable_model_result(job.logical_model_job_id)
        )

    def test_attempt_cannot_change_request_or_configuration(self) -> None:
        transport = RecordingTransport(case_id="case-g36-attempt-drift")
        store = InMemoryAuthorityStore()
        controller = controller_for(store, chat_provider(transport), transport)
        controller.start(
            case_id="case-g36-attempt-drift",
            thread_id="thread-g36-attempt-drift",
            run_id="run-g36-attempt-drift",
            user_message="分析收入",
        )
        controller.deliver_pending_message_binding(
            "case-g36-attempt-drift"
        )
        job = store.list_logical_model_jobs(
            "case-g36-attempt-drift"
        )[0]
        receipt = store.list_provider_attempt_receipts(
            job.logical_model_job_id
        )[0]

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "drifted from logical job",
        ):
            store.record_provider_attempt_request(
                ProviderAttemptRequest(
                    provider_attempt_id="forged-attempt",
                    logical_model_job_id=job.logical_model_job_id,
                    attempt_number=2,
                    prior_provider_attempt_id=receipt.provider_attempt_id,
                    provider_idempotency_key="forged-idempotency-key",
                    request_sha256="0" * 64,
                    model_request_artifact_sha256=(
                        job.model_request_artifact_sha256
                    ),
                    configuration_sha256=job.configuration_sha256,
                    requested_at=NOW,
                )
            )

    def test_retryable_receipt_resumes_at_next_durable_attempt(self) -> None:
        transport = RecordingTransport(
            case_id="case-g36-retry-resume",
            fail_once=True,
        )
        provider = ChatCompletionsProvider(
            ChatCompletionsProviderSettings(
                provider_name="deepseek",
                base_url="https://provider.example/v1",
                api_key="secret-value",
                model="deepseek-v4-pro",
                thinking="enabled",
                max_attempts=3,
            ),
            transport=transport,
        )
        store = _CrashAfterRetryableReceiptStore()
        controller = controller_for(store, provider, transport)
        waiting = controller.start(
            case_id="case-g36-retry-resume",
            thread_id="thread-g36-retry-resume",
            run_id="run-g36-retry-resume",
            user_message="分析收入",
        )
        job_id = waiting.pending_job_ids[0]

        with self.assertRaisesRegex(
            RuntimeError,
            "simulated crash after retryable receipt",
        ):
            controller.deliver_pending_message_binding(
                "case-g36-retry-resume"
            )
        controller.deliver_pending_message_binding(
            "case-g36-retry-resume"
        )

        receipts = store.list_provider_attempt_receipts(job_id)
        attempts = store.list_provider_attempt_requests(job_id)
        projected = store.read_model_execution_records(job_id)
        self.assertEqual(
            projected[1:],
            (attempts, receipts, store.get_durable_model_result(job_id)),
        )
        self.assertEqual(
            tuple(attempt.attempt_number for attempt in attempts),
            (1, 2),
        )
        self.assertEqual(
            attempts[1].prior_provider_attempt_id,
            attempts[0].provider_attempt_id,
        )
        self.assertNotEqual(
            transport.calls[0]["headers"]["Idempotency-Key"],
            transport.calls[1]["headers"]["Idempotency-Key"],
        )
        result = store.get_durable_model_result(job_id)
        self.assertIsNotNone(result)
        self.assertEqual(
            result.provider_attempt_id,
            attempts[1].provider_attempt_id,
        )

    def test_unreceipted_attempt_fences_automatic_retry(self) -> None:
        transport = RecordingTransport(case_id="case-g36-outcome-unknown")
        provider = ChatCompletionsProvider(
            ChatCompletionsProviderSettings(
                provider_name="deepseek",
                base_url="https://provider.example/v1",
                api_key="secret-value",
                model="deepseek-v4-pro",
                thinking="enabled",
                max_attempts=3,
            ),
            transport=transport,
        )
        store = _CrashAfterAttemptRequestStore()
        controller = controller_for(store, provider, transport)
        controller.start(
            case_id="case-g36-outcome-unknown",
            thread_id="thread-g36-outcome-unknown",
            run_id="run-g36-outcome-unknown",
            user_message="分析收入",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "simulated crash after attempt request",
        ):
            controller.deliver_pending_message_binding(
                "case-g36-outcome-unknown"
            )
        self.assertEqual(transport.calls, [])
        with self.assertRaisesRegex(
            RuntimeError,
            "outcome is unknown",
        ):
            controller.deliver_pending_message_binding(
                "case-g36-outcome-unknown"
            )
        self.assertEqual(transport.calls, [])

    def test_clean_view_label_cannot_hide_oracle_payload(self) -> None:
        store = InMemoryAuthorityStore()
        provider = _OracleLeakingScriptedProvider(())
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="g36-oracle-attack-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-g36-oracle-attack",
            thread_id="thread-g36-oracle-attack",
            run_id="run-g36-oracle-attack",
            user_message="分析收入",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "test provider request differs from its typed input",
        ):
            controller.deliver_pending_message_binding(
                "case-g36-oracle-attack"
            )

    def test_selected_role_factory_builds_three_independent_profiles(self) -> None:
        roles = build_selected_gate3_role_providers(
            {
                "WAJE_VNEXT_LLM_PROVIDER": "deepseek",
                "WAJE_VNEXT_LLM_BASE_URL": "https://provider.example/v1",
                "WAJE_VNEXT_LLM_API_KEY": "one-shared-account-secret",
            }
        )
        primary = roles.primary.configuration_identity(
            ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        )
        runtime = roles.runtime_reviewer.configuration_identity(
            ModelExecutionRole.RUNTIME_REVIEWER
        )
        evaluator = roles.evaluation_reviewer.configuration_identity(
            ModelExecutionRole.EVALUATION_REVIEWER
        )
        self.assertEqual(
            (primary.model_ref, primary.thinking),
            ("deepseek-v4-pro", "enabled"),
        )
        self.assertEqual(
            (runtime.model_ref, runtime.thinking),
            ("deepseek-v4-pro", "disabled"),
        )
        self.assertEqual(
            (evaluator.model_ref, evaluator.thinking),
            ("deepseek-v4-flash", "enabled"),
        )
        self.assertEqual(
            len(
                {
                    primary.configuration_sha256,
                    runtime.configuration_sha256,
                    evaluator.configuration_sha256,
                }
            ),
            3,
        )
        self.assertNotIn("one-shared-account-secret", repr(roles))


if __name__ == "__main__":
    unittest.main()
