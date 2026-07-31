"""Durable obligation fan-out/fan-in coordinator."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    OperationIdentity,
)
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.measurement import (
    ObligationExecutionDisposition,
    ResolvedEvidenceObligation,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationCompletion,
    ObligationCompletionRecord,
    ObligationDependency,
    ObligationDispatchRecord,
    ObligationScheduleCheckpoint,
    ObligationScheduleRecord,
    ObligationTerminalStatus,
    admit_obligation_completion,
    build_obligation_dispatch,
    propagate_dependency_terminals,
    same_obligation_business_authority,
    select_runnable_obligations,
)
from waje_vnext.domain.runtime_amendment import (
    JobDisposition,
    JobDispositionRecord,
)
from waje_vnext.domain.runtime_state import OutboxMessage
from waje_vnext.storage.ports import AuthorityStore


OBLIGATION_JOB_CONTRACT_REF = (
    "waje-vnext://runtime/resolved-evidence-obligation-job.v1"
)


class DurableObligationCoordinator:
    def __init__(
        self,
        *,
        store: AuthorityStore,
        owner_id: str,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if not owner_id.strip():
            raise ValueError("owner_id must be non-empty")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._store = store
        self._owner_id = owner_id
        self._lease_duration = lease_duration

    def create_schedule(
        self,
        *,
        case_id: str,
        obligations: tuple[ResolvedEvidenceObligation, ...],
        dependencies: tuple[ObligationDependency, ...],
        causation_id: str,
        created_at: datetime,
    ) -> ObligationScheduleRecord:
        authority = self._store.get_authority_snapshot(case_id)
        checkpoint = self._store.latest_checkpoint(case_id)
        if checkpoint is None:
            raise ValueError(
                "obligation schedule requires an active durable run"
            )
        correlation_id = checkpoint.state_payload.get("run_id")
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError(
                "active checkpoint does not bind a durable run"
            )
        frame_revision_id = authority.accepted_frame_revision_id
        if frame_revision_id is None:
            raise ValueError(
                "obligation schedule requires an accepted Frame"
            )
        schedule_id = _stable_id(
            "obligation-schedule",
            case_id,
            frame_revision_id,
            content_sha256(obligations),
            content_sha256(dependencies),
            authority.content_sha256,
        )
        schedule = ObligationScheduleRecord(
            schedule_id=schedule_id,
            case_id=case_id,
            correlation_id=correlation_id,
            frame_revision_id=frame_revision_id,
            obligations=obligations,
            dependencies=dependencies,
            authority_snapshot=authority,
            authority_snapshot_sha256=authority.content_sha256,
            created_at=created_at,
        )
        payload = {
            "schedule_id": schedule_id,
            "frame_revision_id": frame_revision_id,
            "obligation_ids": tuple(
                item.obligation_id for item in obligations
            ),
            "schedule_sha256": schedule.content_sha256,
        }
        with self._store.atomic():
            current_checkpoint = self._store.latest_checkpoint(case_id)
            if (
                current_checkpoint is None
                or current_checkpoint.state_payload.get("run_id")
                != correlation_id
            ):
                raise ValueError(
                    "active durable run changed before schedule commit"
                )
            self._store.record_obligation_schedule(schedule)
            self._store.append_event(
                case_id=case_id,
                expected_next_cursor=(
                    len(self._store.list_events(case_id)) + 1
                ),
                event_id=_stable_id(
                    "event",
                    schedule_id,
                    "created",
                ),
                event_type=JournalEventType.OBLIGATION_SCHEDULE_CREATED,
                recorded_at=created_at,
                action_id=None,
                authority_ref=schedule_id,
                payload=payload,
                customer_projection={
                    "state": "investigation_obligations_scheduled",
                    "obligation_count": len(obligations),
                },
                operation=_operation(
                    operation_id=_stable_id(
                        "operation",
                        schedule_id,
                        "created",
                    ),
                    idempotency_key=_stable_id(
                        "operation-key",
                        schedule_id,
                        "created",
                    ),
                    causation_id=causation_id,
                    correlation_id=correlation_id,
                    authority_revision=(
                        authority.mailbox_authority_epoch
                    ),
                    payload=payload,
                ),
            )
            self.resume(schedule_id=schedule_id, resumed_at=created_at)
        return schedule

    def resume(
        self,
        *,
        schedule_id: str,
        resumed_at: datetime,
    ) -> ObligationScheduleCheckpoint:
        with self._store.atomic():
            schedule = self._store.get_obligation_schedule(schedule_id)
            current_authority = self._store.get_authority_snapshot(
                schedule.case_id
            )
            if not same_obligation_business_authority(
                schedule.authority_snapshot,
                current_authority,
            ):
                return self._supersede_schedule(
                    schedule=schedule,
                    current_authority=current_authority,
                    recorded_at=resumed_at,
                )
            completion_records = list(
                self._store.list_obligation_completions(schedule_id)
            )
            completed_ids = {
                item.completion.obligation_id
                for item in completion_records
            }
            for obligation in schedule.obligations:
                if (
                    obligation.obligation_id in completed_ids
                    or obligation.execution_disposition
                    is ObligationExecutionDisposition.EXECUTABLE
                ):
                    continue
                status = (
                    ObligationTerminalStatus.TYPED_BOUNDARY
                    if obligation.execution_disposition
                    is ObligationExecutionDisposition.TYPED_BOUNDARY
                    else ObligationTerminalStatus.FAILED
                )
                completion = ObligationCompletion(
                    obligation_id=obligation.obligation_id,
                    dispatch_id="system-terminal:{}".format(
                        obligation.obligation_id
                    ),
                    status=status,
                    result_sha256=content_sha256(
                        {
                            "execution_disposition": (
                                obligation.execution_disposition.value
                            ),
                            "boundary_codes": obligation.boundary_codes,
                        }
                    ),
                )
                record = ObligationCompletionRecord(
                    completion_record_id=_stable_id(
                        "obligation-completion",
                        schedule_id,
                        obligation.obligation_id,
                    ),
                    schedule_id=schedule_id,
                    completion=completion,
                    admitted_authority_snapshot_sha256=(
                        current_authority.content_sha256
                    ),
                    created_at=resumed_at,
                )
                self._store.record_obligation_completion(record)
                completion_records.append(record)
                self._append_completion_event(
                    schedule=schedule,
                    completion=completion,
                    recorded_at=resumed_at,
                    causation_id=schedule.schedule_id,
                )

            propagated = propagate_dependency_terminals(
                obligations=schedule.obligations,
                dependencies=schedule.dependencies,
                completions=tuple(
                    item.completion for item in completion_records
                ),
            )
            known_completion_ids = {
                item.completion.obligation_id
                for item in completion_records
            }
            for completion in propagated:
                if completion.obligation_id in known_completion_ids:
                    continue
                record = ObligationCompletionRecord(
                    completion_record_id=_stable_id(
                        "obligation-completion",
                        schedule_id,
                        completion.obligation_id,
                    ),
                    schedule_id=schedule_id,
                    completion=completion,
                    admitted_authority_snapshot_sha256=(
                        current_authority.content_sha256
                    ),
                    created_at=resumed_at,
                )
                self._store.record_obligation_completion(record)
                completion_records.append(record)
                self._append_completion_event(
                    schedule=schedule,
                    completion=completion,
                    recorded_at=resumed_at,
                    causation_id=schedule.schedule_id,
                )

            completions = tuple(
                item.completion for item in completion_records
            )
            dispatched_ids = {
                item.dispatch.obligation_id
                for item in self._store.list_obligation_dispatches(
                    schedule_id
                )
            }
            runnable = select_runnable_obligations(
                obligations=schedule.obligations,
                dependencies=schedule.dependencies,
                completions=completions,
                current_authority=current_authority,
            )
            for obligation in runnable:
                if obligation.obligation_id in dispatched_ids:
                    continue
                self._enqueue_dispatch(
                    schedule=schedule,
                    obligation=obligation,
                    recorded_at=resumed_at,
                )
            return self._checkpoint(
                schedule=schedule,
                recorded_at=resumed_at,
            )

    def _supersede_schedule(
        self,
        *,
        schedule: ObligationScheduleRecord,
        current_authority,
        recorded_at: datetime,
        arrived_worker_receipts: dict[str, str] | None = None,
    ) -> ObligationScheduleCheckpoint:
        arrived_worker_receipts = arrived_worker_receipts or {}
        completion_records = list(
            self._store.list_obligation_completions(schedule.schedule_id)
        )
        completed_ids = {
            item.completion.obligation_id
            for item in completion_records
        }
        dispatch_by_obligation = {
            item.dispatch.obligation_id: item
            for item in self._store.list_obligation_dispatches(
                schedule.schedule_id
            )
        }
        for obligation in schedule.obligations:
            if obligation.obligation_id in completed_ids:
                continue
            completion = ObligationCompletion(
                obligation_id=obligation.obligation_id,
                dispatch_id=(
                    dispatch_by_obligation[obligation.obligation_id]
                    .dispatch.dispatch_id
                    if obligation.obligation_id in dispatch_by_obligation
                    else "system-superseded:{}".format(
                        obligation.obligation_id
                    )
                ),
                status=ObligationTerminalStatus.SUPERSEDED,
                result_sha256=content_sha256(
                    {
                        "kind": "obligation-authority-superseded.v1",
                        "schedule_authority": (
                            schedule.authority_snapshot_sha256
                        ),
                        "current_authority": (
                            current_authority.content_sha256
                        ),
                        "obligation_id": obligation.obligation_id,
                    }
                ),
            )
            record = ObligationCompletionRecord(
                completion_record_id=_stable_id(
                    "obligation-completion",
                    schedule.schedule_id,
                    obligation.obligation_id,
                ),
                schedule_id=schedule.schedule_id,
                completion=completion,
                admitted_authority_snapshot_sha256=(
                    current_authority.content_sha256
                ),
                created_at=recorded_at,
            )
            self._store.record_obligation_completion(record)
            completion_records.append(record)
            self._append_completion_event(
                schedule=schedule,
                completion=completion,
                recorded_at=recorded_at,
                causation_id=schedule.schedule_id,
            )
        for dispatch_record in dispatch_by_obligation.values():
            message = self._store.get_outbox_message(
                dispatch_record.outbox_message_id
            )
            if (
                self._store.get_job_disposition(
                    message.outbox_message_id
                )
                is not None
            ):
                continue
            completion = next(
                item.completion
                for item in completion_records
                if item.completion.obligation_id
                == dispatch_record.dispatch.obligation_id
            )
            self._store.record_job_disposition(
                JobDispositionRecord(
                    job_disposition_record_id=_stable_id(
                        "job-disposition",
                        message.outbox_message_id,
                        "superseded",
                    ),
                    outbox_message_id=message.outbox_message_id,
                    case_id=message.case_id,
                    job_kind=message.job_kind,
                    disposition=JobDisposition.SUPERSEDED,
                    owner_id=self._owner_id,
                    fencing_token=None,
                    expected_authority_epoch=(
                        message.expected_authority_epoch
                    ),
                    observed_authority_epoch=(
                        current_authority.mailbox_authority_epoch
                    ),
                    result_sha256=arrived_worker_receipts.get(
                        completion.obligation_id,
                        completion.result_sha256,
                    ),
                    reason_code="obligation_authority_superseded",
                    operation=message.operation,
                    completed_at=recorded_at,
                )
            )
        return self._checkpoint(
            schedule=schedule,
            recorded_at=recorded_at,
        )

    def admit_completion(
        self,
        *,
        schedule_id: str,
        obligation_id: str,
        status: ObligationTerminalStatus,
        result_sha256: str,
        completed_at: datetime,
    ) -> ObligationCompletionRecord:
        schedule = self._store.get_obligation_schedule(schedule_id)
        if status not in {
            ObligationTerminalStatus.SATISFIED,
            ObligationTerminalStatus.FAILED,
        }:
            raise ValueError(
                "worker completion status is incompatible with executable "
                "obligation"
            )
        worker_receipt_sha256 = content_sha256(
            {
                "status": status.value,
                "result_sha256": result_sha256,
            }
        )
        prior = tuple(
            item
            for item in self._store.list_obligation_completions(
                schedule_id
            )
            if item.completion.obligation_id == obligation_id
        )
        if prior:
            candidate = prior[0]
            if (
                candidate.completion.status is status
                and candidate.completion.result_sha256 == result_sha256
            ):
                return candidate
            if (
                candidate.completion.status
                is ObligationTerminalStatus.SUPERSEDED
            ):
                dispatch_record = next(
                    (
                        item
                        for item
                        in self._store.list_obligation_dispatches(
                            schedule_id
                        )
                        if item.dispatch.obligation_id == obligation_id
                    ),
                    None,
                )
                if dispatch_record is not None:
                    disposition = self._store.get_job_disposition(
                        dispatch_record.outbox_message_id
                    )
                    if (
                        disposition is not None
                        and disposition.disposition
                        is JobDisposition.SUPERSEDED
                        and disposition.result_sha256
                        in {
                            worker_receipt_sha256,
                            candidate.completion.result_sha256,
                        }
                    ):
                        return candidate
            raise ValueError(
                "obligation already has a different terminal result"
            )
        obligation = next(
            (
                item
                for item in schedule.obligations
                if item.obligation_id == obligation_id
            ),
            None,
        )
        if obligation is None:
            raise ValueError(
                "obligation completion references an unknown obligation"
            )
        dispatch_record = next(
            (
                item
                for item in self._store.list_obligation_dispatches(
                    schedule_id
                )
                if item.dispatch.obligation_id == obligation_id
            ),
            None,
        )
        if dispatch_record is None:
            raise ValueError(
                "obligation completion arrived before durable dispatch"
            )
        message = self._store.get_outbox_message(
            dispatch_record.outbox_message_id
        )
        lease = self._store.acquire_job_lease(
            outbox_message_id=message.outbox_message_id,
            owner_id=self._owner_id,
            now=completed_at,
            expires_at=completed_at + self._lease_duration,
        )
        try:
            with self._store.atomic():
                self._store.assert_job_lease(
                    lease,
                    checked_at=completed_at,
                )
                current = self._store.get_authority_snapshot(
                    schedule.case_id
                )
                if not same_obligation_business_authority(
                    schedule.authority_snapshot,
                    current,
                ):
                    self._supersede_schedule(
                        schedule=schedule,
                        current_authority=current,
                        recorded_at=completed_at,
                        arrived_worker_receipts={
                            obligation_id: worker_receipt_sha256,
                        },
                    )
                    return next(
                        item
                        for item
                        in self._store.list_obligation_completions(
                            schedule_id
                        )
                        if item.completion.obligation_id
                        == obligation_id
                    )
                admitted = admit_obligation_completion(
                    dispatch=dispatch_record.dispatch,
                    obligation=obligation,
                    status=status,
                    result_sha256=result_sha256,
                    current_authority=current,
                    prior_completions=tuple(
                        item.completion
                        for item
                        in self._store.list_obligation_completions(
                            schedule_id
                        )
                    ),
                )[-1]
                record = ObligationCompletionRecord(
                    completion_record_id=_stable_id(
                        "obligation-completion",
                        schedule_id,
                        obligation_id,
                    ),
                    schedule_id=schedule_id,
                    completion=admitted,
                    admitted_authority_snapshot_sha256=(
                        current.content_sha256
                    ),
                    created_at=completed_at,
                )
                self._store.record_obligation_completion(record)
                self._append_completion_event(
                    schedule=schedule,
                    completion=admitted,
                    recorded_at=completed_at,
                    causation_id=message.operation.operation_id,
                )
                self._store.record_job_disposition(
                    JobDispositionRecord(
                        job_disposition_record_id=_stable_id(
                            "job-disposition",
                            message.outbox_message_id,
                            "completed",
                        ),
                        outbox_message_id=message.outbox_message_id,
                        case_id=message.case_id,
                        job_kind=message.job_kind,
                        disposition=JobDisposition.COMPLETED,
                        owner_id=lease.owner_id,
                        fencing_token=lease.fencing_token,
                        expected_authority_epoch=(
                            message.expected_authority_epoch
                        ),
                        observed_authority_epoch=(
                            current.mailbox_authority_epoch
                        ),
                        result_sha256=result_sha256,
                        reason_code="obligation_result_admitted",
                        operation=message.operation,
                        completed_at=completed_at,
                    )
                )
                self.resume(
                    schedule_id=schedule_id,
                    resumed_at=completed_at,
                )
        finally:
            self._store.release_job_lease(lease)
        return record

    def _enqueue_dispatch(
        self,
        *,
        schedule: ObligationScheduleRecord,
        obligation: ResolvedEvidenceObligation,
        recorded_at: datetime,
    ) -> ObligationDispatchRecord:
        dispatch = build_obligation_dispatch(
            obligation=obligation,
            current_authority=schedule.authority_snapshot,
        )
        outbox_message_id = _stable_id(
            "outbox",
            schedule.schedule_id,
            dispatch.dispatch_id,
        )
        payload = {
            "schedule_id": schedule.schedule_id,
            "obligation_id": obligation.obligation_id,
            "obligation": to_jsonable(obligation),
            "dispatch_id": dispatch.dispatch_id,
        }
        event_payload = {
            "schedule_id": schedule.schedule_id,
            "obligation_id": obligation.obligation_id,
            "dispatch_id": dispatch.dispatch_id,
            "outbox_message_id": outbox_message_id,
        }
        event = self._store.append_event(
            case_id=schedule.case_id,
            expected_next_cursor=(
                len(self._store.list_events(schedule.case_id)) + 1
            ),
            event_id=_stable_id(
                "event",
                dispatch.dispatch_id,
                "enqueued",
            ),
            event_type=JournalEventType.OBLIGATION_DISPATCH_ENQUEUED,
            recorded_at=recorded_at,
            action_id=None,
            authority_ref=dispatch.dispatch_id,
            payload=event_payload,
            customer_projection={
                "state": "evidence_obligation_dispatched",
                "obligation_id": obligation.obligation_id,
            },
            operation=_operation(
                operation_id=_stable_id(
                    "operation",
                    dispatch.dispatch_id,
                    "enqueued",
                ),
                idempotency_key=_stable_id(
                    "operation-key",
                    dispatch.dispatch_id,
                    "enqueued",
                ),
                causation_id=schedule.schedule_id,
                correlation_id=schedule.correlation_id,
                authority_revision=(
                    schedule.authority_snapshot.mailbox_authority_epoch
                ),
                payload=event_payload,
            ),
        )
        operation = _operation(
            operation_id=_stable_id(
                "operation",
                outbox_message_id,
            ),
            idempotency_key=_stable_id(
                "outbox-key",
                outbox_message_id,
            ),
            causation_id=event.operation.operation_id,
            correlation_id=schedule.correlation_id,
            authority_revision=(
                schedule.authority_snapshot.mailbox_authority_epoch
            ),
            payload=payload,
        )
        self._store.enqueue_outbox(
            OutboxMessage(
                outbox_message_id=outbox_message_id,
                case_id=schedule.case_id,
                source_event_cursor=event.cursor,
                action_id=None,
                job_kind=AsyncJobKind.OBLIGATION,
                operation=operation,
                expected_head_version=(
                    schedule.authority_snapshot.head_version
                ),
                expected_authority_epoch=(
                    schedule.authority_snapshot.mailbox_authority_epoch
                ),
                authority_snapshot=schedule.authority_snapshot,
                authority_snapshot_sha256=(
                    schedule.authority_snapshot_sha256
                ),
                idempotency_key=operation.idempotency_key,
                destination="obligation-worker",
                contract_ref=OBLIGATION_JOB_CONTRACT_REF,
                payload=payload,
                payload_sha256=content_sha256(payload),
                created_at=recorded_at,
            )
        )
        record = ObligationDispatchRecord(
            dispatch_record_id=_stable_id(
                "obligation-dispatch-record",
                schedule.schedule_id,
                dispatch.dispatch_id,
            ),
            schedule_id=schedule.schedule_id,
            outbox_message_id=outbox_message_id,
            dispatch=dispatch,
            created_at=recorded_at,
        )
        return self._store.record_obligation_dispatch(record)

    def _append_completion_event(
        self,
        *,
        schedule: ObligationScheduleRecord,
        completion: ObligationCompletion,
        recorded_at: datetime,
        causation_id: str,
    ) -> None:
        payload = {
            "schedule_id": schedule.schedule_id,
            "obligation_id": completion.obligation_id,
            "dispatch_id": completion.dispatch_id,
            "status": completion.status.value,
            "result_sha256": completion.result_sha256,
        }
        self._store.append_event(
            case_id=schedule.case_id,
            expected_next_cursor=(
                len(self._store.list_events(schedule.case_id)) + 1
            ),
            event_id=_stable_id(
                "event",
                schedule.schedule_id,
                completion.obligation_id,
                "completed",
            ),
            event_type=JournalEventType.OBLIGATION_COMPLETION_ADMITTED,
            recorded_at=recorded_at,
            action_id=None,
            authority_ref=completion.obligation_id,
            payload=payload,
            customer_projection={
                "state": "evidence_obligation_completed",
                "obligation_id": completion.obligation_id,
                "status": completion.status.value,
            },
            operation=_operation(
                operation_id=_stable_id(
                    "operation",
                    schedule.schedule_id,
                    completion.obligation_id,
                    "completed",
                ),
                idempotency_key=_stable_id(
                    "operation-key",
                    schedule.schedule_id,
                    completion.obligation_id,
                    "completed",
                ),
                causation_id=causation_id,
                correlation_id=schedule.correlation_id,
                authority_revision=(
                    schedule.authority_snapshot.mailbox_authority_epoch
                ),
                payload=payload,
            ),
        )

    def _checkpoint(
        self,
        *,
        schedule: ObligationScheduleRecord,
        recorded_at: datetime,
    ) -> ObligationScheduleCheckpoint:
        dispatches = self._store.list_obligation_dispatches(
            schedule.schedule_id
        )
        completions = self._store.list_obligation_completions(
            schedule.schedule_id
        )
        checkpoints = self._store.list_obligation_schedule_checkpoints(
            schedule.schedule_id
        )
        dispatched = {
            item.dispatch.obligation_id for item in dispatches
        }
        completed = {
            item.completion.obligation_id for item in completions
        }
        state = {
            "dispatched": tuple(sorted(dispatched - completed)),
            "completed": tuple(sorted(completed)),
            "pending": tuple(
                sorted(
                    {
                        item.obligation_id
                        for item in schedule.obligations
                    }
                    - dispatched
                    - completed
                )
            ),
        }
        if checkpoints:
            prior = checkpoints[-1]
            if (
                prior.dispatched_obligation_ids == state["dispatched"]
                and prior.completed_obligation_ids == state["completed"]
                and prior.pending_obligation_ids == state["pending"]
            ):
                return prior
        number = len(checkpoints) + 1
        checkpoint = ObligationScheduleCheckpoint(
            checkpoint_id=_stable_id(
                "obligation-schedule-checkpoint",
                schedule.schedule_id,
                str(number),
                content_sha256(state),
            ),
            schedule_id=schedule.schedule_id,
            checkpoint_number=number,
            prior_checkpoint_id=(
                None if not checkpoints else checkpoints[-1].checkpoint_id
            ),
            schedule_sha256=schedule.content_sha256,
            dispatched_obligation_ids=state["dispatched"],
            completed_obligation_ids=state["completed"],
            pending_obligation_ids=state["pending"],
            authority_snapshot_sha256=(
                schedule.authority_snapshot_sha256
            ),
            created_at=recorded_at,
        )
        self._store.record_obligation_schedule_checkpoint(checkpoint)
        payload = {
            "schedule_id": schedule.schedule_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_number": checkpoint.checkpoint_number,
            **state,
        }
        self._store.append_event(
            case_id=schedule.case_id,
            expected_next_cursor=(
                len(self._store.list_events(schedule.case_id)) + 1
            ),
            event_id=_stable_id(
                "event",
                checkpoint.checkpoint_id,
                "recorded",
            ),
            event_type=(
                JournalEventType.OBLIGATION_SCHEDULE_CHECKPOINTED
            ),
            recorded_at=recorded_at,
            action_id=None,
            authority_ref=checkpoint.checkpoint_id,
            payload=payload,
            customer_projection=None,
            operation=_operation(
                operation_id=_stable_id(
                    "operation",
                    checkpoint.checkpoint_id,
                ),
                idempotency_key=_stable_id(
                    "operation-key",
                    checkpoint.checkpoint_id,
                ),
                causation_id=schedule.schedule_id,
                correlation_id=schedule.correlation_id,
                authority_revision=(
                    schedule.authority_snapshot.mailbox_authority_epoch
                ),
                payload=payload,
            ),
        )
        return checkpoint


def _operation(
    *,
    operation_id: str,
    idempotency_key: str,
    causation_id: str,
    correlation_id: str,
    authority_revision: int,
    payload: dict[str, object],
) -> OperationIdentity:
    return OperationIdentity(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        causation_id=causation_id,
        correlation_id=correlation_id,
        authority_revision=authority_revision,
        payload_sha256=content_sha256(payload),
    )


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256(
        "\x1f".join(parts).encode("utf-8")
    ).hexdigest()
    return "{}-{}".format(parts[0], digest[:24])
