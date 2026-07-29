from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier

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
from waje_vnext.domain.controller import (
    ControllerPhase,
    PersistedAction,
)
from waje_vnext.domain.events import JournalEventType
from waje_vnext.providers import (
    ChatCompletionsProviderSettings,
    ScriptedPrimaryAgentProvider,
)
from waje_vnext.storage import AuthorityNotFound, InMemoryAuthorityStore


NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def frame_proposal() -> AgentActionProposal:
    return AgentActionProposal(
        kind=ActionKind.REVISE_FRAME,
        payload=ReviseFramePayload(
            revision_reason="Define the measurement before querying",
            estimand="Average paid amount difference between exposure windows",
            observation_unit="calendar month",
            numerator="valid paid amount",
            denominator="complete observed months",
            exposure="contract-defined month-start window",
            comparison="mid-month and month-end windows",
            assumptions=("Paid amount contract is valid",),
            alternatives=("Composition shift may explain the pattern",),
            falsification_conditions=(
                "Pattern disappears in complete-month sensitivity",
            ),
            reversal_conditions=(
                "Comparison window exceeds the exposure window",
            ),
            success_conditions=("Comparable windows are measured",),
            stop_conditions=("Required metric contract is unavailable",),
            semantic_contract_refs=("metric:paid_amount:v1",),
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


class Gate2ControllerTest(unittest.TestCase):
    def test_persisted_action_binds_the_exact_business_proposal(self) -> None:
        proposal = frame_proposal()
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

        state = controller.advance("case-gate2")
        frame_id = store.get_case("case-gate2").accepted_frame_revision_id
        self.assertIsNotNone(frame_id)
        state = controller.advance("case-gate2")
        plan_id = store.get_case("case-gate2").accepted_plan_revision_id
        self.assertIsNotNone(plan_id)
        self.assertIsNotNone(
            provider.requests[1].context_packet.accepted_frame_payload
        )

        state = controller.advance("case-gate2")
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
        state = controller.advance("case-gate2")
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
        waiting = controller.advance("case-decision")
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
        proposal = frame_proposal()
        provider = ScriptedPrimaryAgentProvider((proposal, proposal))
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-1",
            clock=lambda: NOW,
        )
        initial = controller.start(
            case_id="case-crash",
            thread_id="thread-crash",
            run_id="run-crash",
            user_message="定义测量",
        )
        store.fail_next_checkpoint = True
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            controller.advance("case-crash")
        self.assertEqual(
            controller.resume("case-crash").content_sha256,
            initial.content_sha256,
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

        recovered = controller.advance("case-crash")
        self.assertEqual(recovered.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertIsNotNone(
            store.get_case("case-crash").accepted_frame_revision_id
        )

    def test_plan_before_frame_is_audited_and_rejected(self) -> None:
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider(
            (plan_proposal(), frame_proposal())
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
        rejected = controller.advance("case-rejection")
        self.assertEqual(rejected.consecutive_rejections, 1)
        self.assertEqual(store.get_case("case-rejection").head_version, 0)
        self.assertIn(
            JournalEventType.ACTION_REJECTED,
            tuple(
                event.event_type
                for event in store.list_events("case-rejection")
            ),
        )
        accepted = controller.advance("case-rejection")
        self.assertEqual(accepted.consecutive_rejections, 0)
        self.assertIsNotNone(
            store.get_case("case-rejection").accepted_frame_revision_id
        )

    def test_frame_change_invalidates_heads_and_plan_lineage_continues(self) -> None:
        revised_frame = replace(
            frame_proposal(),
            payload=replace(
                frame_proposal().payload,
                revision_reason="Change the business exposure definition",
                exposure="first three complete business days",
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
                    frame_proposal(),
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
        controller.advance("case-revision")
        controller.advance("case-revision")
        first_plan_id = store.get_case(
            "case-revision"
        ).accepted_plan_revision_id
        controller.advance("case-revision")
        invalidated = store.get_case("case-revision")
        self.assertIsNone(invalidated.accepted_plan_revision_id)
        self.assertIsNone(invalidated.accepted_answer_version_id)

        controller.advance("case-revision")
        second_plan = store.get_plan(
            store.get_case("case-revision").accepted_plan_revision_id or ""
        )
        self.assertEqual(second_plan.revision_number, 2)
        self.assertEqual(second_plan.prior_plan_revision_id, first_plan_id)

    def test_concurrent_resume_admits_only_one_inflight_proposal(self) -> None:
        barrier = Barrier(2)

        class BarrierProvider:
            def __init__(self, proposal):
                self.proposal = proposal

            def propose(self, request):
                barrier.wait()
                return self.proposal

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
        proposal_a = frame_proposal()
        proposal_b = replace(
            proposal_a,
            payload=replace(
                proposal_a.payload,
                revision_reason="Concurrent alternative measurement",
            ),
        )
        controllers = (
            WAJEController(
                store=store,
                provider=BarrierProvider(proposal_a),
                effect_executor=ScriptedEffectExecutor(()),
                owner_id="worker-a",
                clock=lambda: NOW,
            ),
            WAJEController(
                store=store,
                provider=BarrierProvider(proposal_b),
                effect_executor=ScriptedEffectExecutor(()),
                owner_id="worker-b",
                clock=lambda: NOW,
            ),
        )

        def advance(controller):
            try:
                controller.advance("case-concurrent-runtime")
                return "accepted"
            except ControllerConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(advance, controllers))

        self.assertCountEqual(outcomes, ("accepted", "conflict"))
        frame_events = tuple(
            event
            for event in store.list_events("case-concurrent-runtime")
            if event.event_type is JournalEventType.FRAME_ACCEPTED
        )
        self.assertEqual(len(frame_events), 1)


if __name__ == "__main__":
    unittest.main()
