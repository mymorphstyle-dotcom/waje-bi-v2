"""Single-authority Primary Business Analysis Agent controller."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
from waje_vnext.domain.action_codec import decode_agent_action_proposal
from waje_vnext.domain.admission import admit_action
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    MailboxMessageKind,
    MessageIngressReceipt,
    OperationIdentity,
)
from waje_vnext.domain.authority import (
    CaseLifecycle,
    DecisionRecord,
    InterpretationRecord,
    WorkPlanRevision,
)
from waje_vnext.domain.answering import (
    AnswerCandidateStatus,
    AnswerStatus,
    AnswerVersion,
    build_provisional_answer_candidate,
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
from waje_vnext.domain.evidence import (
    CapabilityResultEnvelope,
    EvidenceAdmissionStatus,
    EvidenceValidityStatus,
)
from waje_vnext.domain.measurement import (
    MessageRole,
    QuestionRevision,
    SourceMessageRef,
)
from waje_vnext.domain.measurement_resolver import (
    validate_executable_design,
)
from waje_vnext.domain.planning import (
    compile_plan_bundle,
    same_business_authority,
)
from waje_vnext.domain.runtime_state import (
    ANSWER_REVIEW_JOB_CONTRACT_REF,
    ActionReceipt,
    CheckpointRecord,
    OutboxMessage,
)
from waje_vnext.domain.obligation_scheduler import (
    OBLIGATION_JOB_CONTRACT_REF,
    build_obligation_schedule_id,
    same_obligation_business_authority,
)
from waje_vnext.domain.runtime_amendment import (
    FrameAdmissionProof,
    DurableModelResult,
    FrameCandidateRecord,
    FrameCandidateSupersessionRecord,
    DeterministicFrameValidationFinding,
    FrameReviewDisposition,
    FrameReviewProposal,
    FrameReviewRecord,
    FrameReviewRequest,
    JobDisposition,
    JobDispositionRecord,
    LogicalModelJob,
    MessageBindingDisposition,
    MessageBindingRequest,
    MessageImpactBinding,
    MessageImpactKind,
    MessageImpactProposal,
    ModelExecutionRole,
    ModelInputViewKind,
    MeasurementObjectionSeverity,
    MeasurementReviewObjection,
    MessageIngressRecord,
    ObjectionClosureRecord,
    PendingUserMessage,
    ProviderAttemptDisposition,
    ProviderAttemptReceipt,
    ProviderAttemptRequest,
    RunTraceEventLink,
    RunTraceManifest,
    RunTraceProfile,
    SemanticAmbiguity,
    SemanticAssertion,
    SemanticSourceSpan,
    TypedSemanticBinding,
    derive_changed_measurement_node_ids,
    measurement_paths_overlap,
)
from waje_vnext.domain.typed_decode import decode_typed_dataclass
from waje_vnext.providers.base import (
    MeasurementReviewerProvider,
    MessageBindingProvider,
    PrimaryAgentProvider,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
)
from waje_vnext.storage.codec import (
    decode_capability_result_envelope,
    decode_controller_state,
    encode_record,
)
from waje_vnext.storage.ports import (
    AuthorityConflict,
    AuthorityNotFound,
    AuthorityStore,
    InvalidAuthorityTransition,
    LeaseConflict,
    LeaseFenceLost,
)

from .effects import (
    EffectExecutionResult,
    EffectExecutor,
    EffectPermanentError,
    EffectTransientError,
)
from .evidence_runtime import EvidenceRuntime
from .obligation_runtime import DurableObligationCoordinator
from .supervision import JobHeartbeatSupervisor


ACTION_CONTRACT_REF = "waje-vnext://contracts/domain/actions.v3"
CONTROLLER_STATE_SCHEMA_REF = (
    "waje-vnext://contracts/domain/controller-state.v1"
)
ACTION_RESULT_SCHEMA_REF = "waje-vnext://runtime/action-result.v1"
EFFECT_CONTRACT_REF = "waje-vnext://runtime/effect-request.v1"
PRIMARY_AGENT_JOB_CONTRACT_REF = (
    "waje-vnext://runtime/primary-agent-job.v1"
)
FRAME_REVIEW_JOB_CONTRACT_REF = (
    "waje-vnext://runtime/frame-review-job.v1"
)
FRAME_REVIEW_CONTRACT_REF = (
    "waje-vnext://contracts/domain/measurement-review.v1"
)
FRAME_REVIEW_INDEPENDENCE_POLICY_REF = (
    "waje-vnext://runtime/reviewer-role-separation.v1"
)
MESSAGE_BINDING_CONTRACT_REF = (
    "waje-vnext://contracts/domain/message-impact-binding.v1"
)
MESSAGE_BINDING_JOB_CONTRACT_REF = (
    "waje-vnext://runtime/message-binding-job.v1"
)
CONTROLLER_WAKE_CONTRACT_REF = "waje-vnext://runtime/controller-wake.v1"
_EFFECT_ACTIONS = {
    ActionKind.INSPECT_SEMANTICS,
    ActionKind.RUN_PROBE,
}
_EVIDENCE_ACTIONS = {
    ActionKind.CALL_CAPABILITY,
    ActionKind.RUN_SENSITIVITY,
}


class ControllerConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _LocalProviderTrace:
    disposition: str
    provider_response_id: str | None
    output_sha256: str | None
    finish_reason: str | None
    usage_payload: dict[str, object]
    completed_at: datetime


class _DurableProviderAttemptObserver:
    def __init__(
        self,
        *,
        store: AuthorityStore,
        model_job: LogicalModelJob,
        result_kind: str,
        result_contract_ref: str,
    ) -> None:
        self._store = store
        self._model_job = model_job
        self._result_kind = result_kind
        self._result_contract_ref = result_contract_ref
        self._prior_attempt_id: str | None = None
        self._attempt_ids: dict[int, str] = {}

    def attempt_numbers(self, max_attempts: int) -> tuple[int, ...]:
        if max_attempts != self._model_job.configuration_identity.max_attempts:
            raise ControllerConflict(
                "provider retry budget differs from durable configuration"
            )
        receipts = {
            receipt.provider_attempt_id: receipt
            for receipt in self._store.list_provider_attempt_receipts(
                self._model_job.logical_model_job_id
            )
        }
        completed_attempts = 0
        prior_attempt_id: str | None = None
        for attempt_number in range(1, max_attempts + 1):
            attempt_id = _stable_id(
                "provider-attempt",
                self._model_job.logical_model_job_id,
                str(attempt_number),
            )
            try:
                request = self._store.get_provider_attempt_request(
                    attempt_id
                )
            except AuthorityNotFound:
                break
            receipt = receipts.get(attempt_id)
            if receipt is None:
                raise ControllerConflict(
                    "provider attempt outcome is unknown; automatic retry is fenced"
                )
            completed_attempts = attempt_number
            prior_attempt_id = request.provider_attempt_id
            if receipt.disposition is ProviderAttemptDisposition.SUCCEEDED:
                raise ControllerConflict(
                    "successful provider attempt lacks its durable result"
                )
            if (
                receipt.disposition
                is not ProviderAttemptDisposition.RETRYABLE_FAILURE
            ):
                raise ProviderPermanentError(
                    "durable provider attempt already reached a terminal failure"
                )
        self._prior_attempt_id = prior_attempt_id
        if completed_attempts >= max_attempts:
            raise ProviderTransientError(
                "durable provider retry budget is exhausted"
            )
        return tuple(range(completed_attempts + 1, max_attempts + 1))

    def dispatch_parameters(self) -> tuple[str, float | None]:
        configuration = self._model_job.configuration_identity
        return configuration.endpoint_ref, configuration.timeout_seconds

    def before_attempt(
        self,
        attempt_number: int,
        provider_request_body,
    ) -> str:
        request_sha256 = content_sha256(provider_request_body)
        if (
            request_sha256
            != self._model_job.model_request_artifact.provider_request_sha256
        ):
            raise ControllerConflict(
                "outbound provider request drifted from durable authority"
            )
        attempt_id = _stable_id(
            "provider-attempt",
            self._model_job.logical_model_job_id,
            str(attempt_number),
        )
        self._store.record_provider_attempt_request(
            ProviderAttemptRequest(
                provider_attempt_id=attempt_id,
                logical_model_job_id=(
                    self._model_job.logical_model_job_id
                ),
                attempt_number=attempt_number,
                prior_provider_attempt_id=self._prior_attempt_id,
                provider_idempotency_key=attempt_id,
                request_sha256=request_sha256,
                model_request_artifact_sha256=(
                    self._model_job.model_request_artifact_sha256
                ),
                configuration_sha256=(
                    self._model_job.configuration_sha256
                ),
                requested_at=self._model_job.created_at,
            )
        )
        self._attempt_ids[attempt_number] = attempt_id
        self._prior_attempt_id = attempt_id
        return attempt_id

    def after_attempt(self, attempt_number: int, trace) -> None:
        if trace.disposition == "succeeded":
            raise ControllerConflict(
                "successful provider attempt requires atomic result commit"
            )
        attempt_id = self._attempt_ids[attempt_number]
        self._store.record_provider_attempt_receipt(
            ProviderAttemptReceipt(
                provider_attempt_receipt_id=_stable_id(
                    "provider-attempt-receipt",
                    attempt_id,
                ),
                provider_attempt_id=attempt_id,
                logical_model_job_id=(
                    self._model_job.logical_model_job_id
                ),
                disposition=ProviderAttemptDisposition(
                    trace.disposition
                ),
                provider_response_id=trace.provider_response_id,
                output_sha256=trace.output_sha256,
                finish_reason=trace.finish_reason,
                usage_payload=dict(trace.usage_payload),
                completed_at=trace.completed_at,
            )
        )

    def after_success(self, attempt_number: int, trace, result) -> None:
        result_payload = to_jsonable(result)
        if not isinstance(result_payload, dict):
            raise ControllerConflict(
                "typed provider result must encode as an object"
            )
        attempt_id = self._attempt_ids[attempt_number]
        receipt_id = _stable_id(
            "provider-attempt-receipt",
            attempt_id,
        )
        receipt = ProviderAttemptReceipt(
            provider_attempt_receipt_id=receipt_id,
            provider_attempt_id=attempt_id,
            logical_model_job_id=self._model_job.logical_model_job_id,
            disposition=ProviderAttemptDisposition.SUCCEEDED,
            provider_response_id=trace.provider_response_id,
            output_sha256=trace.output_sha256,
            finish_reason=trace.finish_reason,
            usage_payload=dict(trace.usage_payload),
            completed_at=trace.completed_at,
        )
        durable_result = DurableModelResult(
            durable_model_result_id=_stable_id(
                "durable-model-result",
                self._model_job.logical_model_job_id,
            ),
            logical_model_job_id=(
                self._model_job.logical_model_job_id
            ),
            provider_attempt_id=attempt_id,
            provider_attempt_receipt_id=receipt_id,
            result_kind=self._result_kind,
            result_contract_ref=self._result_contract_ref,
            result_payload=result_payload,
            output_sha256=content_sha256(result_payload),
            model_request_artifact_sha256=(
                self._model_job.model_request_artifact_sha256
            ),
            configuration_sha256=(
                self._model_job.configuration_sha256
            ),
            recorded_at=trace.completed_at,
        )
        self._store.commit_provider_attempt_success(
            receipt=receipt,
            result=durable_result,
        )


class WAJEController:
    """Owns the accepted action loop, state transitions, and recovery."""

    def __init__(
        self,
        *,
        store: AuthorityStore,
        provider: PrimaryAgentProvider,
        binding_provider: MessageBindingProvider | None = None,
        reviewer_provider: MeasurementReviewerProvider | None = None,
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
        self._binding_provider = binding_provider or provider
        if reviewer_provider is None:
            if not getattr(
                provider,
                "allows_test_role_multiplexing",
                False,
            ):
                raise ValueError(
                    "reviewer_provider requires an independent configuration"
                )
            reviewer_provider = provider
        if not getattr(provider, "allows_test_role_multiplexing", False):
            primary_configuration = _provider_configuration_identity(
                provider,
                ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT,
            )
            reviewer_configuration = _provider_configuration_identity(
                reviewer_provider,
                ModelExecutionRole.RUNTIME_REVIEWER,
            )
            if (
                reviewer_provider is provider
                or primary_configuration is None
                or reviewer_configuration is None
                or primary_configuration.operational_configuration_sha256
                == reviewer_configuration.operational_configuration_sha256
            ):
                raise ValueError(
                    "primary and Reviewer providers require distinct, "
                    "auditable configurations"
                )
            for role_name, configured_provider in (
                ("primary", provider),
                ("binding", self._binding_provider),
                ("reviewer", reviewer_provider),
            ):
                if not getattr(
                    configured_provider,
                    "supports_durable_attempt_observer",
                    False,
                ):
                    raise ValueError(
                        "{} provider must durably journal every outbound "
                        "attempt".format(role_name)
                    )
        self._reviewer_provider = reviewer_provider
        self._effect_executor = effect_executor
        self._owner_id = owner_id
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._lease_duration = lease_duration
        self._obligation_coordinator = DurableObligationCoordinator(
            store=store,
            owner_id=owner_id,
            lease_duration=lease_duration,
        )

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
            case_open_payload = {"thread_id": thread_id}
            self._store.open_case(
                case_id=case_id,
                thread_id=thread_id,
                event_id=_stable_id("event", case_id, "opened"),
                opened_at=now,
                operation=OperationIdentity(
                    operation_id=_stable_id(
                        "operation",
                        case_id,
                        "opened",
                    ),
                    idempotency_key=_stable_id(
                        "operation-key",
                        case_id,
                        "opened",
                    ),
                    causation_id=run_id,
                    correlation_id=run_id,
                    authority_revision=0,
                    payload_sha256=content_sha256(case_open_payload),
                ),
            )
            existing = self._store.latest_checkpoint(case_id)
            starts_new_run = False
            prior_state = None
            if existing is not None:
                prior_state = decode_controller_state(
                    existing.state_payload
                )
                if prior_state.run_id != run_id:
                    if prior_state.phase not in {
                        ControllerPhase.COMPLETED,
                        ControllerPhase.STOPPED,
                    }:
                        raise ControllerConflict(
                            "active case run must reach a terminal "
                            "checkpoint before a new run starts"
                        )
                    if self._store.get_case(
                        case_id
                    ).lifecycle is not CaseLifecycle.OPEN:
                        raise ControllerConflict(
                            "stopped or closed case authority cannot be "
                            "reopened as a new run"
                        )
                    unfinished = tuple(
                        item
                        for item
                        in self._store.list_pending_outbox_messages(
                            case_id=case_id
                        )
                        if item.operation.correlation_id
                        == prior_state.run_id
                    )
                    if unfinished:
                        raise ControllerConflict(
                            "prior run has unfinished durable jobs"
                        )
                    starts_new_run = True
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
                authority_revision=mailbox_head.authority_epoch + 1,
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
            ingress_record = MessageIngressRecord(
                ingress_record_id=_stable_id(
                    "message-ingress-record",
                    message.message_id,
                ),
                case_id=case_id,
                run_id=run_id,
                message_id=message.message_id,
                mailbox_sequence=message.sequence,
                authority_epoch=message.authority_epoch,
                operation=message.operation,
                message_payload_sha256=(
                    message.operation.payload_sha256
                ),
                created_at=now,
            )
            self._store.record_message_ingress(ingress_record)
            binding_job_id = _stable_id(
                "outbox",
                case_id,
                "message-binding",
                str(message.sequence),
            )
            pending = PendingUserMessage(
                pending_message_id=_stable_id(
                    "pending-user-message",
                    message.message_id,
                ),
                ingress_record_id=ingress_record.ingress_record_id,
                case_id=case_id,
                message_id=message.message_id,
                binding_job_id=binding_job_id,
                authority_epoch=message.authority_epoch,
                source_operation_id=message.operation.operation_id,
                created_at=now,
            )
            self._store.record_pending_user_message(pending)
            binding_payload = {
                "pending_message_id": pending.pending_message_id,
                "message_id": message.message_id,
                "binding_contract_ref": MESSAGE_BINDING_CONTRACT_REF,
            }
            binding_idempotency_key = _stable_id(
                "message-binding-key",
                message.operation.idempotency_key,
            )
            binding_operation = OperationIdentity(
                operation_id=_stable_id("operation", binding_job_id),
                idempotency_key=binding_idempotency_key,
                causation_id=message.operation.operation_id,
                correlation_id=run_id,
                authority_revision=message.authority_epoch,
                payload_sha256=content_sha256(binding_payload),
            )
            binding_event_payload = {
                "outbox_message_id": binding_job_id,
                "pending_message_id": pending.pending_message_id,
                "message_id": message.message_id,
                "binding_contract_ref": MESSAGE_BINDING_CONTRACT_REF,
            }
            binding_event_id = _stable_id(
                "event",
                binding_job_id,
                "enqueued",
            )
            binding_event = self._store.append_event(
                case_id=case_id,
                expected_next_cursor=self._last_cursor(case_id) + 1,
                event_id=binding_event_id,
                event_type=(
                    JournalEventType.MESSAGE_BINDING_JOB_ENQUEUED
                ),
                recorded_at=now,
                action_id=None,
                authority_ref=pending.pending_message_id,
                payload=binding_event_payload,
                customer_projection={
                    "state": "understanding_business_request"
                },
                operation=_event_operation(
                    event_id=binding_event_id,
                    idempotency_key=_stable_id(
                        "event-key",
                        binding_idempotency_key,
                        "enqueued",
                    ),
                    causation_id=binding_operation.operation_id,
                    correlation_id=run_id,
                    authority_revision=message.authority_epoch,
                    payload=binding_event_payload,
                ),
            )
            binding_snapshot = self._store.get_authority_snapshot(case_id)
            self._store.enqueue_outbox(
                OutboxMessage(
                    outbox_message_id=binding_job_id,
                    case_id=case_id,
                    source_event_cursor=binding_event.cursor,
                    action_id=None,
                    job_kind=AsyncJobKind.MESSAGE_BINDING,
                    operation=binding_operation,
                    expected_head_version=binding_snapshot.head_version,
                    expected_authority_epoch=message.authority_epoch,
                    authority_snapshot=binding_snapshot,
                    authority_snapshot_sha256=(
                        binding_snapshot.content_sha256
                    ),
                    idempotency_key=binding_idempotency_key,
                    destination="message-binding-provider",
                    contract_ref=MESSAGE_BINDING_JOB_CONTRACT_REF,
                    payload=binding_payload,
                    payload_sha256=content_sha256(binding_payload),
                    created_at=now,
                )
            )
            if existing is None or starts_new_run:
                self._checkpoint(
                    run_id=run_id,
                    case_id=case_id,
                    phase=ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
                    step_number=0,
                    latest_user_message=user_message,
                    pending_action_id=None,
                    pending_job_ids=(binding_job_id,),
                    pending_decision_request_id=None,
                    consecutive_rejections=0,
                    authority_epoch=message.authority_epoch,
                    mailbox_cursor=(
                        0
                        if prior_state is None
                        else prior_state.mailbox_cursor
                    ),
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

    def build_run_trace_manifest(
        self,
        case_id: str,
    ) -> RunTraceManifest:
        """Materialize and verify the durable lineage known for one run."""

        with self._store.consistent_read():
            manifest = self._materialize_run_trace_manifest(case_id)
        return self._store.record_run_trace_manifest(manifest)

    def _materialize_run_trace_manifest(
        self,
        case_id: str,
    ) -> RunTraceManifest:

        state = self.resume(case_id)
        all_events = self._store.list_events(case_id)
        events = tuple(
            event
            for event in all_events
            if event.operation.correlation_id == state.run_id
        )
        if not events:
            raise ControllerConflict("run has no correlated journal events")
        expected_cursors = tuple(
            range(events[0].cursor, events[-1].cursor + 1)
        )
        if tuple(event.cursor for event in events) != expected_cursors:
            raise ControllerConflict(
                "run journal is interleaved with another correlation"
            )
        ingress_records = tuple(
            item
            for item in self._store.list_message_ingress_records(case_id)
            if item.run_id == state.run_id
        )
        outbox = tuple(
            item
            for item in self._store.list_outbox_messages(case_id=case_id)
            if item.operation.correlation_id == state.run_id
        )
        outbox_by_id = {
            item.outbox_message_id: item for item in outbox
        }
        outbox_ids = set(outbox_by_id)
        operation_ids = {
            event.operation.operation_id for event in events
        } | {
            item.operation.operation_id for item in outbox
        }
        action_ids = {
            event.action_id
            for event in events
            if event.action_id is not None
        }
        bindings = tuple(
            item
            for item in self._store.list_message_impact_bindings(case_id)
            if item.logical_model_job_id in outbox_ids
        )
        candidates = tuple(
            item
            for item in self._store.list_frame_candidates(case_id)
            if item.source_action_id in action_ids
        )
        candidate_supersessions = (
            tuple(
                item
                for item
                in self._store.list_frame_candidate_supersessions(case_id)
                if item.source_operation_id in operation_ids
            )
        )
        candidate_ids = {
            item.frame_candidate_id for item in candidates
        }
        reviews = tuple(
            item
            for item in self._store.list_frame_reviews(case_id)
            if item.frame_candidate_id in candidate_ids
        )
        dispositions = tuple(
            item
            for item in self._store.list_job_dispositions(case_id)
            if item.outbox_message_id in outbox_ids
        )
        model_jobs = tuple(
            item
            for item in self._store.list_logical_model_jobs(case_id)
            if item.job_id in outbox_ids
        )
        requests = tuple(
            request
            for job in model_jobs
            for request in self._store.list_provider_attempt_requests(
                job.logical_model_job_id
            )
        )
        receipts = tuple(
            receipt
            for job in model_jobs
            for receipt in self._store.list_provider_attempt_receipts(
                job.logical_model_job_id
            )
        )
        durable_model_results = tuple(
            result
            for job in model_jobs
            if (
                result := self._store.get_durable_model_result(
                    job.logical_model_job_id
                )
            )
            is not None
        )
        for job in model_jobs:
            message = outbox_by_id.get(job.job_id)
            if (
                message is None
                or message.operation.operation_id != job.operation_id
                or message.authority_snapshot_sha256
                != job.authority_snapshot_sha256
            ):
                raise ControllerConflict(
                    "logical model job lineage does not match its outbox job"
                )
            disposition = self._store.get_job_disposition(job.job_id)
            job_receipts = tuple(
                receipt
                for receipt in receipts
                if receipt.logical_model_job_id
                == job.logical_model_job_id
            )
            success_receipts = tuple(
                receipt
                for receipt in job_receipts
                if receipt.disposition
                is ProviderAttemptDisposition.SUCCEEDED
            )
            durable_result = self._store.get_durable_model_result(
                job.logical_model_job_id
            )
            if len(success_receipts) > 1:
                raise ControllerConflict(
                    "logical model job has multiple successful attempts"
                )
            if bool(success_receipts) != (durable_result is not None):
                raise ControllerConflict(
                    "provider success receipt and typed result are incomplete"
                )
            if success_receipts and durable_result is not None:
                success_receipt = success_receipts[0]
                if (
                    durable_result.provider_attempt_receipt_id
                    != success_receipt.provider_attempt_receipt_id
                    or durable_result.provider_attempt_id
                    != success_receipt.provider_attempt_id
                    or durable_result.output_sha256
                    != success_receipt.output_sha256
                    or durable_result.configuration_sha256
                    != job.configuration_sha256
                    or durable_result.model_request_artifact_sha256
                    != job.model_request_artifact_sha256
                    or durable_result.result_contract_ref
                    != job.model_request_artifact.output_contract_ref
                ):
                    raise ControllerConflict(
                        "provider success pair drifted from logical job"
                    )
            if (
                disposition is not None
                and disposition.disposition is JobDisposition.COMPLETED
                and not success_receipts
            ):
                raise ControllerConflict(
                    "completed model job lacks a successful provider receipt"
                )
        for disposition in dispositions:
            message = outbox_by_id.get(disposition.outbox_message_id)
            if (
                message is None
                or disposition.operation != message.operation
                or disposition.case_id != case_id
            ):
                raise ControllerConflict(
                    "job disposition lineage does not match durable outbox"
                )

        plan_revision_ids = _event_authority_refs(
            events,
            JournalEventType.PLAN_ACCEPTED,
        )
        resolution_outcome_ids = _event_authority_refs(
            events,
            JournalEventType.MEASUREMENT_RESOLUTION_RECORDED,
        )
        obligation_ids = _event_authority_refs(
            events,
            JournalEventType.EVIDENCE_OBLIGATION_RECORDED,
        )
        answer_ids = _event_authority_refs(
            events,
            JournalEventType.ANSWER_ACCEPTED,
        )
        answers = tuple(
            self._store.get_answer(answer_id)
            for answer_id in answer_ids
        )
        provisional_answer_ids = tuple(
            answer.answer_version_id
            for answer in answers
            if answer.status is AnswerStatus.PROVISIONAL
        )
        claim_ids = _ordered_unique(
            claim.claim_id
            for answer in answers
            for claim in answer.claims
        )
        effect_attempt_ids = _ordered_unique(
            attempt.effect_attempt_id
            for message in outbox
            for attempt in self._store.list_effect_attempts(
                message.outbox_message_id
            )
        )
        evidence_ids = tuple(
            item.evidence_record_id
            for item in self._store.list_evidence(case_id)
            if item.run_id == state.run_id
        )
        event_operation_lineage = tuple(
            RunTraceEventLink(
                cursor=event.cursor,
                event_id=event.event_id,
                event_type=event.event_type,
                recorded_at=event.recorded_at,
                operation_id=event.operation.operation_id,
                causation_id=event.operation.causation_id,
                correlation_id=event.operation.correlation_id,
                authority_revision=event.operation.authority_revision,
                action_id=event.action_id,
                authority_ref=event.authority_ref,
                payload_sha256=event.operation.payload_sha256,
                event_content_sha256=event.content_sha256,
            )
            for event in events
        )
        lineage = {
            "case_id": case_id,
            "run_id": state.run_id,
            "trace_profile": (
                RunTraceProfile.CASE_AUTHORITY_LANE.value
            ),
            "start_event_cursor": events[0].cursor,
            "terminal_event_cursor": events[-1].cursor,
            "event_operation_lineage": event_operation_lineage,
            "ingress_record_ids": tuple(
                item.ingress_record_id for item in ingress_records
            ),
            "message_binding_ids": tuple(
                item.binding_id for item in bindings
            ),
            "frame_candidate_ids": tuple(
                item.frame_candidate_id for item in candidates
            ),
            "frame_candidate_supersession_ids": tuple(
                item.supersession_record_id
                for item in candidate_supersessions
            ),
            "frame_review_ids": tuple(
                item.frame_review_id for item in reviews
            ),
            "job_disposition_record_ids": tuple(
                item.job_disposition_record_id for item in dispositions
            ),
            "logical_model_job_ids": tuple(
                item.logical_model_job_id for item in model_jobs
            ),
            "provider_attempt_request_ids": tuple(
                item.provider_attempt_id for item in requests
            ),
            "provider_attempt_receipt_ids": tuple(
                item.provider_attempt_receipt_id for item in receipts
            ),
            "durable_model_result_ids": tuple(
                item.durable_model_result_id
                for item in durable_model_results
            ),
            "plan_revision_ids": plan_revision_ids,
            "resolution_outcome_ids": resolution_outcome_ids,
            "obligation_ids": obligation_ids,
            "effect_attempt_ids": effect_attempt_ids,
            "evidence_record_ids": evidence_ids,
            "claim_ids": claim_ids,
            "provisional_answer_version_ids": provisional_answer_ids,
        }
        lineage_sha256 = content_sha256(lineage)
        manifest = RunTraceManifest(
            trace_manifest_id=_stable_id(
                "run-trace-manifest",
                case_id,
                state.run_id,
                lineage_sha256,
            ),
            case_id=case_id,
            run_id=state.run_id,
            trace_profile=RunTraceProfile.CASE_AUTHORITY_LANE,
            start_event_cursor=events[0].cursor,
            terminal_event_cursor=events[-1].cursor,
            event_operation_lineage=event_operation_lineage,
            ingress_record_ids=lineage["ingress_record_ids"],
            message_binding_ids=lineage["message_binding_ids"],
            frame_candidate_ids=lineage["frame_candidate_ids"],
            frame_candidate_supersession_ids=(
                lineage["frame_candidate_supersession_ids"]
            ),
            frame_review_ids=lineage["frame_review_ids"],
            job_disposition_record_ids=(
                lineage["job_disposition_record_ids"]
            ),
            logical_model_job_ids=lineage["logical_model_job_ids"],
            provider_attempt_request_ids=(
                lineage["provider_attempt_request_ids"]
            ),
            provider_attempt_receipt_ids=(
                lineage["provider_attempt_receipt_ids"]
            ),
            durable_model_result_ids=(
                lineage["durable_model_result_ids"]
            ),
            plan_revision_ids=plan_revision_ids,
            resolution_outcome_ids=resolution_outcome_ids,
            obligation_ids=obligation_ids,
            effect_attempt_ids=effect_attempt_ids,
            evidence_record_ids=evidence_ids,
            claim_ids=claim_ids,
            provisional_answer_version_ids=provisional_answer_ids,
            lineage_sha256=lineage_sha256,
            built_at=self._now(),
        )
        return manifest

    def dispatch_outbox(self, message_id: str) -> ControllerState:
        """Idempotently route a durable outbox delivery to its worker handler."""

        message = self._store.get_outbox_message(message_id)
        state = self.resume(message.case_id)
        if self._store.get_job_disposition(message_id) is not None:
            return state
        if (
            message.job_kind is AsyncJobKind.MESSAGE_BINDING
            and self._store.get_mailbox_head(message.case_id).last_sequence
            > state.mailbox_cursor
            and message.outbox_message_id not in state.pending_job_ids
        ):
            state = self._reconcile_mailbox(state)
        if message.job_kind is AsyncJobKind.CONTROLLER_WAKE:
            return self.advance(message.case_id)
        if message.outbox_message_id not in state.pending_job_ids:
            return state
        if message.job_kind is AsyncJobKind.PRIMARY_AGENT:
            return self.deliver_pending_llm(message.case_id)
        if message.job_kind is AsyncJobKind.MESSAGE_BINDING:
            return self.deliver_pending_message_binding(
                message.case_id,
                job_id=message.outbox_message_id,
            )
        if message.job_kind is AsyncJobKind.REVIEWER:
            return self.deliver_pending_frame_review(
                message.case_id,
                job_id=message.outbox_message_id,
            )
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
        job_lease = self._acquire_job(message)
        try:
            if self._job_is_stale(message):
                return self._supersede_job(
                    snapshot,
                    message,
                    job_lease=job_lease,
                )
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
            model_job = self._record_logical_model_job(
                message=message,
                provider=self._provider,
                role="primary_agent",
                typed_request_contract_ref=(
                    PRIMARY_AGENT_JOB_CONTRACT_REF
                ),
                output_contract_ref=ACTION_CONTRACT_REF,
                request=request,
            )
            recovered_proposal = self._load_durable_model_result(
                model_job=model_job,
                result_kind="primary_agent",
                result_contract_ref=ACTION_CONTRACT_REF,
            )
            if recovered_proposal is not None:
                return self._commit_proposal(
                    snapshot,
                    recovered_proposal,
                    message,
                    job_lease,
                    model_job=model_job,
                    provider_attempts=(
                        self._persisted_provider_attempt_records(
                            model_job.logical_model_job_id
                        )
                    ),
                )
            observer_installed = (
                self._install_provider_attempt_observer(
                    provider=self._provider,
                    model_job=model_job,
                    result_kind="primary_agent",
                    result_contract_ref=ACTION_CONTRACT_REF,
                )
            )
            heartbeat = JobHeartbeatSupervisor(
                store=self._store,
                lease=job_lease,
                clock=self._clock,
                lease_duration=self._lease_duration,
            )
            heartbeat.start()
            try:
                proposal = self._provider.propose(request)
            except ProviderError as error:
                self._clear_provider_attempt_observer(
                    self._provider,
                    observer_installed,
                )
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                    raise
                provider_attempts = self._provider_failure_attempt_records(
                    model_job=model_job,
                    provider=self._provider,
                    error=error,
                    completed_at=self._now(),
                )
                return self._commit_provider_failure(
                    snapshot=snapshot,
                    message=message,
                    job_lease=job_lease,
                    model_job=model_job,
                    provider_attempts=provider_attempts,
                    error=error,
                )
            except BaseException:
                self._clear_provider_attempt_observer(
                    self._provider,
                    observer_installed,
                )
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                raise
            self._clear_provider_attempt_observer(
                self._provider,
                observer_installed,
            )
            result_recorded_at = self._now()
            provider_attempts = self._provider_attempt_records(
                model_job=model_job,
                provider=self._provider,
                output=proposal,
                completed_at=result_recorded_at,
            )
            self._persist_successful_model_result(
                model_job=model_job,
                provider_attempts=provider_attempts,
                result=proposal,
                result_kind="primary_agent",
                result_contract_ref=ACTION_CONTRACT_REF,
                recorded_at=result_recorded_at,
            )
            try:
                job_lease = heartbeat.stop_and_get()
            except LeaseFenceLost:
                job_lease = heartbeat.current_lease
                raise
            return self._commit_proposal(
                snapshot,
                proposal,
                message,
                job_lease,
                model_job=model_job,
                provider_attempts=provider_attempts,
            )
        finally:
            self._release_job_lease(job_lease)

    def deliver_pending_message_binding(
        self,
        case_id: str,
        *,
        job_id: str | None = None,
    ) -> ControllerState:
        snapshot = self.resume(case_id)
        if (
            snapshot.phase
            is not ControllerPhase.WAITING_FOR_MESSAGE_BINDING
        ):
            raise ControllerConflict(
                "case has no pending message-binding job"
            )
        selected_job_id = job_id or snapshot.pending_job_ids[0]
        if selected_job_id not in snapshot.pending_job_ids:
            raise ControllerConflict(
                "message-binding job is not pending for this case"
            )
        message = self._store.get_outbox_message(selected_job_id)
        if message.job_kind is not AsyncJobKind.MESSAGE_BINDING:
            raise ControllerConflict(
                "pending job is not a message-binding job"
            )
        job_lease = self._acquire_job(message)
        try:
            pending = self._store.get_pending_user_message(
                str(message.payload["pending_message_id"])
            )
            mailbox_message = next(
                item
                for item in self._store.list_mailbox_messages(case_id)
                if item.message_id == pending.message_id
            )
            case = self._store.get_case(case_id)
            prior_question = (
                None
                if case.accepted_question_revision_id is None
                else self._store.get_question(
                    case.accepted_question_revision_id
                )
            )
            request = MessageBindingRequest(
                logical_model_job_id=message.outbox_message_id,
                case_id=case_id,
                message_id=mailbox_message.message_id,
                message_content=str(
                    mailbox_message.payload["message"]
                ),
                prior_question_text=(
                    None
                    if prior_question is None
                    else "\n".join(
                        source.content
                        for source in prior_question.source_messages
                    )
                ),
                has_accepted_frame=(
                    case.accepted_frame_revision_id is not None
                ),
                binding_contract_ref=str(
                    message.payload["binding_contract_ref"]
                ),
                requested_at=message.created_at,
            )
            bind_method = getattr(
                self._binding_provider,
                "bind_message",
                None,
            )
            if bind_method is None:
                raise ControllerConflict(
                    "message-binding provider is not configured"
                )
            model_job = self._record_logical_model_job(
                message=message,
                provider=self._binding_provider,
                role="message_binding",
                typed_request_contract_ref=(
                    MESSAGE_BINDING_JOB_CONTRACT_REF
                ),
                output_contract_ref=MESSAGE_BINDING_CONTRACT_REF,
                request=request,
            )
            recovered_proposal = self._load_durable_model_result(
                model_job=model_job,
                result_kind="message_binding",
                result_contract_ref=MESSAGE_BINDING_CONTRACT_REF,
            )
            if recovered_proposal is not None:
                return self._commit_message_binding(
                    snapshot=snapshot,
                    message=message,
                    pending=pending,
                    mailbox_message=mailbox_message,
                    proposal=recovered_proposal,
                    job_lease=job_lease,
                    model_job=model_job,
                    provider_attempts=(
                        self._persisted_provider_attempt_records(
                            model_job.logical_model_job_id
                        )
                    ),
                )
            observer_installed = (
                self._install_provider_attempt_observer(
                    provider=self._binding_provider,
                    model_job=model_job,
                    result_kind="message_binding",
                    result_contract_ref=(
                        MESSAGE_BINDING_CONTRACT_REF
                    ),
                )
            )
            heartbeat = JobHeartbeatSupervisor(
                store=self._store,
                lease=job_lease,
                clock=self._clock,
                lease_duration=self._lease_duration,
            )
            heartbeat.start()
            try:
                proposal = bind_method(request)
            except ProviderError as error:
                self._clear_provider_attempt_observer(
                    self._binding_provider,
                    observer_installed,
                )
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                    raise
                provider_attempts = self._provider_failure_attempt_records(
                    model_job=model_job,
                    provider=self._binding_provider,
                    error=error,
                    completed_at=self._now(),
                )
                return self._commit_provider_failure(
                    snapshot=snapshot,
                    message=message,
                    job_lease=job_lease,
                    model_job=model_job,
                    provider_attempts=provider_attempts,
                    error=error,
                )
            except BaseException:
                self._clear_provider_attempt_observer(
                    self._binding_provider,
                    observer_installed,
                )
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                raise
            self._clear_provider_attempt_observer(
                self._binding_provider,
                observer_installed,
            )
            result_recorded_at = self._now()
            provider_attempts = self._provider_attempt_records(
                model_job=model_job,
                provider=self._binding_provider,
                output=proposal,
                completed_at=result_recorded_at,
            )
            self._persist_successful_model_result(
                model_job=model_job,
                provider_attempts=provider_attempts,
                result=proposal,
                result_kind="message_binding",
                result_contract_ref=MESSAGE_BINDING_CONTRACT_REF,
                recorded_at=result_recorded_at,
            )
            try:
                job_lease = heartbeat.stop_and_get()
            except LeaseFenceLost:
                job_lease = heartbeat.current_lease
                raise
            return self._commit_message_binding(
                snapshot=snapshot,
                message=message,
                pending=pending,
                mailbox_message=mailbox_message,
                proposal=proposal,
                job_lease=job_lease,
                model_job=model_job,
                provider_attempts=provider_attempts,
            )
        finally:
            self._release_job_lease(job_lease)

    def _commit_message_binding(
        self,
        *,
        snapshot: ControllerState,
        message: OutboxMessage,
        pending: PendingUserMessage,
        mailbox_message,
        proposal,
        job_lease,
        model_job: LogicalModelJob,
        provider_attempts: tuple[
            tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
            ...,
        ],
    ) -> ControllerState:
        now = self._now()
        content = str(mailbox_message.payload["message"])
        source_spans: dict[
            tuple[int, int],
            SemanticSourceSpan,
        ] = {}

        def span_for(start: int, end: int) -> SemanticSourceSpan:
            if start < 0 or end <= start or end > len(content):
                raise ControllerConflict(
                    "message binding contains an invalid source span"
                )
            key = (start, end)
            span = source_spans.get(key)
            if span is None:
                span = SemanticSourceSpan(
                    span_id=_stable_id(
                        "semantic-span",
                        mailbox_message.message_id,
                        str(start),
                        str(end),
                    ),
                    message_id=mailbox_message.message_id,
                    start_codepoint=start,
                    end_codepoint=end,
                    selected_text_sha256=content_sha256(
                        content[start:end]
                    ),
                )
                source_spans[key] = span
            return span

        assertions = []
        for index, item in enumerate(proposal.assertions, start=1):
            value = _decode_binding_object(
                item.value_json,
                "semantic assertion value",
            )
            source = span_for(
                item.source_start_codepoint,
                item.source_end_codepoint,
            )
            assertions.append(
                SemanticAssertion(
                    assertion_id=_stable_id(
                        "semantic-assertion",
                        mailbox_message.message_id,
                        str(index),
                        item.kind.value,
                        content_sha256(value),
                    ),
                    kind=item.kind,
                    value=value,
                    source_span_ids=(source.span_id,),
                    decision_record_ids=(),
                    material=item.material,
                )
            )
        ambiguities = []
        for index, item in enumerate(proposal.ambiguities, start=1):
            recommended = _decode_binding_object(
                item.recommended_interpretation_json,
                "recommended interpretation",
            )
            source = span_for(
                item.source_start_codepoint,
                item.source_end_codepoint,
            )
            ambiguities.append(
                SemanticAmbiguity(
                    ambiguity_id=_stable_id(
                        "semantic-ambiguity",
                        mailbox_message.message_id,
                        str(index),
                        item.question,
                    ),
                    question=item.question,
                    material=item.material,
                    recommended_interpretation=recommended,
                    source_span_ids=(source.span_id,),
                )
            )
        semantic_binding = TypedSemanticBinding(
            binding_contract_version=MESSAGE_BINDING_CONTRACT_REF,
            source_spans=tuple(source_spans.values()),
            assertions=tuple(assertions),
            ambiguities=tuple(ambiguities),
            decision_ledger_refs=(),
        )
        controller_lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                self._store.assert_job_lease(
                    job_lease,
                    checked_at=now,
                )
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                if mailbox_message.sequence != current.mailbox_cursor + 1:
                    return self._supersede_job_locked(
                        current,
                        message,
                        now=now,
                        job_lease=job_lease,
                    )
                self._persist_provider_attempt_records(
                    model_job=model_job,
                    provider_attempts=provider_attempts,
                )
                case = self._store.get_case(current.case_id)
                prior_question = (
                    None
                    if case.accepted_question_revision_id is None
                    else self._store.get_question(
                        case.accepted_question_revision_id
                    )
                )
                authority_changing = (
                    proposal.disposition
                    is MessageBindingDisposition.ACCEPTED
                    and proposal.impact_kind
                    in {
                        MessageImpactKind.QUESTION_REVISION,
                        MessageImpactKind.FRAME_REVISION,
                        MessageImpactKind.CHALLENGE,
                    }
                )
                if prior_question is None and not authority_changing:
                    raise ControllerConflict(
                        "the first accepted message must establish a question"
                    )
                question_id = (
                    "{}:question:{}".format(
                        current.case_id,
                        (
                            1
                            if prior_question is None
                            else prior_question.revision_number + 1
                        ),
                    )
                    if authority_changing
                    else None
                )
                binding = MessageImpactBinding(
                    binding_id=_stable_id(
                        "message-impact-binding",
                        pending.pending_message_id,
                        proposal.content_sha256,
                    ),
                    pending_message_id=pending.pending_message_id,
                    case_id=current.case_id,
                    message_id=mailbox_message.message_id,
                    authority_epoch=mailbox_message.authority_epoch,
                    source_payload_sha256=(
                        mailbox_message.operation.payload_sha256
                    ),
                    impact_kind=proposal.impact_kind,
                    disposition=proposal.disposition,
                    bound_question_revision_id=question_id,
                    prior_frame_revision_id=(
                        case.accepted_frame_revision_id
                    ),
                    decision_record_ids=(),
                    semantic_binding=semantic_binding,
                    semantic_binding_sha256=(
                        semantic_binding.content_sha256
                    ),
                    logical_model_job_id=message.outbox_message_id,
                    created_at=now,
                )
                self._store.record_message_impact_binding(binding)
                event_payload = {
                    "outbox_message_id": message.outbox_message_id,
                    "binding_id": binding.binding_id,
                    "impact_kind": binding.impact_kind.value,
                    "disposition": binding.disposition.value,
                    "semantic_binding_sha256": (
                        binding.semantic_binding_sha256
                    ),
                }
                self._append_event(
                    case_id=current.case_id,
                    event_id=_stable_id(
                        "event",
                        binding.binding_id,
                        "completed",
                    ),
                    event_type=JournalEventType.MESSAGE_BINDING_COMPLETED,
                    action_id=None,
                    authority_ref=binding.binding_id,
                    payload=event_payload,
                    customer_projection={
                        "state": "business_request_understood",
                        "impact_kind": binding.impact_kind.value,
                        "needs_user_decision": (
                            binding.disposition
                            is MessageBindingDisposition.NEEDS_USER_DECISION
                        ),
                    },
                    causal_operation=message.operation,
                    now=now,
                )
                if authority_changing:
                    assert question_id is not None
                    revision_number = (
                        1
                        if prior_question is None
                        else prior_question.revision_number + 1
                    )
                    source_messages = tuple(
                        SourceMessageRef(
                            message_id=item.message_id,
                            role=MessageRole.USER,
                            sequence=item.sequence,
                            content=str(item.payload["message"]),
                            content_sha256=content_sha256(
                                str(item.payload["message"])
                            ),
                        )
                        for item in self._store.list_mailbox_messages(
                            current.case_id
                        )
                        if item.sequence <= mailbox_message.sequence
                    )
                    acceptance_event_id = _stable_id(
                        "event",
                        question_id,
                        "accepted",
                    )
                    question = QuestionRevision(
                        question_revision_id=question_id,
                        case_id=current.case_id,
                        revision_number=revision_number,
                        prior_question_revision_id=(
                            None
                            if prior_question is None
                            else prior_question.question_revision_id
                        ),
                        source_messages=source_messages,
                        explicit_scope_refs=(),
                        explicit_constraint_refs=(
                            (
                                ()
                                if prior_question is None
                                else prior_question.explicit_constraint_refs
                            )
                            + (binding.binding_id,)
                        ),
                        explicit_correction_refs=(
                            (binding.binding_id,)
                            if prior_question is not None
                            and binding.impact_kind
                            in {
                                MessageImpactKind.QUESTION_REVISION,
                                MessageImpactKind.FRAME_REVISION,
                            }
                            else ()
                        ),
                        explicit_challenge_refs=(
                            (binding.binding_id,)
                            if binding.impact_kind
                            is MessageImpactKind.CHALLENGE
                            else ()
                        ),
                        accepted_clarification_refs=(),
                        acceptance_event_id=acceptance_event_id,
                        accepted_head_version=case.head_version + 1,
                        analysis_cycle_id="{}:cycle:{}".format(
                            current.case_id,
                            revision_number,
                        ),
                        created_at=now,
                    )
                    self._store.accept_question(
                        question,
                        expected_head_version=case.head_version,
                        event_id=acceptance_event_id,
                        recorded_at=now,
                        operation=message.operation,
                    )
                    active_candidate = (
                        self._store.get_active_frame_candidate(
                            current.case_id
                        )
                    )
                    if (
                        active_candidate is not None
                        and active_candidate.question_revision_id
                        != question.question_revision_id
                    ):
                        self._store.supersede_active_frame_candidate(
                            FrameCandidateSupersessionRecord(
                                supersession_record_id=_stable_id(
                                    "frame-candidate-supersession",
                                    active_candidate.frame_candidate_id,
                                    question.question_revision_id,
                                ),
                                case_id=current.case_id,
                                frame_candidate_id=(
                                    active_candidate.frame_candidate_id
                                ),
                                superseded_by_question_revision_id=(
                                    question.question_revision_id
                                ),
                                source_operation_id=(
                                    message.operation.operation_id
                                ),
                                authority_epoch=(
                                    mailbox_message.authority_epoch
                                ),
                                reason_code=(
                                    "accepted_question_authority_changed"
                                ),
                                created_at=now,
                            )
                        )
                context_user_messages = tuple(
                    ContextUserMessageItem.from_message(item)
                    for item in self._store.list_mailbox_messages(
                        current.case_id
                    )
                    if item.sequence <= mailbox_message.sequence
                )
                if (
                    proposal.disposition
                    is MessageBindingDisposition.NEEDS_USER_DECISION
                ):
                    next_state = self._apply_binding_clarification(
                        current=current,
                        binding=binding,
                        proposal=proposal,
                        context_user_messages=context_user_messages,
                        now=now,
                    )
                elif (
                    proposal.disposition
                    is MessageBindingDisposition.ACCEPTED
                    and proposal.impact_kind
                    is MessageImpactKind.STOP_REQUEST
                ):
                    next_state = self._apply_binding_stop(
                        current=current,
                        binding=binding,
                        context_user_messages=context_user_messages,
                        now=now,
                    )
                else:
                    next_state = self._checkpoint(
                        run_id=current.run_id,
                        case_id=current.case_id,
                        phase=ControllerPhase.READY_FOR_AGENT,
                        step_number=current.step_number,
                        latest_user_message=content,
                        pending_action_id=None,
                        pending_job_ids=(),
                        pending_decision_request_id=None,
                        consecutive_rejections=(
                            current.consecutive_rejections + 1
                            if proposal.disposition
                            is MessageBindingDisposition.REJECTED
                            else 0
                        ),
                        authority_epoch=mailbox_message.authority_epoch,
                        mailbox_cursor=mailbox_message.sequence,
                        context_user_messages=context_user_messages,
                        now=now,
                    )
                self._record_job_disposition(
                    message=message,
                    disposition=JobDisposition.COMPLETED,
                    result_sha256=binding.content_sha256,
                    reason_code="message_binding_committed",
                    now=now,
                    job_lease=job_lease,
                    observed_authority_epoch=(
                        mailbox_message.authority_epoch
                    ),
                )
                return next_state
        finally:
            self._store.release_lease(controller_lease)

    def _apply_binding_clarification(
        self,
        *,
        current: ControllerState,
        binding: MessageImpactBinding,
        proposal,
        context_user_messages: tuple[ContextUserMessageItem, ...],
        now: datetime,
    ) -> ControllerState:
        material = tuple(
            item for item in proposal.ambiguities if item.material
        )
        if not material:
            raise ControllerConflict(
                "binding clarification requires material ambiguity"
            )
        payload = AskUserPayload(
            question=material[0].question,
            options=proposal.clarification_options,
            recommended_option_id=proposal.recommended_option_id or "",
            allow_freeform=True,
        )
        action = self._binding_action(
            current=current,
            binding=binding,
            kind=ActionKind.ASK_USER,
            payload=payload,
            now=now,
        )
        self._store.record_action(
            PersistedAction(
                action=action,
                proposal_sha256=AgentActionProposal(
                    kind=action.kind,
                    payload=action.payload,
                ).content_sha256,
                recorded_at=now,
            )
        )
        self._append_action_event(
            action=action,
            accepted=True,
            reason_code="typed_message_ambiguity",
            now=now,
        )
        state, _ = self._apply_action(
            current=current,
            action=action,
            admission_cursor=self._last_cursor(current.case_id),
            now=now,
        )
        return self._checkpoint_with_user_context(
            state,
            context_user_messages=context_user_messages,
            authority_epoch=binding.authority_epoch,
            mailbox_cursor=next(
                item.sequence
                for item in context_user_messages
                if item.message_id == binding.message_id
            ),
            now=now,
        )

    def _apply_binding_stop(
        self,
        *,
        current: ControllerState,
        binding: MessageImpactBinding,
        context_user_messages: tuple[ContextUserMessageItem, ...],
        now: datetime,
    ) -> ControllerState:
        payload = StopPayload(
            reason="user_requested_stop",
            terminal_state="stopped",
        )
        action = self._binding_action(
            current=current,
            binding=binding,
            kind=ActionKind.STOP,
            payload=payload,
            now=now,
        )
        self._store.record_action(
            PersistedAction(
                action=action,
                proposal_sha256=AgentActionProposal(
                    kind=action.kind,
                    payload=action.payload,
                ).content_sha256,
                recorded_at=now,
            )
        )
        self._append_action_event(
            action=action,
            accepted=True,
            reason_code="typed_stop_request",
            now=now,
        )
        state, _ = self._apply_action(
            current=current,
            action=action,
            admission_cursor=self._last_cursor(current.case_id),
            now=now,
        )
        return self._checkpoint_with_user_context(
            state,
            context_user_messages=context_user_messages,
            authority_epoch=binding.authority_epoch,
            mailbox_cursor=next(
                item.sequence
                for item in context_user_messages
                if item.message_id == binding.message_id
            ),
            now=now,
        )

    def _binding_action(
        self,
        *,
        current: ControllerState,
        binding: MessageImpactBinding,
        kind: ActionKind,
        payload,
        now: datetime,
    ) -> ActionEnvelope:
        proposal = AgentActionProposal(kind=kind, payload=payload)
        action_id = _stable_id(
            "action",
            binding.binding_id,
            kind.value,
        )
        idempotency_key = _stable_id(
            "action-key",
            binding.binding_id,
            kind.value,
        )
        return ActionEnvelope(
            action_id=action_id,
            case_id=current.case_id,
            kind=kind,
            expected_head_version=self._store.get_case(
                current.case_id
            ).head_version,
            idempotency_key=idempotency_key,
            issued_at=now,
            payload=payload,
            operation=OperationIdentity(
                operation_id=_stable_id("operation", action_id),
                idempotency_key=idempotency_key,
                causation_id=binding.binding_id,
                correlation_id=current.run_id,
                authority_revision=binding.authority_epoch,
                payload_sha256=proposal.content_sha256,
            ),
        )

    def _checkpoint_with_user_context(
        self,
        state: ControllerState,
        *,
        context_user_messages: tuple[ContextUserMessageItem, ...],
        authority_epoch: int,
        mailbox_cursor: int,
        now: datetime,
    ) -> ControllerState:
        return self._checkpoint(
            run_id=state.run_id,
            case_id=state.case_id,
            phase=state.phase,
            step_number=state.step_number,
            latest_user_message=context_user_messages[-1].content,
            pending_action_id=state.pending_action_id,
            pending_job_ids=state.pending_job_ids,
            pending_decision_request_id=(
                state.pending_decision_request_id
            ),
            consecutive_rejections=state.consecutive_rejections,
            authority_epoch=authority_epoch,
            mailbox_cursor=mailbox_cursor,
            context_user_messages=context_user_messages,
            now=now,
        )

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
            if (
                state.phase
                is ControllerPhase.WAITING_FOR_MESSAGE_BINDING
            ):
                state = self.deliver_pending_message_binding(case_id)
                continue
            if state.phase is ControllerPhase.READY_FOR_AGENT:
                state = self.advance(case_id)
                continue
            if state.phase is ControllerPhase.WAITING_FOR_LLM:
                state = self.deliver_pending_llm(case_id)
                continue
            if (
                state.phase
                is ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW
            ):
                state = self.deliver_pending_frame_review(case_id)
                continue
            if state.phase not in {
                ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
                ControllerPhase.READY_FOR_AGENT,
                ControllerPhase.WAITING_FOR_LLM,
                ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW,
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
                binding_job_id = _stable_id(
                    "outbox",
                    case_id,
                    "message-binding",
                    str(receipt.mailbox_sequence),
                )
                return self._checkpoint(
                    run_id=current.run_id,
                    case_id=case_id,
                    phase=ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
                    step_number=current.step_number,
                    latest_user_message=selected_message,
                    pending_action_id=None,
                    pending_job_ids=(binding_job_id,),
                    pending_decision_request_id=None,
                    consecutive_rejections=0,
                    authority_epoch=receipt.authority_epoch,
                    mailbox_cursor=current.mailbox_cursor,
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
        if snapshot.phase is ControllerPhase.WAITING_FOR_EVIDENCE_ADMISSION:
            selected_job_id = job_id or snapshot.pending_job_ids[0]
            if selected_job_id not in snapshot.pending_job_ids:
                raise ControllerConflict(
                    "evidence job is not pending for this case"
                )
            return self._admit_pending_evidence(
                snapshot,
                outbox_message_id=selected_job_id,
            )
        if snapshot.phase is not ControllerPhase.WAITING_FOR_EFFECT:
            raise ControllerConflict("case has no pending effect")
        selected_job_id = job_id or snapshot.pending_job_ids[0]
        if selected_job_id not in snapshot.pending_job_ids:
            raise ControllerConflict("effect job is not pending for this case")
        message = self._store.get_outbox_message(selected_job_id)
        job_lease = self._acquire_job(message)
        try:
            if (
                message.contract_ref == OBLIGATION_JOB_CONTRACT_REF
                and self._store.find_capability_result_receipt_by_outbox(
                    message.outbox_message_id
                )
                is not None
            ):
                self._release_job_lease(job_lease)
                job_lease = None
                recovered = self._admit_pending_evidence(
                    snapshot,
                    outbox_message_id=message.outbox_message_id,
                )
                if self._job_is_stale(message):
                    return self._reconcile_mailbox(recovered)
                return recovered
            if self._job_is_stale(message):
                return self._supersede_job(
                    snapshot,
                    message,
                    job_lease=job_lease,
                )
            started_at = self._now()
            heartbeat = JobHeartbeatSupervisor(
                store=self._store,
                lease=job_lease,
                clock=self._clock,
                lease_duration=self._lease_duration,
            )
            heartbeat.start()
            try:
                result = self._effect_executor.execute(message)
            except EffectTransientError as error:
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                    raise
                return self._commit_effect_attempt(
                    snapshot=snapshot,
                    message=message,
                    status=EffectAttemptStatus.RETRYABLE_FAILURE,
                    result=None,
                    error_code="transient_effect_failure",
                    error_message=str(error),
                    started_at=started_at,
                    job_lease=job_lease,
                )
            except EffectPermanentError as error:
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                    raise
                return self._commit_effect_attempt(
                    snapshot=snapshot,
                    message=message,
                    status=EffectAttemptStatus.TERMINAL_FAILURE,
                    result=None,
                    error_code="permanent_effect_failure",
                    error_message=str(error),
                    started_at=started_at,
                    job_lease=job_lease,
                )
            except BaseException:
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                raise
            try:
                job_lease = heartbeat.stop_and_get()
            except LeaseFenceLost:
                job_lease = heartbeat.current_lease
                raise
            try:
                committed = self._commit_effect_attempt(
                    snapshot=snapshot,
                    message=message,
                    status=EffectAttemptStatus.SUCCEEDED,
                    result=result,
                    error_code=None,
                    error_message=None,
                    started_at=started_at,
                    job_lease=job_lease,
                )
            except (
                AuthorityConflict,
                ControllerConflict,
                InvalidAuthorityTransition,
                TypeError,
                ValueError,
            ) as error:
                if message.contract_ref != OBLIGATION_JOB_CONTRACT_REF:
                    raise
                committed = self._commit_effect_attempt(
                    snapshot=snapshot,
                    message=message,
                    status=EffectAttemptStatus.TERMINAL_FAILURE,
                    result=None,
                    error_code="invalid_evidence_result_contract",
                    error_message=str(error),
                    started_at=started_at,
                    job_lease=job_lease,
                )
            self._release_job_lease(job_lease)
            job_lease = None
            if (
                message.contract_ref == OBLIGATION_JOB_CONTRACT_REF
                and committed.phase
                is not ControllerPhase.WAITING_FOR_EVIDENCE_ADMISSION
                and self._store.get_job_disposition(
                    message.outbox_message_id
                )
                is None
            ):
                self._recover_landed_evidence(
                    outbox_message_id=message.outbox_message_id,
                    admitted_at=self._now(),
                )
                if self._job_is_stale(message):
                    return self._reconcile_mailbox(committed)
            return committed
        finally:
            if job_lease is not None:
                self._release_job_lease(job_lease)

    def deliver_pending_frame_review(
        self,
        case_id: str,
        *,
        job_id: str | None = None,
    ) -> ControllerState:
        snapshot = self.resume(case_id)
        if (
            snapshot.phase
            is not ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW
        ):
            raise ControllerConflict(
                "case has no pending measurement review"
            )
        selected_job_id = job_id or snapshot.pending_job_ids[0]
        if selected_job_id not in snapshot.pending_job_ids:
            raise ControllerConflict(
                "measurement review job is not pending"
            )
        message = self._store.get_outbox_message(selected_job_id)
        if message.job_kind is not AsyncJobKind.REVIEWER:
            raise ControllerConflict("pending job is not a Reviewer job")
        job_lease = self._acquire_job(message)
        try:
            if self._job_is_stale(message):
                return self._supersede_job(
                    snapshot,
                    message,
                    job_lease=job_lease,
                )
            candidate = self._store.get_frame_candidate(
                str(message.payload["frame_candidate_id"])
            )
            logical_model_job_id = message.outbox_message_id
            question = self._store.get_question(
                candidate.question_revision_id
            )
            bindings = tuple(
                self._store.get_message_impact_binding(binding_id)
                for binding_id in question.explicit_constraint_refs
            )
            prior_review = (
                None
                if candidate.prior_frame_candidate_id is None
                else self._store.get_frame_review_for_candidate(
                    candidate.prior_frame_candidate_id
                )
            )
            objection_closures = tuple(
                self._store.get_objection_closure(
                    _stable_id(
                        "objection-closure",
                        objection_id,
                        candidate.frame_candidate_id,
                    )
                )
                for objection_id in candidate.addressed_objection_ids
            )
            deterministic_findings = tuple(
                DeterministicFrameValidationFinding(
                    code=finding.code,
                    node_refs=finding.node_refs,
                )
                for finding in validate_executable_design(
                    candidate.proposed_frame.measurement_design
                )
            )
            reviewer_configuration_ref = _provider_configuration_ref(
                self._reviewer_provider,
                ModelExecutionRole.RUNTIME_REVIEWER,
            )
            request = FrameReviewRequest(
                logical_model_job_id=logical_model_job_id,
                case_id=case_id,
                frame_candidate=candidate,
                accepted_question=question,
                accepted_message_bindings=bindings,
                prior_frame_review=prior_review,
                objection_closures=objection_closures,
                deterministic_validation_findings=(
                    deterministic_findings
                ),
                review_contract_ref=str(
                    message.payload["review_contract_ref"]
                ),
                reviewer_configuration_ref=(
                    reviewer_configuration_ref
                ),
                independence_policy_ref=(
                    FRAME_REVIEW_INDEPENDENCE_POLICY_REF
                ),
                requested_at=message.created_at,
            )
            review_method = getattr(
                self._reviewer_provider,
                "review",
                None,
            )
            if review_method is None:
                raise ControllerConflict(
                    "measurement Reviewer provider is not configured"
                )
            model_job = self._record_logical_model_job(
                message=message,
                provider=self._reviewer_provider,
                role="measurement_reviewer",
                typed_request_contract_ref=(
                    FRAME_REVIEW_JOB_CONTRACT_REF
                ),
                output_contract_ref=FRAME_REVIEW_CONTRACT_REF,
                request=request,
            )
            recovered_proposal = self._load_durable_model_result(
                model_job=model_job,
                result_kind="measurement_reviewer",
                result_contract_ref=FRAME_REVIEW_CONTRACT_REF,
            )
            if recovered_proposal is not None:
                return self._commit_frame_review(
                    snapshot=snapshot,
                    message=message,
                    candidate=candidate,
                    proposal=recovered_proposal,
                    deterministic_findings=deterministic_findings,
                    logical_model_job_id=logical_model_job_id,
                    job_lease=job_lease,
                    model_job=model_job,
                    provider_attempts=(
                        self._persisted_provider_attempt_records(
                            model_job.logical_model_job_id
                        )
                    ),
                )
            observer_installed = (
                self._install_provider_attempt_observer(
                    provider=self._reviewer_provider,
                    model_job=model_job,
                    result_kind="measurement_reviewer",
                    result_contract_ref=FRAME_REVIEW_CONTRACT_REF,
                )
            )
            heartbeat = JobHeartbeatSupervisor(
                store=self._store,
                lease=job_lease,
                clock=self._clock,
                lease_duration=self._lease_duration,
            )
            heartbeat.start()
            try:
                proposal = review_method(request)
            except ProviderError as error:
                self._clear_provider_attempt_observer(
                    self._reviewer_provider,
                    observer_installed,
                )
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                    raise
                provider_attempts = self._provider_failure_attempt_records(
                    model_job=model_job,
                    provider=self._reviewer_provider,
                    error=error,
                    completed_at=self._now(),
                )
                return self._commit_provider_failure(
                    snapshot=snapshot,
                    message=message,
                    job_lease=job_lease,
                    model_job=model_job,
                    provider_attempts=provider_attempts,
                    error=error,
                )
            except BaseException:
                self._clear_provider_attempt_observer(
                    self._reviewer_provider,
                    observer_installed,
                )
                try:
                    job_lease = heartbeat.stop_and_get()
                except LeaseFenceLost:
                    job_lease = heartbeat.current_lease
                raise
            self._clear_provider_attempt_observer(
                self._reviewer_provider,
                observer_installed,
            )
            result_recorded_at = self._now()
            provider_attempts = self._provider_attempt_records(
                model_job=model_job,
                provider=self._reviewer_provider,
                output=proposal,
                completed_at=result_recorded_at,
            )
            self._persist_successful_model_result(
                model_job=model_job,
                provider_attempts=provider_attempts,
                result=proposal,
                result_kind="measurement_reviewer",
                result_contract_ref=FRAME_REVIEW_CONTRACT_REF,
                recorded_at=result_recorded_at,
            )
            try:
                job_lease = heartbeat.stop_and_get()
            except LeaseFenceLost:
                job_lease = heartbeat.current_lease
                raise
            return self._commit_frame_review(
                snapshot=snapshot,
                message=message,
                candidate=candidate,
                proposal=proposal,
                deterministic_findings=deterministic_findings,
                logical_model_job_id=logical_model_job_id,
                job_lease=job_lease,
                model_job=model_job,
                provider_attempts=provider_attempts,
            )
        finally:
            self._release_job_lease(job_lease)

    def _commit_frame_review(
        self,
        *,
        snapshot: ControllerState,
        message: OutboxMessage,
        candidate: FrameCandidateRecord,
        proposal,
        deterministic_findings: tuple[
            DeterministicFrameValidationFinding,
            ...,
        ],
        logical_model_job_id: str,
        job_lease,
        model_job: LogicalModelJob,
        provider_attempts: tuple[
            tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
            ...,
        ],
    ) -> ControllerState:
        now = self._now()
        reviewer_objections = tuple(
            MeasurementReviewObjection(
                objection_id=_stable_id(
                    "measurement-objection",
                    candidate.frame_candidate_id,
                    str(index),
                    item.code,
                    item.explanation,
                ),
                code=item.code,
                severity=item.severity,
                affected_node_ids=item.affected_node_refs,
                explanation=item.explanation,
            )
            for index, item in enumerate(proposal.objections, start=1)
        )
        deterministic_objections = tuple(
            MeasurementReviewObjection(
                objection_id=_stable_id(
                    "deterministic-measurement-objection",
                    candidate.frame_candidate_id,
                    finding.code,
                    *finding.node_refs,
                ),
                code="deterministic:{}".format(finding.code),
                severity=MeasurementObjectionSeverity.BLOCKING,
                affected_node_ids=finding.node_refs,
                explanation=(
                    "Deterministic measurement validation rejected this "
                    "candidate."
                ),
            )
            for finding in deterministic_findings
        )
        objections = reviewer_objections + deterministic_objections
        effective_disposition = (
            FrameReviewDisposition.BLOCK
            if deterministic_findings
            else proposal.disposition
        )
        closure_record_ids = tuple(
            _stable_id(
                "objection-closure",
                objection_id,
                candidate.frame_candidate_id,
            )
            for objection_id in candidate.addressed_objection_ids
        )
        review = FrameReviewRecord(
            frame_review_id=_stable_id(
                "frame-review",
                candidate.frame_candidate_id,
                proposal.content_sha256,
            ),
            frame_candidate_id=candidate.frame_candidate_id,
            reviewer_job_id=message.outbox_message_id,
            authority_epoch=message.expected_authority_epoch,
            disposition=effective_disposition,
            objections=objections,
            closure_proof_refs=closure_record_ids,
            reviewed_frame_content_sha256=(
                candidate.proposed_frame_content_sha256
            ),
            logical_model_job_id=logical_model_job_id,
            created_at=now,
        )
        controller_lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                self._store.assert_job_lease(
                    job_lease,
                    checked_at=now,
                )
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                if self._job_is_stale(message):
                    return self._supersede_job_locked(
                        current,
                        message,
                        now=now,
                        job_lease=job_lease,
                    )
                self._persist_provider_attempt_records(
                    model_job=model_job,
                    provider_attempts=provider_attempts,
                )
                self._store.record_frame_review(review)
                event_payload = {
                    "outbox_message_id": message.outbox_message_id,
                    "frame_candidate_id": candidate.frame_candidate_id,
                    "frame_review_id": review.frame_review_id,
                    "disposition": review.disposition.value,
                    "objection_count": len(review.objections),
                }
                self._append_event(
                    case_id=current.case_id,
                    event_id=_stable_id(
                        "event",
                        review.frame_review_id,
                        "completed",
                    ),
                    event_type=JournalEventType.REVIEWER_JOB_COMPLETED,
                    action_id=message.action_id,
                    authority_ref=review.frame_review_id,
                    payload=event_payload,
                    customer_projection={
                        "state": "measurement_review_completed",
                        "disposition": review.disposition.value,
                    },
                    causal_operation=message.operation,
                    now=now,
                )
                if review.disposition is FrameReviewDisposition.ACCEPT:
                    authority_snapshot = (
                        self._store.get_authority_snapshot(
                            current.case_id
                        )
                    )
                    proof = FrameAdmissionProof(
                        frame_admission_proof_id=_stable_id(
                            "frame-admission-proof",
                            review.frame_review_id,
                        ),
                        case_id=current.case_id,
                        frame_candidate_id=(
                            candidate.frame_candidate_id
                        ),
                        candidate_generation=(
                            candidate.candidate_generation
                        ),
                        frame_revision_id=(
                            candidate.proposed_frame_revision_id
                        ),
                        frame_content_sha256=(
                            candidate.proposed_frame_content_sha256
                        ),
                        frame_review_id=review.frame_review_id,
                        frame_review_content_sha256=(
                            review.content_sha256
                        ),
                        objection_closure_record_ids=(
                            closure_record_ids
                        ),
                        authority_snapshot=authority_snapshot,
                        authority_snapshot_sha256=(
                            authority_snapshot.content_sha256
                        ),
                        created_at=now,
                    )
                    self._store.record_frame_admission_proof(proof)
                    self._store.accept_frame(
                        candidate.proposed_frame,
                        frame_admission_proof_id=(
                            proof.frame_admission_proof_id
                        ),
                        expected_head_version=current.head_version,
                        event_id=_stable_id(
                            "event",
                            candidate.proposed_frame_revision_id,
                            "accepted",
                        ),
                        recorded_at=now,
                        operation=message.operation,
                    )
                next_state = self._checkpoint(
                    run_id=current.run_id,
                    case_id=current.case_id,
                    phase=ControllerPhase.READY_FOR_AGENT,
                    step_number=current.step_number,
                    latest_user_message=current.latest_user_message,
                    pending_action_id=None,
                    pending_job_ids=(),
                    pending_decision_request_id=None,
                    consecutive_rejections=(
                        0
                        if review.disposition
                        is FrameReviewDisposition.ACCEPT
                        else current.consecutive_rejections + 1
                    ),
                    now=now,
                )
                self._record_job_disposition(
                    message=message,
                    disposition=JobDisposition.COMPLETED,
                    result_sha256=review.content_sha256,
                    reason_code="measurement_review_committed",
                    now=now,
                    job_lease=job_lease,
                )
                return next_state
        finally:
            self._store.release_lease(controller_lease)

    def _commit_proposal(
        self,
        snapshot: ControllerState,
        proposal: AgentActionProposal,
        message: OutboxMessage,
        job_lease,
        *,
        model_job: LogicalModelJob,
        provider_attempts: tuple[
            tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
            ...,
        ],
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
                self._store.assert_job_lease(
                    job_lease,
                    checked_at=now,
                )
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                if self._job_is_stale(message):
                    return self._supersede_job_locked(
                        current,
                        message,
                        now=now,
                        job_lease=job_lease,
                    )
                self._persist_provider_attempt_records(
                    model_job=model_job,
                    provider_attempts=provider_attempts,
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
                    current_query_bindings=(
                        ()
                        if plan is None
                        else self._store.list_query_bindings(
                            plan.plan_revision_id
                        )
                    ),
                )
                if action.kind is ActionKind.RUN_SENSITIVITY:
                    admission = replace(
                        admission,
                        accepted=False,
                        reason_code=(
                            "sensitivity_dispatch_identity_unsealed"
                        ),
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
                    self._record_job_disposition(
                        message=message,
                        disposition=JobDisposition.COMPLETED,
                        result_sha256=proposal.content_sha256,
                        reason_code="proposal_admission_rejected",
                        now=now,
                        job_lease=job_lease,
                    )
                    return next_state
                next_state, outcome_cursor = self._apply_action(
                    current=current,
                    action=action,
                    admission_cursor=admission_event.cursor,
                    now=now,
                )
                self._record_job_disposition(
                    message=message,
                    disposition=JobDisposition.COMPLETED,
                    result_sha256=proposal.content_sha256,
                    reason_code="proposal_committed",
                    now=now,
                    job_lease=job_lease,
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
        deferred_effect_outbox: OutboxMessage | None = None
        action_result_code = "accepted"
        next_consecutive_rejections = 0

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
            prior_candidate = self._store.get_active_frame_candidate(
                case.case_id
            )
            prior_review = (
                None
                if prior_candidate is None
                else self._store.get_frame_review_for_candidate(
                    prior_candidate.frame_candidate_id
                )
            )
            closure_by_objection_id = {
                item.objection_id: item
                for item in payload.objection_closures
            }
            changed_node_ids = (
                ()
                if prior_candidate is None
                else derive_changed_measurement_node_ids(
                    prior_candidate.proposed_frame.measurement_design,
                    frame.measurement_design,
                )
            )
            generation = (
                1
                if prior_candidate is None
                else prior_candidate.candidate_generation + 1
            )
            candidate_id = _stable_id(
                "frame-candidate",
                action.action_id,
                frame.content_sha256,
            )
            review_job_id = _stable_id(
                "outbox",
                candidate_id,
                "measurement-review",
            )
            context_packet = self._store.get_context_packet(
                current.context_packet_id
            )
            candidate = FrameCandidateRecord(
                frame_candidate_id=candidate_id,
                case_id=case.case_id,
                message_binding_id=str(
                    (
                        context_packet.accepted_message_binding_payload
                        or {}
                    ).get("binding_id", "")
                ),
                question_revision_id=frame.question_revision_id,
                proposed_frame_revision_id=frame.frame_revision_id,
                proposed_frame_content_sha256=frame.content_sha256,
                proposed_frame=frame,
                candidate_generation=generation,
                prior_frame_candidate_id=(
                    None
                    if prior_candidate is None
                    else prior_candidate.frame_candidate_id
                ),
                addressed_objection_ids=(
                    payload.addressed_objection_ids
                ),
                authority_epoch=current.authority_epoch,
                source_action_id=action.action_id,
                source_operation_id=action.operation.operation_id,
                review_job_id=review_job_id,
                created_at=now,
            )
            self._store.record_frame_candidate(candidate)
            if prior_candidate is not None:
                if prior_review is None:
                    raise ControllerConflict(
                        "replacement Frame has no prior review"
                    )
                objection_by_id = {
                    objection.objection_id: objection
                    for objection in prior_review.objections
                }
                for objection_id in candidate.addressed_objection_ids:
                    objection = objection_by_id[objection_id]
                    proposal_closure = closure_by_objection_id[
                        objection_id
                    ]
                    relevant_changed_node_ids = tuple(
                        node_id
                        for node_id in changed_node_ids
                        if any(
                            measurement_paths_overlap(
                                node_id,
                                affected_node_id,
                            )
                            for affected_node_id
                            in objection.affected_node_ids
                        )
                    )
                    closure_material = {
                        "kind": "objection-closure-derivation.v1",
                        "objection_id": objection_id,
                        "source_frame_review_id": (
                            prior_review.frame_review_id
                        ),
                        "source_frame_candidate_id": (
                            prior_candidate.frame_candidate_id
                        ),
                        "replacement_frame_candidate_id": (
                            candidate.frame_candidate_id
                        ),
                        "objection_content_sha256": content_sha256(
                            objection
                        ),
                        "changed_node_ids": (
                            relevant_changed_node_ids
                        ),
                        "closure_explanation": (
                            proposal_closure.explanation
                        ),
                        "created_by_action_id": action.action_id,
                    }
                    self._store.record_objection_closure(
                        ObjectionClosureRecord(
                            objection_closure_id=_stable_id(
                                "objection-closure",
                                objection_id,
                                candidate.frame_candidate_id,
                            ),
                            objection_id=objection_id,
                            source_frame_review_id=(
                                prior_review.frame_review_id
                            ),
                            source_frame_candidate_id=(
                                prior_candidate.frame_candidate_id
                            ),
                            replacement_frame_candidate_id=(
                                candidate.frame_candidate_id
                            ),
                            objection_content_sha256=content_sha256(
                                objection
                            ),
                            changed_node_ids=(
                                relevant_changed_node_ids
                            ),
                            closure_explanation=(
                                proposal_closure.explanation
                            ),
                            derivation_proof_sha256=content_sha256(
                                closure_material
                            ),
                            created_by_action_id=action.action_id,
                            created_at=now,
                        )
                    )
            review_payload = {
                "frame_candidate_id": candidate.frame_candidate_id,
                "frame_content_sha256": frame.content_sha256,
                "candidate_generation": generation,
                "review_contract_ref": FRAME_REVIEW_CONTRACT_REF,
            }
            review_event_id = _stable_id(
                "event",
                review_job_id,
                "enqueued",
            )
            review_event = self._append_event(
                case_id=case.case_id,
                event_id=review_event_id,
                event_type=JournalEventType.REVIEWER_JOB_ENQUEUED,
                action_id=action.action_id,
                authority_ref=candidate.frame_candidate_id,
                payload=review_payload,
                customer_projection={
                    "state": "reviewing_measurement_design",
                },
                causal_operation=action.operation,
                now=now,
            )
            review_operation = OperationIdentity(
                operation_id=_stable_id("operation", review_job_id),
                idempotency_key=_stable_id(
                    "review-job-key",
                    candidate.frame_candidate_id,
                ),
                causation_id=action.operation.operation_id,
                correlation_id=action.operation.correlation_id,
                authority_revision=current.authority_epoch,
                payload_sha256=content_sha256(review_payload),
            )
            authority_snapshot = self._store.get_authority_snapshot(
                case.case_id
            )
            self._store.enqueue_outbox(
                OutboxMessage(
                    outbox_message_id=review_job_id,
                    case_id=case.case_id,
                    source_event_cursor=review_event.cursor,
                    action_id=action.action_id,
                    job_kind=AsyncJobKind.REVIEWER,
                    operation=review_operation,
                    expected_head_version=case.head_version,
                    expected_authority_epoch=current.authority_epoch,
                    authority_snapshot=authority_snapshot,
                    authority_snapshot_sha256=(
                        authority_snapshot.content_sha256
                    ),
                    idempotency_key=review_operation.idempotency_key,
                    destination="measurement-reviewer-provider",
                    contract_ref=FRAME_REVIEW_JOB_CONTRACT_REF,
                    payload=review_payload,
                    payload_sha256=content_sha256(review_payload),
                    created_at=now,
                )
            )
            phase = ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW
            pending_job_ids = (review_job_id,)
            outcome_cursor = review_event.cursor
        elif action.kind is ActionKind.REVISE_PLAN:
            assert isinstance(payload, RevisePlanPayload)
            prior = self._latest_plan(case.case_id)
            frame = self._store.get_frame(
                case.accepted_frame_revision_id or ""
            )
            authority_snapshot = self._store.get_authority_snapshot(
                case.case_id
            )
            available_outcomes = self._store.list_measurement_resolutions(
                frame.frame_revision_id
            )
            obligations = self._store.list_evidence_obligations(
                frame.frame_revision_id
            )
            proposed_obligation_ids = {
                obligation_id
                for task in payload.tasks
                for obligation_id in task.obligation_ids
            }
            selected_outcome_ids = {
                obligation.resolution_outcome_id
                for obligation in obligations
                if obligation.obligation_id
                in proposed_obligation_ids
            }
            outcomes = tuple(
                outcome
                for outcome in available_outcomes
                if outcome.resolution_outcome_id
                in selected_outcome_ids
            )
            admissions = tuple(
                self._store.get_measurement_resolution_admission(
                    item.resolution_outcome_id
                )
                for item in outcomes
            )
            bundle = compile_plan_bundle(
                case=case,
                authority_snapshot=authority_snapshot,
                frame=frame,
                outcomes=outcomes,
                admissions=admissions,
                obligations=obligations,
                proposed_tasks=payload.tasks,
                plan_revision_id=_stable_id(
                    "plan",
                    action.action_id,
                    payload.revision_reason,
                ),
                revision_number=1 if prior is None else prior.revision_number + 1,
                prior_plan_revision_id=(
                    None if prior is None else prior.plan_revision_id
                ),
                created_by_action_id=action.action_id,
                created_at=now,
                revision_reason=payload.revision_reason,
            )
            case = self._store.accept_plan_bundle(
                bundle,
                expected_head_version=case.head_version,
                event_id=_stable_id(
                    "event",
                    bundle.plan.plan_revision_id,
                    "accepted",
                ),
                recorded_at=now,
                operation=action.operation,
            )
            outcome_cursor = self._last_cursor(case.case_id)
        elif action.kind is ActionKind.RECORD_INTERPRETATION:
            assert isinstance(payload, RecordInterpretationPayload)
            admissions = (
                self._store.list_evidence_admissions(
                    case_id=case.case_id
                )
            )
            admission_by_evidence_id = {}
            for evidence_record_id in payload.evidence_record_ids:
                accepted = tuple(
                    item
                    for item in admissions
                    if item.evidence_record_id == evidence_record_id
                    and item.status is EvidenceAdmissionStatus.ACCEPTED
                )
                if len(accepted) != 1:
                    raise ControllerConflict(
                        "interpretation requires one accepted Evidence "
                        "admission per record"
                    )
                admission_by_evidence_id[
                    evidence_record_id
                ] = accepted[0]
            validity_by_evidence_id = {
                evidence_record_id: (
                    self._store.latest_evidence_validity(
                        evidence_record_id
                    )
                )
                for evidence_record_id in payload.evidence_record_ids
            }
            if any(
                item.status
                is not EvidenceValidityStatus.ADMITTED_VALID
                for item in validity_by_evidence_id.values()
            ):
                raise ControllerConflict(
                    "interpretation requires current admitted-valid Evidence"
                )
            interpretation = InterpretationRecord(
                interpretation_id=_stable_id(
                    "interpretation",
                    action.action_id,
                ),
                case_id=case.case_id,
                frame_revision_id=case.accepted_frame_revision_id or "",
                evidence_record_ids=payload.evidence_record_ids,
                evidence_admission_ids=tuple(
                    admission_by_evidence_id[
                        evidence_record_id
                    ].evidence_admission_id
                    for evidence_record_id
                    in payload.evidence_record_ids
                ),
                evidence_validity_ids=tuple(
                    validity_by_evidence_id[
                        evidence_record_id
                    ].evidence_validity_id
                    for evidence_record_id
                    in payload.evidence_record_ids
                ),
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
                causal_operation=action.operation,
                now=now,
            )
            outcome_cursor = event.cursor
            phase = ControllerPhase.WAITING_FOR_USER
            pending_decision_id = request.decision_request_id
        elif action.kind is ActionKind.PROPOSE_ANSWER:
            assert isinstance(payload, ProposeAnswerPayload)
            current_authority = self._store.get_authority_snapshot(
                case.case_id
            )
            plan_revision_id = case.accepted_plan_revision_id
            if plan_revision_id is None:
                raise ControllerConflict(
                    "answer proposal requires an accepted Plan"
                )
            adoption = self._store.get_plan_adoption(plan_revision_id)
            prior = self._store.latest_answer(case.case_id)
            candidate = build_provisional_answer_candidate(
                case_id=case.case_id,
                current_authority=current_authority,
                plan_adoption=adoption,
                version_number=(
                    1 if prior is None else prior.version_number + 1
                ),
                prior_answer_version_id=(
                    None if prior is None else prior.answer_version_id
                ),
                claims=payload.claims,
                narrative_blocks=payload.narrative_blocks,
                created_by_action_id=action.action_id,
                created_at=now,
            )
            bundle, case = (
                self._store.accept_provisional_answer_candidate(
                    candidate=candidate,
                    expected_head_version=case.head_version,
                    event_id=_stable_id(
                        "event",
                        candidate.answer_candidate_id,
                        "accepted",
                    ),
                    recorded_at=now,
                    operation=action.operation,
                )
            )
            outcome_cursor = self._last_cursor(case.case_id)
            if bundle.status is AnswerCandidateStatus.REJECTED:
                phase = ControllerPhase.READY_FOR_AGENT
                next_consecutive_rejections = (
                    current.consecutive_rejections + 1
                )
                action_result_code = "answer_precheck_rejected"
            else:
                answer = bundle.answer
                if answer is None:
                    raise ControllerConflict(
                        "accepted answer candidate lacks provisional Answer"
                    )
                review_job_id = _stable_id(
                    "outbox",
                    answer.answer_version_id,
                    "provisional-answer-review",
                )
                review_payload = {
                    "answer_candidate_id": (
                        candidate.answer_candidate_id
                    ),
                    "answer_candidate_content_sha256": (
                        candidate.content_sha256
                    ),
                    "answer_version_id": answer.answer_version_id,
                    "answer_version_content_sha256": (
                        answer.content_sha256
                    ),
                    "claim_precheck_ids": tuple(
                        item.claim_precheck_id
                        for item in bundle.prechecks
                    ),
                    "claim_precheck_content_sha256s": tuple(
                        item.content_sha256 for item in bundle.prechecks
                    ),
                }
                review_event = self._append_event(
                    case_id=case.case_id,
                    event_id=_stable_id(
                        "event",
                        review_job_id,
                        "enqueued",
                    ),
                    event_type=JournalEventType.REVIEWER_JOB_ENQUEUED,
                    action_id=action.action_id,
                    authority_ref=answer.answer_version_id,
                    payload=review_payload,
                    customer_projection={
                        "state": "reviewing_provisional_answer",
                    },
                    causal_operation=action.operation,
                    now=now,
                )
                review_operation = OperationIdentity(
                    operation_id=_stable_id(
                        "operation",
                        review_job_id,
                    ),
                    idempotency_key=_stable_id(
                        "answer-review-job-key",
                        answer.answer_version_id,
                    ),
                    causation_id=action.operation.operation_id,
                    correlation_id=action.operation.correlation_id,
                    authority_revision=current.authority_epoch,
                    payload_sha256=content_sha256(review_payload),
                )
                authority_snapshot = (
                    self._store.get_authority_snapshot(case.case_id)
                )
                deferred_effect_outbox = OutboxMessage(
                    outbox_message_id=review_job_id,
                    case_id=case.case_id,
                    source_event_cursor=review_event.cursor,
                    action_id=action.action_id,
                    job_kind=AsyncJobKind.REVIEWER,
                    operation=review_operation,
                    expected_head_version=case.head_version,
                    expected_authority_epoch=current.authority_epoch,
                    authority_snapshot=authority_snapshot,
                    authority_snapshot_sha256=(
                        authority_snapshot.content_sha256
                    ),
                    idempotency_key=review_operation.idempotency_key,
                    destination="provisional-answer-reviewer-provider",
                    contract_ref=ANSWER_REVIEW_JOB_CONTRACT_REF,
                    payload=review_payload,
                    payload_sha256=content_sha256(review_payload),
                    created_at=now,
                )
                outcome_cursor = review_event.cursor
                phase = ControllerPhase.WAITING_FOR_REVIEW
                pending_job_ids = (review_job_id,)
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
                operation=action.operation,
            )
            outcome_cursor = self._last_cursor(case.case_id)
            phase = (
                ControllerPhase.STOPPED
                if lifecycle is CaseLifecycle.STOPPED
                else ControllerPhase.COMPLETED
            )
        elif action.kind in _EVIDENCE_ACTIONS:
            evidence_outbox = self._resolve_evidence_dispatch(
                action,
                now=now,
            )
            evidence_disposition = self._store.get_job_disposition(
                evidence_outbox.outbox_message_id
            )
            if evidence_disposition is None:
                phase = ControllerPhase.WAITING_FOR_EFFECT
                pending_job_ids = (evidence_outbox.outbox_message_id,)
            elif evidence_disposition.disposition is JobDisposition.COMPLETED:
                receipt = (
                    self._store.find_capability_result_receipt_by_outbox(
                        evidence_outbox.outbox_message_id
                    )
                )
                admissions = (
                    ()
                    if receipt is None
                    else tuple(
                        item
                        for item in self._store.list_evidence_admissions(
                            case_id=action.case_id
                        )
                        if item.capability_result_receipt_id
                        == receipt.capability_result_receipt_id
                    )
                )
                if receipt is None or not admissions:
                    raise ControllerConflict(
                        "completed evidence dispatch lacks its canonical "
                        "receipt and admission"
                    )
                phase = ControllerPhase.READY_FOR_AGENT
                action_result_code = "canonical_evidence_reused"
            else:
                phase = ControllerPhase.READY_FOR_AGENT
                action_result_code = (
                    "evidence_dispatch_terminal_requires_plan_revision"
                )
                next_consecutive_rejections = (
                    current.consecutive_rejections + 1
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
                causal_operation=action.operation,
                now=now,
            )
            outbox = replace(outbox, source_event_cursor=event.cursor)
            deferred_effect_outbox = outbox
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
                    ControllerPhase.WAITING_FOR_MEASUREMENT_REVIEW,
                    ControllerPhase.WAITING_FOR_REVIEW,
                }
                else None
            ),
            pending_job_ids=pending_job_ids,
            pending_decision_request_id=pending_decision_id,
            consecutive_rejections=next_consecutive_rejections,
            now=now,
        )
        self._record_receipt(
            action=action,
            event_cursor=outcome_cursor,
            state=next_state,
            result_code=action_result_code,
            now=now,
        )
        if deferred_effect_outbox is not None:
            self._store.enqueue_outbox(deferred_effect_outbox)
        return next_state, outcome_cursor

    def _resolve_evidence_dispatch(
        self,
        action: ActionEnvelope,
        *,
        now: datetime,
    ) -> OutboxMessage:
        payload = action.payload
        if not isinstance(
            payload,
            CallCapabilityPayload | RunSensitivityPayload,
        ):
            raise ControllerConflict(
                "evidence action requires a typed capability payload"
            )
        if isinstance(payload, RunSensitivityPayload):
            raise ControllerConflict(
                "sensitivity execution requires a sealed sensitivity "
                "identity in the obligation dispatch contract"
            )
        authority = self._store.get_authority_snapshot(action.case_id)
        plan_revision_id = authority.accepted_plan_revision_id
        frame_revision_id = authority.accepted_frame_revision_id
        if plan_revision_id is None or frame_revision_id is None:
            raise ControllerConflict(
                "evidence dispatch requires accepted Frame and Plan"
            )
        adoption = self._store.get_plan_adoption(plan_revision_id)
        schedule_id = build_obligation_schedule_id(
            case_id=action.case_id,
            correlation_id=action.operation.correlation_id,
            frame_revision_id=frame_revision_id,
            plan_revision_id=plan_revision_id,
            plan_adoption_id=adoption.plan_adoption_id,
            plan_adoption_content_sha256=adoption.content_sha256,
            authority=authority,
        )
        try:
            schedule = self._store.get_obligation_schedule(schedule_id)
        except AuthorityNotFound:
            schedule = self._obligation_coordinator.create_schedule(
                case_id=action.case_id,
                causation_id=action.operation.operation_id,
                created_at=now,
            )
        else:
            self._obligation_coordinator.resume(
                schedule_id=schedule.schedule_id,
                resumed_at=now,
            )
        matches = tuple(
            record
            for record in self._store.list_obligation_dispatches(
                schedule.schedule_id
            )
            if record.dispatch.task_id == payload.task_id
            and record.dispatch.query_binding_id
            == payload.query_binding_id
        )
        if len(matches) != 1:
            raise ControllerConflict(
                "evidence action must resolve to one runnable obligation "
                "dispatch"
            )
        message = self._store.get_outbox_message(
            matches[0].outbox_message_id
        )
        if message.contract_ref != OBLIGATION_JOB_CONTRACT_REF:
            raise ControllerConflict(
                "evidence action resolved outside the obligation runtime"
            )
        return message

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
        job_lease,
    ) -> ControllerState:
        completed_at = self._now()
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                self._store.assert_job_lease(
                    job_lease,
                    checked_at=completed_at,
                )
                current = self.resume(snapshot.case_id)
                evidence_success = (
                    status is EffectAttemptStatus.SUCCEEDED
                    and message.contract_ref == OBLIGATION_JOB_CONTRACT_REF
                )
                if not evidence_success:
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
                if evidence_success:
                    assert result is not None
                    envelope = _decode_evidence_effect_result(result)
                    if (
                        envelope.outbox_message_id
                        != message.outbox_message_id
                    ):
                        raise ControllerConflict(
                            "capability result envelope changes the sealed "
                            "obligation outbox"
                        )
                    evidence_runtime = EvidenceRuntime(
                        store=self._store,
                        owner_id=self._owner_id,
                        profile=envelope.evidence_record.profile,
                        lease_duration=self._lease_duration,
                        obligation_coordinator=(
                            self._obligation_coordinator
                        ),
                    )
                    evidence_runtime.land_result(
                        envelope=envelope,
                        job_lease=job_lease,
                        received_at=completed_at,
                    )
                    if self._job_is_stale(message):
                        return current
                    if current.content_sha256 != snapshot.content_sha256:
                        return current
                    return self._checkpoint(
                        run_id=current.run_id,
                        case_id=current.case_id,
                        phase=(
                            ControllerPhase.WAITING_FOR_EVIDENCE_ADMISSION
                        ),
                        step_number=current.step_number,
                        latest_user_message=current.latest_user_message,
                        pending_action_id=current.pending_action_id,
                        pending_job_ids=current.pending_job_ids,
                        pending_decision_request_id=None,
                        consecutive_rejections=(
                            current.consecutive_rejections
                        ),
                        now=completed_at,
                    )
                if self._job_is_stale(message):
                    return self._supersede_job_locked(
                        current,
                        message,
                        now=completed_at,
                        job_lease=job_lease,
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
                    causal_operation=message.operation,
                    now=completed_at,
                )
                if status is EffectAttemptStatus.SUCCEEDED:
                    assert result is not None
                    self._record_job_disposition(
                        message=message,
                        disposition=JobDisposition.COMPLETED,
                        result_sha256=result.content_sha256,
                        reason_code="effect_result_committed",
                        now=completed_at,
                        job_lease=job_lease,
                    )
                elif status is EffectAttemptStatus.TERMINAL_FAILURE:
                    self._record_job_disposition(
                        message=message,
                        disposition=JobDisposition.TERMINAL_FAILURE,
                        result_sha256=None,
                        reason_code=error_code or "terminal_effect_failure",
                        now=completed_at,
                        job_lease=job_lease,
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

    def _admit_pending_evidence(
        self,
        snapshot: ControllerState,
        *,
        outbox_message_id: str,
    ) -> ControllerState:
        admitted_at = self._now()
        receipt = self._store.find_capability_result_receipt_by_outbox(
            outbox_message_id
        )
        if receipt is None:
            raise ControllerConflict(
                "evidence admission is waiting for a durable T1 receipt"
            )
        envelope = self._store.get_capability_result_envelope(
            receipt.capability_result_envelope_id
        )
        runtime = EvidenceRuntime(
            store=self._store,
            owner_id=self._owner_id,
            profile=envelope.evidence_record.profile,
            lease_duration=self._lease_duration,
            obligation_coordinator=self._obligation_coordinator,
        )
        runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=admitted_at,
        )
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                remaining_job_ids = tuple(
                    item
                    for item in current.pending_job_ids
                    if item != outbox_message_id
                )
                phase = (
                    ControllerPhase.WAITING_FOR_EVIDENCE_ADMISSION
                    if remaining_job_ids
                    else ControllerPhase.READY_FOR_AGENT
                )
                return self._checkpoint(
                    run_id=current.run_id,
                    case_id=current.case_id,
                    phase=phase,
                    step_number=current.step_number,
                    latest_user_message=current.latest_user_message,
                    pending_action_id=(
                        current.pending_action_id
                        if remaining_job_ids
                        else None
                    ),
                    pending_job_ids=remaining_job_ids,
                    pending_decision_request_id=None,
                    consecutive_rejections=current.consecutive_rejections,
                    now=admitted_at,
                )
        finally:
            self._store.release_lease(lease)

    def _recover_landed_evidence(
        self,
        *,
        outbox_message_id: str,
        admitted_at: datetime,
    ) -> None:
        receipt = self._store.find_capability_result_receipt_by_outbox(
            outbox_message_id
        )
        if receipt is None:
            raise ControllerConflict(
                "typed evidence effect completed without a T1 receipt"
            )
        envelope = self._store.get_capability_result_envelope(
            receipt.capability_result_envelope_id
        )
        EvidenceRuntime(
            store=self._store,
            owner_id=self._owner_id,
            profile=envelope.evidence_record.profile,
            lease_duration=self._lease_duration,
            obligation_coordinator=self._obligation_coordinator,
        ).admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=admitted_at,
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
        resolutions = (
            ()
            if frame is None
            else self._store.list_measurement_resolutions(
                frame.frame_revision_id
            )
        )
        obligations = (
            ()
            if frame is None
            else self._store.list_evidence_obligations(
                frame.frame_revision_id
            )
        )
        plan_adoption = (
            None
            if plan is None
            else self._store.get_plan_adoption(plan.plan_revision_id)
        )
        query_bindings = (
            ()
            if plan is None
            else self._store.list_query_bindings(
                plan.plan_revision_id
            )
        )
        answer = (
            None
            if case.accepted_answer_version_id is None
            else self._store.get_answer(
                case.accepted_answer_version_id
            )
        )
        binding = (
            None
            if question is None or not question.explicit_constraint_refs
            else self._store.get_message_impact_binding(
                question.explicit_constraint_refs[-1]
            )
        )
        frame_candidate = self._store.get_active_frame_candidate(case_id)
        frame_review = (
            None
            if frame_candidate is None
            else self._store.get_frame_review_for_candidate(
                frame_candidate.frame_candidate_id
            )
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
        admissions = self._store.list_evidence_admissions(
            case_id=case_id
        )
        admitted_evidence_ids = {
            item.evidence_record_id
            for item in admissions
            if item.status is EvidenceAdmissionStatus.ACCEPTED
        }
        evidence = tuple(
            item
            for item in self._store.list_evidence(case_id)
            if item.evidence_record_id in admitted_evidence_ids
            and self._store.latest_evidence_validity(
                item.evidence_record_id
            ).status
            is EvidenceValidityStatus.ADMITTED_VALID
        )[-MAX_CONTEXT_EVIDENCE:]
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
            accepted_message_binding=binding,
            active_frame_candidate=frame_candidate,
            latest_frame_review=frame_review,
            available_measurement_resolutions=resolutions,
            available_evidence_obligations=obligations,
            accepted_plan_adoption=plan_adoption,
            accepted_query_bindings=query_bindings,
            recent_events=business_events,
            evidence_index=tuple(
                ContextEvidenceItem(
                    evidence_record_id=record.evidence_record_id,
                    evidence_type=record.evidence_type_ref,
                    strength=record.evidence_strength.value,
                    business_summary=record.business_summary,
                    limitation_count=len(record.limitation_refs),
                    frame_revision_id=record.frame_revision_id,
                    plan_revision_id=record.plan_revision_id,
                    task_id=record.task_id,
                    snapshot_release_ref=(
                        record.data_context.snapshot_release_ref
                    ),
                )
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
            causal_operation=action.operation,
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
        causal_operation: OperationIdentity,
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
                causation_id=causal_operation.operation_id,
                correlation_id=causal_operation.correlation_id,
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
        authority_snapshot = self._store.get_authority_snapshot(
            action.case_id
        )
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
            authority_snapshot=authority_snapshot,
            authority_snapshot_sha256=(
                authority_snapshot.content_sha256
            ),
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
                authority_snapshot = (
                    self._store.get_authority_snapshot(current.case_id)
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
                        authority_snapshot=authority_snapshot,
                        authority_snapshot_sha256=(
                            authority_snapshot.content_sha256
                        ),
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

    def _record_logical_model_job(
        self,
        *,
        message: OutboxMessage,
        provider,
        role: str,
        typed_request_contract_ref: str,
        output_contract_ref: str,
        request,
    ) -> LogicalModelJob:
        describe = getattr(provider, "describe_invocation", None)
        if describe is None:
            raise ControllerConflict(
                "model provider lacks exact invocation authority"
            )
        prepared = describe(
            logical_model_job_id=message.outbox_message_id,
            logical_job_kind=role,
            request=request,
            typed_request_contract_ref=typed_request_contract_ref,
            output_contract_ref=output_contract_ref,
            created_at=message.created_at,
        )
        configuration = prepared.configuration_identity
        artifact = prepared.request_artifact
        _validate_provider_configuration_source(
            provider=provider,
            configuration=configuration,
        )
        _validate_prepared_invocation_authority(
            role=role,
            request=request,
            typed_request_contract_ref=typed_request_contract_ref,
            output_contract_ref=output_contract_ref,
            configuration=configuration,
            artifact=artifact,
        )
        record = LogicalModelJob(
            logical_model_job_id=message.outbox_message_id,
            case_id=message.case_id,
            job_id=message.outbox_message_id,
            operation_id=message.operation.operation_id,
            role=role,
            provider_ref=configuration.provider_ref,
            model_ref=configuration.model_ref,
            prompt_contract_ref=artifact.prompt_bundle_ref,
            input_sha256=content_sha256(request),
            configuration_identity=configuration,
            configuration_sha256=(
                configuration.configuration_sha256
            ),
            model_request_artifact=artifact,
            model_request_artifact_sha256=artifact.content_sha256,
            authority_snapshot_sha256=(
                message.authority_snapshot_sha256
            ),
            created_at=message.created_at,
        )
        return self._store.record_logical_model_job(record)

    def _install_provider_attempt_observer(
        self,
        *,
        provider,
        model_job: LogicalModelJob,
        result_kind: str,
        result_contract_ref: str,
    ) -> bool:
        install = getattr(provider, "install_attempt_observer", None)
        if install is not None:
            install(
                _DurableProviderAttemptObserver(
                    store=self._store,
                    model_job=model_job,
                    result_kind=result_kind,
                    result_contract_ref=result_contract_ref,
                )
            )
            return True
        self._store.record_provider_attempt_request(
            ProviderAttemptRequest(
                provider_attempt_id=_stable_id(
                    "provider-attempt",
                    model_job.logical_model_job_id,
                    "1",
                ),
                logical_model_job_id=model_job.logical_model_job_id,
                attempt_number=1,
                prior_provider_attempt_id=None,
                provider_idempotency_key=_stable_id(
                    "provider-attempt",
                    model_job.logical_model_job_id,
                    "1",
                ),
                request_sha256=(
                    model_job.model_request_artifact.provider_request_sha256
                ),
                model_request_artifact_sha256=(
                    model_job.model_request_artifact_sha256
                ),
                configuration_sha256=model_job.configuration_sha256,
                requested_at=model_job.created_at,
            )
        )
        return False

    @staticmethod
    def _clear_provider_attempt_observer(
        provider,
        installed: bool,
    ) -> None:
        if not installed:
            return
        clear = getattr(provider, "clear_attempt_observer", None)
        if clear is None:
            raise ControllerConflict(
                "provider cannot clear its durable attempt observer"
            )
        clear()

    def _persist_successful_model_result(
        self,
        *,
        model_job: LogicalModelJob,
        provider_attempts: tuple[
            tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
            ...,
        ],
        result,
        result_kind: str,
        result_contract_ref: str,
        recorded_at: datetime,
    ) -> DurableModelResult:
        existing = self._store.get_durable_model_result(
            model_job.logical_model_job_id
        )
        if existing is not None:
            if (
                existing.result_kind != result_kind
                or existing.result_contract_ref != result_contract_ref
                or existing.output_sha256 != content_sha256(result)
            ):
                raise ControllerConflict(
                    "durable model result changed before authority admission"
                )
            return existing
        successful = tuple(
            (request, receipt)
            for request, receipt in provider_attempts
            if receipt.disposition
            is ProviderAttemptDisposition.SUCCEEDED
        )
        if len(successful) != 1:
            raise ControllerConflict(
                "typed provider result requires one successful attempt"
            )
        result_payload = to_jsonable(result)
        if not isinstance(result_payload, dict):
            raise ControllerConflict(
                "typed provider result must encode as an object"
            )
        request, receipt = successful[0]
        for prior_request, prior_receipt in provider_attempts:
            self._store.record_provider_attempt_request(prior_request)
            if (
                prior_receipt.disposition
                is not ProviderAttemptDisposition.SUCCEEDED
            ):
                self._store.record_provider_attempt_receipt(prior_receipt)
        durable_result = DurableModelResult(
            durable_model_result_id=_stable_id(
                "durable-model-result",
                model_job.logical_model_job_id,
            ),
            logical_model_job_id=model_job.logical_model_job_id,
            provider_attempt_id=request.provider_attempt_id,
            provider_attempt_receipt_id=(
                receipt.provider_attempt_receipt_id
            ),
            result_kind=result_kind,
            result_contract_ref=result_contract_ref,
            result_payload=result_payload,
            output_sha256=content_sha256(result_payload),
            model_request_artifact_sha256=(
                model_job.model_request_artifact_sha256
            ),
            configuration_sha256=model_job.configuration_sha256,
            recorded_at=recorded_at,
        )
        return self._store.commit_provider_attempt_success(
            receipt=receipt,
            result=durable_result,
        )

    def _load_durable_model_result(
        self,
        *,
        model_job: LogicalModelJob,
        result_kind: str,
        result_contract_ref: str,
    ):
        record = self._store.get_durable_model_result(
            model_job.logical_model_job_id
        )
        if record is None:
            return None
        if (
            record.result_kind != result_kind
            or record.result_contract_ref != result_contract_ref
        ):
            raise ControllerConflict(
                "durable model result contract does not match its job"
            )
        payload = to_jsonable(record.result_payload)
        if not isinstance(payload, dict):
            raise ControllerConflict(
                "durable model result payload is not an object"
            )
        if result_kind == "primary_agent":
            return decode_agent_action_proposal(payload)
        if result_kind == "message_binding":
            return decode_typed_dataclass(
                MessageImpactProposal,
                payload,
            )
        if result_kind == "measurement_reviewer":
            return decode_typed_dataclass(
                FrameReviewProposal,
                payload,
            )
        raise ControllerConflict("unknown durable model result kind")

    def _persisted_provider_attempt_records(
        self,
        logical_model_job_id: str,
    ) -> tuple[
        tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
        ...,
    ]:
        return tuple(
            (
                self._store.get_provider_attempt_request(
                    receipt.provider_attempt_id
                ),
                receipt,
            )
            for receipt in self._store.list_provider_attempt_receipts(
                logical_model_job_id
            )
        )

    def _provider_attempt_records(
        self,
        *,
        model_job: LogicalModelJob,
        provider,
        output,
        completed_at: datetime,
    ) -> tuple[
        tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
        ...,
    ]:
        if getattr(
            provider,
            "supports_durable_attempt_observer",
            False,
        ):
            persisted = self._persisted_provider_attempt_records(
                model_job.logical_model_job_id
            )
            if persisted:
                return persisted
        take_trace = getattr(
            provider,
            "take_last_attempt_trace",
            None,
        )
        traces = () if take_trace is None else tuple(take_trace())
        output_sha = content_sha256(output)
        if not traces:
            traces = (
                _LocalProviderTrace(
                    disposition="succeeded",
                    provider_response_id=(
                        "local-result:{}".format(output_sha[:24])
                    ),
                    output_sha256=output_sha,
                    finish_reason="typed_result",
                    usage_payload={},
                    completed_at=completed_at,
                ),
            )
        records = []
        prior_attempt_id = None
        for attempt_number, trace in enumerate(traces, start=1):
            disposition = ProviderAttemptDisposition(
                trace.disposition
            )
            if (
                disposition is ProviderAttemptDisposition.SUCCEEDED
                and trace.output_sha256 != output_sha
            ):
                raise ControllerConflict(
                    "provider trace output does not match typed result"
                )
            attempt_id = _stable_id(
                "provider-attempt",
                model_job.logical_model_job_id,
                str(attempt_number),
            )
            request = ProviderAttemptRequest(
                provider_attempt_id=attempt_id,
                logical_model_job_id=(
                    model_job.logical_model_job_id
                ),
                attempt_number=attempt_number,
                prior_provider_attempt_id=prior_attempt_id,
                provider_idempotency_key=attempt_id,
                request_sha256=(
                    model_job.model_request_artifact.provider_request_sha256
                ),
                model_request_artifact_sha256=(
                    model_job.model_request_artifact_sha256
                ),
                configuration_sha256=model_job.configuration_sha256,
                requested_at=model_job.created_at,
            )
            receipt = ProviderAttemptReceipt(
                provider_attempt_receipt_id=_stable_id(
                    "provider-attempt-receipt",
                    attempt_id,
                ),
                provider_attempt_id=attempt_id,
                logical_model_job_id=(
                    model_job.logical_model_job_id
                ),
                disposition=disposition,
                provider_response_id=trace.provider_response_id,
                output_sha256=trace.output_sha256,
                finish_reason=trace.finish_reason,
                usage_payload=dict(trace.usage_payload),
                completed_at=trace.completed_at,
            )
            records.append((request, receipt))
            prior_attempt_id = attempt_id
        return tuple(records)

    def _provider_failure_attempt_records(
        self,
        *,
        model_job: LogicalModelJob,
        provider,
        error: ProviderError,
        completed_at: datetime,
    ) -> tuple[
        tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
        ...,
    ]:
        if getattr(
            provider,
            "supports_durable_attempt_observer",
            False,
        ):
            persisted = self._persisted_provider_attempt_records(
                model_job.logical_model_job_id
            )
            if persisted:
                return persisted
        take_trace = getattr(
            provider,
            "take_last_attempt_trace",
            None,
        )
        traces = () if take_trace is None else tuple(take_trace())
        if not traces:
            traces = (
                _LocalProviderTrace(
                    disposition=(
                        "retryable_failure"
                        if isinstance(error, ProviderTransientError)
                        else "terminal_failure"
                    ),
                    provider_response_id=None,
                    output_sha256=None,
                    finish_reason=None,
                    usage_payload={},
                    completed_at=completed_at,
                ),
            )
        records = []
        prior_attempt_id = None
        for attempt_number, trace in enumerate(traces, start=1):
            disposition = ProviderAttemptDisposition(
                trace.disposition
            )
            if disposition is ProviderAttemptDisposition.SUCCEEDED:
                raise ControllerConflict(
                    "failed provider call cannot contain a successful attempt"
                )
            attempt_id = _stable_id(
                "provider-attempt",
                model_job.logical_model_job_id,
                str(attempt_number),
            )
            request = ProviderAttemptRequest(
                provider_attempt_id=attempt_id,
                logical_model_job_id=model_job.logical_model_job_id,
                attempt_number=attempt_number,
                prior_provider_attempt_id=prior_attempt_id,
                provider_idempotency_key=attempt_id,
                request_sha256=(
                    model_job.model_request_artifact.provider_request_sha256
                ),
                model_request_artifact_sha256=(
                    model_job.model_request_artifact_sha256
                ),
                configuration_sha256=model_job.configuration_sha256,
                requested_at=model_job.created_at,
            )
            receipt = ProviderAttemptReceipt(
                provider_attempt_receipt_id=_stable_id(
                    "provider-attempt-receipt",
                    attempt_id,
                ),
                provider_attempt_id=attempt_id,
                logical_model_job_id=model_job.logical_model_job_id,
                disposition=disposition,
                provider_response_id=trace.provider_response_id,
                output_sha256=trace.output_sha256,
                finish_reason=trace.finish_reason,
                usage_payload=dict(trace.usage_payload),
                completed_at=trace.completed_at,
            )
            records.append((request, receipt))
            prior_attempt_id = attempt_id
        return tuple(records)

    def _persist_provider_attempt_records(
        self,
        *,
        model_job: LogicalModelJob,
        provider_attempts: tuple[
            tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
            ...,
        ],
    ) -> None:
        persisted = self._store.get_logical_model_job(
            model_job.logical_model_job_id
        )
        if persisted != model_job:
            raise ControllerConflict(
                "logical model job changed before result admission"
            )
        for request, receipt in provider_attempts:
            self._store.record_provider_attempt_request(request)
            if receipt.disposition is ProviderAttemptDisposition.SUCCEEDED:
                result = self._store.get_durable_model_result(
                    model_job.logical_model_job_id
                )
                if (
                    result is None
                    or result.provider_attempt_id
                    != receipt.provider_attempt_id
                    or result.provider_attempt_receipt_id
                    != receipt.provider_attempt_receipt_id
                    or result.output_sha256 != receipt.output_sha256
                ):
                    raise ControllerConflict(
                        "successful provider attempt lacks atomic typed result"
                    )
                continue
            self._store.record_provider_attempt_receipt(receipt)

    def _commit_provider_failure(
        self,
        *,
        snapshot: ControllerState,
        message: OutboxMessage,
        job_lease,
        model_job: LogicalModelJob,
        provider_attempts: tuple[
            tuple[ProviderAttemptRequest, ProviderAttemptReceipt],
            ...,
        ],
        error: ProviderError,
    ) -> ControllerState:
        now = self._now()
        reason_code = (
            "provider_attempts_exhausted"
            if isinstance(error, ProviderTransientError)
            else "permanent_provider_failure"
        )
        controller_lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                self._store.assert_job_lease(
                    job_lease,
                    checked_at=now,
                )
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                if self._job_is_stale(message):
                    return self._supersede_job_locked(
                        current,
                        message,
                        now=now,
                        job_lease=job_lease,
                    )
                self._persist_provider_attempt_records(
                    model_job=model_job,
                    provider_attempts=provider_attempts,
                )
                event_payload = {
                    "outbox_message_id": message.outbox_message_id,
                    "job_kind": message.job_kind.value,
                    "logical_model_job_id": (
                        model_job.logical_model_job_id
                    ),
                    "reason_code": reason_code,
                    "attempt_count": len(provider_attempts),
                }
                self._append_event(
                    case_id=message.case_id,
                    event_id=_stable_id(
                        "event",
                        message.outbox_message_id,
                        "terminal-failure",
                    ),
                    event_type=JournalEventType.JOB_TERMINALLY_FAILED,
                    action_id=message.action_id,
                    authority_ref=message.outbox_message_id,
                    payload=event_payload,
                    customer_projection={
                        "state": "blocked",
                        "reason": "analysis_provider_unavailable",
                    },
                    causal_operation=message.operation,
                    now=now,
                )
                self._record_job_disposition(
                    message=message,
                    disposition=JobDisposition.TERMINAL_FAILURE,
                    result_sha256=None,
                    reason_code=reason_code,
                    now=now,
                    job_lease=job_lease,
                )
                return self._checkpoint(
                    run_id=current.run_id,
                    case_id=current.case_id,
                    phase=ControllerPhase.BLOCKED,
                    step_number=current.step_number,
                    latest_user_message=current.latest_user_message,
                    pending_action_id=None,
                    pending_job_ids=(),
                    pending_decision_request_id=None,
                    consecutive_rejections=current.consecutive_rejections,
                    now=now,
                )
        finally:
            self._store.release_lease(controller_lease)

    def _job_is_stale(self, message: OutboxMessage) -> bool:
        current = self._store.get_authority_snapshot(message.case_id)
        if message.contract_ref == OBLIGATION_JOB_CONTRACT_REF:
            return not same_obligation_business_authority(
                message.authority_snapshot,
                current,
            )
        if message.job_kind in {
            AsyncJobKind.SEMANTIC_INSPECTION,
            AsyncJobKind.DATA_PROBE,
            AsyncJobKind.CAPABILITY,
            AsyncJobKind.SENSITIVITY,
        }:
            return not same_business_authority(
                message.authority_snapshot,
                current,
            )
        return current != message.authority_snapshot

    def _supersede_job(
        self,
        snapshot: ControllerState,
        message: OutboxMessage,
        *,
        job_lease,
    ) -> ControllerState:
        now = self._now()
        lease = self._acquire(snapshot)
        try:
            with self._store.atomic():
                self._store.assert_job_lease(
                    job_lease,
                    checked_at=now,
                )
                current = self.resume(snapshot.case_id)
                _require_same_checkpoint(snapshot, current)
                return self._supersede_job_locked(
                    current,
                    message,
                    now=now,
                    job_lease=job_lease,
                )
        finally:
            self._store.release_lease(lease)

    def _supersede_job_locked(
        self,
        current: ControllerState,
        message: OutboxMessage,
        *,
        now: datetime,
        job_lease=None,
    ) -> ControllerState:
        self._append_job_superseded_event(message, now=now)
        self._record_job_disposition(
            message=message,
            disposition=JobDisposition.SUPERSEDED,
            result_sha256=None,
            reason_code="authority_fence_changed",
            now=now,
            job_lease=job_lease,
        )
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

    def _record_job_disposition(
        self,
        *,
        message: OutboxMessage,
        disposition: JobDisposition,
        result_sha256: str | None,
        reason_code: str,
        now: datetime,
        job_lease=None,
        observed_authority_epoch: int | None = None,
    ) -> JobDispositionRecord:
        observed_epoch = (
            self._store.get_mailbox_head(
                message.case_id
            ).authority_epoch
            if observed_authority_epoch is None
            else observed_authority_epoch
        )
        record = JobDispositionRecord(
            job_disposition_record_id=_stable_id(
                "job-disposition",
                message.outbox_message_id,
                disposition.value,
            ),
            outbox_message_id=message.outbox_message_id,
            case_id=message.case_id,
            job_kind=message.job_kind,
            disposition=disposition,
            owner_id=(
                "authority-controller:{}".format(self._owner_id)
                if job_lease is None
                else job_lease.owner_id
            ),
            fencing_token=(
                None
                if job_lease is None
                else job_lease.fencing_token
            ),
            expected_authority_epoch=(
                message.expected_authority_epoch
            ),
            observed_authority_epoch=observed_epoch,
            result_sha256=result_sha256,
            reason_code=reason_code,
            operation=message.operation,
            completed_at=now,
        )
        return self._store.record_job_disposition(record)

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
        pending_messages = self._store.list_mailbox_messages(
            current.case_id,
            after_sequence=current.mailbox_cursor,
        )
        if not pending_messages:
            return current
        sequence_by_message_id = {
            item.message_id: item.sequence
            for item in pending_messages
        }
        binding_jobs = tuple(
            item
            for item in self._store.list_pending_outbox_messages(
                case_id=current.case_id
            )
            if item.job_kind is AsyncJobKind.MESSAGE_BINDING
            and str(item.payload.get("message_id", ""))
            in sequence_by_message_id
        )
        if not binding_jobs:
            raise ControllerConflict(
                "mailbox advance has no durable message-binding job"
            )
        selected_binding = min(
            binding_jobs,
            key=lambda item: sequence_by_message_id[
                str(item.payload["message_id"])
            ],
        )
        superseded_refs = {
            event.authority_ref
            for event in self._store.list_events(current.case_id)
            if event.event_type is JournalEventType.JOB_SUPERSEDED
        }
        supersede_ids = set(current.pending_job_ids)
        supersede_ids.discard(selected_binding.outbox_message_id)
        for job_id in sorted(supersede_ids):
            if self._store.get_job_disposition(job_id) is not None:
                continue
            message = self._store.get_outbox_message(job_id)
            if job_id not in superseded_refs:
                self._append_job_superseded_event(
                    message,
                    now=now,
                )
            self._record_job_disposition(
                message=message,
                disposition=JobDisposition.SUPERSEDED,
                result_sha256=None,
                reason_code="mailbox_authority_advanced",
                now=now,
            )
        messages = self._store.list_mailbox_messages(current.case_id)
        context_user_messages = tuple(
            ContextUserMessageItem.from_message(message)
            for message in messages
        )
        latest_user_message = context_user_messages[-1].content
        return self._checkpoint(
            run_id=current.run_id,
            case_id=current.case_id,
            phase=ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
            step_number=current.step_number,
            latest_user_message=latest_user_message,
            pending_action_id=None,
            pending_job_ids=(selected_binding.outbox_message_id,),
            pending_decision_request_id=None,
            consecutive_rejections=0,
            authority_epoch=head.authority_epoch,
            mailbox_cursor=current.mailbox_cursor,
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
        return self._store.latest_answer(case_id)

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

    def _release_job_lease(self, lease) -> None:
        try:
            self._store.release_job_lease(lease)
        except LeaseFenceLost:
            # A lost worker is already fenced at the authority commit boundary.
            # Releasing its obsolete token has no remaining state transition.
            return

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("controller clock must return timezone-aware time")
        return value


def _validate_prepared_invocation_authority(
    *,
    role: str,
    request,
    typed_request_contract_ref: str,
    output_contract_ref: str,
    configuration,
    artifact,
) -> None:
    if role == "primary_agent" and isinstance(request, PrimaryAgentRequest):
        expected_execution_role = (
            ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        )
        expected_view_kind = ModelInputViewKind.AGENT_WORLD_VIEW
        expected_view_ref = request.context_packet.packet_id
        expected_view_sha256 = request.context_packet.content_sha256
        expected_tool_choice = "required"
    elif role == "message_binding" and isinstance(
        request,
        MessageBindingRequest,
    ):
        expected_execution_role = (
            ModelExecutionRole.PRIMARY_BUSINESS_ANALYSIS_AGENT
        )
        expected_view_kind = ModelInputViewKind.MESSAGE_BINDING_VIEW
        expected_view_ref = request.message_id
        expected_view_sha256 = content_sha256(request)
        expected_tool_choice = {
            "type": "function",
            "function": {"name": "submit_message_impact"},
        }
    elif role == "measurement_reviewer" and isinstance(
        request,
        FrameReviewRequest,
    ):
        expected_execution_role = ModelExecutionRole.RUNTIME_REVIEWER
        expected_view_kind = ModelInputViewKind.MEASUREMENT_REVIEW_VIEW
        expected_view_ref = request.frame_candidate.frame_candidate_id
        expected_view_sha256 = content_sha256(request)
        expected_tool_choice = {
            "type": "function",
            "function": {"name": "submit_measurement_review"},
        }
    else:
        raise ControllerConflict(
            "logical model job kind does not match its typed request"
        )
    if (
        configuration.execution_role is not expected_execution_role
        or artifact.execution_role is not expected_execution_role
        or artifact.logical_job_kind != role
        or artifact.input_view_kind is not expected_view_kind
        or artifact.input_view_ref != expected_view_ref
        or artifact.input_view_sha256 != expected_view_sha256
        or artifact.typed_request_contract_ref
        != typed_request_contract_ref
        or artifact.typed_request_sha256 != content_sha256(request)
        or artifact.output_contract_ref != output_contract_ref
    ):
        raise ControllerConflict(
            "prepared model invocation crosses its typed authority"
        )
    body = to_jsonable(artifact.provider_request_body)
    if not isinstance(body, dict):
        raise ControllerConflict("provider request artifact is not an object")
    if configuration.protocol_ref == "python-typed-test-double.v1":
        if body != {
            "protocol": "python-typed-test-double.v1",
            "typed_request": to_jsonable(request),
        }:
            raise ControllerConflict(
                "test provider request differs from its typed input"
            )
        return
    if configuration.protocol_ref != "openai-compatible-chat-completions.v1":
        raise ControllerConflict("provider protocol lacks an exact verifier")
    expected_stable_parameter_keys = {
        "temperature",
        "top_p",
        "tool_choice_policy",
        "parallel_tool_calls",
    }
    if "seed" in configuration.stable_parameters:
        expected_stable_parameter_keys.add("seed")
    if (
        set(configuration.stable_parameters)
        != expected_stable_parameter_keys
        or configuration.stable_parameters["tool_choice_policy"]
        != "contract_selected"
    ):
        raise ControllerConflict(
            "provider configuration contains unbound stable parameters"
        )
    from waje_vnext.providers.chat_completions import (
        compile_trusted_chat_invocation,
    )

    trusted_material = compile_trusted_chat_invocation(
        logical_job_kind=role,
        request=request,
        configuration=configuration,
    )
    trusted_prompt_sha256 = content_sha256(
        {
            "messages": (
                {
                    "role": "system",
                    "content": trusted_material.system_instruction,
                },
            )
        }
    )
    trusted_tool_sha256 = content_sha256(trusted_material.tools)
    if (
        body != to_jsonable(trusted_material.payload)
        or artifact.input_view_kind
        is not trusted_material.input_view_kind
        or artifact.input_view_ref != trusted_material.input_view_ref
        or artifact.input_view_sha256
        != trusted_material.input_view_sha256
        or artifact.prompt_bundle_ref
        != trusted_material.prompt_bundle_ref
        or artifact.prompt_bundle_sha256 != trusted_prompt_sha256
        or artifact.tool_bundle_ref != trusted_material.tool_bundle_ref
        or artifact.tool_bundle_sha256 != trusted_tool_sha256
        or artifact.decoder_release_ref
        != trusted_material.decoder_release_ref
    ):
        raise ControllerConflict(
            "provider request differs from the trusted invocation contract"
        )
    allowed_body_fields = {
        "model",
        "thinking",
        "temperature",
        "top_p",
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
    }
    if "seed" in configuration.stable_parameters:
        allowed_body_fields.add("seed")
    if set(body) != allowed_body_fields:
        raise ControllerConflict(
            "provider request body contains unbound fields"
        )
    messages = body.get("messages")
    tools = body.get("tools")
    if (
        not isinstance(messages, list)
        or len(messages) != 2
        or messages[0].get("role") != "system"
        or messages[1].get("role") != "user"
        or not isinstance(tools, list)
    ):
        raise ControllerConflict(
            "provider request message or tool structure drifted"
        )
    try:
        rendered_typed_request = json.loads(messages[1]["content"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ControllerConflict(
            "provider user message is not the typed request"
        ) from error
    if rendered_typed_request != to_jsonable(request):
        raise ControllerConflict(
            "provider user message differs from its typed request"
        )
    expected_prompt_sha256 = content_sha256(
        {"messages": (messages[0],)}
    )
    expected_tool_sha256 = content_sha256(tools)
    expected_output_sha256 = content_sha256(
        {
            "output_contract_ref": output_contract_ref,
            "tool_bundle_sha256": expected_tool_sha256,
            "decoder_release_ref": artifact.decoder_release_ref,
            "decoder_release_sha256": artifact.decoder_release_sha256,
        }
    )
    if (
        artifact.prompt_bundle_sha256 != expected_prompt_sha256
        or artifact.tool_bundle_sha256 != expected_tool_sha256
        or artifact.output_contract_sha256 != expected_output_sha256
        or artifact.decoder_release_sha256
        != configuration.adapter_release_sha256
        or body["model"] != configuration.model_ref
        or body["thinking"] != {"type": configuration.thinking}
        or body["temperature"]
        != configuration.stable_parameters["temperature"]
        or body["top_p"] != configuration.stable_parameters["top_p"]
        or body["parallel_tool_calls"]
        != configuration.stable_parameters["parallel_tool_calls"]
        or body["tool_choice"] != expected_tool_choice
        or (
            "seed" in configuration.stable_parameters
            and body["seed"] != configuration.stable_parameters["seed"]
        )
    ):
        raise ControllerConflict(
            "provider request body differs from its invocation authority"
        )


def _validate_provider_configuration_source(
    *,
    provider,
    configuration,
) -> None:
    if configuration.protocol_ref == "python-typed-test-double.v1":
        return
    if configuration.protocol_ref != "openai-compatible-chat-completions.v1":
        raise ControllerConflict(
            "provider protocol lacks a sealed configuration verifier"
        )
    from waje_vnext.providers.chat_completions import (
        ChatCompletionsProvider,
    )

    if type(provider) is not ChatCompletionsProvider:
        raise ControllerConflict(
            "provider concrete adapter release is not registered"
        )
    sealed = ChatCompletionsProvider.configuration_identity(
        provider,
        configuration.execution_role,
    )
    if configuration != sealed:
        raise ControllerConflict(
            "provider configuration differs from sealed adapter settings"
        )


def _provider_configuration_ref(
    provider,
    execution_role: ModelExecutionRole,
) -> str | None:
    identity = _provider_configuration_identity(provider, execution_role)
    if identity is not None:
        return identity.configuration_sha256
    return getattr(provider, "configuration_ref", None)


def _provider_configuration_identity(
    provider,
    execution_role: ModelExecutionRole,
):
    identity_factory = getattr(
        provider,
        "configuration_identity",
        None,
    )
    if identity_factory is not None:
        return identity_factory(execution_role)
    return None


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
        return "capability"
    if isinstance(payload, RunSensitivityPayload):
        return "sensitivity"
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


def _decode_evidence_effect_result(
    result: EffectExecutionResult,
) -> CapabilityResultEnvelope:
    payload = result.payload.get("capability_result_envelope")
    if not isinstance(payload, Mapping):
        raise ControllerConflict(
            "evidence-producing effect must return a typed "
            "CapabilityResultEnvelope"
        )
    try:
        return decode_capability_result_envelope(payload)
    except (KeyError, TypeError, ValueError) as error:
        raise ControllerConflict(
            "evidence-producing effect returned an invalid typed envelope"
        ) from error


def _event_authority_refs(
    events,
    event_type: JournalEventType,
) -> tuple[str, ...]:
    return _ordered_unique(
        event.authority_ref
        for event in events
        if event.event_type is event_type
        and event.authority_ref is not None
    )


def _ordered_unique(values) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _decode_binding_object(raw: str, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ControllerConflict(
            "{} must contain valid JSON".format(label)
        ) from error
    if not isinstance(decoded, dict) or not decoded:
        raise ControllerConflict(
            "{} must decode to a non-empty object".format(label)
        )
    return decoded
