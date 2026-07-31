from __future__ import annotations

from datetime import date, datetime

from test_gate3_3_measurement_resolver import (
    make_context,
    make_request,
    make_trusted_registry,
    make_trusted_resolver,
)
from waje_vnext.domain.async_runtime import OperationIdentity
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.measurement import MeasurementDerivationAuthority
from waje_vnext.domain.planning import (
    PlanBundle,
    ProposedWorkTask,
    compile_plan_bundle,
)


def record_plan_bundle(
    *,
    store,
    case,
    frame,
    created_at: datetime,
    action_id: str = "action-plan-current",
    revision_reason: str = "Investigate every accepted obligation",
    plan_revision_id: str = "plan-1",
    prior_plan=None,
    proposed_tasks: tuple[ProposedWorkTask, ...] | None = None,
    correlation_id: str | None = None,
    measurement_authority=None,
    operation: OperationIdentity | None = None,
) -> tuple[object, PlanBundle]:
    if measurement_authority is None:
        outcomes, admissions, obligations = record_measurement_authority(
            store=store,
            case=case,
            frame=frame,
            created_at=created_at,
            correlation_id=correlation_id,
        )
    else:
        outcomes, admissions, obligations = measurement_authority
    if proposed_tasks is None:
        proposed_tasks = tuple(
            ProposedWorkTask(
                proposal_task_key=(
                    "measure-accepted-contrast"
                    if len(obligations) == 1
                    else f"investigate-{index}"
                ),
                business_purpose=(
                    "Collect evidence that closes the accepted requirement"
                ),
                capability_intent_ref=(
                    "waje-vnext://capability-intent/"
                    "measurement-evidence.v1"
                ),
                obligation_ids=(obligation.obligation_id,),
                depends_on_task_keys=(),
            )
            for index, obligation in enumerate(obligations, start=1)
        )
    bundle = compile_plan_bundle(
        case=case,
        authority_snapshot=store.get_authority_snapshot(case.case_id),
        frame=frame,
        outcomes=outcomes,
        admissions=admissions,
        obligations=obligations,
        proposed_tasks=proposed_tasks,
        plan_revision_id=plan_revision_id,
        revision_number=(
            1 if prior_plan is None else prior_plan.revision_number + 1
        ),
        prior_plan_revision_id=(
            None
            if prior_plan is None
            else prior_plan.plan_revision_id
        ),
        created_by_action_id=action_id,
        created_at=created_at,
        revision_reason=revision_reason,
    )
    case = store.accept_plan_bundle(
        bundle,
        expected_head_version=case.head_version,
        event_id=content_sha256(
            {
                "kind": "test-plan-event",
                "plan_revision_id": plan_revision_id,
            }
        ),
        recorded_at=created_at,
        operation=operation,
    )
    return case, bundle


def record_measurement_authority(
    *,
    store,
    case,
    frame,
    created_at: datetime,
    correlation_id: str | None = None,
    resolution_requests_by_estimand_id=None,
):
    resolver = make_trusted_resolver()
    derivation_authority = (
        MeasurementDerivationAuthority.from_authority_snapshot(
            store.get_authority_snapshot(case.case_id)
        )
    )
    resolved_entries = []
    for estimand in frame.measurement_design.estimands:
        context = make_context(as_of=created_at)
        request = (
            make_request(frame, anchor=date(2026, 6, 1))
            if resolution_requests_by_estimand_id is None
            else resolution_requests_by_estimand_id[
                estimand.estimand_id
            ]
        )
        registry = make_trusted_registry(request, context)
        outcome = resolver.resolve_measurement(
            frame=frame,
            derivation_authority=derivation_authority,
            estimand_id=estimand.estimand_id,
            context=context,
            request=request,
            trusted_input_registry=registry,
            created_at=created_at,
        )
        admission = resolver.admit_resolution(
            frame=frame,
            outcome=outcome,
            context=context,
            request=request,
            trusted_input_registry=registry,
        )
        resolved_entries.append(
            (outcome, admission, context, request, registry)
        )
    outcomes = tuple(item[0] for item in resolved_entries)
    admissions = tuple(item[1] for item in resolved_entries)
    obligations = tuple(
        obligation
        for outcome, _, context, request, registry in resolved_entries
        for obligation in resolver.compile_evidence_obligations(
            frame=frame,
            outcome=outcome,
            context=context,
            resolution_request=request,
            trusted_input_registry=registry,
            created_at=created_at,
        )
    )
    for outcome, admission in zip(outcomes, admissions, strict=True):
        event_payload = {
            "content_sha256": content_sha256(outcome)
        }
        event_id = content_sha256(
            {
                "kind": "test-resolution-event",
                "outcome_id": outcome.resolution_outcome_id,
            }
        )
        store.record_measurement_resolution(
            outcome,
            admission=admission,
            expected_head_version=case.head_version,
            event_id=event_id,
            operation=_operation(
                event_id=event_id,
                case=case,
                payload=event_payload,
                correlation_id=correlation_id,
                authority_revision=(
                    derivation_authority.mailbox_authority_epoch
                ),
            ),
        )
    for obligation in obligations:
        event_payload = {
            "content_sha256": content_sha256(obligation)
        }
        event_id = content_sha256(
            {
                "kind": "test-obligation-event",
                "obligation_id": obligation.obligation_id,
            }
        )
        store.record_evidence_obligation(
            obligation,
            expected_head_version=case.head_version,
            event_id=event_id,
            operation=_operation(
                event_id=event_id,
                case=case,
                payload=event_payload,
                correlation_id=correlation_id,
                authority_revision=(
                    derivation_authority.mailbox_authority_epoch
                ),
            ),
        )
    return outcomes, admissions, obligations


def _operation(
    *,
    event_id: str,
    case,
    payload,
    correlation_id: str | None,
    authority_revision: int,
) -> OperationIdentity | None:
    if correlation_id is None:
        return None
    return OperationIdentity(
        operation_id=f"{event_id}:operation",
        idempotency_key=f"{event_id}:key",
        causation_id=case.accepted_frame_revision_id or case.case_id,
        correlation_id=correlation_id,
        authority_revision=authority_revision,
        payload_sha256=content_sha256(payload),
    )
