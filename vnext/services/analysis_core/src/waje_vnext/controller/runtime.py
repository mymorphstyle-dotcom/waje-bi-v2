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
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    MailboxMessageKind,
    MessageIngressReceipt,
    OperationIdentity,
)
from waje_vnext.domain.authority import (
    AnswerClaim,
    AnswerStatus,
    AnswerVersion,
    CaseLifecycle,
    ClaimVerifierStatus,
    DecisionRecord,
    InterpretationRecord,
    WorkPlanRevision,
)
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.identity import build_analysis_frame_revision
from waje_vnext.domain.context import (
    MAX_CONTEXT_DECISIONS,
    MAX_CONTEXT_EVIDENCE,
    MAX_CONTEXT_EVENTS,
    MAX_CONTEXT_OBJECTIONS,
    ContextDecisionItem,
    ContextEvidenceItem,
    ContextEventItem,
    ContextReviewerObjectionItem,
    ContextUserMessageItem,
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
from waje_vnext.domain.measurement import (
    MessageRole,
    QuestionRevision,
    SourceMessageRef,
)
from waje_vnext.domain.runtime_state import (
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)
from waje_vnext.providers.base import PrimaryAgentProvider
from waje_vnext.storage.codec import decode_controller_state, encode_record
from waje_vnext.storage.ports import AuthorityStore, LeaseConflict

from .effects import (
    EffectExecutionResult,
    EffectExecutor,
    EffectPermanentError,
    EffectTransientError,
)


ACTION_CONTRACT_REF = "waje-vnext://contracts/domain/actions.v3"
CONTROLLER_STATE_SCHEMA_REF = (
    "waje-vnext://contracts/domain/controller-state.v1"
)
ACTION_RESULT_SCHEMA_REF = "waje-vnext://runtime/action-result.v1"
EFFECT_CONTRACT_REF = "waje-vnext://runtime/effect-request.v1"
PRIMARY_AGENT_JOB_CONTRACT_REF = (
    "waje-vnext://runtime/primary-agent-job.v1"
)
CONTROLLER_WAKE_CONTRACT_REF = "waje-vnext://runtime/controller-wake.v1"
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
        self.ingress_message(
            case_id=case_id,
            thread_id=thread_id,
            run_id=run_id,
            user_message=user_message,
            kind=MailboxMessageKind.USER_MESSAGE,
            idempotency_key=_stable_id(
                "ingress-key",
                case_id,
                run_id,
                content_sha256(user_message),
            ),
        )
        return self.resume(case_id)

    def ingress_message(
        self,
        *,
        case_id: str,
        thread_id: str,
        run_id: str,
        user_message: str,
        kind: MailboxMessageKind = MailboxMessageKind.USER_CORRECTION,
        idempotency_key: str,
    ) -> MessageIngressReceipt:
        """Persist a user command, journal fact, and controller wake atomically."""

        if not user_message.strip():
            raise ValueError("user_message must be non-empty")
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
            prior_message = next(
                (
                    candidate
                    for candidate in self._store.list_mailbox_messages(case_id)
                    if candidate.operation.idempotency_key == idempotency_key
                ),
                None,
            )
            if prior_message is not None:
                if (
                    prior_message.kind is not kind
                    or prior_message.payload.get("message") != user_message
                ):
                    raise ControllerConflict(
                        "ingress idempotency key has different content"
                    )
                prior_event = next(
                    event
                    for event in self._store.list_events(case_id)
                    if event.event_type
                    is JournalEventType.MESSAGE_INGRESSED
                    and event.authority_ref == prior_message.message_id
                )
                return MessageIngressReceipt(
                    case_id=case_id,
                    run_id=run_id,
                    message_id=prior_message.message_id,
                    operation_id=prior_message.operation.operation_id,
                    mailbox_sequence=prior_message.sequence,
                    authority_epoch=prior_message.authority_epoch,
                    event_cursor=prior_event.cursor,
                )
            mailbox_head = self._store.get_mailbox_head(case_id)
            message_payload = {"message": user_message}
            message_id = _stable_id(
                "message",
                case_id,
                idempotency_key,
                content_sha256(message_payload),
            )
            message_operation = OperationIdentity(
                operation_id=_stable_id("operation", message_id),
                idempotency_key=idempotency_key,
                causation_id=message_id,
                correlation_id=run_id,
                authority_revision=mailbox_head.authority_epoch,
                payload_sha256=content_sha256(message_payload),
            )
            message = self._store.append_mailbox_message(
                message_id=message_id,
                case_id=case_id,
                kind=kind,
                operation=message_operation,
                payload=message_payload,
                created_at=now,
            )
            event_payload = {
                "message_id": message.message_id,
                "message_kind": message.kind.value,
                "mailbox_sequence": message.sequence,
                "authority_epoch": message.authority_epoch,
                "message_payload_sha256": message.operation.payload_sha256,
            }
            ingress_event_id = _stable_id(
                "event",
                message.message_id,
                "ingressed",
            )
            ingress_event = self._store.append_event(
                case_id=case_id,
                expected_next_cursor=self._last_cursor(case_id) + 1,
                event_id=ingress_event_id,
                event_type=JournalEventType.MESSAGE_INGRESSED,
                recorded_at=now,
                action_id=None,
                authority_ref=message.message_id,
                payload=event_payload,
                customer_projection={
                    "state": "message_received",
                    "message_kind": message.kind.value,
                    "mailbox_sequence": message.sequence,
                },
                operation=_event_operation(
                    event_id=ingress_event_id,
                    idempotency_key=_stable_id(
                        "event-key",
                        message.operation.idempotency_key,
                        "ingressed",
                    ),
                    causation_id=message.operation.operation_id,
                    correlation_id=run_id,
                    authority_revision=message.authority_epoch,
                    payload=event_payload,
                ),
            )
            current_case = self._store.get_case(case_id)
            if (
                kind is MailboxMessageKind.USER_MESSAGE
                and current_case.accepted_question_revision_id is None
            ):
                question_event_id = _stable_id(
                    "event",
                    message.message_id,
                    "question-accepted",
                )
                question = QuestionRevision(
                    question_revision_id=f"{case_id}:question:1",
                    case_id=case_id,
                    revision_number=1,
                    prior_question_revision_id=None,
                    source_messages=(
                        SourceMessageRef(
                            message_id=message.message_id,
                            role=MessageRole.USER,
                            sequence=message.sequence,
                            content=user_message,
                            content_sha256=content_sha256(user_message),
                        ),
                    ),
                    explicit_scope_refs=(),
                    explicit_constraint_refs=(),
                    explicit_correction_refs=(),
                    explicit_challenge_refs=(),
                    accepted_clarification_refs=(),
                    acceptance_event_id=question_event_id,
                    accepted_head_version=current_case.head_version + 1,
                    analysis_cycle_id=f"{case_id}:cycle:1",
                    created_at=now,
                )
                self._store.accept_question(
                    question,
                    expected_head_version=current_case.head_version,
                    event_id=question_event_id,
                    recorded_at=now,
                )
            wake_payload = {
                "case_id": case_id,
                "run_id": run_id,
                "mailbox_sequence": message.sequence,
                "authority_epoch": message.authority_epoch,
            }
            wake_idempotency_key = _stable_id(
                "wake-key",
                message.operation.idempotency_key,
            )
            wake_operation = OperationIdentity(
                operation_id=_stable_id(
                    "operation",
                    case_id,
                    "wake",
                    str(message.sequence),
                ),
                idempotency_key=wake_idempotency_key,
                causation_id=message.operation.operation_id,
                correlation_id=run_id,
                authority_revision=message.authority_epoch,
                payload_sha256=content_sha256(wake_payload),
            )
            self._store.enqueue_outbox(
                OutboxMessage(
                    outbox_message_id=_stable_id(
                        "outbox",
                        case_id,
                        "wake",
                        str(message.sequence),
                    ),
                    case_id=case_id,
                    source_event_cursor=ingress_event.cursor,
                    action_id=None,
                    job_kind=AsyncJobKind.CONTROLLER_WAKE,
                    operation=wake_operation,
                    expected_head_version=self._store.get_case(
                        case_id
                    ).head_version,
                    expected_authority_epoch=message.authority_epoch,
                    idempotency_key=wake_idempotency_key,
                    destination="case-controller",
                    contract_ref=CONTROLLER_WAKE_CONTRACT_REF,
                    payload=wake_payload,
                    payload_sha256=content_sha256(wake_payload),
                    created_at=now,
                )
            )
            if existing is None:
                self._checkpoint(
                    run_id=run_id,
                    case_id=case_id,
                    phase=ControllerPhase.READY_FOR_AGENT,
                    step_number=0,
                    latest_user_message=user_message,
                    pending_action_id=None,
                    pending_job_ids=(),
                    pending_decision_request_id=None,
                    consecutive_rejections=0,
                    authority_epoch=message.authority_epoch,
                    mailbox_cursor=0,
                    context_user_messages=(
                        ContextUserMessageItem.from_message(message),
                    ),
                    now=now,
                )
            return MessageIngressReceipt(
                case_id=case_id,
                run_id=run_id,
                message_id=message.message_id,
                operation_id=message.operation.operation_id,
                mailbox_sequence=message.sequence,
                authority_epoch=message.authority_epoch,
                event_cursor=ingress_event.cursor,
            )

    def resume(self, case_id: str) -> ControllerState:
        checkpoint = self._store.latest_checkpoint(case_id)
        if checkpoint is None:
            raise ControllerConflict("case has no durable controller checkpoint")
        return decode_controller_state(checkpoint.state_payload)

    def dispatch_outbox(self, message_id: str) -> ControllerState:
        """Idempotently route a durable outbox delivery to its worker handler."""

        message = self._store.get_outbox_message(message_id)
        state = self.resume(message.case_id)
        if message.job_kind is AsyncJobKind.CONTROLLER_WAKE:
            return self.advance(message.case_id)
        if message.outbox_message_id not in state.pending_job_ids:
            return state
        if message.job_kind is AsyncJobKind.PRIMARY_AGENT:
            return self.deliver_pending_llm(message.case_id)
        if message.job_kind in {
            AsyncJobKind.SEMANTIC_INSPECTION,
            AsyncJobKind.DATA_PROBE,
            AsyncJobKind.CAPABILITY,
            AsyncJobKind.SENSITIVITY,
        }:
            return self.deliver_pending_effect(
                message.case_id,
                job_id=message.outbox_message_id,
            )
        raise ControllerConflict(
            "job kind {} is owned by a later Gate worker".format(
                message.job_kind.value
            )
        )

    def advance(self, case_id: str) -> ControllerState:
        state = self.resume(case_id)
        mailbox_head = self._store.get_mailbox_head(case_id)
        if mailbox_head.last_sequence > state.mailbox_cursor:
            state = self._reconcile_mailbox(state)
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
        return self._enqueue_primary_agent_job(state, request)

    def deliver_pending_llm(self, case_id: str) -> ControllerState:
        snapshot = self.resume(case_id)
        if snapshot.phase is not ControllerPhase.WAITING_FOR_LLM:
            raise ControllerConflict("case has no pending LLM job")
        if len(snapshot.pending_job_ids) != 1:
            raise ControllerConflict(
                "primary agent delivery requires exactly one pending job"
            )
        message = self._store.get_outbox_message(snapshot.pending_job_ids[0])
        if message.job_kind is not AsyncJobKind.PRIMARY_AGENT:
            raise ControllerConflict("pending job is not a primary agent job")
        if self._job_is_stale(message):
            return self._supersede_job(snapshot, message)
        job_lease = self._acquire_job(message)
        try:
            if self._job_is_stale(message):
                return self._supersede_job(snapshot, message)
            packet = self._store.get_context_packet(
                str(message.payload["context_packet_id"])
            )
            request = PrimaryAgentRequest(
                turn_id=str(message.payload["turn_id"]),
                run_id=str(message.payload["run_id"]),
                context_packet=packet,
                allowed_actions=tuple(
                    ActionKind(value)
                    for value in message.payload["allowed_actions"]
                ),
                action_contract_ref=str(
                    message.payload["action_contract_ref"]
                ),
                requested_at=message.created_at,
            )
            proposal = self._provider.propose(request)
            return self._commit_proposal(snapshot, proposal, message)
        finally:
            self._store.release_job_lease(job_lease)

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
            if state.phase is ControllerPhase.READY_FOR_AGENT:
                state = self.advance(case_id)
                continue
            if state.phase is ControllerPhase.WAITING_FOR_LLM:
                state = self.deliver_pending_llm(case_id)
                continue
            if state.phase not in {
                ControllerPhase.READY_FOR_AGENT,
                ControllerPhase.WAITING_FOR_LLM,
            }:
                return state
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
        selected_message = (
            "用户选择：{}".format(selected_option_id)
            if selected_option_id is not None
            else "用户补充：{}".format(freeform_response)
        )
        receipt = self.ingress_message(
            case_id=case_id,
            thread_id=self._store.get_case(case_id).thread_id,
            run_id=snapshot.run_id,
            user_message=selected_message,
            kind=MailboxMessageKind.USER_MESSAGE,
            idempotency_key=_stable_id(
                "decision-ingress-key",
                request.decision_request_id,
                selected_option_id or freeform_response or "",
            ),
        )
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
                return self._checkpoint(
                    run_id=current.run_id,
                    case_id=case_id,
                    phase=ControllerPhase.READY_FOR_AGENT,
                    step_number=current.step_number,
                    latest_user_message=selected_message,
                    pending_action_id=None,
                    pending_job_ids=(),
                    pending_decision_request_id=None,
                    consecutive_rejections=0,
                    authority_epoch=receipt.authority_epoch,
                    mailbox_cursor=receipt.mailbox_sequence,
                    context_user_messages=(
                        self._store.get_context_packet(
                            current.context_packet_id
                        ).user_messages
                        + (
                            ContextUserMessageItem.from_message(
                                next(
                                    message
                                    for message
                                    in self._store.list_mailbox_messages(
                                        case_id
                                    )
                                    if message.message_id
                                    == receipt.message_id
                                )
                            ),
                        )
                    ),
                    now=now,
                )
        finally:
            self._store.release_lease(lease)

    def deliver_pending_effect(
        self,
        case_id: str,
        *,
        job_id: str | None = None,
    ) -> ControllerState:
        snapshot = self.resume(case_id)
        if snapshot.phase is not ControllerPhase.WAITING_FOR_EFFECT:
            raise ControllerConflict("case has no pending effect")
        selected_job_id = job_id or snapshot.pending_job_ids[0]
        if selected_job_id not in snapshot.pending_job_ids:
            raise ControllerConflict("effect job is not pending for this case")
        message = self._store.get_outbox_message(selected_job_id)
        if self._job_is_stale(message):
            return self._supersede_job(snapshot, message)
        job_lease = self._acquire_job(message)
        try:
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
        finally:
            self._store.release_job_lease(job_lease)

    def _commit_proposal(
        self,
        snapshot: ControllerState,
        proposal: AgentActionProposal,
        message: OutboxMessage,
    ) -> ControllerState:
        now = self._now()
        action_id = _stable_id(
            "action",
            snapshot.run_id,
            str(snapshot.step_number + 1),
            proposal.content_sha256,
        )
        action_idempotency_key = _stable_id(
            "action-key",
            snapshot.run_id,
            str(snapshot.step_number + 1),
            snapshot.context_packet_id,
        )
        action = ActionEnvelope(
            action_id=action_id,
            case_id=snapshot.case_id,
            kind=proposal.kind,
            expected_head_version=snapshot.head_version,
            idempotency_key=action_idempotency_key,
            operation=OperationIdentity(
                operation_id=_stable_id("operation", action_id),
                idempotency_key=action_idempotency_key,
                causation_id=message.operation.operation_id,
                correlation_id=message.operation.correlation_id,
                authority_revision=snapshot.authority_epoch,
                payload_sha256=proposal.content_sha256,
            ),
            issued_at=now,
            payload=proposal.payload,
        )
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                if self._job_is_stale(message):
                    return self._supersede_job_locked(
                        current,
                        message,
                        now=now,
                    )
                completed_payload = {
                    "outbox_message_id": message.outbox_message_id,
                    "proposal_sha256": proposal.content_sha256,
                    "action_kind": proposal.kind.value,
                }
                completed_event_id = _stable_id(
                    "event",
                    message.outbox_message_id,
                    "completed",
                )
                self._store.append_event(
                    case_id=current.case_id,
                    expected_next_cursor=self._last_cursor(current.case_id) + 1,
                    event_id=completed_event_id,
                    event_type=JournalEventType.LLM_JOB_COMPLETED,
                    recorded_at=now,
                    action_id=action.action_id,
                    authority_ref=message.outbox_message_id,
                    payload=completed_payload,
                    customer_projection={"state": "analysis_decision_ready"},
                    operation=_event_operation(
                        event_id=completed_event_id,
                        idempotency_key=_stable_id(
                            "event-key",
                            message.operation.idempotency_key,
                            "completed",
                        ),
                        causation_id=message.operation.operation_id,
                        correlation_id=message.operation.correlation_id,
                        authority_revision=current.authority_epoch,
                        payload=completed_payload,
                    ),
                )
                persisted = PersistedAction(
                    action=action,
                    proposal_sha256=proposal.content_sha256,
                    recorded_at=now,
                )
                self._store.record_action(persisted)
                case = self._store.get_case(snapshot.case_id)
                plan = (
                    None
                    if case.accepted_plan_revision_id is None
                    else self._store.get_plan(case.accepted_plan_revision_id)
                )
                admission = admit_action(
                    case=case,
                    action=action,
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
                        pending_job_ids=(),
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
        pending_job_ids: tuple[str, ...] = ()
        pending_decision_id: str | None = None
        outcome_cursor = admission_cursor

        if action.kind is ActionKind.REVISE_FRAME:
            assert isinstance(payload, ReviseFramePayload)
            question = self._store.get_question(
                payload.question_revision_id
            )
            prior = (
                None
                if case.accepted_frame_revision_id is None
                else self._store.get_frame(case.accepted_frame_revision_id)
            )
            frame = build_analysis_frame_revision(
                question=question,
                frame_revision_id=_stable_id(
                    "frame",
                    action.action_id,
                    payload.revision_reason_ref,
                ),
                case_id=case.case_id,
                revision_number=1 if prior is None else prior.revision_number + 1,
                prior_frame_revision_id=(
                    None if prior is None else prior.frame_revision_id
                ),
                created_by_action_id=action.action_id,
                created_at=now,
                revision_reason_ref=payload.revision_reason_ref,
                measurement_design=payload.measurement_design,
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
                customer_projection={
                    "state": "investigating",
                    "action_kind": action.kind.value,
                },
                now=now,
            )
            outbox = replace(outbox, source_event_cursor=event.cursor)
            self._store.enqueue_outbox(outbox)
            outcome_cursor = event.cursor
            phase = ControllerPhase.WAITING_FOR_EFFECT
            pending_job_ids = (outbox.outbox_message_id,)
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
            pending_job_ids=pending_job_ids,
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
                if self._job_is_stale(message):
                    return self._supersede_job_locked(
                        current,
                        message,
                        now=completed_at,
                    )
                remaining_job_ids = tuple(
                    job_id
                    for job_id in current.pending_job_ids
                    if job_id != message.outbox_message_id
                )
                if status is EffectAttemptStatus.SUCCEEDED:
                    assert result is not None
                    event_type = JournalEventType.EFFECT_COMPLETED
                    projection = {
                        "state": "completed",
                        "business_summary": result.business_summary,
                        "result": result.payload,
                    }
                    phase = (
                        ControllerPhase.WAITING_FOR_EFFECT
                        if remaining_job_ids
                        else ControllerPhase.READY_FOR_AGENT
                    )
                    pending_job_ids = remaining_job_ids
                    pending_action_id = (
                        current.pending_action_id
                        if remaining_job_ids
                        else None
                    )
                elif status is EffectAttemptStatus.RETRYABLE_FAILURE:
                    event_type = JournalEventType.EFFECT_ATTEMPT_FAILED
                    projection = None
                    phase = ControllerPhase.WAITING_FOR_EFFECT
                    pending_job_ids = current.pending_job_ids
                    pending_action_id = current.pending_action_id
                else:
                    event_type = JournalEventType.EFFECT_ATTEMPT_FAILED
                    projection = {
                        "state": "blocked",
                        "reason_code": error_code or "effect_failure",
                    }
                    phase = (
                        ControllerPhase.WAITING_FOR_EFFECT
                        if remaining_job_ids
                        else ControllerPhase.READY_FOR_AGENT
                    )
                    pending_job_ids = remaining_job_ids
                    pending_action_id = (
                        current.pending_action_id
                        if remaining_job_ids
                        else None
                    )
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
                    pending_job_ids=pending_job_ids,
                    pending_decision_request_id=None,
                    consecutive_rejections=current.consecutive_rejections,
                    now=completed_at,
                )
        finally:
            self._store.release_lease(lease)

    def _checkpoint(
        self,
        *,
        run_id: str,
        case_id: str,
        phase: ControllerPhase,
        step_number: int,
        latest_user_message: str,
        pending_action_id: str | None,
        pending_job_ids: tuple[str, ...],
        pending_decision_request_id: str | None,
        consecutive_rejections: int,
        now: datetime,
        authority_epoch: int | None = None,
        mailbox_cursor: int | None = None,
        context_user_messages: tuple[ContextUserMessageItem, ...] | None = None,
    ) -> ControllerState:
        if authority_epoch is None or mailbox_cursor is None:
            prior_checkpoint = self._store.latest_checkpoint(case_id)
            if prior_checkpoint is None:
                raise ControllerConflict(
                    "initial checkpoint requires mailbox authority"
                )
            prior_state = decode_controller_state(
                prior_checkpoint.state_payload
            )
            authority_epoch = (
                prior_state.authority_epoch
                if authority_epoch is None
                else authority_epoch
            )
            mailbox_cursor = (
                prior_state.mailbox_cursor
                if mailbox_cursor is None
                else mailbox_cursor
            )
            if context_user_messages is None:
                context_user_messages = self._store.get_context_packet(
                    prior_state.context_packet_id
                ).user_messages
        if context_user_messages is None:
            raise ControllerConflict(
                "initial checkpoint requires user message context"
            )
        packet = self._build_context(
            case_id=case_id,
            user_messages=context_user_messages,
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
            authority_epoch=authority_epoch,
            mailbox_cursor=mailbox_cursor,
            last_event_cursor=checkpoint_cursor,
            context_packet_id=packet.packet_id,
            latest_user_message=latest_user_message,
            pending_action_id=pending_action_id,
            pending_job_ids=pending_job_ids,
            pending_decision_request_id=pending_decision_request_id,
            accepted_answer_version_id=case.accepted_answer_version_id,
            consecutive_rejections=consecutive_rejections,
            updated_at=now,
        )
        state_payload = encode_record(state)
        state_sha = content_sha256(state_payload)
        checkpoint_event_id = _stable_id(
            "event",
            checkpoint_id,
            "recorded",
        )
        checkpoint_payload = {
            "context_packet_id": packet.packet_id,
            "context_sha256": packet.content_sha256,
            "state_sha256": state_sha,
        }
        self._store.append_event(
            case_id=case_id,
            expected_next_cursor=checkpoint_cursor,
            event_id=checkpoint_event_id,
            event_type=JournalEventType.CHECKPOINT_RECORDED,
            recorded_at=now,
            action_id=pending_action_id,
            authority_ref=checkpoint_id,
            payload=checkpoint_payload,
            customer_projection=None,
            operation=_event_operation(
                event_id=checkpoint_event_id,
                idempotency_key=_stable_id(
                    "event-key",
                    checkpoint_id,
                    "recorded",
                ),
                causation_id=pending_action_id or packet.packet_id,
                correlation_id=run_id,
                authority_revision=authority_epoch,
                payload=checkpoint_payload,
            ),
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
        user_messages: tuple[ContextUserMessageItem, ...],
        now: datetime,
        step_number: int,
        run_id: str,
    ):
        case = self._store.get_case(case_id)
        question = (
            None
            if case.accepted_question_revision_id is None
            else self._store.get_question(
                case.accepted_question_revision_id
            )
        )
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
        business_events = tuple(
            ContextEventItem.from_event(event)
            for event in events
            if event.cursor >= event_start
            and event.customer_projection is not None
        )
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
            content_sha256(user_messages),
        )
        return build_context_packet(
            packet_id=packet_id,
            case=case,
            user_messages=user_messages,
            relevant_event_cursor_start=event_start,
            relevant_event_cursor_end=event_end,
            accepted_question=question,
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
            operation=_event_operation(
                event_id=event_id,
                idempotency_key=_stable_id(
                    "event-key",
                    event_id,
                ),
                causation_id=action_id or authority_ref or event_id,
                correlation_id=case_id,
                authority_revision=self._store.get_mailbox_head(
                    case_id
                ).authority_epoch,
                payload=payload,
            ),
        )

    def _make_outbox(
        self,
        action: ActionEnvelope,
        *,
        now: datetime,
    ) -> OutboxMessage:
        payload = {
            "action_kind": action.kind.value,
            "request": to_jsonable(action.payload),
            "expected_head_version": action.expected_head_version,
        }
        idempotency_key = _stable_id("effect-key", action.action_id)
        return OutboxMessage(
            outbox_message_id=_stable_id("outbox", action.action_id),
            case_id=action.case_id,
            source_event_cursor=1,
            action_id=action.action_id,
            job_kind=_effect_job_kind(action),
            operation=OperationIdentity(
                operation_id=_stable_id(
                    "operation",
                    action.action_id,
                    "effect",
                ),
                idempotency_key=idempotency_key,
                causation_id=action.operation.operation_id,
                correlation_id=action.operation.correlation_id,
                authority_revision=action.operation.authority_revision,
                payload_sha256=content_sha256(payload),
            ),
            expected_head_version=action.expected_head_version,
            expected_authority_epoch=action.operation.authority_revision,
            idempotency_key=idempotency_key,
            destination=_effect_destination(action),
            contract_ref=EFFECT_CONTRACT_REF,
            payload=payload,
            payload_sha256=content_sha256(payload),
            created_at=now,
        )

    def _enqueue_primary_agent_job(
        self,
        snapshot: ControllerState,
        request: PrimaryAgentRequest,
    ) -> ControllerState:
        now = self._now()
        payload = {
            "turn_id": request.turn_id,
            "run_id": request.run_id,
            "context_packet_id": request.context_packet.packet_id,
            "context_sha256": request.context_packet.content_sha256,
            "allowed_actions": tuple(
                action.value for action in request.allowed_actions
            ),
            "action_contract_ref": request.action_contract_ref,
        }
        message_id = _stable_id("outbox", request.turn_id, "primary-agent")
        idempotency_key = _stable_id(
            "primary-agent-key",
            request.turn_id,
        )
        operation = OperationIdentity(
            operation_id=_stable_id("operation", message_id),
            idempotency_key=idempotency_key,
            causation_id=request.context_packet.packet_id,
            correlation_id=request.run_id,
            authority_revision=snapshot.authority_epoch,
            payload_sha256=content_sha256(payload),
        )
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                if (
                    self._store.get_mailbox_head(
                        current.case_id
                    ).authority_epoch
                    != current.authority_epoch
                ):
                    return self._reconcile_mailbox_locked(current, now=now)
                event_payload = {
                    "outbox_message_id": message_id,
                    "context_packet_id": request.context_packet.packet_id,
                    "context_sha256": request.context_packet.content_sha256,
                }
                event_id = _stable_id(
                    "event",
                    message_id,
                    "enqueued",
                )
                event = self._store.append_event(
                    case_id=current.case_id,
                    expected_next_cursor=self._last_cursor(current.case_id) + 1,
                    event_id=event_id,
                    event_type=JournalEventType.LLM_JOB_ENQUEUED,
                    recorded_at=now,
                    action_id=None,
                    authority_ref=message_id,
                    payload=event_payload,
                    customer_projection={"state": "thinking"},
                    operation=_event_operation(
                        event_id=event_id,
                        idempotency_key=_stable_id(
                            "event-key",
                            idempotency_key,
                            "enqueued",
                        ),
                        causation_id=operation.operation_id,
                        correlation_id=operation.correlation_id,
                        authority_revision=current.authority_epoch,
                        payload=event_payload,
                    ),
                )
                self._store.enqueue_outbox(
                    OutboxMessage(
                        outbox_message_id=message_id,
                        case_id=current.case_id,
                        source_event_cursor=event.cursor,
                        action_id=None,
                        job_kind=AsyncJobKind.PRIMARY_AGENT,
                        operation=operation,
                        expected_head_version=current.head_version,
                        expected_authority_epoch=current.authority_epoch,
                        idempotency_key=idempotency_key,
                        destination="primary-agent-provider",
                        contract_ref=PRIMARY_AGENT_JOB_CONTRACT_REF,
                        payload=payload,
                        payload_sha256=content_sha256(payload),
                        created_at=now,
                    )
                )
                return self._checkpoint(
                    run_id=current.run_id,
                    case_id=current.case_id,
                    phase=ControllerPhase.WAITING_FOR_LLM,
                    step_number=current.step_number,
                    latest_user_message=current.latest_user_message,
                    pending_action_id=None,
                    pending_job_ids=(message_id,),
                    pending_decision_request_id=None,
                    consecutive_rejections=current.consecutive_rejections,
                    now=now,
                )
        finally:
            self._store.release_lease(lease)

    def _job_is_stale(self, message: OutboxMessage) -> bool:
        case = self._store.get_case(message.case_id)
        mailbox = self._store.get_mailbox_head(message.case_id)
        return (
            case.head_version != message.expected_head_version
            or mailbox.authority_epoch
            != message.expected_authority_epoch
        )

    def _supersede_job(
        self,
        snapshot: ControllerState,
        message: OutboxMessage,
    ) -> ControllerState:
        now = self._now()
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                return self._supersede_job_locked(
                    current,
                    message,
                    now=now,
                )
        finally:
            self._store.release_lease(lease)

    def _supersede_job_locked(
        self,
        current: ControllerState,
        message: OutboxMessage,
        *,
        now: datetime,
    ) -> ControllerState:
        self._append_job_superseded_event(message, now=now)
        return self._reconcile_mailbox_locked(current, now=now)

    def _append_job_superseded_event(
        self,
        message: OutboxMessage,
        *,
        now: datetime,
    ) -> None:
        event_payload = {
            "outbox_message_id": message.outbox_message_id,
            "job_kind": message.job_kind.value,
            "expected_head_version": message.expected_head_version,
            "actual_head_version": self._store.get_case(
                message.case_id
            ).head_version,
            "expected_authority_epoch": message.expected_authority_epoch,
            "actual_authority_epoch": self._store.get_mailbox_head(
                message.case_id
            ).authority_epoch,
            "reason_code": "authority_fence_changed",
        }
        event_id = _stable_id(
            "event",
            message.outbox_message_id,
            "superseded",
        )
        self._store.append_event(
            case_id=message.case_id,
            expected_next_cursor=self._last_cursor(message.case_id) + 1,
            event_id=event_id,
            event_type=JournalEventType.JOB_SUPERSEDED,
            recorded_at=now,
            action_id=message.action_id,
            authority_ref=message.outbox_message_id,
            payload=event_payload,
            customer_projection={
                "state": "superseded",
                "reason": "newer_user_or_authority_revision",
            },
            operation=_event_operation(
                event_id=event_id,
                idempotency_key=_stable_id(
                    "event-key",
                    message.operation.idempotency_key,
                    "superseded",
                ),
                causation_id=message.operation.operation_id,
                correlation_id=message.operation.correlation_id,
                authority_revision=self._store.get_mailbox_head(
                    message.case_id
                ).authority_epoch,
                payload=event_payload,
            ),
        )

    def _reconcile_mailbox(
        self,
        snapshot: ControllerState,
    ) -> ControllerState:
        now = self._now()
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                return self._reconcile_mailbox_locked(current, now=now)
        finally:
            self._store.release_lease(lease)

    def _reconcile_mailbox_locked(
        self,
        current: ControllerState,
        *,
        now: datetime,
    ) -> ControllerState:
        head = self._store.get_mailbox_head(current.case_id)
        superseded_refs = {
            event.authority_ref
            for event in self._store.list_events(current.case_id)
            if event.event_type is JournalEventType.JOB_SUPERSEDED
        }
        for job_id in current.pending_job_ids:
            if job_id not in superseded_refs:
                self._append_job_superseded_event(
                    self._store.get_outbox_message(job_id),
                    now=now,
                )
        pending_messages = self._store.list_mailbox_messages(
            current.case_id,
            after_sequence=current.mailbox_cursor,
        )
        if not pending_messages:
            return current
        messages = self._store.list_mailbox_messages(current.case_id)
        context_user_messages = tuple(
            ContextUserMessageItem.from_message(message)
            for message in messages
        )
        latest_user_message = context_user_messages[-1].content
        return self._checkpoint(
            run_id=current.run_id,
            case_id=current.case_id,
            phase=ControllerPhase.READY_FOR_AGENT,
            step_number=current.step_number,
            latest_user_message=latest_user_message,
            pending_action_id=None,
            pending_job_ids=(),
            pending_decision_request_id=None,
            consecutive_rejections=0,
            authority_epoch=head.authority_epoch,
            mailbox_cursor=head.last_sequence,
            context_user_messages=context_user_messages,
            now=now,
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

    def _acquire_job(self, message: OutboxMessage):
        now = self._now()
        try:
            return self._store.acquire_job_lease(
                outbox_message_id=message.outbox_message_id,
                owner_id=self._owner_id,
                now=now,
                expires_at=now + self._lease_duration,
            )
        except LeaseConflict as error:
            raise ControllerConflict(
                "job already has an active worker"
            ) from error

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
            ActionKind.RUN_PROBE,
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


def _effect_job_kind(action: ActionEnvelope) -> AsyncJobKind:
    if action.kind is ActionKind.INSPECT_SEMANTICS:
        return AsyncJobKind.SEMANTIC_INSPECTION
    if action.kind is ActionKind.RUN_PROBE:
        return AsyncJobKind.DATA_PROBE
    if action.kind is ActionKind.CALL_CAPABILITY:
        return AsyncJobKind.CAPABILITY
    if action.kind is ActionKind.RUN_SENSITIVITY:
        return AsyncJobKind.SENSITIVITY
    raise TypeError("action is not an effect action")


def _event_operation(
    *,
    event_id: str,
    idempotency_key: str,
    causation_id: str,
    correlation_id: str,
    authority_revision: int,
    payload: dict[str, object],
) -> OperationIdentity:
    return OperationIdentity(
        operation_id=_stable_id("operation", event_id),
        idempotency_key=idempotency_key,
        causation_id=causation_id,
        correlation_id=correlation_id,
        authority_revision=authority_revision,
        payload_sha256=content_sha256(payload),
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
