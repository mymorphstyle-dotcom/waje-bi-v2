from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

from gate1_fixtures import make_measurement_design
from gate3_plan_fixtures import record_measurement_authority
from test_gate3_3_measurement_resolver import make_trusted_verifier
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
    InspectSemanticsPayload,
    ProposeAnswerPayload,
    ProposedClaim,
    ProposedObjectionClosure,
    ReviseFramePayload,
    RevisePlanPayload,
)
from waje_vnext.domain.authority import (
    AnswerStatus,
    ClaimVerifierStatus,
    DecisionOption,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.planning import ProposedWorkTask
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    MailboxMessageKind,
    OperationIdentity,
)
from waje_vnext.domain.controller import (
    ControllerPhase,
    EffectAttemptStatus,
    PersistedAction,
)
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.measurement import (
    ObligationSatisfactionRecord,
    ObligationSatisfactionStatus,
)
from waje_vnext.domain.runtime_state import OutboxMessage
from waje_vnext.domain.runtime_amendment import (
    DispatcherRecoveryCursor,
    FrameReviewDisposition,
    FrameReviewProposal,
    JobDisposition,
    MeasurementObjectionSeverity,
    ProposedMeasurementObjection,
)
from waje_vnext.providers import (
    ChatCompletionsProviderSettings,
    ProviderPermanentError,
    ScriptedPrimaryAgentProvider as BaseScriptedPrimaryAgentProvider,
)
from waje_vnext.storage import (
    AuthorityConflict,
    AuthorityNotFound,
    InMemoryAuthorityStore as BaseInMemoryAuthorityStore,
    InvalidAuthorityTransition,
    LeaseConflict,
    LeaseFenceLost,
)


NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


class InMemoryAuthorityStore(BaseInMemoryAuthorityStore):
    """Gate 2 harness with trusted G3 measurement authority available."""

    def __init__(self) -> None:
        super().__init__(
            resolution_input_verifier=make_trusted_verifier()
        )

    def list_measurement_resolutions(self, frame_revision_id):
        existing = super().list_measurement_resolutions(
            frame_revision_id
        )
        if existing:
            return existing
        frame = self.get_frame(frame_revision_id)
        case = self.get_case(frame.case_id)
        record_measurement_authority(
            store=self,
            case=case,
            frame=frame,
            created_at=NOW,
            correlation_id=str(
                self.latest_checkpoint(
                    frame.case_id
                ).state_payload["run_id"]
            ),
        )
        return super().list_measurement_resolutions(
            frame_revision_id
        )


class ScriptedPrimaryAgentProvider(BaseScriptedPrimaryAgentProvider):
    """Ground scripted intent in the current closed authority packet."""

    def propose(self, request):
        proposal = super().propose(request)
        packet = request.context_packet
        if proposal.kind is ActionKind.REVISE_PLAN:
            obligation_ids = tuple(
                str(item["obligation_id"])
                for item
                in packet.available_evidence_obligation_payloads
            )
            if not obligation_ids:
                return proposal
            return replace(
                proposal,
                payload=replace(
                    proposal.payload,
                    tasks=(
                        ProposedWorkTask(
                            proposal_task_key=(
                                "measure-accepted-contrast"
                            ),
                            business_purpose=(
                                "Measure the accepted comparison"
                            ),
                            capability_intent_ref=(
                                "waje-vnext://capability-intent/"
                                "measurement-evidence.v1"
                            ),
                            obligation_ids=obligation_ids,
                            depends_on_task_keys=(),
                        ),
                    ),
                ),
            )
        if proposal.kind is ActionKind.CALL_CAPABILITY:
            binding = packet.accepted_query_binding_payloads[0]
            return replace(
                proposal,
                payload=CallCapabilityPayload(
                    task_id=str(binding["task_id"]),
                    query_binding_id=str(
                        binding["query_binding_id"]
                    ),
                ),
            )
        return proposal


class RepairingMeasurementProvider(ScriptedPrimaryAgentProvider):
    """Exercises a blocking review followed by an explicit Frame repair."""

    def __init__(self) -> None:
        super().__init__(())
        self._proposal_count = 0
        self._review_count = 0

    def propose(self, request):
        self.requests.append(request)
        self._proposal_count += 1
        case_id = request.context_packet.case_id
        if self._proposal_count == 1:
            return frame_proposal(case_id)
        review_payload = request.context_packet.latest_frame_review_payload
        if review_payload is None:
            raise AssertionError("repair turn is missing the blocking review")
        objection_id = str(review_payload["objections"][0]["objection_id"])
        proposal = frame_proposal(case_id)
        design = proposal.payload.measurement_design
        repaired_design = replace(
            design,
            eligibilities=(
                replace(
                    design.eligibilities[0],
                    minimum_coverage_ratio="1",
                ),
            ),
        )
        return replace(
            proposal,
            payload=replace(
                proposal.payload,
                revision_reason_ref="reason:close-review-objection",
                measurement_design=repaired_design,
                objection_closures=(
                    ProposedObjectionClosure(
                        objection_id=objection_id,
                        explanation=(
                            "Require complete coverage before the paired "
                            "window is eligible."
                        ),
                    ),
                ),
            ),
        )

    def review(self, request):
        self.review_requests.append(request)
        self._review_count += 1
        if self._review_count == 1:
            return FrameReviewProposal(
                disposition=FrameReviewDisposition.BLOCK,
                objections=(
                    ProposedMeasurementObjection(
                        code="incomplete_period_policy_ambiguous",
                        severity=MeasurementObjectionSeverity.BLOCKING,
                        affected_node_refs=(
                            (
                                "measurement_design.eligibilities.0."
                                "minimum_coverage_ratio"
                            ),
                        ),
                        explanation=(
                            "The first candidate does not make partial-period "
                            "eligibility auditable."
                        ),
                    ),
                ),
                review_summary="Revise the measurement design before admission.",
            )
        return FrameReviewProposal(
            disposition=FrameReviewDisposition.ACCEPT,
            objections=(),
            review_summary="The replacement closes the blocking objection.",
        )


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
                ProposedWorkTask(
                    proposal_task_key="measure-accepted-contrast",
                    business_purpose="Measure the within-month pattern",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(content_sha256("placeholder"),),
                    depends_on_task_keys=(),
                ),
            ),
        ),
    )


def capability_proposal() -> AgentActionProposal:
    return AgentActionProposal(
        kind=ActionKind.CALL_CAPABILITY,
        payload=CallCapabilityPayload(
            task_id="task-placeholder",
            query_binding_id=content_sha256("query-placeholder"),
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
    current = controller.resume(case_id)
    if (
        current.phase
        is ControllerPhase.WAITING_FOR_MESSAGE_BINDING
    ):
        current = controller.deliver_pending_message_binding(case_id)
    if current.phase is not ControllerPhase.READY_FOR_AGENT:
        return current
    waiting = controller.advance(case_id)
    if waiting.phase is not ControllerPhase.WAITING_FOR_LLM:
        return waiting
    delivered = controller.deliver_pending_llm(case_id)
    if (
        delivered.phase
        is ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW
    ):
        return controller.deliver_pending_frame_review(case_id)
    return delivered


class Gate2ControllerTest(unittest.TestCase):
    def test_blocking_frame_review_requires_explicit_repair_closure(self) -> None:
        store = InMemoryAuthorityStore()
        provider = RepairingMeasurementProvider()
        controller = WAJEController(
            store=store,
            provider=provider,
            reviewer_provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-frame-repair",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-frame-repair",
            thread_id="thread-frame-repair",
            run_id="run-frame-repair",
            user_message="比较两个经营时段，并排除不完整周期。",
        )
        controller.deliver_pending_message_binding("case-frame-repair")
        controller.advance("case-frame-repair")
        first_waiting_review = controller.deliver_pending_llm(
            "case-frame-repair"
        )
        self.assertEqual(
            first_waiting_review.phase,
            ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW,
        )
        first_candidate = store.get_active_frame_candidate(
            "case-frame-repair"
        )
        assert first_candidate is not None

        blocked = controller.deliver_pending_frame_review(
            "case-frame-repair"
        )
        self.assertEqual(blocked.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertIsNone(
            store.get_case("case-frame-repair").accepted_frame_revision_id
        )
        first_review = store.get_frame_review_for_candidate(
            first_candidate.frame_candidate_id
        )
        assert first_review is not None
        self.assertEqual(
            first_review.disposition,
            FrameReviewDisposition.BLOCK,
        )

        controller.advance("case-frame-repair")
        second_waiting_review = controller.deliver_pending_llm(
            "case-frame-repair"
        )
        self.assertEqual(
            second_waiting_review.phase,
            ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW,
        )
        repair_context = provider.requests[-1].context_packet
        self.assertEqual(
            repair_context.latest_frame_review_payload[
                "frame_review_id"
            ],
            first_review.frame_review_id,
        )
        replacement = store.get_active_frame_candidate(
            "case-frame-repair"
        )
        assert replacement is not None
        self.assertEqual(
            replacement.prior_frame_candidate_id,
            first_candidate.frame_candidate_id,
        )
        self.assertEqual(
            replacement.addressed_objection_ids,
            (first_review.objections[0].objection_id,),
        )

        accepted = controller.deliver_pending_frame_review(
            "case-frame-repair"
        )
        self.assertEqual(accepted.phase, ControllerPhase.READY_FOR_AGENT)
        accepted_frame_id = store.get_case(
            "case-frame-repair"
        ).accepted_frame_revision_id
        self.assertEqual(
            accepted_frame_id,
            replacement.proposed_frame_revision_id,
        )
        replacement_review = store.get_frame_review_for_candidate(
            replacement.frame_candidate_id
        )
        assert replacement_review is not None
        self.assertEqual(
            replacement_review.disposition,
            FrameReviewDisposition.ACCEPT,
        )
        self.assertEqual(len(replacement_review.closure_proof_refs), 1)

    def test_frame_review_commit_crash_resumes_same_candidate(self) -> None:
        class FailFirstReviewerDispositionStore(InMemoryAuthorityStore):
            def __init__(self) -> None:
                super().__init__()
                self.failed = False

            def record_job_disposition(self, disposition):
                result = super().record_job_disposition(disposition)
                if (
                    disposition.job_kind.value == "reviewer"
                    and not self.failed
                ):
                    self.failed = True
                    raise RuntimeError(
                        "simulated crash before reviewer commit"
                    )
                return result

        store = FailFirstReviewerDispositionStore()
        provider = ScriptedPrimaryAgentProvider(
            (frame_proposal("case-review-recovery"),)
        )
        controller = WAJEController(
            store=store,
            provider=provider,
            reviewer_provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-review-recovery",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-review-recovery",
            thread_id="thread-review-recovery",
            run_id="run-review-recovery",
            user_message="定义可审查的测量口径。",
        )
        controller.deliver_pending_message_binding(
            "case-review-recovery"
        )
        controller.advance("case-review-recovery")
        waiting = controller.deliver_pending_llm(
            "case-review-recovery"
        )
        review_job_id = waiting.pending_job_ids[0]
        candidate = store.get_active_frame_candidate(
            "case-review-recovery"
        )
        assert candidate is not None

        with self.assertRaisesRegex(
            RuntimeError,
            "simulated crash before reviewer commit",
        ):
            controller.deliver_pending_frame_review(
                "case-review-recovery"
            )
        self.assertEqual(
            controller.resume("case-review-recovery").content_sha256,
            waiting.content_sha256,
        )
        self.assertIsNone(
            store.get_frame_review_for_candidate(
                candidate.frame_candidate_id
            )
        )
        self.assertIsNone(store.get_job_disposition(review_job_id))
        self.assertIsNone(
            store.get_case(
                "case-review-recovery"
            ).accepted_frame_revision_id
        )

        recovered = controller.deliver_pending_frame_review(
            "case-review-recovery"
        )
        self.assertEqual(recovered.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertEqual(len(provider.review_requests), 1)
        self.assertEqual(
            store.get_case(
                "case-review-recovery"
            ).accepted_frame_revision_id,
            candidate.proposed_frame_revision_id,
        )
        self.assertEqual(
            store.get_job_disposition(
                review_job_id
            ).disposition,
            JobDisposition.COMPLETED,
        )

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
        self.assertEqual(
            state.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )

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
        trace = controller.build_run_trace_manifest("case-gate2")
        self.assertEqual(trace.plan_revision_ids, (plan_id,))
        self.assertEqual(len(trace.effect_attempt_ids), 2)
        self.assertEqual(
            trace.claim_ids,
            (answer.claims[0].claim_id,),
        )
        self.assertEqual(
            trace.provisional_answer_version_ids,
            (answer.answer_version_id,),
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

    def test_generic_effect_tolerates_sibling_obligation_state(self) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(
                (
                    frame_proposal("case-effect-sibling"),
                    plan_proposal(),
                    capability_proposal(),
                )
            ),
            effect_executor=ScriptedEffectExecutor(
                (
                    EffectExecutionResult(
                        payload={"rows": 1},
                        business_summary="Effect completed",
                    ),
                )
            ),
            owner_id="worker-effect-sibling",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-effect-sibling",
            thread_id="thread-effect-sibling",
            run_id="run-effect-sibling",
            user_message="调查一个可恢复的业务变化",
        )
        complete_agent_turn(controller, "case-effect-sibling")
        complete_agent_turn(controller, "case-effect-sibling")
        waiting = complete_agent_turn(
            controller,
            "case-effect-sibling",
        )
        plan_id = store.get_case(
            "case-effect-sibling"
        ).accepted_plan_revision_id
        assert plan_id is not None
        obligation_id = store.get_plan_adoption(
            plan_id
        ).obligation_ids[0]
        store.record_obligation_satisfaction(
            ObligationSatisfactionRecord(
                satisfaction_record_id=(
                    "satisfaction-generic-effect-sibling"
                ),
                obligation_id=obligation_id,
                status=ObligationSatisfactionStatus.OPEN,
                evidence_admission_record_ids=(),
                evidence_use_binding_ids=(),
                resolution_boundary_outcome_id=None,
                contradiction_disposition_refs=(),
                verifier_policy_version="obligation-satisfaction.v1",
                input_set_sha256=content_sha256(
                    {
                        "obligation_id": obligation_id,
                        "status": "open",
                    }
                ),
                created_at=NOW,
            ),
            event_id="event-generic-effect-sibling",
        )
        outbox = store.get_outbox_message(waiting.pending_job_ids[0])
        self.assertFalse(controller._job_is_stale(outbox))

        resumed = controller.deliver_pending_effect(
            "case-effect-sibling"
        )
        self.assertEqual(resumed.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertTrue(
            any(
                event.event_type is JournalEventType.EFFECT_COMPLETED
                for event in store.list_events("case-effect-sibling")
            )
        )

    def test_effect_outbox_replay_requires_exact_admitted_request(
        self,
    ) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=BaseScriptedPrimaryAgentProvider(
                (
                    AgentActionProposal(
                        kind=ActionKind.INSPECT_SEMANTICS,
                        payload=InspectSemanticsPayload(
                            question="Inspect the governed metric contract",
                            contract_refs=("metric:payment-amount:v1",),
                        ),
                    ),
                )
            ),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-effect-outbox",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-effect-outbox",
            thread_id="thread-effect-outbox",
            run_id="run-effect-outbox",
            user_message="检查 effect authority",
        )
        waiting = complete_agent_turn(
            controller,
            "case-effect-outbox",
        )
        message = store.get_outbox_message(
            waiting.pending_job_ids[0]
        )

        self.assertEqual(store.enqueue_outbox(message), message)
        for field_name, forged_value in (
            ("task_id", "forged-task"),
            ("query_binding_id", "f" * 64),
            ("sensitivity_id", "forged-sensitivity"),
        ):
            with self.subTest(field_name=field_name):
                request = dict(message.payload["request"])
                request[field_name] = forged_value
                payload = {
                    **dict(message.payload),
                    "request": request,
                }
                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "exact admitted action request",
                ):
                    store.enqueue_outbox(
                        replace(
                            message,
                            payload=payload,
                            payload_sha256=content_sha256(payload),
                            operation=replace(
                                message.operation,
                                payload_sha256=content_sha256(payload),
                            ),
                        )
                    )

    def test_rejected_effect_action_cannot_be_enqueued(self) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=BaseScriptedPrimaryAgentProvider(
                (capability_proposal(),)
            ),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-rejected-effect",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-rejected-effect",
            thread_id="thread-rejected-effect",
            run_id="run-rejected-effect",
            user_message="没有 Plan 时尝试 capability",
        )
        complete_agent_turn(controller, "case-rejected-effect")
        rejected_event = next(
            event
            for event in store.list_events("case-rejected-effect")
            if event.event_type is JournalEventType.ACTION_REJECTED
        )
        persisted = store.get_action(rejected_event.action_id or "")
        case = store.get_case("case-rejected-effect")
        authority = store.get_authority_snapshot(case.case_id)
        assert isinstance(
            persisted.action.payload,
            CallCapabilityPayload,
        )
        payload = {
            "action_kind": persisted.action.kind.value,
            "request": {
                "task_id": persisted.action.payload.task_id,
                "query_binding_id": (
                    persisted.action.payload.query_binding_id
                ),
            },
            "expected_head_version": (
                persisted.action.expected_head_version
            ),
        }
        operation = OperationIdentity(
            operation_id="operation-rejected-effect-outbox",
            idempotency_key="rejected-effect-outbox-key",
            causation_id=persisted.action.operation.operation_id,
            correlation_id=persisted.action.operation.correlation_id,
            authority_revision=authority.mailbox_authority_epoch,
            payload_sha256=content_sha256(payload),
        )
        message = OutboxMessage(
            outbox_message_id="outbox-rejected-effect",
            case_id=case.case_id,
            source_event_cursor=rejected_event.cursor,
            action_id=persisted.action.action_id,
            job_kind=AsyncJobKind.CAPABILITY,
            operation=operation,
            expected_head_version=case.head_version,
            expected_authority_epoch=(
                authority.mailbox_authority_epoch
            ),
            authority_snapshot=authority,
            authority_snapshot_sha256=authority.content_sha256,
            idempotency_key=operation.idempotency_key,
            destination="capability",
            contract_ref="waje-vnext://runtime/effect-request.v1",
            payload=payload,
            payload_sha256=content_sha256(payload),
            created_at=NOW,
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "admission proof|currently admitted",
        ):
            store.enqueue_outbox(message)

    def test_run_trace_manifest_closes_durable_model_lineage(self) -> None:
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider(
            (frame_proposal("case-run-trace"),)
        )
        controller = WAJEController(
            store=store,
            provider=provider,
            reviewer_provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-run-trace",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-run-trace",
            thread_id="thread-run-trace",
            run_id="run-run-trace",
            user_message="定义可审查的收入测量口径。",
        )
        complete_agent_turn(controller, "case-run-trace")

        manifest = controller.build_run_trace_manifest("case-run-trace")
        self.assertEqual(manifest.run_id, "run-run-trace")
        self.assertEqual(len(manifest.ingress_record_ids), 1)
        self.assertEqual(len(manifest.message_binding_ids), 1)
        self.assertEqual(len(manifest.frame_candidate_ids), 1)
        self.assertEqual(len(manifest.frame_review_ids), 1)
        self.assertEqual(len(manifest.logical_model_job_ids), 3)
        self.assertEqual(
            len(manifest.provider_attempt_receipt_ids),
            3,
        )
        self.assertEqual(
            len(manifest.job_disposition_record_ids),
            3,
        )
        self.assertEqual(
            store.get_run_trace_manifest(
                manifest.trace_manifest_id
            ),
            manifest,
        )
        replayed = controller.build_run_trace_manifest("case-run-trace")
        self.assertEqual(replayed, manifest)

    def test_terminal_case_run_can_start_a_new_replayable_analysis_cycle(
        self,
    ) -> None:
        case_id = "case-multiple-runs"
        provider = ScriptedPrimaryAgentProvider(
            (
                frame_proposal(case_id),
                plan_proposal(),
                answer_proposal(),
            )
        )
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-multiple-runs",
            clock=lambda: NOW,
        )
        controller.start(
            case_id=case_id,
            thread_id="thread-multiple-runs",
            run_id="run-multiple-runs-1",
            user_message="先完成第一轮经营分析",
        )
        controller.deliver_pending_message_binding(case_id)
        controller.advance(case_id)
        controller.deliver_pending_llm(case_id)
        controller.deliver_pending_frame_review(case_id)
        controller.advance(case_id)
        controller.deliver_pending_llm(case_id)
        controller.advance(case_id)
        completed = controller.deliver_pending_llm(case_id)
        self.assertEqual(completed.phase, ControllerPhase.COMPLETED)
        first_manifest = controller.build_run_trace_manifest(case_id)

        started = controller.start(
            case_id=case_id,
            thread_id="thread-multiple-runs",
            run_id="run-multiple-runs-2",
            user_message="开始第二轮独立经营问题",
        )
        self.assertEqual(
            started.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        self.assertEqual(started.run_id, "run-multiple-runs-2")
        ready = controller.deliver_pending_message_binding(case_id)
        self.assertEqual(ready.phase, ControllerPhase.READY_FOR_AGENT)
        second_manifest = controller.build_run_trace_manifest(case_id)
        self.assertGreater(
            second_manifest.start_event_cursor,
            first_manifest.terminal_event_cursor,
        )
        self.assertEqual(
            {
                link.correlation_id
                for link in second_manifest.event_operation_lineage
            },
            {"run-multiple-runs-2"},
        )
        self.assertEqual(
            store.get_run_trace_manifest(
                first_manifest.trace_manifest_id
            ),
            first_manifest,
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
        self.assertEqual(
            resumed.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        resumed = controller.deliver_pending_message_binding(
            "case-decision"
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
        controller.deliver_pending_message_binding("case-crash")
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
        self.assertEqual(
            recovered.phase,
            ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW,
        )
        recovered = controller.deliver_pending_frame_review("case-crash")
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
        bootstrap.deliver_pending_message_binding(
            "case-concurrent-runtime"
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
        active_candidate = store.get_active_frame_candidate(
            "case-concurrent-runtime"
        )
        winner = next(
            controller
            for controller in controllers
            if controller.resume("case-concurrent-runtime").phase
            is ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW
        )
        winner.deliver_pending_frame_review("case-concurrent-runtime")
        frame_events = tuple(
            event
            for event in store.list_events("case-concurrent-runtime")
            if event.event_type is JournalEventType.FRAME_ACCEPTED
        )
        self.assertEqual(len(frame_events), 1)
        self.assertEqual(
            store.get_case(
                "case-concurrent-runtime"
            ).accepted_frame_revision_id,
            active_candidate.proposed_frame_revision_id,
        )

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
        controller.deliver_pending_message_binding(
            "case-correction-llm"
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

        self.assertEqual(
            resumed.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        resumed = controller.deliver_pending_message_binding(
            "case-correction-llm"
        )
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

    def test_correction_fences_pending_measurement_review(self) -> None:
        store = InMemoryAuthorityStore()
        provider = ScriptedPrimaryAgentProvider(
            (frame_proposal("case-correction-review"),)
        )
        controller = WAJEController(
            store=store,
            provider=provider,
            reviewer_provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-correction-review",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-correction-review",
            thread_id="thread-correction-review",
            run_id="run-correction-review",
            user_message="先按自然月比较。",
        )
        controller.deliver_pending_message_binding(
            "case-correction-review"
        )
        controller.advance("case-correction-review")
        waiting = controller.deliver_pending_llm(
            "case-correction-review"
        )
        review_job_id = waiting.pending_job_ids[0]
        candidate = store.get_active_frame_candidate(
            "case-correction-review"
        )
        assert candidate is not None

        controller.ingress_message(
            case_id="case-correction-review",
            thread_id="thread-correction-review",
            run_id="run-correction-review",
            user_message="改成业务结算周期，前一口径作废。",
            kind=MailboxMessageKind.USER_CORRECTION,
            idempotency_key="case-correction-review:correction",
        )
        reconciled = controller.dispatch_outbox(review_job_id)
        self.assertEqual(
            reconciled.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        self.assertEqual(provider.review_requests, [])
        disposition = store.get_job_disposition(review_job_id)
        assert disposition is not None
        self.assertEqual(disposition.disposition, JobDisposition.SUPERSEDED)
        self.assertIsNone(
            store.get_case(
                "case-correction-review"
            ).accepted_frame_revision_id
        )
        self.assertIsNone(
            store.get_frame_review_for_candidate(
                candidate.frame_candidate_id
            )
        )
        rebound = controller.deliver_pending_message_binding(
            "case-correction-review"
        )
        self.assertEqual(rebound.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertEqual(
            store.get_case(
                "case-correction-review"
            ).accepted_question_revision_id,
            "case-correction-review:question:2",
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

        self.assertEqual(
            resumed.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        resumed = controller.deliver_pending_message_binding(
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
        binding_provider = ScriptedPrimaryAgentProvider(())
        controller = WAJEController(
            store=store,
            provider=binding_provider,
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
        self.assertEqual(
            waiting.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        waiting = controller.deliver_pending_message_binding(
            "case-mailbox-burst"
        )
        waiting = controller.advance("case-mailbox-burst")
        self.assertEqual(
            waiting.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        waiting = controller.deliver_pending_message_binding(
            "case-mailbox-burst"
        )
        waiting = controller.advance("case-mailbox-burst")
        self.assertEqual(
            waiting.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        waiting = controller.deliver_pending_message_binding(
            "case-mailbox-burst"
        )
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
        self.assertEqual(
            (
                "解释昨天收入变化",
                "先排除异常渠道",
                "同时改用业务结算日",
            ),
            tuple(
                item.message_content
                for item in binding_provider.binding_requests
            ),
        )
        self.assertEqual(
            3,
            len(
                store.list_message_impact_bindings(
                    "case-mailbox-burst"
                )
            ),
        )

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
        self.assertEqual(waiting.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertNotEqual(initial.content_sha256, waiting.content_sha256)
        waiting = controller.advance("case-dispatch")
        self.assertEqual(waiting.phase, ControllerPhase.WAITING_FOR_LLM)

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
        review_job = next(
            message
            for message in store.list_outbox_messages(
                case_id="case-dispatch"
            )
            if message.job_kind.value == "reviewer"
        )
        completed = controller.dispatch_outbox(review_job.outbox_message_id)
        duplicate_completion = controller.dispatch_outbox(
            review_job.outbox_message_id
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

    def test_message_binding_commit_recovers_after_process_failure(
        self,
    ) -> None:
        class FailFirstBindingCommitStore(InMemoryAuthorityStore):
            def __init__(self) -> None:
                super().__init__()
                self.failed = False

            def record_message_impact_binding(self, binding):
                result = super().record_message_impact_binding(binding)
                if not self.failed:
                    self.failed = True
                    raise RuntimeError(
                        "simulated crash during binding commit"
                    )
                return result

        store = FailFirstBindingCommitStore()
        provider = ScriptedPrimaryAgentProvider(())
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-binding-recovery",
            clock=lambda: NOW,
        )
        started = controller.start(
            case_id="case-binding-recovery",
            thread_id="thread-binding-recovery",
            run_id="run-binding-recovery",
            user_message="调查昨天收入变化。",
        )
        binding_job_id = started.pending_job_ids[0]
        with self.assertRaisesRegex(
            RuntimeError,
            "simulated crash during binding commit",
        ):
            controller.deliver_pending_message_binding(
                "case-binding-recovery"
            )
        self.assertEqual(
            controller.resume("case-binding-recovery").content_sha256,
            started.content_sha256,
        )
        self.assertIsNone(store.get_job_disposition(binding_job_id))
        self.assertIsNone(
            store.get_case(
                "case-binding-recovery"
            ).accepted_question_revision_id
        )

        recovered = controller.deliver_pending_message_binding(
            "case-binding-recovery"
        )
        self.assertEqual(recovered.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertEqual(
            store.get_case(
                "case-binding-recovery"
            ).accepted_question_revision_id,
            "case-binding-recovery:question:1",
        )
        self.assertEqual(len(provider.binding_requests), 1)
        self.assertEqual(
            store.get_job_disposition(
                binding_job_id
            ).disposition,
            JobDisposition.COMPLETED,
        )

    def test_terminal_dispositions_close_every_processed_outbox(self) -> None:
        store = InMemoryAuthorityStore()
        proposal = frame_proposal("case-job-disposition")
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider((proposal,)),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-job-disposition",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-job-disposition",
            thread_id="thread-job-disposition",
            run_id="run-job-disposition",
            user_message="定义收入测量",
        )
        controller.deliver_pending_message_binding(
            "case-job-disposition"
        )
        controller.advance("case-job-disposition")
        llm_job = next(
            item
            for item in store.list_outbox_messages(
                case_id="case-job-disposition"
            )
            if item.job_kind.value == "primary_agent"
        )
        controller.deliver_pending_llm("case-job-disposition")
        review_job = next(
            item
            for item in store.list_outbox_messages(
                case_id="case-job-disposition"
            )
            if item.job_kind.value == "reviewer"
        )
        controller.deliver_pending_frame_review(
            "case-job-disposition"
        )

        dispositions = tuple(
            store.get_job_disposition(item.outbox_message_id)
            for item in store.list_outbox_messages(
                case_id="case-job-disposition"
            )
        )
        self.assertTrue(all(item is not None for item in dispositions))
        self.assertTrue(
            all(
                item.disposition is JobDisposition.COMPLETED
                for item in dispositions
                if item is not None
            )
        )
        self.assertEqual(
            store.get_job_disposition(
                llm_job.outbox_message_id
            ).result_sha256,
            proposal.content_sha256,
        )
        self.assertIsNotNone(
            store.get_job_disposition(review_job.outbox_message_id)
        )
        self.assertEqual(
            store.list_pending_outbox_messages(
                case_id="case-job-disposition"
            ),
            (),
        )
        with self.assertRaises(LeaseConflict):
            store.acquire_job_lease(
                outbox_message_id=llm_job.outbox_message_id,
                owner_id="worker-replay",
                now=NOW,
                expires_at=NOW + timedelta(minutes=1),
            )

    def test_heartbeat_failure_blocks_provider_result_commit(self) -> None:
        heartbeat_attempted = Event()

        class FailingHeartbeatStore(InMemoryAuthorityStore):
            def heartbeat_job_lease(
                self,
                lease,
                *,
                heartbeat_at,
                expires_at,
            ):
                heartbeat_attempted.set()
                raise LeaseFenceLost("simulated heartbeat failure")

        class WaitingProvider:
            allows_test_role_multiplexing = True

            def bind_message(self, request):
                return ScriptedPrimaryAgentProvider(()).bind_message(
                    request
                )

            def propose(self, request):
                if not heartbeat_attempted.wait(timeout=1):
                    raise AssertionError("heartbeat was not attempted")
                return frame_proposal("case-heartbeat-failure")

            def review(self, request):
                raise AssertionError("review must not run")

        store = FailingHeartbeatStore()
        controller = WAJEController(
            store=store,
            provider=WaitingProvider(),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-heartbeat-failure",
            clock=lambda: NOW,
            lease_duration=timedelta(milliseconds=30),
        )
        controller.start(
            case_id="case-heartbeat-failure",
            thread_id="thread-heartbeat-failure",
            run_id="run-heartbeat-failure",
            user_message="定义收入测量",
        )
        controller.deliver_pending_message_binding(
            "case-heartbeat-failure"
        )
        controller.advance("case-heartbeat-failure")
        llm_job = next(
            item
            for item in store.list_pending_outbox_messages(
                case_id="case-heartbeat-failure"
            )
            if item.job_kind.value == "primary_agent"
        )
        with self.assertRaisesRegex(
            LeaseFenceLost,
            "periodic job heartbeat failed",
        ):
            controller.deliver_pending_llm("case-heartbeat-failure")
        self.assertIsNone(
            store.get_case(
                "case-heartbeat-failure"
            ).accepted_frame_revision_id
        )
        self.assertIsNone(
            store.get_job_disposition(llm_job.outbox_message_id)
        )

    def test_expired_worker_token_cannot_commit_after_takeover(self) -> None:
        store = InMemoryAuthorityStore()

        class TakeoverProvider(ScriptedPrimaryAgentProvider):
            def propose(self, request):
                self.requests.append(request)
                job = next(
                    item
                    for item in store.list_pending_outbox_messages(
                        case_id="case-lease-takeover"
                    )
                    if item.job_kind.value == "primary_agent"
                )
                store.acquire_job_lease(
                    outbox_message_id=job.outbox_message_id,
                    owner_id="replacement-worker",
                    now=NOW + timedelta(minutes=2),
                    expires_at=NOW + timedelta(minutes=4),
                )
                return frame_proposal("case-lease-takeover")

        provider = TakeoverProvider(())
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="expired-worker",
            clock=lambda: NOW,
            lease_duration=timedelta(minutes=1),
        )
        controller.start(
            case_id="case-lease-takeover",
            thread_id="thread-lease-takeover",
            run_id="run-lease-takeover",
            user_message="建立收入测量口径。",
        )
        controller.deliver_pending_message_binding(
            "case-lease-takeover"
        )
        waiting = controller.advance("case-lease-takeover")
        job_id = waiting.pending_job_ids[0]
        with self.assertRaises(LeaseFenceLost):
            controller.deliver_pending_llm("case-lease-takeover")
        self.assertIsNone(
            store.get_case(
                "case-lease-takeover"
            ).accepted_frame_revision_id
        )
        self.assertIsNone(
            store.get_active_frame_candidate("case-lease-takeover")
        )
        self.assertIsNone(store.get_job_disposition(job_id))

    def test_dispatcher_recovery_cursor_is_durable_and_monotonic(
        self,
    ) -> None:
        store = InMemoryAuthorityStore()
        initial = DispatcherRecoveryCursor(
            dispatcher_id="dispatcher-a",
            last_outbox_created_at=None,
            last_source_event_cursor=None,
            last_outbox_message_id=None,
            updated_at=NOW,
        )
        first = DispatcherRecoveryCursor(
            dispatcher_id="dispatcher-a",
            last_outbox_created_at=NOW,
            last_source_event_cursor=11,
            last_outbox_message_id="outbox-001",
            updated_at=NOW + timedelta(seconds=1),
        )
        later = DispatcherRecoveryCursor(
            dispatcher_id="dispatcher-a",
            last_outbox_created_at=NOW,
            last_source_event_cursor=12,
            last_outbox_message_id="outbox-002",
            updated_at=NOW + timedelta(seconds=2),
        )

        self.assertIsNone(
            store.get_dispatcher_recovery_cursor("dispatcher-a")
        )
        self.assertEqual(
            store.advance_dispatcher_recovery_cursor(initial),
            initial,
        )
        self.assertEqual(
            store.advance_dispatcher_recovery_cursor(first),
            first,
        )
        self.assertEqual(
            store.advance_dispatcher_recovery_cursor(later),
            later,
        )
        self.assertEqual(
            store.advance_dispatcher_recovery_cursor(
                replace(
                    later,
                    updated_at=NOW + timedelta(seconds=3),
                )
            ),
            later,
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "cannot move backwards",
        ):
            store.advance_dispatcher_recovery_cursor(first)
        self.assertEqual(
            store.get_dispatcher_recovery_cursor("dispatcher-a"),
            later,
        )

    def test_permanent_provider_failure_has_terminal_disposition(
        self,
    ) -> None:
        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-provider-failure",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-provider-failure",
            thread_id="thread-provider-failure",
            run_id="run-provider-failure",
            user_message="建立收入测量口径",
        )
        controller.deliver_pending_message_binding(
            "case-provider-failure"
        )
        waiting = controller.advance("case-provider-failure")
        job_id = waiting.pending_job_ids[0]

        blocked = controller.deliver_pending_llm(
            "case-provider-failure"
        )

        self.assertEqual(blocked.phase, ControllerPhase.BLOCKED)
        self.assertEqual(blocked.pending_job_ids, ())
        disposition = store.get_job_disposition(job_id)
        self.assertIsNotNone(disposition)
        self.assertEqual(
            disposition.disposition,
            JobDisposition.TERMINAL_FAILURE,
        )
        receipts = store.list_provider_attempt_receipts(job_id)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            receipts[0].disposition.value,
            "terminal_failure",
        )
        self.assertTrue(
            any(
                event.event_type
                is JournalEventType.JOB_TERMINALLY_FAILED
                for event in store.list_events("case-provider-failure")
            )
        )
        self.assertEqual(
            store.list_pending_outbox_messages(
                case_id="case-provider-failure"
            ),
            (),
        )

    def test_binding_provider_failure_is_terminally_disposed(self) -> None:
        class FailingBindingProvider(ScriptedPrimaryAgentProvider):
            def bind_message(self, request):
                self.binding_requests.append(request)
                raise ProviderPermanentError("invalid binding response")

        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=FailingBindingProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-binding-failure",
            clock=lambda: NOW,
        )
        waiting = controller.start(
            case_id="case-binding-failure",
            thread_id="thread-binding-failure",
            run_id="run-binding-failure",
            user_message="分析昨天收入",
        )
        job_id = waiting.pending_job_ids[0]

        blocked = controller.deliver_pending_message_binding(
            "case-binding-failure"
        )

        self.assertEqual(blocked.phase, ControllerPhase.BLOCKED)
        self.assertEqual(
            store.get_job_disposition(job_id).disposition,
            JobDisposition.TERMINAL_FAILURE,
        )

    def test_reviewer_provider_failure_is_terminally_disposed(self) -> None:
        class FailingReviewerProvider(ScriptedPrimaryAgentProvider):
            def review(self, request):
                self.review_requests.append(request)
                raise ProviderPermanentError("invalid review response")

        store = InMemoryAuthorityStore()
        controller = WAJEController(
            store=store,
            provider=FailingReviewerProvider(
                (frame_proposal("case-reviewer-failure"),)
            ),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="worker-reviewer-failure",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-reviewer-failure",
            thread_id="thread-reviewer-failure",
            run_id="run-reviewer-failure",
            user_message="建立收入测量口径",
        )
        controller.deliver_pending_message_binding(
            "case-reviewer-failure"
        )
        controller.advance("case-reviewer-failure")
        waiting = controller.deliver_pending_llm(
            "case-reviewer-failure"
        )
        job_id = waiting.pending_job_ids[0]

        blocked = controller.deliver_pending_frame_review(
            "case-reviewer-failure"
        )

        self.assertEqual(blocked.phase, ControllerPhase.BLOCKED)
        self.assertEqual(
            store.get_job_disposition(job_id).disposition,
            JobDisposition.TERMINAL_FAILURE,
        )


if __name__ == "__main__":
    unittest.main()
