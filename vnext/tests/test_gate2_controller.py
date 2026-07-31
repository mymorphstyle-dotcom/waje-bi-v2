from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier

from gate1_fixtures import make_measurement_design
from waje_vnext.controller import (
    ControllerConflict,
    EffectExecutionResult,
    EffectTransientError,
    ScriptedEffectExecutor,
    WAJEController,
)
from waje_vnext.domain.actions import (
    ActionEnvelope,
    ActionKind,
    AgentActionProposal,
    AskUserPayload,
    CallCapabilityPayload,
    ProposeAnswerPayload,
    ProposedClaim,
    ReviseFramePayload,
    RevisePlanPayload,
)
from waje_vnext.domain.authority import (
    AnswerStatus,
    ClaimVerifierStatus,
    DecisionOption,
    WorkTask,
)
from waje_vnext.domain.async_runtime import MailboxMessageKind
from waje_vnext.domain.controller import (
    ControllerPhase,
    EffectAttemptStatus,
    PersistedAction,
)
from waje_vnext.domain.events import JournalEventType
from waje_vnext.providers import (
    ChatCompletionsProviderSettings,
    ScriptedPrimaryAgentProvider,
)
from waje_vnext.storage import (
    AuthorityConflict,
    AuthorityNotFound,
    InMemoryAuthorityStore,
    LeaseConflict,
    LeaseFenceLost,
)


NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def frame_proposal(
    case_id: str = "case-gate2",
) -> AgentActionProposal:
    question_id = f"{case_id}:question:1"
    return AgentActionProposal(
        kind=ActionKind.REVISE_FRAME,
        payload=ReviseFramePayload(
            question_revision_id=question_id,
            revision_reason_ref="reason:define-measurement-before-query",
            measurement_design=make_measurement_design(
                question_id=question_id,
                include_source_span=False,
            ),
        ),
    )


def plan_proposal() -> AgentActionProposal:
    return AgentActionProposal(
        kind=ActionKind.REVISE_PLAN,
        payload=RevisePlanPayload(
            revision_reason="Investigate the accepted measurement",
            tasks=(
                WorkTask(
                    task_id="task-pattern",
                    business_purpose="Measure the within-month pattern",
                    capability_intent="periodic pattern comparison",
                    target_claim_ids=("claim-pattern",),
                    depends_on_task_ids=(),
                    success_conditions=("Comparable windows are measured",),
                    stop_conditions=("Coverage is insufficient",),
                ),
            ),
        ),
    )


def capability_proposal() -> AgentActionProposal:
    return AgentActionProposal(
        kind=ActionKind.CALL_CAPABILITY,
        payload=CallCapabilityPayload(
            task_id="task-pattern",
            capability_name="periodic_pattern_compare",
            parameters={"metric": "paid_amount"},
        ),
    )


def answer_proposal() -> AgentActionProposal:
    return AgentActionProposal(
        kind=ActionKind.PROPOSE_ANSWER,
        payload=ProposeAnswerPayload(
            claims=(
                ProposedClaim(
                    claim_id="claim-boundary",
                    statement="The completed probe supports a bounded result",
                    applicability="Accepted frame and plan",
                    evidence_record_ids=(),
                    boundary_ref="effect result pending EvidenceRecord in Gate 4",
                    limitations=("Capability evidence materialization is pending",),
                ),
            ),
            narrative_markdown="The current answer remains bounded and provisional.",
        ),
    )


def ask_user_proposal() -> AgentActionProposal:
    return AgentActionProposal(
        kind=ActionKind.ASK_USER,
        payload=AskUserPayload(
            question="Which business time boundary should govern the comparison?",
            options=(
                DecisionOption(
                    option_id="calendar",
                    label="Calendar month",
                    impact="Uses calendar-month completeness",
                ),
                DecisionOption(
                    option_id="billing",
                    label="Billing cycle",
                    impact="Uses account billing boundaries",
                ),
            ),
            recommended_option_id="calendar",
        ),
    )


def complete_agent_turn(
    controller: WAJEController,
    case_id: str,
):
    waiting = controller.advance(case_id)
    if waiting.phase is not ControllerPhase.WAITING_FOR_LLM:
        return waiting
    return controller.deliver_pending_llm(case_id)


class Gate2ControllerTest(unittest.TestCase):
    def test_persisted_action_binds_the_exact_business_proposal(self) -> None:
        proposal = frame_proposal("case-binding")
        action = ActionEnvelope(
            action_id="action-binding",
            case_id="case-binding",
            kind=proposal.kind,
            expected_head_version=0,
            idempotency_key="action-binding-key",
            issued_at=NOW,
            payload=proposal.payload,
        )
        with self.assertRaisesRegex(ValueError, "business proposal"):
            PersistedAction(
                action=action,
                proposal_sha256="0" * 64,
                recorded_at=NOW,
            )

    def test_in_memory_secondary_uniqueness_matches_postgres(self) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider((ask_user_proposal(),)),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-secondary-uniqueness",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-secondary-uniqueness",
            thread_id="thread-secondary-uniqueness",
            run_id="run-secondary-uniqueness",
            user_message="确认业务时间边界",
        )
        waiting = complete_agent_turn(
            controller,
            "case-secondary-uniqueness",
        )
        request = store.get_decision_request(
            waiting.pending_decision_request_id or ""
        )
        persisted = store.get_action(request.action_id)
        duplicate_action = replace(
            persisted,
            action=replace(
                persisted.action,
                action_id="action-secondary-duplicate",
                operation=replace(
                    persisted.action.operation,
                    operation_id="operation-secondary-duplicate",
                ),
            ),
        )
        with self.assertRaises(AuthorityConflict):
            store.record_action(duplicate_action)
        with self.assertRaises(AuthorityConflict):
            store.record_decision_request(
                replace(
                    request,
                    decision_request_id="decision-request-secondary-duplicate",
                )
            )
        checkpoint = store.latest_checkpoint(
            "case-secondary-uniqueness"
        )
        assert checkpoint is not None
        with self.assertRaises(AuthorityConflict):
            store.record_checkpoint(
                replace(
                    checkpoint,
                    checkpoint_id="checkpoint-secondary-duplicate",
                )
            )

    def test_dynamic_loop_retry_and_resume_preserve_authority(self) -> None:
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider(
            (
                frame_proposal(),
                plan_proposal(),
                capability_proposal(),
                answer_proposal(),
            )
        )
        effects = ScriptedEffectExecutor(
            (
                EffectTransientError("warehouse temporarily unavailable"),
                EffectExecutionResult(
                    payload={"rows": 24, "direction": "higher"},
                    business_summary="Comparable windows were measured",
                ),
            )
        )
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=effects,
            owner_id="worker-1",
            clock=lambda: NOW,
        )

        state = controller.start(
            case_id="case-gate2",
            thread_id="thread-gate2",
            run_id="run-gate2",
            user_message="月初付费金额是否更高？",
        )
        self.assertEqual(state.phase, ControllerPhase.READY_FOR_AGENT)

        state = complete_agent_turn(controller, "case-gate2")
        frame_id = store.get_case("case-gate2").accepted_frame_revision_id
        self.assertIsNotNone(frame_id)
        state = complete_agent_turn(controller, "case-gate2")
        plan_id = store.get_case("case-gate2").accepted_plan_revision_id
        self.assertIsNotNone(plan_id)
        self.assertIsNotNone(
            provider.requests[1].context_packet.accepted_frame_payload
        )

        state = complete_agent_turn(controller, "case-gate2")
        self.assertEqual(state.phase, ControllerPhase.WAITING_FOR_EFFECT)
        head_before_retry = store.get_case("case-gate2").head_version

        state = controller.deliver_pending_effect("case-gate2")
        self.assertEqual(state.phase, ControllerPhase.WAITING_FOR_EFFECT)
        self.assertEqual(
            store.get_case("case-gate2").head_version,
            head_before_retry,
        )
        self.assertEqual(
            store.get_case("case-gate2").accepted_frame_revision_id,
            frame_id,
        )
        self.assertEqual(
            store.get_case("case-gate2").accepted_plan_revision_id,
            plan_id,
        )

        state = controller.deliver_pending_effect("case-gate2")
        self.assertEqual(state.phase, ControllerPhase.READY_FOR_AGENT)
        with self.assertRaisesRegex(ControllerConflict, "no pending effect"):
            controller.deliver_pending_effect("case-gate2")
        state = complete_agent_turn(controller, "case-gate2")
        self.assertEqual(state.phase, ControllerPhase.COMPLETED)

        answer = store.get_answer(state.accepted_answer_version_id or "")
        self.assertEqual(answer.status, AnswerStatus.PROVISIONAL)
        self.assertEqual(
            answer.claims[0].verifier_status,
            ClaimVerifierStatus.PENDING,
        )

        replacement = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-2",
            clock=lambda: NOW,
        )
        self.assertEqual(
            replacement.resume("case-gate2").content_sha256,
            state.content_sha256,
        )

        event_types = tuple(
            event.event_type for event in store.list_events("case-gate2")
        )
        self.assertIn(JournalEventType.EFFECT_ATTEMPT_FAILED, event_types)
        self.assertIn(JournalEventType.EFFECT_COMPLETED, event_types)
        business_events = provider.requests[-1].context_packet.recent_events
        self.assertTrue(
            any(
                event.event_type == JournalEventType.EFFECT_COMPLETED.value
                for event in business_events
            )
        )
        completed_event = next(
            event
            for event in business_events
            if event.event_type == JournalEventType.EFFECT_COMPLETED.value
        )
        self.assertEqual(
            completed_event.business_projection["result"]["rows"],
            24,
        )
        self.assertFalse(
            any(
                event.event_type
                == JournalEventType.EFFECT_ATTEMPT_FAILED.value
                for event in business_events
            )
        )

    def test_ask_user_freeform_resumes_through_decision_record(self) -> None:
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider((ask_user_proposal(),))
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-1",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-decision",
            thread_id="thread-decision",
            run_id="run-decision",
            user_message="比较经营表现",
        )
        waiting = complete_agent_turn(controller, "case-decision")
        self.assertEqual(waiting.phase, ControllerPhase.WAITING_FOR_USER)

        resumed = controller.submit_user_decision(
            "case-decision",
            freeform_response="按自然月，但排除未结束月份",
        )
        self.assertEqual(resumed.phase, ControllerPhase.READY_FOR_AGENT)
        decision = store.list_decisions("case-decision")[0]
        self.assertIsNone(decision.selected_option_id)
        self.assertEqual(
            decision.freeform_response,
            "按自然月，但排除未结束月份",
        )
        packet = store.get_context_packet(resumed.context_packet_id)
        self.assertEqual(
            packet.decision_index[0].freeform_response,
            "按自然月，但排除未结束月份",
        )

    def test_atomic_failure_rolls_back_action_and_can_retry(self) -> None:
        class FailingCheckpointStore(InMemoryAuthorityStore):
            fail_next_checkpoint = False

            def record_checkpoint(self, checkpoint):
                recorded = super().record_checkpoint(checkpoint)
                if self.fail_next_checkpoint:
                    self.fail_next_checkpoint = False
                    raise RuntimeError("simulated crash before commit")
                return recorded

        store = FailingCheckpointStore()
        proposal = frame_proposal("case-crash")
        provider = ScriptedPrimaryAgentProvider((proposal, proposal))
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-1",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-crash",
            thread_id="thread-crash",
            run_id="run-crash",
            user_message="定义测量",
        )
        scheduled = controller.advance("case-crash")
        self.assertEqual(scheduled.phase, ControllerPhase.WAITING_FOR_LLM)
        store.fail_next_checkpoint = True
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            controller.deliver_pending_llm("case-crash")
        self.assertEqual(
            controller.resume("case-crash").content_sha256,
            scheduled.content_sha256,
        )
        self.assertIsNone(
            store.get_case("case-crash").accepted_frame_revision_id
        )
        with self.assertRaises(AuthorityNotFound):
            store.get_action(
                "action-{}".format(
                    __import__("hashlib")
                    .sha256(
                        "\x1f".join(
                            (
                                "run-crash",
                                "1",
                                proposal.content_sha256,
                            )
                        ).encode("utf-8")
                    )
                    .hexdigest()[:24]
                )
            )

        recovered = controller.deliver_pending_llm("case-crash")
        self.assertEqual(recovered.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertIsNotNone(
            store.get_case("case-crash").accepted_frame_revision_id
        )

    def test_plan_before_frame_is_audited_and_rejected(self) -> None:
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider(
            (plan_proposal(), frame_proposal("case-rejection"))
        )
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-1",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-rejection",
            thread_id="thread-rejection",
            run_id="run-rejection",
            user_message="先做调查",
        )
        rejected = complete_agent_turn(controller, "case-rejection")
        self.assertEqual(rejected.consecutive_rejections, 1)
        self.assertEqual(store.get_case("case-rejection").head_version, 1)
        self.assertIn(
            JournalEventType.ACTION_REJECTED,
            tuple(
                event.event_type
                for event in store.list_events("case-rejection")
            ),
        )
        accepted = complete_agent_turn(controller, "case-rejection")
        self.assertEqual(accepted.consecutive_rejections, 0)
        self.assertIsNotNone(
            store.get_case("case-rejection").accepted_frame_revision_id
        )

    def test_frame_change_invalidates_heads_and_plan_lineage_continues(self) -> None:
        revised_frame = replace(
            frame_proposal("case-revision"),
            payload=replace(
                frame_proposal("case-revision").payload,
                revision_reason_ref="reason:change-exposure-definition",
                measurement_design=make_measurement_design(
                    question_id="case-revision:question:1",
                    window_days=3,
                    include_source_span=False,
                ),
            ),
        )
        revised_plan = replace(
            plan_proposal(),
            payload=replace(
                plan_proposal().payload,
                revision_reason="Re-plan for the revised exposure",
            ),
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(
                (
                    frame_proposal("case-revision"),
                    plan_proposal(),
                    revised_frame,
                    revised_plan,
                )
            ),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-1",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-revision",
            thread_id="thread-revision",
            run_id="run-revision",
            user_message="调整业务口径",
        )
        complete_agent_turn(controller, "case-revision")
        complete_agent_turn(controller, "case-revision")
        first_plan_id = store.get_case(
            "case-revision"
        ).accepted_plan_revision_id
        complete_agent_turn(controller, "case-revision")
        invalidated = store.get_case("case-revision")
        self.assertIsNone(invalidated.accepted_plan_revision_id)
        self.assertIsNone(invalidated.accepted_answer_version_id)

        complete_agent_turn(controller, "case-revision")
        second_plan = store.get_plan(
            store.get_case("case-revision").accepted_plan_revision_id or ""
        )
        self.assertEqual(second_plan.revision_number, 2)
        self.assertEqual(second_plan.prior_plan_revision_id, first_plan_id)

    def test_concurrent_resume_admits_only_one_inflight_proposal(self) -> None:
        barrier = Barrier(2)

        store = InMemoryAuthorityStore()
        bootstrap = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="bootstrap",
            clock=lambda: NOW,
        )
        bootstrap.start(
            case_id="case-concurrent-runtime",
            thread_id="thread-concurrent-runtime",
            run_id="run-concurrent-runtime",
            user_message="并发恢复测试",
        )
        proposal_a = frame_proposal("case-concurrent-runtime")
        proposal_b = replace(
            proposal_a,
            payload=replace(
                proposal_a.payload,
                revision_reason_ref="reason:concurrent-alternative",
            ),
        )
        controllers = (
            WAJEController(
                store=store,
                provider=ScriptedPrimaryAgentProvider((proposal_a,)),
                effect_executor=ScriptedEffectExecutor(()),
                owner_id="worker-a",
                clock=lambda: NOW,
            ),
            WAJEController(
                store=store,
                provider=ScriptedPrimaryAgentProvider((proposal_b,)),
                effect_executor=ScriptedEffectExecutor(()),
                owner_id="worker-b",
                clock=lambda: NOW,
            ),
        )
        scheduled = bootstrap.advance("case-concurrent-runtime")
        self.assertEqual(scheduled.phase, ControllerPhase.WAITING_FOR_LLM)

        def deliver(controller):
            try:
                barrier.wait()
                controller.deliver_pending_llm("case-concurrent-runtime")
                return "accepted"
            except ControllerConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(deliver, controllers))

        self.assertCountEqual(outcomes, ("accepted", "conflict"))
        frame_events = tuple(
            event
            for event in store.list_events("case-concurrent-runtime")
            if event.event_type is JournalEventType.FRAME_ACCEPTED
        )
        self.assertEqual(len(frame_events), 1)

    def test_correction_fences_inflight_llm_before_authority_commit(
        self,
    ) -> None:
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider(
            (frame_proposal("case-correction-llm"),)
        )
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-correction-llm",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-correction-llm",
            thread_id="thread-correction-llm",
            run_id="run-correction-llm",
            user_message="先按自然月分析",
        )
        waiting = controller.advance("case-correction-llm")
        self.assertEqual(waiting.phase, ControllerPhase.WAITING_FOR_LLM)

        receipt = controller.ingress_message(
            case_id="case-correction-llm",
            thread_id="thread-correction-llm",
            run_id="run-correction-llm",
            user_message="改为按业务结算周期分析",
            kind=MailboxMessageKind.USER_CORRECTION,
            idempotency_key="correction-llm-key",
        )
        resumed = controller.deliver_pending_llm("case-correction-llm")

        self.assertEqual(resumed.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertEqual(resumed.authority_epoch, receipt.authority_epoch)
        self.assertEqual(
            resumed.latest_user_message,
            "改为按业务结算周期分析",
        )
        self.assertIsNone(
            store.get_case("case-correction-llm").accepted_frame_revision_id
        )
        self.assertEqual(provider.requests, [])
        self.assertIn(
            JournalEventType.JOB_SUPERSEDED,
            tuple(
                event.event_type
                for event in store.list_events("case-correction-llm")
            ),
        )
        packet = store.get_context_packet(resumed.context_packet_id)
        self.assertEqual(
            (
                "先按自然月分析",
                "改为按业务结算周期分析",
            ),
            tuple(item.content for item in packet.user_messages),
        )

    def test_correction_during_effect_preserves_attempt_but_rejects_result(
        self,
    ) -> None:
        class CorrectionDuringEffect:
            controller: WAJEController

            def execute(self, message):
                self.controller.ingress_message(
                    case_id=message.case_id,
                    thread_id="thread-correction-effect",
                    run_id="run-correction-effect",
                    user_message="排除异常渠道后重新调查",
                    kind=MailboxMessageKind.USER_CORRECTION,
                    idempotency_key="correction-effect-key",
                )
                return EffectExecutionResult(
                    payload={"rows": 9, "direction": "higher"},
                    business_summary="Old-scope result completed",
                )

        store = InMemoryAuthorityStore()
        executor = CorrectionDuringEffect()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(
                (
                    frame_proposal("case-correction-effect"),
                    plan_proposal(),
                    capability_proposal(),
                )
            ),
            effect_executor=executor,
            owner_id="worker-correction-effect",
            clock=lambda: NOW,
        )
        executor.controller = controller
        controller.start(
            case_id="case-correction-effect",
            thread_id="thread-correction-effect",
            run_id="run-correction-effect",
            user_message="调查付费变化",
        )
        complete_agent_turn(controller, "case-correction-effect")
        complete_agent_turn(controller, "case-correction-effect")
        waiting = complete_agent_turn(
            controller,
            "case-correction-effect",
        )
        job_id = waiting.pending_job_ids[0]

        resumed = controller.deliver_pending_effect(
            "case-correction-effect"
        )

        self.assertEqual(resumed.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertEqual(
            resumed.latest_user_message,
            "排除异常渠道后重新调查",
        )
        attempts = store.list_effect_attempts(job_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(
            attempts[0].status,
            EffectAttemptStatus.SUCCEEDED,
        )
        events = store.list_events("case-correction-effect")
        self.assertTrue(
            any(
                event.event_type is JournalEventType.JOB_SUPERSEDED
                and event.authority_ref == job_id
                for event in events
            )
        )
        self.assertFalse(
            any(
                event.event_type is JournalEventType.EFFECT_COMPLETED
                and event.authority_ref == attempts[0].effect_attempt_id
                for event in events
            )
        )

    def test_ingress_is_idempotent_and_keeps_one_mailbox_authority(
        self,
    ) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-ingress",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-ingress",
            thread_id="thread-ingress",
            run_id="run-ingress",
            user_message="检查收入健康度",
        )
        first = controller.ingress_message(
            case_id="case-ingress",
            thread_id="thread-ingress",
            run_id="run-ingress",
            user_message="补充看渠道集中度",
            idempotency_key="same-ingress-key",
        )
        duplicate = controller.ingress_message(
            case_id="case-ingress",
            thread_id="thread-ingress",
            run_id="run-ingress",
            user_message="补充看渠道集中度",
            idempotency_key="same-ingress-key",
        )

        self.assertEqual(first, duplicate)
        self.assertEqual(store.get_mailbox_head("case-ingress").last_sequence, 2)
        self.assertEqual(store.get_mailbox_head("case-ingress").authority_epoch, 2)

    def test_mailbox_burst_preserves_ordered_full_user_lineage(self) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-mailbox-burst",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-mailbox-burst",
            thread_id="thread-mailbox-burst",
            run_id="run-mailbox-burst",
            user_message="解释昨天收入变化",
        )
        controller.ingress_message(
            case_id="case-mailbox-burst",
            thread_id="thread-mailbox-burst",
            run_id="run-mailbox-burst",
            user_message="先排除异常渠道",
            idempotency_key="burst-message-2",
        )
        controller.ingress_message(
            case_id="case-mailbox-burst",
            thread_id="thread-mailbox-burst",
            run_id="run-mailbox-burst",
            user_message="同时改用业务结算日",
            idempotency_key="burst-message-3",
        )

        waiting = controller.advance("case-mailbox-burst")
        packet = store.get_context_packet(waiting.context_packet_id)
        self.assertEqual(
            (
                "解释昨天收入变化",
                "先排除异常渠道",
                "同时改用业务结算日",
            ),
            tuple(item.content for item in packet.user_messages),
        )
        self.assertEqual((1, 2, 3), tuple(
            item.sequence for item in packet.user_messages
        ))
        self.assertEqual(3, waiting.mailbox_cursor)

    def test_cross_process_outbox_dispatch_is_discoverable_and_idempotent(
        self,
    ) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(
                (frame_proposal("case-dispatch"),)
            ),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-dispatch",
            clock=lambda: NOW,
        )
        initial = controller.start(
            case_id="case-dispatch",
            thread_id="thread-dispatch",
            run_id="run-dispatch",
            user_message="定义收入测量",
        )
        wake = store.list_outbox_messages(case_id="case-dispatch")[0]

        waiting = controller.dispatch_outbox(wake.outbox_message_id)
        duplicate_wake = controller.dispatch_outbox(wake.outbox_message_id)
        self.assertEqual(waiting.content_sha256, duplicate_wake.content_sha256)
        self.assertEqual(waiting.phase, ControllerPhase.WAITING_FOR_LLM)
        self.assertNotEqual(initial.content_sha256, waiting.content_sha256)

        llm_job = next(
            message
            for message in store.list_outbox_messages(
                case_id="case-dispatch"
            )
            if message.job_kind.value == "primary_agent"
        )
        completed = controller.dispatch_outbox(llm_job.outbox_message_id)
        duplicate_completion = controller.dispatch_outbox(
            llm_job.outbox_message_id
        )
        self.assertEqual(
            completed.content_sha256,
            duplicate_completion.content_sha256,
        )
        self.assertIsNotNone(
            store.get_case("case-dispatch").accepted_frame_revision_id
        )

    def test_mailbox_retry_ignores_retry_timestamp_and_job_fence_is_monotonic(
        self,
    ) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-store-conformance",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-store-conformance",
            thread_id="thread-store-conformance",
            run_id="run-store-conformance",
            user_message="检查收入证据",
        )
        original = store.list_mailbox_messages(
            "case-store-conformance"
        )[0]
        replayed = store.append_mailbox_message(
            message_id=original.message_id,
            case_id=original.case_id,
            kind=original.kind,
            operation=original.operation,
            payload={"message": "检查收入证据"},
            created_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(original, replayed)

        wake = store.list_outbox_messages(
            case_id="case-store-conformance"
        )[0]
        with self.assertRaisesRegex(
            ValueError,
            "outbox payload hash must match operation identity",
        ):
            replace(
                wake,
                operation=replace(
                    wake.operation,
                    payload_sha256="0" * 64,
                ),
            )
        first = store.acquire_job_lease(
            outbox_message_id=wake.outbox_message_id,
            owner_id="worker-a",
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
        store.release_job_lease(first)
        second = store.acquire_job_lease(
            outbox_message_id=wake.outbox_message_id,
            owner_id="worker-b",
            now=NOW + timedelta(seconds=10),
            expires_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(first.fencing_token + 1, second.fencing_token)
        with self.assertRaises(LeaseFenceLost):
            store.heartbeat_job_lease(
                second,
                heartbeat_at=NOW + timedelta(minutes=3),
                expires_at=NOW + timedelta(minutes=4),
            )

        first_case_lease = store.acquire_lease(
            case_id="case-store-conformance",
            run_id="run-store-conformance",
            owner_id="worker-case-a",
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
        with self.assertRaises(LeaseConflict):
            store.acquire_lease(
                case_id="case-store-conformance",
                run_id="run-store-conformance",
                owner_id="worker-case-a",
                now=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(minutes=2),
            )
        store.release_lease(first_case_lease)
        second_case_lease = store.acquire_lease(
            case_id="case-store-conformance",
            run_id="run-store-conformance",
            owner_id="worker-case-b",
            now=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(
            first_case_lease.fencing_token + 1,
            second_case_lease.fencing_token,
        )
        with self.assertRaises(LeaseFenceLost):
            store.release_lease(first_case_lease)

    def test_ingress_transaction_rolls_back_journal_mailbox_and_outbox(
        self,
    ) -> None:
        class FailingWakeStore(InMemoryAuthorityStore):
            def enqueue_outbox(self, message):
                super().enqueue_outbox(message)
                raise RuntimeError("simulated outbox write failure")

        store = FailingWakeStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-ingress-rollback",
            clock=lambda: NOW,
        )

        with self.assertRaisesRegex(RuntimeError, "outbox write failure"):
            controller.start(
                case_id="case-ingress-rollback",
                thread_id="thread-ingress-rollback",
                run_id="run-ingress-rollback",
                user_message="调查异常波动",
            )
        with self.assertRaises(AuthorityNotFound):
            store.get_case("case-ingress-rollback")


if __name__ == "__main__":
    unittest.main()
