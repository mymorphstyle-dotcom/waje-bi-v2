"""Storage-backed validation for self-verifying run trace manifests."""

from __future__ import annotations

from waje_vnext.domain.answering import AnswerStatus
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.runtime_amendment import (
    RunTraceEventLink,
    RunTraceManifest,
)


def validate_run_trace_manifest_references(
    store,
    record: RunTraceManifest,
) -> None:
    events = tuple(
        event
        for event in store.list_events(record.case_id)
        if event.operation.correlation_id == record.run_id
    )
    if not events:
        raise ValueError("run trace requires a durable journal")
    expected_links = tuple(
        RunTraceEventLink(
            cursor=event.cursor,
            event_id=event.event_id,
            operation_id=event.operation.operation_id,
            causation_id=event.operation.causation_id,
            correlation_id=event.operation.correlation_id,
            authority_revision=event.operation.authority_revision,
            payload_sha256=event.operation.payload_sha256,
        )
        for event in events
    )
    if (
        record.event_operation_lineage != expected_links
        or record.start_event_cursor != events[0].cursor
        or record.terminal_event_cursor != events[-1].cursor
    ):
        raise ValueError(
            "run trace event lineage does not match the durable journal"
        )

    ingress = tuple(
        item
        for item in store.list_message_ingress_records(record.case_id)
        if item.run_id == record.run_id
    )
    _require_exact(
        record.ingress_record_ids,
        tuple(item.ingress_record_id for item in ingress),
        "ingress",
    )
    outbox = tuple(
        item
        for item in store.list_outbox_messages(case_id=record.case_id)
        if item.operation.correlation_id == record.run_id
    )
    outbox_ids = {item.outbox_message_id for item in outbox}
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
        for item in store.list_message_impact_bindings(record.case_id)
        if item.logical_model_job_id in outbox_ids
    )
    _require_exact(
        record.message_binding_ids,
        tuple(item.binding_id for item in bindings),
        "message binding",
    )
    candidates = tuple(
        item
        for item in store.list_frame_candidates(record.case_id)
        if item.source_action_id in action_ids
    )
    candidate_ids = {
        item.frame_candidate_id for item in candidates
    }
    _require_exact(
        record.frame_candidate_ids,
        tuple(item.frame_candidate_id for item in candidates),
        "frame candidate",
    )
    _require_exact(
        record.frame_candidate_supersession_ids,
        tuple(
            item.supersession_record_id
            for item in store.list_frame_candidate_supersessions(
                record.case_id
            )
            if item.source_operation_id in operation_ids
        ),
        "frame candidate supersession",
    )
    _require_exact(
        record.frame_review_ids,
        tuple(
            item.frame_review_id
            for item in store.list_frame_reviews(record.case_id)
            if item.frame_candidate_id in candidate_ids
        ),
        "frame review",
    )
    _require_exact(
        record.job_disposition_record_ids,
        tuple(
            item.job_disposition_record_id
            for item in store.list_job_dispositions(record.case_id)
            if item.outbox_message_id in outbox_ids
        ),
        "job disposition",
    )
    model_jobs = tuple(
        item
        for item in store.list_logical_model_jobs(record.case_id)
        if item.job_id in outbox_ids
    )
    _require_exact(
        record.logical_model_job_ids,
        tuple(item.logical_model_job_id for item in model_jobs),
        "logical model job",
    )
    _require_exact(
        record.provider_attempt_receipt_ids,
        tuple(
            receipt.provider_attempt_receipt_id
            for job in model_jobs
            for receipt in store.list_provider_attempt_receipts(
                job.logical_model_job_id
            )
        ),
        "provider attempt receipt",
    )
    _require_exact(
        record.durable_model_result_ids,
        tuple(
            result.durable_model_result_id
            for job in model_jobs
            if (
                result := store.get_durable_model_result(
                    job.logical_model_job_id
                )
            )
            is not None
        ),
        "durable model result",
    )
    _require_exact(
        record.plan_revision_ids,
        _event_refs(events, JournalEventType.PLAN_ACCEPTED),
        "plan",
    )
    _require_exact(
        record.resolution_outcome_ids,
        _event_refs(
            events,
            JournalEventType.MEASUREMENT_RESOLUTION_RECORDED,
        ),
        "measurement resolution",
    )
    _require_exact(
        record.obligation_ids,
        _event_refs(
            events,
            JournalEventType.EVIDENCE_OBLIGATION_RECORDED,
        ),
        "evidence obligation",
    )
    _require_exact(
        record.effect_attempt_ids,
        _ordered_unique(
            attempt.effect_attempt_id
            for message in outbox
            for attempt in store.list_effect_attempts(
                message.outbox_message_id
            )
        ),
        "effect attempt",
    )
    _require_exact(
        record.evidence_record_ids,
        _event_refs(events, JournalEventType.EVIDENCE_RECORDED),
        "evidence",
    )
    answer_ids = _event_refs(events, JournalEventType.ANSWER_ACCEPTED)
    answers = tuple(store.get_answer(answer_id) for answer_id in answer_ids)
    _require_exact(
        record.claim_ids,
        _ordered_unique(
            claim.claim_id
            for answer in answers
            for claim in answer.claims
        ),
        "claim",
    )
    _require_exact(
        record.provisional_answer_version_ids,
        tuple(
            answer.answer_version_id
            for answer in answers
            if answer.status is AnswerStatus.PROVISIONAL
        ),
        "provisional answer",
    )


def _event_refs(events, event_type: JournalEventType) -> tuple[str, ...]:
    return tuple(
        event.authority_ref
        for event in events
        if event.event_type is event_type
        and event.authority_ref is not None
    )


def _ordered_unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _require_exact(
    actual: tuple[str, ...],
    expected: tuple[str, ...],
    label: str,
) -> None:
    if actual != expected:
        raise ValueError(
            f"run trace {label} references do not match durable state"
        )
