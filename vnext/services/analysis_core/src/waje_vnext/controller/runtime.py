"""Single-authority Primary Business Analysis Agent controller."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from waje_vnext.domain.actions import (
    ActionEnvelope,
    ActionKind,
    AgentActionProposal,
    AskUserPayload,
    CallCapabilityPayload,
    InspectSemanticsPayload,
    ProposeAnswerPayload,
    RecordInterpretationPayload,
    ReviseFramePayload,
    RevisePlanPayload,
    RunProbePayload,
    RunSensitivityPayload,
    StopPayload,
)
from waje_vnext.domain.admission import admit_action
from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerClaim,
    AnswerStatus,
    AnswerVersion,
    CaseLifecycle,
    ClaimVerifierStatus,
    DecisionRecord,
    EvidenceRecord,
    InterpretationRecord,
    WorkPlanRevision,
)
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.context import (
    MAX_CONTEXT_DECISIONS,
    MAX_CONTEXT_EVIDENCE,
    MAX_CONTEXT_EVENTS,
    MAX_CONTEXT_OBJECTIONS,
    ContextDecisionItem,
    ContextEvidenceItem,
    ContextEventItem,
    ContextReviewerObjectionItem,
    build_context_packet,
)
from waje_vnext.domain.controller import (
    ControllerPhase,
    ControllerState,
    EffectAttemptRecord,
    EffectAttemptStatus,
    PersistedAction,
    PrimaryAgentRequest,
    UserDecisionRequest,
)
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.runtime_state import (
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)
from waje_vnext.providers.base import PrimaryAgentProvider
from waje_vnext.storage.codec import decode_controller_state, encode_record
from waje_vnext.storage.ports import AuthorityStore

from .effects import (
    EvidenceDraft,
    EffectExecutionResult,
    EffectExecutor,
    EffectPermanentError,
    EffectTransientError,
)


ACTION_CONTRACT_REF = "waje-vnext://contracts/domain/actions.v1"
CONTROLLER_STATE_SCHEMA_REF = (
    "waje-vnext://contracts/domain/controller-state.v1"
)
ACTION_RESULT_SCHEMA_REF = "waje-vnext://runtime/action-result.v1"
EFFECT_CONTRACT_REF = "waje-vnext://runtime/effect-request.v1"
ANSWER_VERIFIER_POLICY = "answer-verifier.v1"
_EFFECT_ACTIONS = {
    ActionKind.INSPECT_SEMANTICS,
    ActionKind.RUN_PROBE,
    ActionKind.CALL_CAPABILITY,
    ActionKind.RUN_SENSITIVITY,
}


class ControllerConflict(RuntimeError):
    pass


class WAJEController:
    """Owns the accepted action loop, state transitions, and recovery."""

    def __init__(
        self,
        *,
        store: AuthorityStore,
        provider: PrimaryAgentProvider,
        effect_executor: EffectExecutor,
        owner_id: str,
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id must be non-empty")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._store = store
        self._provider = provider
        self._effect_executor = effect_executor
        self._owner_id = owner_id
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._lease_duration = lease_duration

    def start(
        self,
        *,
        case_id: str,
        thread_id: str,
        run_id: str,
        user_message: str,
    ) -> ControllerState:
        now = self._now()
        with self._store.atomic():
            self._store.open_case(
                case_id=case_id,
                thread_id=thread_id,
                event_id=_stable_id("event", case_id, "opened"),
                opened_at=now,
            )
            existing = self._store.latest_checkpoint(case_id)
            if existing is not None:
                state = decode_controller_state(existing.state_payload)
                if state.run_id != run_id:
                    raise ControllerConflict(
                        "case already belongs to another controller run"
                    )
                return state
            return self._checkpoint(
                run_id=run_id,
                case_id=case_id,
                phase=ControllerPhase.READY_FOR_AGENT,
                step_number=0,
                latest_user_message=user_message,
                pending_action_id=None,
                pending_outbox_message_id=None,
                pending_decision_request_id=None,
                consecutive_rejections=0,
                now=now,
            )

    def resume(self, case_id: str) -> ControllerState:
        checkpoint = self._store.latest_checkpoint(case_id)
        if checkpoint is None:
            raise ControllerConflict("case has no durable controller checkpoint")
        return decode_controller_state(checkpoint.state_payload)

    def advance(self, case_id: str) -> ControllerState:
        state = self.resume(case_id)
        if state.phase is not ControllerPhase.READY_FOR_AGENT:
            return state
        packet = self._store.get_context_packet(state.context_packet_id)
        request = PrimaryAgentRequest(
            turn_id=_stable_id(
                "turn",
                state.run_id,
                str(state.step_number + 1),
                packet.content_sha256,
            ),
            run_id=state.run_id,
            context_packet=packet,
            allowed_actions=_allowed_actions(self._store.get_case(case_id)),
            action_contract_ref=ACTION_CONTRACT_REF,
            requested_at=self._now(),
        )
        proposal = self._provider.propose(request)
        return self._commit_proposal(state, proposal)

    def run_until_boundary(
        self,
        case_id: str,
        *,
        max_agent_steps: int = 100,
    ) -> ControllerState:
        if max_agent_steps < 1:
            raise ValueError("max_agent_steps must be positive")
        state = self.resume(case_id)
        for _ in range(max_agent_steps):
            if state.phase is not ControllerPhase.READY_FOR_AGENT:
                return state
            state = self.advance(case_id)
        raise ControllerConflict(
            "agent step safety limit reached without an interruption boundary"
        )

    def submit_user_decision(
        self,
        case_id: str,
        *,
        selected_option_id: str | None = None,
        freeform_response: str | None = None,
    ) -> ControllerState:
        snapshot = self.resume(case_id)
        if snapshot.phase is not ControllerPhase.WAITING_FOR_USER:
            raise ControllerConflict("case is not waiting for a user decision")
        request = self._store.get_decision_request(
            snapshot.pending_decision_request_id or ""
        )
        if (selected_option_id is None) == (freeform_response is None):
            raise ValueError(
                "provide exactly one selected option or freeform response"
            )
        option_ids = {option.option_id for option in request.options}
        if (
            selected_option_id is not None
            and selected_option_id not in option_ids
        ):
            raise ValueError("selected option is not present in the request")
        if freeform_response is not None and not freeform_response.strip():
            raise ValueError("freeform_response must be non-empty")
        now = self._now()
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                current = self.resume(case_id)
                _require_same_checkpoint(snapshot, current)
                decision = DecisionRecord(
                    decision_record_id=_stable_id(
                        "decision",
                        request.decision_request_id,
                        selected_option_id or freeform_response or "",
                    ),
                    case_id=case_id,
                    question=request.question,
                    options=request.options,
                    selected_option_id=selected_option_id,
                    freeform_response=freeform_response,
                    source="user",
                    created_at=now,
                )
                self._store.record_decision(
                    decision,
                    event_id=_stable_id(
                        "event",
                        decision.decision_record_id,
                        "recorded",
                    ),
                )
                selected_message = (
                    "用户选择：{}".format(selected_option_id)
                    if selected_option_id is not None
                    else "用户补充：{}".format(freeform_response)
                )
                return self._checkpoint(
                    run_id=current.run_id,
                    case_id=case_id,
                    phase=ControllerPhase.READY_FOR_AGENT,
                    step_number=current.step_number,
                    latest_user_message=selected_message,
                    pending_action_id=None,
                    pending_outbox_message_id=None,
                    pending_decision_request_id=None,
                    consecutive_rejections=0,
                    now=now,
                )
        finally:
            self._store.release_lease(lease)

    def deliver_pending_effect(self, case_id: str) -> ControllerState:
        snapshot = self.resume(case_id)
        if snapshot.phase is not ControllerPhase.WAITING_FOR_EFFECT:
            raise ControllerConflict("case has no pending effect")
        message = self._store.get_outbox_message(
            snapshot.pending_outbox_message_id or ""
        )
        started_at = self._now()
        try:
            result = self._effect_executor.execute(message)
        except EffectTransientError as error:
            return self._commit_effect_attempt(
                snapshot=snapshot,
                message=message,
                status=EffectAttemptStatus.RETRYABLE_FAILURE,
                result=None,
                error_code="transient_effect_failure",
                error_message=str(error),
                started_at=started_at,
            )
        except EffectPermanentError as error:
            return self._commit_effect_attempt(
                snapshot=snapshot,
                message=message,
                status=EffectAttemptStatus.TERMINAL_FAILURE,
                result=None,
                error_code="permanent_effect_failure",
                error_message=str(error),
                started_at=started_at,
            )
        return self._commit_effect_attempt(
            snapshot=snapshot,
            message=message,
            status=EffectAttemptStatus.SUCCEEDED,
            result=result,
            error_code=None,
            error_message=None,
            started_at=started_at,
        )

    def _commit_proposal(
        self,
        snapshot: ControllerState,
        proposal: AgentActionProposal,
    ) -> ControllerState:
        now = self._now()
        action = ActionEnvelope(
            action_id=_stable_id(
                "action",
                snapshot.run_id,
                str(snapshot.step_number + 1),
                proposal.content_sha256,
            ),
            case_id=snapshot.case_id,
            kind=proposal.kind,
            expected_head_version=snapshot.head_version,
            idempotency_key=_stable_id(
                "action-key",
                snapshot.run_id,
                str(snapshot.step_number + 1),
                snapshot.context_packet_id,
            ),
            issued_at=now,
            payload=proposal.payload,
        )
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                persisted = PersistedAction(
                    action=action,
                    proposal_sha256=proposal.content_sha256,
                    recorded_at=now,
                )
                self._store.record_action(persisted)
                case = self._store.get_case(snapshot.case_id)
                frame = (
                    None
                    if case.accepted_frame_revision_id is None
                    else self._store.get_frame(
                        case.accepted_frame_revision_id
                    )
                )
                plan = (
                    None
                    if case.accepted_plan_revision_id is None
                    else self._store.get_plan(case.accepted_plan_revision_id)
                )
                admission = admit_action(
                    case=case,
                    action=action,
                    current_frame=frame,
                    current_plan=plan,
                )
                admission_event = self._append_action_event(
                    action=action,
                    accepted=admission.accepted,
                    reason_code=admission.reason_code,
                    now=now,
                )
                if not admission.accepted:
                    next_state = self._checkpoint(
                        run_id=current.run_id,
                        case_id=current.case_id,
                        phase=ControllerPhase.READY_FOR_AGENT,
                        step_number=current.step_number + 1,
                        latest_user_message=current.latest_user_message,
                        pending_action_id=None,
                        pending_outbox_message_id=None,
                        pending_decision_request_id=None,
                        consecutive_rejections=current.consecutive_rejections + 1,
                        now=now,
                    )
                    self._record_receipt(
                        action=action,
                        event_cursor=admission_event.cursor,
                        state=next_state,
                        result_code=admission.reason_code,
                        now=now,
                    )
                    return next_state
                next_state, outcome_cursor = self._apply_action(
                    current=current,
                    action=action,
                    admission_cursor=admission_event.cursor,
                    now=now,
                )
                self._record_receipt(
                    action=action,
                    event_cursor=outcome_cursor,
                    state=next_state,
                    result_code="accepted",
                    now=now,
                )
                return next_state
        finally:
            self._store.release_lease(lease)

    def _apply_action(
        self,
        *,
        current: ControllerState,
        action: ActionEnvelope,
        admission_cursor: int,
        now: datetime,
    ) -> tuple[ControllerState, int]:
        payload = action.payload
        case = self._store.get_case(current.case_id)
        phase = ControllerPhase.READY_FOR_AGENT
        pending_outbox_id: str | None = None
        pending_decision_id: str | None = None
        outcome_cursor = admission_cursor

        if action.kind is ActionKind.REVISE_FRAME:
            assert isinstance(payload, ReviseFramePayload)
            prior = (
                None
                if case.accepted_frame_revision_id is None
                else self._store.get_frame(case.accepted_frame_revision_id)
            )
            frame = AnalysisFrameRevision(
                frame_revision_id=_stable_id(
                    "frame",
                    action.action_id,
                    payload.revision_reason,
                ),
                case_id=case.case_id,
                revision_number=1 if prior is None else prior.revision_number + 1,
                prior_frame_revision_id=(
                    None if prior is None else prior.frame_revision_id
                ),
                created_by_action_id=action.action_id,
                created_at=now,
                revision_reason=payload.revision_reason,
                estimand=payload.estimand,
                population=payload.population,
                time_scope=payload.time_scope,
                observation_unit=payload.observation_unit,
                primary_estimator=payload.primary_estimator,
                comparison=payload.comparison,
                exposure=payload.exposure,
                measurement_rationale=payload.measurement_rationale,
                assumptions=payload.assumptions,
                alternatives=payload.alternatives,
                requirements=payload.requirements,
                falsification_conditions=payload.falsification_conditions,
                reversal_conditions=payload.reversal_conditions,
                success_conditions=payload.success_conditions,
                stop_conditions=payload.stop_conditions,
                decision_record_ids=payload.decision_record_ids,
                semantic_contract_refs=payload.semantic_contract_refs,
            )
            case = self._store.accept_frame(
                frame,
                expected_head_version=case.head_version,
                event_id=_stable_id("event", frame.frame_revision_id, "accepted"),
                recorded_at=now,
            )
            outcome_cursor = self._last_cursor(case.case_id)
        elif action.kind is ActionKind.REVISE_PLAN:
            assert isinstance(payload, RevisePlanPayload)
            prior = self._latest_plan(case.case_id)
            plan = WorkPlanRevision(
                plan_revision_id=_stable_id(
                    "plan",
                    action.action_id,
                    payload.revision_reason,
                ),
                case_id=case.case_id,
                frame_revision_id=case.accepted_frame_revision_id or "",
                revision_number=1 if prior is None else prior.revision_number + 1,
                prior_plan_revision_id=(
                    None if prior is None else prior.plan_revision_id
                ),
                created_by_action_id=action.action_id,
                created_at=now,
                revision_reason=payload.revision_reason,
                tasks=payload.tasks,
            )
            case = self._store.accept_plan(
                plan,
                expected_head_version=case.head_version,
                event_id=_stable_id("event", plan.plan_revision_id, "accepted"),
                recorded_at=now,
            )
            outcome_cursor = self._last_cursor(case.case_id)
        elif action.kind is ActionKind.RECORD_INTERPRETATION:
            assert isinstance(payload, RecordInterpretationPayload)
            interpretation = InterpretationRecord(
                interpretation_id=_stable_id(
                    "interpretation",
                    action.action_id,
                ),
                case_id=case.case_id,
                frame_revision_id=case.accepted_frame_revision_id or "",
                evidence_record_ids=payload.evidence_record_ids,
                interpretation=payload.interpretation,
                created_by_action_id=action.action_id,
                created_at=now,
            )
            self._store.record_interpretation(
                interpretation,
                event_id=_stable_id(
                    "event",
                    interpretation.interpretation_id,
                    "recorded",
                ),
            )
            outcome_cursor = self._last_cursor(case.case_id)
        elif action.kind is ActionKind.ASK_USER:
            assert isinstance(payload, AskUserPayload)
            request = UserDecisionRequest(
                decision_request_id=_stable_id(
                    "decision-request",
                    action.action_id,
                ),
                case_id=case.case_id,
                action_id=action.action_id,
                question=payload.question,
                options=payload.options,
                recommended_option_id=payload.recommended_option_id,
                allow_freeform=payload.allow_freeform,
                requested_at=now,
            )
            self._store.record_decision_request(request)
            event = self._append_event(
                case_id=case.case_id,
                event_id=_stable_id(
                    "event",
                    request.decision_request_id,
                    "requested",
                ),
                event_type=JournalEventType.USER_DECISION_REQUESTED,
                action_id=action.action_id,
                authority_ref=request.decision_request_id,
                payload={
                    "recommended_option_id": request.recommended_option_id,
                },
                customer_projection={
                    "question": request.question,
                    "options": tuple(
                        {
                            "option_id": option.option_id,
                            "label": option.label,
                            "impact": option.impact,
                        }
                        for option in request.options
                    ),
                    "recommended_option_id": request.recommended_option_id,
                },
                now=now,
            )
            outcome_cursor = event.cursor
            phase = ControllerPhase.WAITING_FOR_USER
            pending_decision_id = request.decision_request_id
        elif action.kind is ActionKind.PROPOSE_ANSWER:
            assert isinstance(payload, ProposeAnswerPayload)
            self._validate_answer_evidence_bindings(
                case_id=case.case_id,
                frame_revision_id=case.accepted_frame_revision_id or "",
                plan_revision_id=case.accepted_plan_revision_id or "",
                payload=payload,
            )
            prior = self._latest_answer(case.case_id)
            answer = AnswerVersion(
                answer_version_id=_stable_id("answer", action.action_id),
                case_id=case.case_id,
                frame_revision_id=case.accepted_frame_revision_id or "",
                plan_revision_id=case.accepted_plan_revision_id or "",
                version_number=(
                    1 if prior is None else prior.version_number + 1
                ),
                prior_answer_version_id=(
                    None if prior is None else prior.answer_version_id
                ),
                status=AnswerStatus.PROVISIONAL,
                claims=tuple(
                    AnswerClaim(
                        claim_id=claim.claim_id,
                        statement=claim.statement,
                        applicability=claim.applicability,
                        evidence_record_ids=claim.evidence_record_ids,
                        boundary_ref=claim.boundary_ref,
                        limitations=claim.limitations,
                        verifier_status=ClaimVerifierStatus.PENDING,
                        reviewer_objection_ids=(),
                    )
                    for claim in payload.claims
                ),
                narrative_markdown=payload.narrative_markdown,
                verifier_policy_version=ANSWER_VERIFIER_POLICY,
                unresolved_blocking_objection_ids=(),
                settlement_fingerprint=None,
                created_by_action_id=action.action_id,
                created_at=now,
            )
            case = self._store.accept_answer(
                answer,
                expected_head_version=case.head_version,
                event_id=_stable_id("event", answer.answer_version_id, "accepted"),
                recorded_at=now,
            )
            outcome_cursor = self._last_cursor(case.case_id)
            phase = ControllerPhase.COMPLETED
        elif action.kind is ActionKind.STOP:
            assert isinstance(payload, StopPayload)
            lifecycle = CaseLifecycle(payload.terminal_state)
            case = self._store.transition_case_lifecycle(
                case_id=case.case_id,
                lifecycle=lifecycle,
                expected_head_version=case.head_version,
                event_id=_stable_id(
                    "event",
                    action.action_id,
                    payload.terminal_state,
                ),
                action_id=action.action_id,
                recorded_at=now,
            )
            outcome_cursor = self._last_cursor(case.case_id)
            phase = (
                ControllerPhase.STOPPED
                if lifecycle is CaseLifecycle.STOPPED
                else ControllerPhase.COMPLETED
            )
        elif action.kind in _EFFECT_ACTIONS:
            outbox = self._make_outbox(action, now=now)
            task_id = _effect_task_id(action)
            customer_projection = {
                "state": "investigating",
                "action_kind": action.kind.value,
            }
            if task_id is not None:
                customer_projection["task_id"] = task_id
            event = self._append_event(
                case_id=case.case_id,
                event_id=_stable_id(
                    "event",
                    outbox.outbox_message_id,
                    "enqueued",
                ),
                event_type=JournalEventType.EFFECT_ENQUEUED,
                action_id=action.action_id,
                authority_ref=outbox.outbox_message_id,
                payload={
                    "destination": outbox.destination,
                    "payload_sha256": outbox.payload_sha256,
                },
                customer_projection=customer_projection,
                now=now,
            )
            outbox = replace(outbox, source_event_cursor=event.cursor)
            self._store.enqueue_outbox(outbox)
            outcome_cursor = event.cursor
            phase = ControllerPhase.WAITING_FOR_EFFECT
            pending_outbox_id = outbox.outbox_message_id
        else:
            raise AssertionError("unhandled admitted action")

        next_state = self._checkpoint(
            run_id=current.run_id,
            case_id=current.case_id,
            phase=phase,
            step_number=current.step_number + 1,
            latest_user_message=current.latest_user_message,
            pending_action_id=(
                action.action_id
                if phase
                in {
                    ControllerPhase.WAITING_FOR_USER,
                    ControllerPhase.WAITING_FOR_EFFECT,
                }
                else None
            ),
            pending_outbox_message_id=pending_outbox_id,
            pending_decision_request_id=pending_decision_id,
            consecutive_rejections=0,
            now=now,
        )
        return next_state, outcome_cursor

    def _commit_effect_attempt(
        self,
        *,
        snapshot: ControllerState,
        message: OutboxMessage,
        status: EffectAttemptStatus,
        result: EffectExecutionResult | None,
        error_code: str | None,
        error_message: str | None,
        started_at: datetime,
    ) -> ControllerState:
        completed_at = self._now()
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                attempts = self._store.list_effect_attempts(
                    message.outbox_message_id
                )
                prior = None if not attempts else attempts[-1]
                attempt = EffectAttemptRecord(
                    effect_attempt_id=_stable_id(
                        "effect-attempt",
                        message.outbox_message_id,
                        str(len(attempts) + 1),
                        status.value,
                    ),
                    outbox_message_id=message.outbox_message_id,
                    case_id=message.case_id,
                    attempt_number=len(attempts) + 1,
                    prior_attempt_id=(
                        None if prior is None else prior.effect_attempt_id
                    ),
                    status=status,
                    result_payload=(
                        None
                        if result is None
                        else {
                            "payload": result.payload,
                            "business_summary": result.business_summary,
                            "evidence": tuple(
                                to_jsonable(draft)
                                for draft in result.evidence
                            ),
                        }
                    ),
                    result_sha256=(
                        None if result is None else result.content_sha256
                    ),
                    error_code=error_code,
                    error_message=error_message,
                    started_at=started_at,
                    completed_at=completed_at,
                )
                self._store.record_effect_attempt(attempt)
                evidence_ids: tuple[str, ...] = ()
                if status is EffectAttemptStatus.SUCCEEDED:
                    assert result is not None
                    action = self._store.get_action(message.action_id).action
                    task_id = _effect_task_id(action)
                    evidence_ids = self._record_effect_evidence(
                        current=current,
                        message=message,
                        result=result,
                        recorded_at=completed_at,
                    )
                    event_type = JournalEventType.EFFECT_COMPLETED
                    projection = {
                        "state": "completed",
                        "business_summary": result.business_summary,
                        "evidence_record_ids": evidence_ids,
                    }
                    if task_id is not None:
                        projection["task_id"] = task_id
                    phase = ControllerPhase.READY_FOR_AGENT
                    pending_outbox_id = None
                    pending_action_id = None
                elif status is EffectAttemptStatus.RETRYABLE_FAILURE:
                    event_type = JournalEventType.EFFECT_ATTEMPT_FAILED
                    projection = None
                    phase = ControllerPhase.WAITING_FOR_EFFECT
                    pending_outbox_id = message.outbox_message_id
                    pending_action_id = current.pending_action_id
                else:
                    action = self._store.get_action(message.action_id).action
                    task_id = _effect_task_id(action)
                    event_type = JournalEventType.EFFECT_ATTEMPT_FAILED
                    projection = {
                        "state": "blocked",
                        "reason_code": error_code or "effect_failure",
                    }
                    if task_id is not None:
                        projection["task_id"] = task_id
                    phase = ControllerPhase.READY_FOR_AGENT
                    pending_outbox_id = None
                    pending_action_id = None
                self._append_event(
                    case_id=message.case_id,
                    event_id=_stable_id(
                        "event",
                        attempt.effect_attempt_id,
                        status.value,
                    ),
                    event_type=event_type,
                    action_id=message.action_id,
                    authority_ref=attempt.effect_attempt_id,
                    payload={
                        "status": status.value,
                        "outbox_message_id": message.outbox_message_id,
                        "error_code": error_code,
                        "result_sha256": (
                            None
                            if result is None
                            else result.content_sha256
                        ),
                        "evidence_record_ids": evidence_ids,
                    },
                    customer_projection=projection,
                    now=completed_at,
                )
                return self._checkpoint(
                    run_id=current.run_id,
                    case_id=current.case_id,
                    phase=phase,
                    step_number=current.step_number,
                    latest_user_message=current.latest_user_message,
                    pending_action_id=pending_action_id,
                    pending_outbox_message_id=pending_outbox_id,
                    pending_decision_request_id=None,
                    consecutive_rejections=current.consecutive_rejections,
                    now=completed_at,
                )
        finally:
            self._store.release_lease(lease)

    def _record_effect_evidence(
        self,
        *,
        current: ControllerState,
        message: OutboxMessage,
        result: EffectExecutionResult,
        recorded_at: datetime,
    ) -> tuple[str, ...]:
        if not result.evidence:
            return ()
        case = self._store.get_case(current.case_id)
        if (
            case.accepted_frame_revision_id is None
            or case.accepted_plan_revision_id is None
        ):
            raise ControllerConflict(
                "effect evidence requires accepted frame and plan"
            )
        frame = self._store.get_frame(case.accepted_frame_revision_id)
        action = self._store.get_action(message.action_id).action
        expected_task_id = _effect_task_id(action)
        if expected_task_id is None:
            raise ControllerConflict(
                "semantic inspection cannot materialize analysis evidence"
            )
        evidence_ids: list[str] = []
        for index, draft in enumerate(result.evidence):
            _validate_evidence_draft(
                draft=draft,
                action=action,
                expected_task_id=expected_task_id,
                frame_contract_refs=frame.semantic_contract_refs,
            )
            evidence_id = _stable_id(
                "evidence",
                message.outbox_message_id,
                str(index),
                draft.content_sha256,
            )
            evidence = EvidenceRecord(
                evidence_record_id=evidence_id,
                case_id=case.case_id,
                frame_revision_id=case.accepted_frame_revision_id,
                plan_revision_id=case.accepted_plan_revision_id,
                task_id=draft.task_id,
                capability_name=draft.capability_name,
                query_spec_ref=draft.query_spec_ref,
                semantic_contract_refs=draft.semantic_contract_refs,
                snapshot_release_ref=draft.snapshot_release_ref,
                grain=draft.grain,
                evidence_type=draft.evidence_type,
                strength=draft.strength,
                business_summary=draft.business_summary,
                limitations=draft.limitations,
                provenance=draft.provenance,
                payload_sha256=draft.payload_sha256,
                inline_payload=draft.inline_payload,
                result_handle=draft.result_handle,
                created_at=recorded_at,
            )
            self._store.record_evidence(
                evidence,
                expected_head_version=case.head_version,
                event_id=_stable_id("event", evidence_id, "recorded"),
                recorded_at=recorded_at,
            )
            evidence_ids.append(evidence_id)
        return tuple(evidence_ids)

    def _validate_answer_evidence_bindings(
        self,
        *,
        case_id: str,
        frame_revision_id: str,
        plan_revision_id: str,
        payload: ProposeAnswerPayload,
    ) -> None:
        for claim in payload.claims:
            for evidence_id in claim.evidence_record_ids:
                evidence = self._store.get_evidence(evidence_id)
                if (
                    evidence.case_id != case_id
                    or evidence.frame_revision_id != frame_revision_id
                    or evidence.plan_revision_id != plan_revision_id
                ):
                    raise ControllerConflict(
                        "answer evidence does not bind the accepted authority"
                    )

    def _checkpoint(
        self,
        *,
        run_id: str,
        case_id: str,
        phase: ControllerPhase,
        step_number: int,
        latest_user_message: str,
        pending_action_id: str | None,
        pending_outbox_message_id: str | None,
        pending_decision_request_id: str | None,
        consecutive_rejections: int,
        now: datetime,
    ) -> ControllerState:
        packet = self._build_context(
            case_id=case_id,
            user_message=latest_user_message,
            now=now,
            step_number=step_number,
            run_id=run_id,
        )
        self._store.record_context_packet(packet)
        case = self._store.get_case(case_id)
        current_cursor = self._last_cursor(case_id)
        checkpoint_cursor = current_cursor + 1
        checkpoint_id = _stable_id(
            "checkpoint",
            run_id,
            str(step_number),
            str(checkpoint_cursor),
            packet.content_sha256,
        )
        state = ControllerState(
            run_id=run_id,
            case_id=case_id,
            phase=phase,
            step_number=step_number,
            head_version=case.head_version,
            last_event_cursor=checkpoint_cursor,
            context_packet_id=packet.packet_id,
            latest_user_message=latest_user_message,
            pending_action_id=pending_action_id,
            pending_outbox_message_id=pending_outbox_message_id,
            pending_decision_request_id=pending_decision_request_id,
            accepted_answer_version_id=case.accepted_answer_version_id,
            consecutive_rejections=consecutive_rejections,
            updated_at=now,
        )
        state_payload = encode_record(state)
        state_sha = content_sha256(state_payload)
        self._store.append_event(
            case_id=case_id,
            expected_next_cursor=checkpoint_cursor,
            event_id=_stable_id("event", checkpoint_id, "recorded"),
            event_type=JournalEventType.CHECKPOINT_RECORDED,
            recorded_at=now,
            action_id=pending_action_id,
            authority_ref=checkpoint_id,
            payload={
                "context_packet_id": packet.packet_id,
                "context_sha256": packet.content_sha256,
                "state_sha256": state_sha,
            },
            customer_projection=None,
        )
        checkpoint = CheckpointRecord(
            checkpoint_id=checkpoint_id,
            case_id=case_id,
            head_version=case.head_version,
            event_cursor=checkpoint_cursor,
            context_packet_id=packet.packet_id,
            context_sha256=packet.content_sha256,
            state_schema_ref=CONTROLLER_STATE_SCHEMA_REF,
            state_payload=state_payload,
            state_sha256=state_sha,
            created_at=now,
        )
        self._store.record_checkpoint(checkpoint)
        return state

    def _build_context(
        self,
        *,
        case_id: str,
        user_message: str,
        now: datetime,
        step_number: int,
        run_id: str,
    ):
        case = self._store.get_case(case_id)
        frame = (
            None
            if case.accepted_frame_revision_id is None
            else self._store.get_frame(case.accepted_frame_revision_id)
        )
        plan = (
            None
            if case.accepted_plan_revision_id is None
            else self._store.get_plan(case.accepted_plan_revision_id)
        )
        answer = (
            None
            if case.accepted_answer_version_id is None
            else self._store.get_answer(case.accepted_answer_version_id)
        )
        events = self._store.list_events(case_id)
        event_end = events[-1].cursor
        event_start = max(1, event_end - MAX_CONTEXT_EVENTS + 1)
        business_event_items = []
        for event in events:
            if event.cursor < event_start:
                continue
            agent_result = self._agent_result_for_event(event)
            if event.customer_projection is None and agent_result is None:
                continue
            business_event_items.append(
                ContextEventItem.from_event(
                    event,
                    agent_result=agent_result,
                )
            )
        business_events = tuple(business_event_items)
        evidence = self._store.list_evidence(case_id)[-MAX_CONTEXT_EVIDENCE:]
        decisions = self._store.list_decisions(case_id)[-MAX_CONTEXT_DECISIONS:]
        objections = self._store.list_reviewer_objections(case_id)[
            -MAX_CONTEXT_OBJECTIONS:
        ]
        packet_id = _stable_id(
            "context",
            run_id,
            str(step_number),
            str(case.head_version),
            str(event_end),
            content_sha256(user_message),
        )
        return build_context_packet(
            packet_id=packet_id,
            case=case,
            user_message=user_message,
            relevant_event_cursor_start=event_start,
            relevant_event_cursor_end=event_end,
            accepted_frame=frame,
            accepted_plan=plan,
            accepted_answer=answer,
            recent_events=business_events,
            evidence_index=tuple(
                ContextEvidenceItem.from_record(record)
                for record in evidence
            ),
            decision_index=tuple(
                ContextDecisionItem.from_record(record)
                for record in decisions
            ),
            reviewer_objection_index=tuple(
                ContextReviewerObjectionItem.from_record(record)
                for record in objections
            ),
            built_at=now,
        )

    def _agent_result_for_event(self, event):
        if event.event_type is JournalEventType.ACTION_REJECTED:
            return {"admission": event.payload}
        if event.event_type is not JournalEventType.EFFECT_COMPLETED:
            return None
        outbox_message_id = event.payload.get("outbox_message_id")
        if not isinstance(outbox_message_id, str):
            return None
        attempts = self._store.list_effect_attempts(outbox_message_id)
        succeeded = tuple(
            attempt
            for attempt in attempts
            if attempt.status is EffectAttemptStatus.SUCCEEDED
        )
        if not succeeded:
            return None
        return succeeded[-1].result_payload

    def _append_action_event(
        self,
        *,
        action: ActionEnvelope,
        accepted: bool,
        reason_code: str,
        now: datetime,
    ):
        return self._append_event(
            case_id=action.case_id,
            event_id=_stable_id(
                "event",
                action.action_id,
                "admitted" if accepted else "rejected",
            ),
            event_type=(
                JournalEventType.ACTION_ADMITTED
                if accepted
                else JournalEventType.ACTION_REJECTED
            ),
            action_id=action.action_id,
            authority_ref=action.action_id,
            payload={
                "action_kind": action.kind.value,
                "reason_code": reason_code,
                "request_sha256": action.content_sha256,
            },
            customer_projection=None,
            now=now,
        )

    def _append_event(
        self,
        *,
        case_id: str,
        event_id: str,
        event_type: JournalEventType,
        action_id: str | None,
        authority_ref: str | None,
        payload: dict[str, object],
        customer_projection: dict[str, object] | None,
        now: datetime,
    ):
        return self._store.append_event(
            case_id=case_id,
            expected_next_cursor=self._last_cursor(case_id) + 1,
            event_id=event_id,
            event_type=event_type,
            recorded_at=now,
            action_id=action_id,
            authority_ref=authority_ref,
            payload=payload,
            customer_projection=customer_projection,
        )

    def _make_outbox(
        self,
        action: ActionEnvelope,
        *,
        now: datetime,
    ) -> OutboxMessage:
        case = self._store.get_case(action.case_id)
        payload = {
            "action_kind": action.kind.value,
            "request": to_jsonable(action.payload),
            "expected_head_version": action.expected_head_version,
            "frame_revision_id": case.accepted_frame_revision_id,
            "plan_revision_id": case.accepted_plan_revision_id,
        }
        return OutboxMessage(
            outbox_message_id=_stable_id("outbox", action.action_id),
            case_id=action.case_id,
            source_event_cursor=1,
            action_id=action.action_id,
            idempotency_key=_stable_id("effect-key", action.action_id),
            destination=_effect_destination(action),
            contract_ref=EFFECT_CONTRACT_REF,
            payload=payload,
            payload_sha256=content_sha256(payload),
            created_at=now,
        )

    def _record_receipt(
        self,
        *,
        action: ActionEnvelope,
        event_cursor: int,
        state: ControllerState,
        result_code: str,
        now: datetime,
    ) -> None:
        result_payload = {
            "result_code": result_code,
            "phase": state.phase.value,
            "head_version": state.head_version,
            "checkpoint_event_cursor": state.last_event_cursor,
        }
        self._store.record_action_receipt(
            ActionReceipt(
                case_id=action.case_id,
                idempotency_key=action.idempotency_key,
                action_id=action.action_id,
                request_sha256=action.content_sha256,
                result_schema_ref=ACTION_RESULT_SCHEMA_REF,
                result_payload=result_payload,
                result_sha256=content_sha256(result_payload),
                event_cursor=event_cursor,
                recorded_at=now,
            )
        )

    def _last_cursor(self, case_id: str) -> int:
        events = self._store.list_events(case_id)
        if not events:
            raise ControllerConflict("case event journal is empty")
        return events[-1].cursor

    def _latest_plan(self, case_id: str) -> WorkPlanRevision | None:
        for event in reversed(self._store.list_events(case_id)):
            if event.event_type is JournalEventType.PLAN_ACCEPTED:
                return self._store.get_plan(event.authority_ref or "")
        return None

    def _latest_answer(self, case_id: str) -> AnswerVersion | None:
        for event in reversed(self._store.list_events(case_id)):
            if event.event_type is JournalEventType.ANSWER_ACCEPTED:
                return self._store.get_answer(event.authority_ref or "")
        return None

    def _acquire(self, state: ControllerState):
        now = self._now()
        return self._store.acquire_lease(
            case_id=state.case_id,
            run_id=state.run_id,
            owner_id=self._owner_id,
            now=now,
            expires_at=now + self._lease_duration,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("controller clock must return timezone-aware time")
        return value


def _allowed_actions(case) -> tuple[ActionKind, ...]:
    if case.accepted_frame_revision_id is None:
        return (
            ActionKind.REVISE_FRAME,
            ActionKind.INSPECT_SEMANTICS,
            ActionKind.ASK_USER,
            ActionKind.STOP,
        )
    if case.accepted_plan_revision_id is None:
        return (
            ActionKind.REVISE_FRAME,
            ActionKind.REVISE_PLAN,
            ActionKind.INSPECT_SEMANTICS,
            ActionKind.ASK_USER,
            ActionKind.STOP,
        )
    return tuple(ActionKind)


def _effect_destination(action: ActionEnvelope) -> str:
    payload = action.payload
    if isinstance(payload, InspectSemanticsPayload):
        return "semantic_inspection"
    if isinstance(payload, RunProbePayload):
        return "analysis_probe"
    if isinstance(payload, CallCapabilityPayload):
        return "capability:{}".format(payload.capability_name)
    if isinstance(payload, RunSensitivityPayload):
        return "sensitivity:{}".format(payload.variant_label)
    raise TypeError("action is not an effect action")


def _effect_task_id(action: ActionEnvelope) -> str | None:
    payload = action.payload
    if isinstance(
        payload,
        RunProbePayload | CallCapabilityPayload | RunSensitivityPayload,
    ):
        return payload.task_id
    return None


def _validate_evidence_draft(
    *,
    draft: EvidenceDraft,
    action: ActionEnvelope,
    expected_task_id: str,
    frame_contract_refs: tuple[str, ...],
) -> None:
    if draft.task_id != expected_task_id:
        raise ControllerConflict(
            "effect evidence task does not match the admitted action"
        )
    payload = action.payload
    if (
        isinstance(payload, CallCapabilityPayload)
        and draft.capability_name != payload.capability_name
    ):
        raise ControllerConflict(
            "effect evidence capability does not match the admitted action"
        )
    if (
        isinstance(payload, RunProbePayload)
        and draft.capability_name != payload.probe_kind
    ):
        raise ControllerConflict(
            "probe evidence kind does not match the admitted action"
        )
    missing_contracts = set(frame_contract_refs) - set(
        draft.semantic_contract_refs
    )
    if missing_contracts:
        raise ControllerConflict(
            "effect evidence omits accepted semantic contract bindings"
        )


def _require_same_checkpoint(
    expected: ControllerState,
    actual: ControllerState,
) -> None:
    if (
        expected.run_id != actual.run_id
        or expected.context_packet_id != actual.context_packet_id
        or expected.last_event_cursor != actual.last_event_cursor
        or expected.content_sha256 != actual.content_sha256
    ):
        raise ControllerConflict(
            "controller state changed while work was in flight"
        )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return "{}-{}".format(prefix, digest[:24])
