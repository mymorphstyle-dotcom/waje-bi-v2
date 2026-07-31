from __future__ import annotations

import itertools
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime

from gate1_fixtures import make_frame
from gate3_plan_fixtures import (
    record_measurement_authority,
    record_plan_bundle,
)
from tests.test_gate2_controller import frame_proposal
from waje_vnext.controller import (
    DurableObligationCoordinator,
    ScriptedEffectExecutor,
    WAJEController,
)
from waje_vnext.domain.async_runtime import AsyncJobKind, AuthoritySnapshot
from waje_vnext.domain.authority import CaseLifecycle
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.measurement import (
    ClaimStrengthCeiling,
    IdentificationLevel,
    ObligationExecutionDisposition,
)
from waje_vnext.domain.measurement_resolver import (
    TrustedMeasurementResolver,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationCompletion,
    ObligationCompletionRecord,
    ObligationDependency,
    ObligationPlanBinding,
    ObligationTerminalStatus,
    admit_obligation_completion,
    build_obligation_dispatch as build_plan_bound_dispatch,
    propagate_dependency_terminals,
    select_runnable_obligations,
)
from waje_vnext.domain.planning import ProposedWorkTask
from waje_vnext.providers import ScriptedPrimaryAgentProvider
from waje_vnext.storage import (
    InMemoryAuthorityStore,
    InvalidAuthorityTransition,
)
from tests.test_gate3_3_measurement_resolver import (
    make_context,
    make_derivation_authority,
    make_request,
    make_trusted_registry,
    make_trusted_signer,
    make_trusted_verifier,
)


NOW = datetime(2026, 7, 31, 8, tzinfo=UTC)


def build_obligation_dispatch(*, obligation, current_authority):
    return build_plan_bound_dispatch(
        obligation=obligation,
        plan_binding=ObligationPlanBinding(
            obligation_id=obligation.obligation_id,
            task_id=content_sha256(
                {"task": obligation.obligation_id}
            ),
            query_binding_id=content_sha256(
                {"query": obligation.obligation_id}
            ),
        ),
        plan_revision_id=(
            current_authority.accepted_plan_revision_id or ""
        ),
        current_authority=current_authority,
    )


def authority(frame, *, obligation_version: int = 0):
    return AuthoritySnapshot(
        case_id=frame.case_id,
        head_version=2,
        mailbox_authority_epoch=1,
        accepted_question_revision_id=frame.question_revision_id,
        accepted_frame_revision_id=frame.frame_revision_id,
        accepted_plan_revision_id="plan-obligation-test",
        active_frame_candidate_generation=1,
        active_frame_candidate_sha256=frame.content_sha256,
        obligation_state_version=obligation_version,
        evidence_admission_state_version=0,
        contradiction_state_version=0,
    )


def obligations():
    frame = make_frame()
    design = frame.measurement_design
    requirement = design.evidence_requirements[0]
    requirements = tuple(
        replace(
            requirement,
            evidence_requirement_id=f"requirement-{index}",
            required_evidence_type_refs=(
                f"evidence-type:test:{index}",
            ),
        )
        for index in range(1, 4)
    )
    estimand = replace(
        design.estimands[0],
        evidence_requirement_ids=tuple(
            item.evidence_requirement_id for item in requirements
        ),
    )
    completion = replace(
        design.completion_specs[0],
        required_evidence_requirement_ids=(
            estimand.evidence_requirement_ids
        ),
    )
    design = replace(
        design,
        evidence_requirements=requirements,
        estimands=(estimand,),
        completion_specs=(completion,),
    )
    frame = make_frame(measurement_design=design)
    context = make_context()
    request = make_request(
        frame,
        anchor=datetime(2026, 7, 1).date(),
    )
    trusted_registry = make_trusted_registry(request)
    resolver = TrustedMeasurementResolver(
        make_trusted_verifier(),
        make_trusted_signer(),
    )
    outcome = resolver.resolve_measurement(
        frame=frame,
        derivation_authority=make_derivation_authority(frame),
        estimand_id=estimand.estimand_id,
        context=context,
        request=request,
        trusted_input_registry=trusted_registry,
        created_at=NOW,
    )
    return (
        frame,
        resolver.compile_evidence_obligations(
            frame=frame,
            outcome=outcome,
            context=context,
            resolution_request=request,
            trusted_input_registry=trusted_registry,
            created_at=NOW,
        ),
    )


def accepted_single_obligation_runtime(
    case_id: str,
    *,
    store: InMemoryAuthorityStore | None = None,
    storage_clock=None,
    obligation_count: int = 1,
    typed_boundary: bool = False,
    mixed_boundary: bool = False,
    claim_strength_ceiling: ClaimStrengthCeiling = (
        ClaimStrengthCeiling.DESCRIPTIVE
    ),
):
    if typed_boundary and mixed_boundary:
        raise ValueError("typed_boundary and mixed_boundary are mutually exclusive")
    store = (
        InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier(),
            clock=storage_clock or (lambda: NOW),
        )
        if store is None
        else store
    )
    proposal = frame_proposal(case_id)
    if claim_strength_ceiling is not ClaimStrengthCeiling.DESCRIPTIVE:
        design = proposal.payload.measurement_design
        identification = design.identifications[0]
        identification_level = IdentificationLevel(
            claim_strength_ceiling.value
        )
        if identification_level is IdentificationLevel.CAUSAL:
            identification = replace(
                identification,
                level=identification_level,
                counterfactual_ref="counterfactual:controlled-world:v1",
                positivity_ref="positivity:controlled-world:v1",
                consistency_ref="consistency:controlled-world:v1",
                interference_ref="interference:controlled-world:v1",
            )
        else:
            identification = replace(
                identification,
                level=identification_level,
            )
        proposal = replace(
            proposal,
            payload=replace(
                proposal.payload,
                measurement_design=replace(
                    design,
                    identifications=(identification,),
                    estimands=tuple(
                        replace(
                            estimand,
                            claim_strength_ceiling=(
                                claim_strength_ceiling
                            ),
                        )
                        for estimand in design.estimands
                    ),
                ),
            ),
        )
    if mixed_boundary:
        design = proposal.payload.measurement_design
        base_requirement = design.evidence_requirements[0]
        boundary_requirement = replace(
            base_requirement,
            evidence_requirement_id=f"requirement-{case_id}-boundary",
            target_estimand_ids=(f"estimand-{case_id}-boundary",),
            required_evidence_type_refs=(
                f"evidence-type:{case_id}:boundary",
            ),
        )
        base_estimand = design.estimands[0]
        boundary_estimand = replace(
            base_estimand,
            estimand_id=f"estimand-{case_id}-boundary",
            evidence_requirement_ids=(
                boundary_requirement.evidence_requirement_id,
            ),
        )
        completion = replace(
            design.completion_specs[0],
            target_estimand_ids=(
                base_estimand.estimand_id,
                boundary_estimand.estimand_id,
            ),
            required_evidence_requirement_ids=(
                base_requirement.evidence_requirement_id,
                boundary_requirement.evidence_requirement_id,
            ),
        )
        proposal = replace(
            proposal,
            payload=replace(
                proposal.payload,
                measurement_design=replace(
                    design,
                    evidence_requirements=(
                        base_requirement,
                        boundary_requirement,
                    ),
                    estimands=(base_estimand, boundary_estimand),
                    completion_specs=(completion,),
                ),
            ),
        )
    if obligation_count > 1:
        design = proposal.payload.measurement_design
        requirement = design.evidence_requirements[0]
        requirements = tuple(
            replace(
                requirement,
                evidence_requirement_id=(
                    f"requirement-{case_id}-{index}"
                ),
                required_evidence_type_refs=(
                    f"evidence-type:{case_id}:{index}",
                ),
            )
            for index in range(1, obligation_count + 1)
        )
        estimand = replace(
            design.estimands[0],
            evidence_requirement_ids=tuple(
                item.evidence_requirement_id for item in requirements
            ),
        )
        completion = replace(
            design.completion_specs[0],
            required_evidence_requirement_ids=(
                estimand.evidence_requirement_ids
            ),
        )
        proposal = replace(
            proposal,
            payload=replace(
                proposal.payload,
                measurement_design=replace(
                    design,
                    evidence_requirements=requirements,
                    estimands=(estimand,),
                    completion_specs=(completion,),
                ),
            ),
        )
    provider = ScriptedPrimaryAgentProvider((proposal,))
    controller = WAJEController(
        store=store,
        provider=provider,
        effect_executor=ScriptedEffectExecutor(()),
        owner_id="authority-worker",
        clock=lambda: NOW,
    )
    run_id = f"run-{case_id}"
    controller.start(
        case_id=case_id,
        thread_id=f"thread-{case_id}",
        run_id=run_id,
        user_message="建立一个可恢复的证据调查。",
    )
    controller.deliver_pending_message_binding(case_id)
    controller.advance(case_id)
    controller.deliver_pending_llm(case_id)
    controller.deliver_pending_frame_review(case_id)
    frame_id = store.get_case(case_id).accepted_frame_revision_id
    assert frame_id is not None
    frame = store.get_frame(frame_id)
    case = store.get_case(case_id)
    resolution_requests = None
    if mixed_boundary:
        estimands = frame.measurement_design.estimands
        resolution_requests = {
            estimands[0].estimand_id: make_request(
                frame,
                anchor=date(2026, 7, 1),
            ),
            estimands[1].estimand_id: make_request(
                frame,
                anchor=date(2026, 7, 1),
                expected="7",
                observed="6",
                valid="6",
                invalid="0",
                missing="1",
            ),
        }
    elif typed_boundary:
        estimand_id = (
            frame.measurement_design.estimands[0].estimand_id
        )
        resolution_requests = {
            estimand_id: make_request(
                frame,
                anchor=date(2026, 7, 1),
                expected="7",
                observed="6",
                valid="6",
                invalid="0",
                missing="1",
            )
        }
    measurement_authority = record_measurement_authority(
        store=store,
        case=case,
        frame=frame,
        created_at=NOW,
        correlation_id=run_id,
        resolution_requests_by_estimand_id=resolution_requests,
    )
    _, _, items = measurement_authority
    task_keys = tuple(
        f"investigate-{index}"
        for index in range(1, len(items) + 1)
    )
    proposed_tasks = tuple(
        ProposedWorkTask(
            proposal_task_key=task_keys[index],
            business_purpose="Close the accepted evidence obligation",
            capability_intent_ref=(
                "waje-vnext://capability-intent/"
                + (
                    "measurement-evidence.v1"
                    if item.execution_disposition
                    is ObligationExecutionDisposition.EXECUTABLE
                    else "boundary-inspection.v1"
                )
            ),
            obligation_ids=(item.obligation_id,),
            depends_on_task_keys=(
                ()
                if index == 0
                else (task_keys[index - 1],)
            ),
        )
        for index, item in enumerate(items)
    )
    case, _ = record_plan_bundle(
        store=store,
        case=case,
        frame=frame,
        created_at=NOW,
        plan_revision_id=f"{case_id}:plan:1",
        correlation_id=run_id,
        proposed_tasks=proposed_tasks,
        measurement_authority=measurement_authority,
    )
    dependencies = tuple(
        ObligationDependency(
            item.obligation_id,
            ()
            if index == 0
            else (items[index - 1].obligation_id,),
        )
        for index, item in enumerate(items)
    )
    scheduler = DurableObligationCoordinator(
        store=store,
        owner_id="obligation-worker",
    )
    return (
        controller,
        store,
        scheduler,
        items,
        dependencies,
        run_id,
    )


class ObligationSchedulerTest(unittest.TestCase):
    def test_storage_rejects_noncanonical_schedule_identity(self) -> None:
        (
            _,
            store,
            scheduler,
            _,
            _,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-canonical-schedule-id"
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-canonical-schedule-id",
            causation_id="accepted-frame",
            created_at=NOW,
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "schedule ID is not canonical",
        ):
            store.record_obligation_schedule(
                replace(schedule, schedule_id="forged-schedule-id")
            )

    def test_mixed_topology_only_selects_executable_obligations(self) -> None:
        frame, items = obligations()
        boundary = replace(
            items[1],
            execution_disposition=(
                ObligationExecutionDisposition.TYPED_BOUNDARY
            ),
            boundary_code="incomplete_period",
        )
        mixed = (items[0], boundary, items[2])
        runnable = select_runnable_obligations(
            obligations=mixed,
            dependencies=tuple(
                ObligationDependency(item.obligation_id, ())
                for item in mixed
            ),
            completions=(),
            current_authority=authority(frame),
        )

        self.assertEqual(
            {item.obligation_id for item in runnable},
            {
                items[0].obligation_id,
                items[2].obligation_id,
            },
        )

    def test_accepted_all_boundary_plan_completes_without_dispatch(
        self,
    ) -> None:
        (
            _,
            store,
            scheduler,
            items,
            _,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-all-boundary",
            typed_boundary=True,
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-all-boundary",
            causation_id="accepted-frame",
            created_at=NOW,
        )

        self.assertTrue(
            all(
                item.execution_disposition
                is ObligationExecutionDisposition.TYPED_BOUNDARY
                for item in items
            )
        )
        self.assertEqual(
            store.list_obligation_dispatches(schedule.schedule_id),
            (),
        )
        self.assertFalse(
            any(
                item.job_kind is AsyncJobKind.OBLIGATION
                for item in store.list_outbox_messages(
                    case_id=schedule.case_id
                )
            )
        )
        completions = store.list_obligation_completions(
            schedule.schedule_id
        )
        self.assertEqual(len(completions), len(items))
        self.assertTrue(
            all(
                item.completion.status
                is ObligationTerminalStatus.TYPED_BOUNDARY
                for item in completions
            )
        )
        checkpoint = (
            store.list_obligation_schedule_checkpoints(
                schedule.schedule_id
            )[-1]
        )
        self.assertEqual(checkpoint.pending_obligation_ids, ())
        self.assertEqual(checkpoint.dispatched_obligation_ids, ())

    def test_parallel_completion_is_order_invariant(self) -> None:
        frame, items = obligations()
        dependencies = tuple(
            ObligationDependency(item.obligation_id, ())
            for item in items
        )
        base = authority(frame)
        dispatches = {
            item.obligation_id: build_obligation_dispatch(
                obligation=item,
                current_authority=base,
            )
            for item in items
        }
        expected_ids = {item.obligation_id for item in items}
        self.assertEqual(
            {
                item.obligation_id
                for item in select_runnable_obligations(
                    obligations=items,
                    dependencies=dependencies,
                    completions=(),
                    current_authority=base,
                )
            },
            expected_ids,
        )

        terminal_sets = []
        for order in itertools.permutations(items):
            completions = ()
            for index, item in enumerate(order, start=1):
                completions = admit_obligation_completion(
                    dispatch=dispatches[item.obligation_id],
                    obligation=item,
                    status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                    result_sha256=content_sha256(
                        {"obligation_id": item.obligation_id}
                    ),
                    current_authority=authority(
                        frame,
                        obligation_version=index - 1,
                    ),
                    prior_completions=completions,
                )
            terminal_sets.append(
                {
                    (item.obligation_id, item.result_sha256)
                    for item in completions
                }
            )
        self.assertTrue(
            all(result == terminal_sets[0] for result in terminal_sets)
        )

    def test_duplicate_is_idempotent_and_conflict_is_rejected(self) -> None:
        frame, items = obligations()
        item = items[0]
        dispatch = build_obligation_dispatch(
            obligation=item,
            current_authority=authority(frame),
        )
        result_sha = content_sha256({"result": "stable"})
        first = admit_obligation_completion(
            dispatch=dispatch,
            obligation=item,
            status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            result_sha256=result_sha,
            current_authority=authority(frame),
            prior_completions=(),
        )
        duplicate = admit_obligation_completion(
            dispatch=dispatch,
            obligation=item,
            status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            result_sha256=result_sha,
            current_authority=authority(
                frame,
                obligation_version=1,
            ),
            prior_completions=first,
        )
        self.assertEqual(duplicate, first)
        with self.assertRaisesRegex(ValueError, "different terminal"):
            admit_obligation_completion(
                dispatch=dispatch,
                obligation=item,
                status=ObligationTerminalStatus.FAILED,
                result_sha256=content_sha256({"result": "different"}),
                current_authority=authority(
                    frame,
                    obligation_version=1,
                ),
                prior_completions=first,
            )

    def test_correction_or_frame_change_fences_completion(self) -> None:
        frame, items = obligations()
        item = items[0]
        dispatch = build_obligation_dispatch(
            obligation=item,
            current_authority=authority(frame),
        )
        with self.assertRaisesRegex(ValueError, "authority is stale"):
            admit_obligation_completion(
                dispatch=dispatch,
                obligation=item,
                status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                result_sha256=content_sha256({"result": "late"}),
                current_authority=replace(
                    authority(frame),
                    mailbox_authority_epoch=2,
                ),
                prior_completions=(),
            )
        with self.assertRaisesRegex(ValueError, "authority is stale"):
            admit_obligation_completion(
                dispatch=dispatch,
                obligation=item,
                status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                result_sha256=content_sha256({"result": "late"}),
                current_authority=replace(
                    authority(frame),
                    accepted_frame_revision_id="frame-new",
                ),
                prior_completions=(),
            )

    def test_dependency_waits_for_successful_prerequisite_execution(
        self,
    ) -> None:
        frame, items = obligations()
        dependencies = (
            ObligationDependency(items[0].obligation_id, ()),
            ObligationDependency(
                items[1].obligation_id,
                (items[0].obligation_id,),
            ),
            ObligationDependency(
                items[2].obligation_id,
                (items[1].obligation_id,),
            ),
        )
        first_runnable = select_runnable_obligations(
            obligations=items,
            dependencies=dependencies,
            completions=(),
            current_authority=authority(frame),
        )
        self.assertEqual(first_runnable, (items[0],))
        dispatch = build_obligation_dispatch(
            obligation=items[0],
            current_authority=authority(frame),
        )
        first_completion = admit_obligation_completion(
            dispatch=dispatch,
            obligation=items[0],
            status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            result_sha256=content_sha256(
                {"result": "execution_succeeded"}
            ),
            current_authority=authority(frame),
            prior_completions=(),
        )
        second_runnable = select_runnable_obligations(
            obligations=items,
            dependencies=dependencies,
            completions=first_completion,
            current_authority=authority(
                frame,
                obligation_version=1,
            ),
        )
        self.assertEqual(second_runnable, (items[1],))

    def test_execution_success_does_not_record_evidence_satisfaction(
        self,
    ) -> None:
        (
            _,
            store,
            scheduler,
            items,
            dependencies,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-execution-only"
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-execution-only",
            causation_id="accepted-frame",
            created_at=NOW,
        )
        authority_before = store.get_authority_snapshot(schedule.case_id)
        completion = scheduler.admit_completion(
            schedule_id=schedule.schedule_id,
            obligation_id=items[0].obligation_id,
            status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            result_sha256=content_sha256({"result": "available"}),
            completed_at=NOW,
        )
        authority_after = store.get_authority_snapshot(schedule.case_id)

        self.assertEqual(
            completion.completion.status,
            ObligationTerminalStatus.EXECUTION_SUCCEEDED,
        )
        self.assertEqual(
            authority_after.obligation_state_version,
            authority_before.obligation_state_version,
        )
        self.assertNotIn(
            JournalEventType.OBLIGATION_SATISFACTION_RECORDED,
            {
                event.event_type
                for event in store.list_events(schedule.case_id)
            },
        )
        completion_event = next(
            event
            for event in store.list_events(schedule.case_id)
            if event.event_type
            is JournalEventType.OBLIGATION_COMPLETION_ADMITTED
        )
        self.assertEqual(
            completion_event.customer_projection,
            {
                "state": "execution_succeeded",
                "obligation_id": items[0].obligation_id,
                "status": "execution_succeeded",
            },
        )

    def test_generic_outbox_cannot_inject_obligation_work(self) -> None:
        (
            _,
            store,
            scheduler,
            _,
            _,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-generic-outbox"
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-generic-outbox",
            causation_id="accepted-frame",
            created_at=NOW,
        )
        message = next(
            item
            for item in store.list_outbox_messages(
                case_id=schedule.case_id
            )
            if item.job_kind is AsyncJobKind.OBLIGATION
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "requires schedule dispatch admission",
        ):
            store.enqueue_outbox(message)
        dispatch_record = store.list_obligation_dispatches(
            schedule.schedule_id
        )[0]
        dispatch_event = next(
            event
            for event in store.list_events(schedule.case_id)
            if event.event_type
            is JournalEventType.OBLIGATION_DISPATCH_ENQUEUED
        )
        self.assertEqual(
            dispatch_event.authority_ref,
            dispatch_record.dispatch_record_id,
        )
        forged_payload = dict(message.payload)
        forged_payload["obligation"] = {
            "forged": "different persisted obligation"
        }
        forged_sha256 = content_sha256(forged_payload)
        forged_message = replace(
            message,
            operation=replace(
                message.operation,
                payload_sha256=forged_sha256,
            ),
            payload=forged_payload,
            payload_sha256=forged_sha256,
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "does not exactly bind",
        ):
            store.record_obligation_dispatch(
                message=forged_message,
                record=dispatch_record,
            )

    def test_terminal_case_fences_obligation_runtime_and_keeps_facts(
        self,
    ) -> None:
        for lifecycle in (
            CaseLifecycle.STOPPED,
            CaseLifecycle.CLOSED,
        ):
            with self.subTest(lifecycle=lifecycle.value):
                case_id = f"case-obligation-{lifecycle.value}"
                (
                    _,
                    store,
                    scheduler,
                    items,
                    _,
                    _,
                ) = accepted_single_obligation_runtime(case_id)
                schedule = scheduler.create_schedule(
                    case_id=case_id,
                    causation_id="accepted-frame",
                    created_at=NOW,
                )
                dispatches_before = store.list_obligation_dispatches(
                    schedule.schedule_id
                )
                checkpoints_before = (
                    store.list_obligation_schedule_checkpoints(
                        schedule.schedule_id
                    )
                )
                events_before = store.list_events(case_id)
                case = store.get_case(case_id)
                store.transition_case_lifecycle(
                    case_id=case_id,
                    lifecycle=lifecycle,
                    expected_head_version=case.head_version,
                    event_id=f"{case_id}:event:{lifecycle.value}",
                    action_id=f"{case_id}:action:{lifecycle.value}",
                    recorded_at=NOW,
                )
                events_after_terminal = store.list_events(case_id)

                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "terminal case fences obligation runtime",
                ):
                    scheduler.resume(
                        schedule_id=schedule.schedule_id,
                        resumed_at=NOW,
                    )
                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "terminal case fences obligation runtime",
                ):
                    scheduler.admit_completion(
                        schedule_id=schedule.schedule_id,
                        obligation_id=items[0].obligation_id,
                        status=(
                            ObligationTerminalStatus.EXECUTION_SUCCEEDED
                        ),
                        result_sha256=content_sha256(
                            {"result": "arrived-after-terminal"}
                        ),
                        completed_at=NOW,
                    )

                self.assertEqual(
                    store.list_obligation_dispatches(
                        schedule.schedule_id
                    ),
                    dispatches_before,
                )
                self.assertEqual(
                    store.list_obligation_schedule_checkpoints(
                        schedule.schedule_id
                    ),
                    checkpoints_before,
                )
                self.assertEqual(
                    store.list_obligation_completions(
                        schedule.schedule_id
                    ),
                    (),
                )
                self.assertEqual(
                    store.list_events(case_id),
                    events_after_terminal,
                )
                self.assertEqual(
                    len(events_after_terminal),
                    len(events_before) + 1,
                )

    def test_worker_cannot_turn_executable_work_into_typed_boundary(
        self,
    ) -> None:
        frame, items = obligations()
        dispatch = build_obligation_dispatch(
            obligation=items[0],
            current_authority=authority(frame),
        )
        with self.assertRaisesRegex(ValueError, "incompatible"):
            admit_obligation_completion(
                dispatch=dispatch,
                obligation=items[0],
                status=ObligationTerminalStatus.TYPED_BOUNDARY,
                result_sha256=content_sha256(
                    {"forged": "typed-boundary"}
                ),
                current_authority=authority(frame),
                prior_completions=(),
            )

    def test_unadopted_typed_boundary_cannot_change_schedule(
        self,
    ) -> None:
        (
            _,
            store,
            scheduler,
            items,
            _,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-typed-boundary"
        )
        boundary = replace(
            items[0],
            execution_disposition=(
                ObligationExecutionDisposition.TYPED_BOUNDARY
            ),
            boundary_code="incomplete_period",
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-typed-boundary",
            causation_id="accepted-frame",
            created_at=NOW,
        )

        completions = store.list_obligation_completions(
            schedule.schedule_id
        )
        self.assertEqual(completions, ())
        self.assertEqual(
            schedule.obligations,
            items,
        )
        self.assertNotEqual(schedule.obligations[0], boundary)
        self.assertEqual(
            len(
                store.list_obligation_dispatches(
                    schedule.schedule_id
                )
            ),
            1,
        )

    def test_failed_prerequisite_closes_all_dependents(self) -> None:
        frame, items = obligations()
        dependencies = (
            ObligationDependency(items[0].obligation_id, ()),
            ObligationDependency(
                items[1].obligation_id,
                (items[0].obligation_id,),
            ),
            ObligationDependency(
                items[2].obligation_id,
                (items[1].obligation_id,),
            ),
        )
        dispatch = build_obligation_dispatch(
            obligation=items[0],
            current_authority=authority(frame),
        )
        first = admit_obligation_completion(
            dispatch=dispatch,
            obligation=items[0],
            status=ObligationTerminalStatus.FAILED,
            result_sha256=content_sha256({"failure": "source"}),
            current_authority=authority(frame),
            prior_completions=(),
        )
        terminal = propagate_dependency_terminals(
            obligations=items,
            dependencies=dependencies,
            completions=first,
        )
        self.assertEqual(len(terminal), 3)
        self.assertEqual(
            {item.status for item in terminal},
            {ObligationTerminalStatus.FAILED},
        )
        self.assertEqual(
            select_runnable_obligations(
                obligations=items,
                dependencies=dependencies,
                completions=terminal,
                current_authority=authority(
                    frame,
                    obligation_version=3,
                ),
            ),
            (),
        )

    def test_durable_failed_prerequisite_persists_terminal_fan_in(
        self,
    ) -> None:
        (
            _,
            store,
            scheduler,
            items,
            _,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-failed-fan-in",
            obligation_count=3,
        )
        dependencies = (
            ObligationDependency(items[0].obligation_id, ()),
            ObligationDependency(
                items[1].obligation_id,
                (items[0].obligation_id,),
            ),
            ObligationDependency(
                items[2].obligation_id,
                (items[1].obligation_id,),
            ),
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-failed-fan-in",
            causation_id="accepted-frame",
            created_at=NOW,
        )
        scheduler.admit_completion(
            schedule_id=schedule.schedule_id,
            obligation_id=items[0].obligation_id,
            status=ObligationTerminalStatus.FAILED,
            result_sha256=content_sha256(
                {"failure": "capability-terminal"}
            ),
            completed_at=NOW,
        )
        completions = store.list_obligation_completions(
            schedule.schedule_id
        )
        self.assertEqual(len(completions), 3)
        self.assertEqual(
            {
                item.completion.status
                for item in completions
            },
            {ObligationTerminalStatus.FAILED},
        )
        checkpoint = (
            store.list_obligation_schedule_checkpoints(
                schedule.schedule_id
            )[-1]
        )
        self.assertEqual(checkpoint.pending_obligation_ids, ())
        self.assertEqual(checkpoint.dispatched_obligation_ids, ())

    def test_storage_rejects_forged_system_prerequisite_terminal(
        self,
    ) -> None:
        (
            _,
            store,
            scheduler,
            items,
            dependencies,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-forged-system-terminal",
            obligation_count=3,
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-forged-system-terminal",
            causation_id="accepted-frame",
            created_at=NOW,
        )
        forged = ObligationCompletionRecord(
            completion_record_id="forged-system-terminal",
            schedule_id=schedule.schedule_id,
            completion=ObligationCompletion(
                obligation_id=items[0].obligation_id,
                dispatch_id=(
                    f"system-prerequisite:{items[0].obligation_id}"
                ),
                status=ObligationTerminalStatus.TYPED_BOUNDARY,
                result_sha256=content_sha256(
                    {"forged": "root-has-no-prerequisite"}
                ),
            ),
            admitted_authority_snapshot_sha256=(
                store.get_authority_snapshot(
                    schedule.case_id
                ).content_sha256
            ),
            created_at=NOW,
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "not graph-derived",
        ):
            store.record_obligation_completion(forged)

    def test_durable_scheduler_recovers_and_fans_in_without_duplicate_work(
        self,
    ) -> None:
        case_id = "case-durable-obligations"
        proposal = frame_proposal(case_id)
        design = proposal.payload.measurement_design
        requirement = design.evidence_requirements[0]
        requirements = tuple(
            replace(
                requirement,
                evidence_requirement_id=f"requirement-durable-{index}",
                required_evidence_type_refs=(
                    f"evidence-type:durable:{index}",
                ),
            )
            for index in range(1, 4)
        )
        estimand = replace(
            design.estimands[0],
            evidence_requirement_ids=tuple(
                item.evidence_requirement_id for item in requirements
            ),
        )
        completed_design = replace(
            design,
            evidence_requirements=requirements,
            estimands=(estimand,),
            completion_specs=(
                replace(
                    design.completion_specs[0],
                    required_evidence_requirement_ids=(
                        estimand.evidence_requirement_ids
                    ),
                ),
            ),
        )
        proposal = replace(
            proposal,
            payload=replace(
                proposal.payload,
                measurement_design=completed_design,
            ),
        )
        provider = ScriptedPrimaryAgentProvider((proposal,))
        store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        controller = WAJEController(
            store=store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="authority-worker",
            clock=lambda: NOW,
        )
        controller.start(
            case_id=case_id,
            thread_id="thread-durable-obligations",
            run_id="run-durable-obligations",
            user_message="调查一组有依赖关系的证据问题",
        )
        controller.deliver_pending_message_binding(case_id)
        controller.advance(case_id)
        controller.deliver_pending_llm(case_id)
        controller.deliver_pending_frame_review(case_id)
        accepted_frame_id = store.get_case(
            case_id
        ).accepted_frame_revision_id
        assert accepted_frame_id is not None
        frame = store.get_frame(accepted_frame_id)
        case = store.get_case(case_id)
        _, _, items = record_measurement_authority(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            correlation_id="run-durable-obligations",
        )
        task_keys = tuple(
            f"durable-task-{index}" for index in range(len(items))
        )
        proposed_tasks = tuple(
            ProposedWorkTask(
                proposal_task_key=task_keys[index],
                business_purpose="Run durable dependent evidence work",
                capability_intent_ref=(
                    "waje-vnext://capability-intent/"
                    "measurement-evidence.v1"
                ),
                obligation_ids=(item.obligation_id,),
                depends_on_task_keys=(
                    ()
                    if index == 0
                    else (task_keys[index - 1],)
                ),
            )
            for index, item in enumerate(items)
        )
        case, _ = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id=f"{case_id}:plan:1",
            proposed_tasks=proposed_tasks,
            correlation_id="run-durable-obligations",
        )
        dependencies = (
            ObligationDependency(items[0].obligation_id, ()),
            ObligationDependency(
                items[1].obligation_id,
                (items[0].obligation_id,),
            ),
            ObligationDependency(
                items[2].obligation_id,
                (items[1].obligation_id,),
            ),
        )
        scheduler = DurableObligationCoordinator(
            store=store,
            owner_id="obligation-worker",
        )
        schedule = scheduler.create_schedule(
            case_id=case_id,
            causation_id="accepted-frame",
            created_at=NOW,
        )
        self.assertEqual(
            schedule.correlation_id,
            "run-durable-obligations",
        )
        first_pending = tuple(
            message
            for message in store.list_pending_outbox_messages(
                case_id=case_id
            )
            if message.job_kind is AsyncJobKind.OBLIGATION
        )
        self.assertEqual(len(first_pending), 1)
        self.assertEqual(
            first_pending[0].payload["obligation_id"],
            items[0].obligation_id,
        )

        restarted = DurableObligationCoordinator(
            store=store,
            owner_id="obligation-worker-restarted",
        )
        restarted.resume(
            schedule_id=schedule.schedule_id,
            resumed_at=NOW,
        )
        self.assertEqual(
            len(
                tuple(
                    message
                    for message in store.list_pending_outbox_messages(
                        case_id=case_id
                    )
                    if message.job_kind is AsyncJobKind.OBLIGATION
                )
            ),
            1,
        )
        with self.assertRaisesRegex(ValueError, "unknown obligation"):
            restarted.admit_completion(
                schedule_id=schedule.schedule_id,
                obligation_id="obligation-unknown",
                status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                result_sha256=content_sha256({"result": "unknown"}),
                completed_at=NOW,
            )
        with self.assertRaisesRegex(ValueError, "before durable dispatch"):
            restarted.admit_completion(
                schedule_id=schedule.schedule_id,
                obligation_id=items[1].obligation_id,
                status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                result_sha256=content_sha256(
                    {"result": "arrived-before-dispatch"}
                ),
                completed_at=NOW,
            )
        for item in items:
            restarted.admit_completion(
                schedule_id=schedule.schedule_id,
                obligation_id=item.obligation_id,
                status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                result_sha256=content_sha256(
                    {"obligation_id": item.obligation_id}
                ),
                completed_at=NOW,
            )
        final_checkpoint = (
            store.list_obligation_schedule_checkpoints(
                schedule.schedule_id
            )[-1]
        )
        self.assertEqual(
            set(final_checkpoint.completed_obligation_ids),
            {item.obligation_id for item in items},
        )
        self.assertEqual(final_checkpoint.pending_obligation_ids, ())
        self.assertEqual(final_checkpoint.dispatched_obligation_ids, ())

    def test_schedule_and_first_dispatch_share_one_atomic_commit(self) -> None:
        (
            _,
            store,
            _,
            items,
            dependencies,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-create-crash"
        )

        class CrashBeforeDispatchCoordinator(
            DurableObligationCoordinator
        ):
            def resume(self, *, schedule_id, resumed_at):
                raise RuntimeError("simulated crash before first dispatch")

        scheduler = CrashBeforeDispatchCoordinator(
            store=store,
            owner_id="crashing-obligation-worker",
        )
        with self.assertRaisesRegex(RuntimeError, "before first dispatch"):
            scheduler.create_schedule(
                case_id="case-obligation-create-crash",
                causation_id="accepted-frame",
                created_at=NOW,
            )
        self.assertFalse(
            any(
                event.event_type
                is JournalEventType.OBLIGATION_SCHEDULE_CREATED
                for event in store.list_events(
                    "case-obligation-create-crash"
                )
            )
        )
        self.assertFalse(
            any(
                message.job_kind is AsyncJobKind.OBLIGATION
                for message in store.list_outbox_messages(
                    case_id="case-obligation-create-crash"
                )
            )
        )

    def test_completion_and_dependent_resume_share_atomic_commit(self) -> None:
        class FailNextCheckpointStore(InMemoryAuthorityStore):
            def __init__(self) -> None:
                super().__init__(
                    resolution_input_verifier=make_trusted_verifier()
                )
                self.fail_next_checkpoint = False

            def record_obligation_schedule_checkpoint(self, checkpoint):
                if self.fail_next_checkpoint:
                    self.fail_next_checkpoint = False
                    raise RuntimeError(
                        "simulated crash before dependent checkpoint"
                    )
                return super().record_obligation_schedule_checkpoint(
                    checkpoint
                )

        store = FailNextCheckpointStore()
        (
            _,
            _,
            scheduler,
            items,
            dependencies,
            _,
        ) = accepted_single_obligation_runtime(
            "case-obligation-completion-crash",
            store=store,
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-completion-crash",
            causation_id="accepted-frame",
            created_at=NOW,
        )
        dispatch = store.list_obligation_dispatches(
            schedule.schedule_id
        )[0]
        store.fail_next_checkpoint = True
        with self.assertRaisesRegex(
            RuntimeError,
            "before dependent checkpoint",
        ):
            scheduler.admit_completion(
                schedule_id=schedule.schedule_id,
                obligation_id=items[0].obligation_id,
                status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                result_sha256=content_sha256({"result": "first"}),
                completed_at=NOW,
            )
        self.assertEqual(
            store.list_obligation_completions(schedule.schedule_id),
            (),
        )
        self.assertIsNone(
            store.get_job_disposition(dispatch.outbox_message_id)
        )
        scheduler.admit_completion(
            schedule_id=schedule.schedule_id,
            obligation_id=items[0].obligation_id,
            status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            result_sha256=content_sha256({"result": "first"}),
            completed_at=NOW,
        )
        self.assertEqual(
            len(store.list_obligation_completions(schedule.schedule_id)),
            1,
        )

    def test_authority_change_terminally_supersedes_schedule(self) -> None:
        (
            controller,
            store,
            scheduler,
            items,
            dependencies,
            run_id,
        ) = accepted_single_obligation_runtime(
            "case-obligation-superseded"
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-superseded",
            causation_id="accepted-frame",
            created_at=NOW,
        )
        self.assertEqual(schedule.correlation_id, run_id)
        dispatch = store.list_obligation_dispatches(
            schedule.schedule_id
        )[0]
        controller.ingress_message(
            case_id="case-obligation-superseded",
            thread_id="thread-case-obligation-superseded",
            run_id=run_id,
            user_message="改用新的业务口径重新测量。",
            idempotency_key="superseding-correction",
        )
        checkpoint = scheduler.resume(
            schedule_id=schedule.schedule_id,
            resumed_at=NOW,
        )
        completions = store.list_obligation_completions(
            schedule.schedule_id
        )
        self.assertEqual(
            {item.completion.status for item in completions},
            {ObligationTerminalStatus.SUPERSEDED},
        )
        self.assertEqual(checkpoint.pending_obligation_ids, ())
        self.assertEqual(
            store.get_job_disposition(
                dispatch.outbox_message_id
            ).disposition.value,
            "superseded",
        )
        self.assertNotIn(
            dispatch.outbox_message_id,
            {
                item.outbox_message_id
                for item in store.list_pending_outbox_messages(
                    case_id="case-obligation-superseded"
                )
            },
        )

    def test_late_worker_completion_closes_stale_schedule_itself(
        self,
    ) -> None:
        (
            controller,
            store,
            scheduler,
            items,
            dependencies,
            run_id,
        ) = accepted_single_obligation_runtime(
            "case-obligation-late-worker"
        )
        schedule = scheduler.create_schedule(
            case_id="case-obligation-late-worker",
            causation_id="accepted-frame",
            created_at=NOW,
        )
        dispatch = store.list_obligation_dispatches(
            schedule.schedule_id
        )[0]
        controller.ingress_message(
            case_id="case-obligation-late-worker",
            thread_id="thread-case-obligation-late-worker",
            run_id=run_id,
            user_message="更正业务口径，旧结果不要进入答案。",
            idempotency_key="late-worker-correction",
        )
        completion = scheduler.admit_completion(
            schedule_id=schedule.schedule_id,
            obligation_id=items[0].obligation_id,
            status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            result_sha256=content_sha256({"late": "worker-result"}),
            completed_at=NOW,
        )
        self.assertIs(
            completion.completion.status,
            ObligationTerminalStatus.SUPERSEDED,
        )
        self.assertEqual(
            store.list_obligation_schedule_checkpoints(
                schedule.schedule_id
            )[-1].pending_obligation_ids,
            (),
        )
        self.assertEqual(
            store.get_job_disposition(
                dispatch.outbox_message_id
            ).disposition.value,
            "superseded",
        )
        replay = scheduler.admit_completion(
            schedule_id=schedule.schedule_id,
            obligation_id=items[0].obligation_id,
            status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            result_sha256=content_sha256({"late": "worker-result"}),
            completed_at=NOW,
        )
        self.assertEqual(replay, completion)
        with self.assertRaisesRegex(ValueError, "different terminal"):
            scheduler.admit_completion(
                schedule_id=schedule.schedule_id,
                obligation_id=items[0].obligation_id,
                status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
                result_sha256=content_sha256(
                    {"late": "different-worker-result"}
                ),
                completed_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
