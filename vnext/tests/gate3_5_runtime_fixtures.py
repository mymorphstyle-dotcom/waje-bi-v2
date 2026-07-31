from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from test_gate3_2_obligation_scheduler import (
    NOW,
    accepted_single_obligation_runtime,
)
from waje_vnext.controller import EvidenceRuntime
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.evidence import (
    CapabilityResultEnvelope,
    EstimatePayload,
    EvidenceAdmissionProfile,
    InlineResultMaterial,
    ResultMaterialKind,
    build_capability_result_envelope,
    build_conformance_execution_provenance,
    build_evidence_record,
    build_obligation_satisfaction,
)
from waje_vnext.domain.measurement import (
    ClaimStrengthCeiling,
    ObligationExecutionDisposition,
)
from waje_vnext.domain.planning import (
    build_conformance_execution_spec,
    build_logical_execution_attempt,
)


class MutableTestClock:
    def __init__(self, current) -> None:
        self.current = current

    def __call__(self):
        return self.current

    def set(self, current) -> None:
        self.current = current


@dataclass(frozen=True, slots=True)
class EvidenceRuntimeWorld:
    controller: object
    store: object
    scheduler: object
    obligations: tuple[object, ...]
    run_id: str
    schedule: object
    dispatch: object
    outbox: object
    binding: object
    obligation: object
    outcome: object
    scope: object
    spec: object
    attempt: object
    envelope: CapabilityResultEnvelope
    runtime: EvidenceRuntime
    boundary_obligation: object | None
    boundary_outcome: object | None
    boundary_satisfaction: object | None
    storage_clock: MutableTestClock


def land_evidence_runtime_world(
    world: EvidenceRuntimeWorld,
    *,
    received_at=NOW,
):
    lease = world.store.acquire_job_lease(
        outbox_message_id=world.envelope.outbox_message_id,
        owner_id=f"landing-worker:{world.schedule.case_id}",
        now=received_at,
        expires_at=received_at + timedelta(minutes=5),
    )
    try:
        return world.runtime.land_result(
            envelope=world.envelope,
            job_lease=lease,
            received_at=received_at,
        )
    finally:
        world.store.release_job_lease(lease)


def build_evidence_runtime_world(
    case_id: str,
    *,
    store: object | None = None,
    owner_id: str = "gate35-evidence-admission-worker",
    evidence_strength: ClaimStrengthCeiling = (
        ClaimStrengthCeiling.DESCRIPTIVE
    ),
    limitation_refs: tuple[str, ...] = (
        "limitation:conformance-fixture-only",
    ),
    mixed_boundary: bool = False,
) -> EvidenceRuntimeWorld:
    storage_clock = MutableTestClock(NOW)
    (
        controller,
        authority_store,
        scheduler,
        obligations,
        _,
        run_id,
    ) = accepted_single_obligation_runtime(
        case_id,
        store=store,
        storage_clock=storage_clock,
        claim_strength_ceiling=evidence_strength,
        mixed_boundary=mixed_boundary,
    )
    schedule = scheduler.create_schedule(
        case_id=case_id,
        causation_id="gate35-evidence-runtime",
        created_at=NOW,
    )
    dispatch = authority_store.list_obligation_dispatches(
        schedule.schedule_id
    )[0]
    outbox = authority_store.get_outbox_message(
        dispatch.outbox_message_id
    )
    binding = authority_store.get_query_binding(
        dispatch.dispatch.query_binding_id
    )
    obligation = next(
        item
        for item in obligations
        if item.obligation_id == binding.obligation_id
    )
    outcome = authority_store.get_measurement_resolution(
        binding.resolution_outcome_id
    )
    frame = authority_store.get_frame(schedule.frame_revision_id)
    scope = next(
        item
        for item in frame.measurement_design.scopes
        if item.scope_id == binding.requirement_binding.scope_id
    )
    snapshot = authority_store.get_authority_snapshot(schedule.case_id)
    spec = build_conformance_execution_spec(
        query_binding=binding,
        fixture_ref=(
            "waje-vnext://conformance-fixture/"
            "gate3-5-evidence-runtime.v1"
        ),
        fixture_content_sha256=content_sha256(
            {"fixture": "gate3-5-evidence-runtime.v1"}
        ),
        result_contract_ref=(
            "waje-vnext://result-contract/aggregate-contrast.v1"
        ),
        execution_policy_ref=(
            "waje-vnext://execution-policy/conformance.v1"
        ),
        created_at=NOW,
    )
    authority_store.record_conformance_execution_spec(
        spec,
        expected_authority_snapshot=snapshot,
    )
    attempt = build_logical_execution_attempt(
        spec=spec,
        authority_snapshot=snapshot,
        attempt_number=1,
        prior_attempt=None,
        retry_reason_code=None,
        requested_at=NOW,
    )
    authority_store.record_logical_execution_attempt(attempt)
    provenance = build_conformance_execution_provenance(
        binding=binding,
        spec=spec,
        attempt=attempt,
        current_authority=snapshot,
    )
    windows = binding.resolved_measurement_instance.windows
    evidence = build_evidence_record(
        run_id=run_id,
        profile=EvidenceAdmissionProfile.CONFORMANCE,
        binding=binding,
        obligation=obligation,
        outcome=outcome,
        execution_provenance=provenance,
        actual_scope=scope,
        actual_windows=windows,
        actual_exposure_facts=tuple(
            fact
            for window in windows
            for fact in window.exposure_facts
        ),
        evidence_type_ref=obligation.evidence_type_refs[0],
        evidence_strength=evidence_strength,
        estimate=EstimatePayload(
            estimate_schema_ref=(
                "waje-vnext://estimate-schema/window-contrast.v1"
            ),
            estimate_content_sha256=content_sha256(
                {
                    "left_daily_average": "110.00",
                    "right_daily_average": "100.00",
                }
            ),
            uncertainty_schema_ref=None,
            uncertainty_content_sha256=None,
        ),
        result_material=InlineResultMaterial(
            kind=ResultMaterialKind.INLINE,
            payload_content_sha256=content_sha256(
                {"rows": "bounded-conformance-result"}
            ),
            schema_ref=(
                "waje-vnext://result-schema/window-contrast.v1"
            ),
            row_count=2,
            byte_count=256,
        ),
        business_summary=(
            "目标窗口的有效观察日归一化金额高于对照窗口。"
        ),
        limitation_refs=limitation_refs,
        produced_at=NOW,
    )
    envelope = build_capability_result_envelope(
        evidence_record=evidence,
        run_id=run_id,
        schedule_id=schedule.schedule_id,
        dispatch_record_id=dispatch.dispatch_record_id,
        outbox_message_id=dispatch.outbox_message_id,
        logical_execution_attempt_id=(
            attempt.logical_execution_attempt_id
        ),
        logical_execution_attempt_content_sha256=attempt.content_sha256,
        produced_at=NOW,
    )
    runtime = EvidenceRuntime(
        store=authority_store,
        owner_id=owner_id,
        profile=EvidenceAdmissionProfile.CONFORMANCE,
    )
    boundary_obligation = next(
        (
            item
            for item in obligations
            if item.execution_disposition
            is ObligationExecutionDisposition.TYPED_BOUNDARY
        ),
        None,
    )
    boundary_outcome = (
        None
        if boundary_obligation is None
        else authority_store.get_measurement_resolution(
            boundary_obligation.resolution_outcome_id
        )
    )
    boundary_satisfaction = (
        None
        if boundary_obligation is None or boundary_outcome is None
        else build_obligation_satisfaction(
            obligation=boundary_obligation,
            admissions=(),
            validities=(),
            boundary_outcome=boundary_outcome,
            prior=None,
            recorded_at=NOW,
        )
    )
    return EvidenceRuntimeWorld(
        controller=controller,
        store=authority_store,
        scheduler=scheduler,
        obligations=obligations,
        run_id=run_id,
        schedule=schedule,
        dispatch=dispatch,
        outbox=outbox,
        binding=binding,
        obligation=obligation,
        outcome=outcome,
        scope=scope,
        spec=spec,
        attempt=attempt,
        envelope=envelope,
        runtime=runtime,
        boundary_obligation=boundary_obligation,
        boundary_outcome=boundary_outcome,
        boundary_satisfaction=boundary_satisfaction,
        storage_clock=storage_clock,
    )


def forge_conformance_provenance_envelope(
    world: EvidenceRuntimeWorld,
) -> CapabilityResultEnvelope:
    """Build a valid envelope that lies about its persisted execution spec."""

    evidence = world.envelope.evidence_record
    forged_provenance = replace(
        evidence.execution_provenance,
        fixture_ref=(
            "waje-vnext://conformance-fixture/forged-unpersisted.v1"
        ),
    )
    forged_evidence = build_evidence_record(
        run_id=evidence.run_id,
        profile=evidence.profile,
        binding=world.binding,
        obligation=world.obligation,
        outcome=world.outcome,
        execution_provenance=forged_provenance,
        actual_scope=evidence.actual_scope,
        actual_windows=evidence.actual_windows,
        actual_exposure_facts=evidence.actual_exposure_facts,
        evidence_type_ref=evidence.evidence_type_ref,
        evidence_strength=evidence.evidence_strength,
        estimate=evidence.estimate,
        result_material=evidence.result_material,
        business_summary=evidence.business_summary,
        limitation_refs=evidence.limitation_refs,
        produced_at=evidence.produced_at,
    )
    return build_capability_result_envelope(
        evidence_record=forged_evidence,
        run_id=world.envelope.run_id,
        schedule_id=world.envelope.schedule_id,
        dispatch_record_id=world.envelope.dispatch_record_id,
        outbox_message_id=world.envelope.outbox_message_id,
        logical_execution_attempt_id=(
            world.envelope.logical_execution_attempt_id
        ),
        logical_execution_attempt_content_sha256=(
            world.envelope.logical_execution_attempt_content_sha256
        ),
        produced_at=world.envelope.produced_at,
    )
