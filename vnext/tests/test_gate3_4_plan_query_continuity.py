from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta

from gate1_fixtures import (
    NOW,
    accept_initial_question,
    make_frame,
    make_measurement_design,
    make_operation,
    record_reviewed_frame,
)
from gate3_plan_fixtures import (
    record_measurement_authority,
    record_plan_bundle,
)
from test_gate3_3_measurement_resolver import (
    make_request,
    make_trusted_verifier,
)
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    MailboxMessageKind,
    OperationIdentity,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.planning import (
    CAPABILITY_INTENT_REGISTRY,
    CAPABILITY_INTENT_REGISTRY_SHA256,
    CAPABILITY_INTENT_REGISTRY_VERSION,
    ProposedWorkTask,
    build_conformance_execution_spec,
    build_logical_execution_attempt,
    compile_plan_bundle,
    same_business_authority,
    validate_plan_bundle,
)
from waje_vnext.domain.measurement import (
    ClaimStrengthCeiling,
    EvidenceComposition,
    FalsificationSpec,
    RequirementBoundaryPolicy,
    ReversalSpec,
    SensitivitySpec,
)
from waje_vnext.domain.runtime_state import OutboxMessage
from waje_vnext.storage.in_memory import InMemoryAuthorityStore
from waje_vnext.storage.ports import (
    AuthorityConflict,
    AuthorityNotFound,
    InvalidAuthorityTransition,
    StaleHead,
)

class Gate34PlanQueryContinuityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = self.store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(self.store, case)
        self.frame = make_frame(question=question)
        proof_id = record_reviewed_frame(self.store, self.frame)
        self.case = self.store.accept_frame(
            self.frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-frame",
            recorded_at=self.frame.created_at,
        )
        self.case, self.bundle = record_plan_bundle(
            store=self.store,
            case=self.case,
            frame=self.frame,
            created_at=NOW,
        )

    def test_plan_closes_exact_frame_obligations(self) -> None:
        obligations = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )
        adopted = {
            obligation_id
            for task in self.bundle.plan.tasks
            for obligation_id in task.obligation_ids
        }
        self.assertEqual(
            adopted,
            {item.obligation_id for item in obligations},
        )
        self.assertEqual(
            self.bundle.adoption.obligation_ids,
            tuple(item.obligation_id for item in obligations),
        )
        self.assertTrue(self.bundle.query_bindings)
        self.assertEqual(
            self.bundle.adoption.capability_intent_registry_version,
            CAPABILITY_INTENT_REGISTRY_VERSION,
        )
        self.assertEqual(
            self.bundle.adoption.capability_intent_registry_sha256,
            CAPABILITY_INTENT_REGISTRY_SHA256,
        )

    def test_multiple_evidence_slots_allow_grouped_and_split_plans(
        self,
    ) -> None:
        store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = store.open_case(
            case_id="case-multi-slot",
            thread_id="thread-multi-slot",
            event_id="event-multi-slot-open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(store, case)
        design = make_measurement_design(
            question_id=question.question_revision_id,
            source_message_id=question.source_messages[0].message_id,
            source_text=question.source_messages[0].content,
        )
        requirement = replace(
            design.evidence_requirements[0],
            required_evidence_type_refs=(
                "evidence:primary-estimate",
                "evidence:independent-reconciliation",
                "evidence:robustness-check",
            ),
        )
        frame = make_frame(
            case_id=case.case_id,
            question=question,
            frame_id="frame-multi-slot",
            measurement_design=replace(
                design,
                evidence_requirements=(requirement,),
            ),
        )
        proof_id = record_reviewed_frame(store, frame)
        case = store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-multi-slot-frame",
            recorded_at=NOW,
        )
        _, _, obligations = record_measurement_authority(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
        )
        self.assertEqual(len(obligations), 3)
        case, grouped = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id="plan-multi-slot-grouped",
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="collect-together",
                    business_purpose=(
                        "Collect compatible evidence in one investigation"
                    ),
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=tuple(
                        item.obligation_id for item in obligations
                    ),
                    depends_on_task_keys=(),
                ),
            ),
        )
        self.assertEqual(len(grouped.plan.tasks), 1)
        self.assertEqual(len(grouped.query_bindings), 3)
        case, split = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id="plan-multi-slot-split",
            prior_plan=grouped.plan,
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="collect-primary",
                    business_purpose="Collect the primary estimate",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(obligations[0].obligation_id,),
                    depends_on_task_keys=(),
                ),
                ProposedWorkTask(
                    proposal_task_key="collect-reconciliation",
                    business_purpose="Collect independent reconciliation",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(obligations[1].obligation_id,),
                    depends_on_task_keys=("collect-primary",),
                ),
                ProposedWorkTask(
                    proposal_task_key="collect-robustness",
                    business_purpose="Collect the robustness check",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(obligations[2].obligation_id,),
                    depends_on_task_keys=("collect-reconciliation",),
                ),
            ),
        )
        self.assertEqual(len(split.plan.tasks), 3)
        self.assertNotEqual(
            grouped.plan.plan_revision_id,
            split.plan.plan_revision_id,
        )
        self.assertEqual(
            grouped.plan.frame_revision_id,
            split.plan.frame_revision_id,
        )
        case, parallel = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id="plan-multi-slot-parallel",
            prior_plan=split.plan,
            proposed_tasks=tuple(
                ProposedWorkTask(
                    proposal_task_key=f"parallel-{index}",
                    business_purpose="Collect one independent evidence slot",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(obligation.obligation_id,),
                    depends_on_task_keys=(),
                )
                for index, obligation in enumerate(obligations, start=1)
            ),
        )
        self.assertTrue(
            all(not task.depends_on_task_ids for task in parallel.plan.tasks)
        )
        case, forward_declared = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id="plan-multi-slot-forward-declared",
            prior_plan=parallel.plan,
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="third",
                    business_purpose="Close the final dependent evidence slot",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(obligations[2].obligation_id,),
                    depends_on_task_keys=("second",),
                ),
                ProposedWorkTask(
                    proposal_task_key="second",
                    business_purpose="Close the middle evidence slot",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(obligations[1].obligation_id,),
                    depends_on_task_keys=("first",),
                ),
                ProposedWorkTask(
                    proposal_task_key="first",
                    business_purpose="Close the root evidence slot",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(obligations[0].obligation_id,),
                    depends_on_task_keys=(),
                ),
            ),
        )
        self.assertEqual(len(forward_declared.plan.tasks), 3)
        invalid_vectors = (
            (
                "cycle",
                (
                    ProposedWorkTask(
                        proposal_task_key="cycle-a",
                        business_purpose="Cycle A",
                        capability_intent_ref=(
                            "waje-vnext://capability-intent/"
                            "measurement-evidence.v1"
                        ),
                        obligation_ids=(obligations[0].obligation_id,),
                        depends_on_task_keys=("cycle-b",),
                    ),
                    ProposedWorkTask(
                        proposal_task_key="cycle-b",
                        business_purpose="Cycle B",
                        capability_intent_ref=(
                            "waje-vnext://capability-intent/"
                            "measurement-evidence.v1"
                        ),
                        obligation_ids=(
                            obligations[1].obligation_id,
                            obligations[2].obligation_id,
                        ),
                        depends_on_task_keys=("cycle-a",),
                    ),
                ),
                "acyclic",
            ),
            (
                "unknown",
                (
                    ProposedWorkTask(
                        proposal_task_key="known",
                        business_purpose="Unknown dependency",
                        capability_intent_ref=(
                            "waje-vnext://capability-intent/"
                            "measurement-evidence.v1"
                        ),
                        obligation_ids=tuple(
                            item.obligation_id for item in obligations
                        ),
                        depends_on_task_keys=("missing-task",),
                    ),
                ),
                "unknown dependencies",
            ),
            (
                "duplicate-owner",
                (
                    ProposedWorkTask(
                        proposal_task_key="owner-a",
                        business_purpose="First owner",
                        capability_intent_ref=(
                            "waje-vnext://capability-intent/"
                            "measurement-evidence.v1"
                        ),
                        obligation_ids=tuple(
                            item.obligation_id for item in obligations
                        ),
                        depends_on_task_keys=(),
                    ),
                    ProposedWorkTask(
                        proposal_task_key="owner-b",
                        business_purpose="Second owner",
                        capability_intent_ref=(
                            "waje-vnext://capability-intent/"
                            "measurement-evidence.v1"
                        ),
                        obligation_ids=(obligations[0].obligation_id,),
                        depends_on_task_keys=(),
                    ),
                ),
                "duplicate obligation ownership",
            ),
        )
        for label, proposed_tasks, message in invalid_vectors:
            with self.subTest(invalid_topology=label):
                with self.assertRaisesRegex(ValueError, message):
                    record_plan_bundle(
                        store=store,
                        case=case,
                        frame=frame,
                        created_at=NOW,
                        plan_revision_id=f"plan-invalid-{label}",
                        prior_plan=forward_declared.plan,
                        proposed_tasks=proposed_tasks,
                    )

    def test_all_boundary_plan_has_no_query_binding(self) -> None:
        store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = store.open_case(
            case_id="case-all-boundary",
            thread_id="thread-all-boundary",
            event_id="event-all-boundary-open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(store, case)
        frame = make_frame(
            case_id=case.case_id,
            question=question,
            frame_id="frame-all-boundary",
        )
        proof_id = record_reviewed_frame(store, frame)
        case = store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-all-boundary-frame",
            recorded_at=NOW,
        )
        estimand_id = frame.measurement_design.estimands[0].estimand_id
        short_request = make_request(
            frame,
            anchor=date(2026, 6, 1),
            expected="7",
            observed="6",
            valid="6",
            invalid="0",
            missing="1",
        )
        outcomes, admissions, obligations = (
            record_measurement_authority(
                store=store,
                case=case,
                frame=frame,
                created_at=NOW,
                resolution_requests_by_estimand_id={
                    estimand_id: short_request,
                },
            )
        )
        bundle = compile_plan_bundle(
            case=case,
            authority_snapshot=store.get_authority_snapshot(
                case.case_id
            ),
            frame=frame,
            outcomes=outcomes,
            admissions=admissions,
            obligations=obligations,
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="record-boundary",
                    business_purpose=(
                        "Record the accepted measurement boundary"
                    ),
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "boundary-inspection.v1"
                    ),
                    obligation_ids=tuple(
                        item.obligation_id for item in obligations
                    ),
                    depends_on_task_keys=(),
                ),
            ),
            plan_revision_id="plan-all-boundary",
            revision_number=1,
            prior_plan_revision_id=None,
            created_by_action_id="action-plan-all-boundary",
            created_at=NOW,
            revision_reason="Preserve typed boundaries without querying",
        )
        self.assertEqual(bundle.query_bindings, ())
        self.assertTrue(bundle.plan.tasks)
        self.assertTrue(
            all(not task.query_binding_ids for task in bundle.plan.tasks)
        )
        accepted = store.accept_plan_bundle(
            bundle,
            expected_head_version=case.head_version,
            event_id="event-all-boundary-plan",
            recorded_at=NOW,
        )
        self.assertEqual(
            accepted.accepted_plan_revision_id,
            bundle.plan.plan_revision_id,
        )
        self.assertEqual(
            store.list_query_bindings(bundle.plan.plan_revision_id),
            (),
        )
        self.assertEqual(
            store.get_plan_adoption(bundle.plan.plan_revision_id),
            bundle.adoption,
        )

    def test_any_and_at_least_requirements_plan_all_candidate_slots(
        self,
    ) -> None:
        vectors = (
            (EvidenceComposition.ANY, None, 2),
            (EvidenceComposition.AT_LEAST, 2, 3),
        )
        for composition, minimum_count, slot_count in vectors:
            with self.subTest(composition=composition.value):
                case_id = f"case-composition-{composition.value}"
                store = InMemoryAuthorityStore(
                    resolution_input_verifier=make_trusted_verifier()
                )
                case = store.open_case(
                    case_id=case_id,
                    thread_id=f"thread-{composition.value}",
                    event_id=f"event-open-{composition.value}",
                    opened_at=NOW,
                )
                case, question = accept_initial_question(store, case)
                design = make_measurement_design(
                    question_id=question.question_revision_id,
                    source_message_id=question.source_messages[0].message_id,
                    source_text=question.source_messages[0].content,
                )
                requirement = replace(
                    design.evidence_requirements[0],
                    required_evidence_type_refs=tuple(
                        f"evidence:candidate:{index}"
                        for index in range(1, slot_count + 1)
                    ),
                    composition=composition,
                    minimum_count=minimum_count,
                )
                frame = make_frame(
                    case_id=case_id,
                    question=question,
                    frame_id=f"frame-{composition.value}",
                    measurement_design=replace(
                        design,
                        evidence_requirements=(requirement,),
                    ),
                )
                proof_id = record_reviewed_frame(store, frame)
                case = store.accept_frame(
                    frame,
                    frame_admission_proof_id=proof_id,
                    expected_head_version=case.head_version,
                    event_id=f"event-frame-{composition.value}",
                    recorded_at=NOW,
                )
                _, _, obligations = record_measurement_authority(
                    store=store,
                    case=case,
                    frame=frame,
                    created_at=NOW,
                )
                case, bundle = record_plan_bundle(
                    store=store,
                    case=case,
                    frame=frame,
                    created_at=NOW,
                    plan_revision_id=f"plan-{composition.value}",
                    proposed_tasks=(
                        ProposedWorkTask(
                            proposal_task_key="collect-candidates",
                            business_purpose=(
                                "Collect governed candidate evidence"
                            ),
                            capability_intent_ref=(
                                "waje-vnext://capability-intent/"
                                "measurement-evidence.v1"
                            ),
                            obligation_ids=tuple(
                                item.obligation_id
                                for item in obligations
                            ),
                            depends_on_task_keys=(),
                        ),
                    ),
                )
                self.assertEqual(len(obligations), slot_count)
                self.assertEqual(
                    len(bundle.query_bindings),
                    slot_count,
                )
                self.assertEqual(
                    set(bundle.adoption.obligation_ids),
                    {
                        item.obligation_id
                        for item in obligations
                    },
                )

    def test_nondefault_frame_authority_projects_field_for_field(
        self,
    ) -> None:
        store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = store.open_case(
            case_id="case-positive-projection-oracle",
            thread_id="thread-positive-projection-oracle",
            event_id="event-positive-projection-open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(store, case)
        design = make_measurement_design(
            question_id=question.question_revision_id,
            source_message_id=question.source_messages[0].message_id,
            source_text=question.source_messages[0].content,
        )
        estimand_id = design.estimands[0].estimand_id
        requirement_id = design.evidence_requirements[
            0
        ].evidence_requirement_id
        projected_scope = replace(
            design.scopes[0],
            scope_id="scope:positive-projection",
            predicate_ref="predicate:positive-projection",
        )
        falsification = FalsificationSpec(
            falsification_id="falsification:positive-projection",
            observable_condition_ref="condition:placebo-window",
            evidence_requirement_ids=(requirement_id,),
            disposition_policy_ref="disposition:degrade-on-failure",
        )
        reversal = ReversalSpec(
            reversal_id="reversal:positive-projection",
            result_condition_ref="condition:direction-reverses",
            affected_estimand_ids=(estimand_id,),
            direction_change_ref="direction:reverse",
        )
        requirement = replace(
            design.evidence_requirements[0],
            required_evidence_type_refs=(
                "evidence:primary",
                "evidence:reconciliation",
                "evidence:robustness",
            ),
            composition=EvidenceComposition.AT_LEAST,
            minimum_count=2,
            minimum_strength=ClaimStrengthCeiling.ACCOUNTING,
            scope_id=projected_scope.scope_id,
            contradiction_policy_ref=(
                "contradiction:positive-projection"
            ),
            boundary_policy=RequirementBoundaryPolicy.BLOCK,
            allowed_boundary_codes=(),
            linked_falsification_ids=(
                falsification.falsification_id,
            ),
            linked_reversal_ids=(reversal.reversal_id,),
        )
        estimand = replace(
            design.estimands[0],
            scope_ceiling_id=projected_scope.scope_id,
            claim_strength_ceiling=(
                ClaimStrengthCeiling.ASSOCIATIONAL
            ),
            falsification_ids=(falsification.falsification_id,),
            reversal_ids=(reversal.reversal_id,),
        )
        frame = make_frame(
            case_id=case.case_id,
            question=question,
            frame_id="frame-positive-projection-oracle",
            measurement_design=replace(
                design,
                scopes=(projected_scope,),
                evidence_requirements=(requirement,),
                falsifications=(falsification,),
                reversals=(reversal,),
                estimands=(estimand,),
            ),
        )
        proof_id = record_reviewed_frame(store, frame)
        case = store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-positive-projection-frame",
            recorded_at=NOW,
        )
        _, _, obligations = record_measurement_authority(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
        )
        case, bundle = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id="plan-positive-projection-oracle",
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="collect-projection-oracle",
                    business_purpose="Collect the governed evidence slots",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=tuple(
                        item.obligation_id for item in obligations
                    ),
                    depends_on_task_keys=(),
                ),
            ),
        )
        self.assertEqual(len(bundle.query_bindings), 3)
        for binding in bundle.query_bindings:
            with self.subTest(
                evidence_type=(
                    binding.requirement_binding
                    .obligation_evidence_type_refs[0]
                )
            ):
                measurement = binding.measurement_binding
                projected = binding.requirement_binding
                self.assertEqual(
                    measurement.claim_strength_ceiling,
                    ClaimStrengthCeiling.ASSOCIATIONAL,
                )
                self.assertEqual(
                    measurement.scope_ceiling_id,
                    "scope:positive-projection",
                )
                self.assertEqual(
                    measurement.falsification_ids,
                    ("falsification:positive-projection",),
                )
                self.assertEqual(
                    measurement.reversal_ids,
                    ("reversal:positive-projection",),
                )
                self.assertEqual(
                    projected.composition,
                    EvidenceComposition.AT_LEAST,
                )
                self.assertEqual(projected.minimum_count, 2)
                self.assertEqual(
                    projected.minimum_strength,
                    ClaimStrengthCeiling.ACCOUNTING,
                )
                self.assertEqual(
                    projected.scope_id,
                    "scope:positive-projection",
                )
                self.assertEqual(
                    projected.contradiction_policy_ref,
                    "contradiction:positive-projection",
                )
                self.assertEqual(
                    projected.boundary_policy,
                    RequirementBoundaryPolicy.BLOCK,
                )
                self.assertEqual(projected.allowed_boundary_codes, ())
                self.assertEqual(
                    projected.linked_falsification_ids,
                    ("falsification:positive-projection",),
                )
                self.assertEqual(
                    projected.linked_reversal_ids,
                    ("reversal:positive-projection",),
                )
        self.assertEqual(
            store.list_query_bindings(bundle.plan.plan_revision_id),
            bundle.query_bindings,
        )

    def test_multiple_executable_estimands_keep_distinct_bindings(
        self,
    ) -> None:
        store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = store.open_case(
            case_id="case-multi-executable",
            thread_id="thread-multi-executable",
            event_id="event-multi-executable-open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(store, case)
        design = make_measurement_design(
            question_id=question.question_revision_id,
            source_message_id=question.source_messages[0].message_id,
            source_text=question.source_messages[0].content,
        )
        first_estimand = design.estimands[0]
        first_contrast = design.contrasts[0]
        second_estimand_id = "estimand-second-executable"
        second_variable = replace(
            design.variables[0],
            variable_id="variable-second-executable",
            concept_ref="metric:second-executable:v1",
            expression_contract_ref="metric:second-executable:v1",
        )
        second_metric = replace(
            design.metric_expressions[0],
            metric_expression_id="metric-second-executable",
            output_variable_id=second_variable.variable_id,
            numerator_variable_ids=(second_variable.variable_id,),
        )
        second_estimator = replace(
            design.estimators[0],
            estimator_id="estimator-second-executable",
            metric_expression_id=second_metric.metric_expression_id,
        )
        shared_requirement = replace(
            design.evidence_requirements[0],
            target_estimand_ids=(
                first_estimand.estimand_id,
                second_estimand_id,
            ),
        )
        second_estimand = replace(
            first_estimand,
            estimand_id=second_estimand_id,
            variable_ids=(second_variable.variable_id,),
            estimator_id=second_estimator.estimator_id,
            evidence_requirement_ids=(
                shared_requirement.evidence_requirement_id,
            ),
        )
        second_completion = replace(
            design.completion_specs[0],
            completion_spec_id="completion-second-executable",
            target_estimand_ids=(second_estimand_id,),
        )
        frame = make_frame(
            case_id=case.case_id,
            question=question,
            frame_id="frame-multi-executable",
            measurement_design=replace(
                design,
                variables=(
                    *design.variables,
                    second_variable,
                ),
                metric_expressions=(
                    *design.metric_expressions,
                    second_metric,
                ),
                estimators=(
                    *design.estimators,
                    second_estimator,
                ),
                evidence_requirements=(shared_requirement,),
                completion_specs=(
                    design.completion_specs[0],
                    second_completion,
                ),
                estimands=(first_estimand, second_estimand),
            ),
        )
        proof_id = record_reviewed_frame(store, frame)
        case = store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-multi-executable-frame",
            recorded_at=NOW,
        )
        outcomes, admissions, obligations = record_measurement_authority(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            resolution_requests_by_estimand_id={
                first_estimand.estimand_id: make_request(
                    frame,
                    anchor=date(2026, 6, 1),
                ),
                second_estimand_id: make_request(
                    frame,
                    anchor=date(2026, 7, 1),
                ),
            },
        )
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(obligations), 2)
        grouped_tasks = (
            ProposedWorkTask(
                proposal_task_key="measure-both-estimands",
                business_purpose=(
                    "Collect the shared requirement for both estimands"
                ),
                capability_intent_ref=(
                    "waje-vnext://capability-intent/"
                    "measurement-evidence.v1"
                ),
                obligation_ids=tuple(
                    item.obligation_id for item in obligations
                ),
                depends_on_task_keys=(),
            ),
        )
        case, grouped = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id="plan-multi-executable-grouped",
            proposed_tasks=grouped_tasks,
            measurement_authority=(
                outcomes,
                admissions,
                obligations,
            ),
        )
        self.assertEqual(len(grouped.plan.tasks), 1)
        self.assertEqual(len(grouped.query_bindings), 2)

        obligation_by_estimand = {
            item.estimand_id: item for item in obligations
        }
        outcome_by_estimand = {
            item.estimand_id: item for item in outcomes
        }
        expected_window_ids = {
            first_estimand.estimand_id: tuple(
                item.window_rule_id
                for item in first_contrast.operands
            ),
            second_estimand_id: tuple(
                item.window_rule_id
                for item in first_contrast.operands
            ),
        }
        expected_estimators = {
            first_estimand.estimand_id: first_estimand.estimator_id,
            second_estimand_id: second_estimator.estimator_id,
        }
        expected_actual_windows = {
            first_estimand.estimand_id: (
                (date(2026, 6, 1), date(2026, 6, 7), 0),
                (date(2026, 5, 25), date(2026, 5, 31), -1),
            ),
            second_estimand_id: (
                (date(2026, 7, 1), date(2026, 7, 7), 0),
                (date(2026, 6, 24), date(2026, 6, 30), -1),
            ),
        }
        for binding in grouped.query_bindings:
            with self.subTest(estimand_id=binding.estimand_id):
                outcome = outcome_by_estimand[binding.estimand_id]
                obligation = obligation_by_estimand[binding.estimand_id]
                self.assertEqual(
                    binding.resolution_outcome_id,
                    outcome.resolution_outcome_id,
                )
                self.assertEqual(
                    binding.semantic_measurement_id,
                    outcome.semantic_measurement_id,
                )
                self.assertEqual(
                    binding.authority_binding_id,
                    outcome.authority_binding_id,
                )
                self.assertEqual(
                    binding.obligation_id,
                    obligation.obligation_id,
                )
                self.assertEqual(
                    tuple(
                        item.window_rule_id
                        for item in (
                            binding.resolved_measurement_instance.windows
                        )
                    ),
                    expected_window_ids[binding.estimand_id],
                )
                self.assertEqual(
                    binding.measurement_binding.estimator_id,
                    expected_estimators[binding.estimand_id],
                )
                self.assertEqual(
                    tuple(
                        (
                            item.actual_start,
                            item.actual_end,
                            item.period_offset,
                        )
                        for item in (
                            binding.resolved_measurement_instance.windows
                        )
                    ),
                    expected_actual_windows[binding.estimand_id],
                )
        self.assertEqual(
            len(
                {
                    item.resolved_measurement_instance
                    .resolver_input_bundle_sha256
                    for item in grouped.query_bindings
                }
            ),
            2,
        )
        self.assertNotEqual(
            grouped.query_bindings[0]
            .resolved_measurement_instance.windows[0]
            .selected_calendar_dates_sha256,
            grouped.query_bindings[1]
            .resolved_measurement_instance.windows[0]
            .selected_calendar_dates_sha256,
        )

        obligations_by_estimand = {
            item.estimand_id: item for item in obligations
        }
        before_split_case = case
        case, split = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW + timedelta(minutes=1),
            plan_revision_id="plan-multi-executable-split",
            prior_plan=grouped.plan,
            proposed_tasks=tuple(
                ProposedWorkTask(
                    proposal_task_key=f"measure-{estimand_id}",
                    business_purpose=(
                        "Collect the shared requirement for one estimand"
                    ),
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=(
                        obligations_by_estimand[
                            estimand_id
                        ].obligation_id,
                    ),
                    depends_on_task_keys=(),
                )
                for estimand_id in (
                    first_estimand.estimand_id,
                    second_estimand_id,
                )
            ),
            measurement_authority=(
                outcomes,
                admissions,
                obligations,
            ),
        )
        self.assertEqual(len(split.plan.tasks), 2)
        self.assertEqual(len(split.query_bindings), 2)

        first_binding, second_binding = split.query_bindings
        mutations = {
            "outcome": replace(
                first_binding,
                resolution_outcome_id=(
                    second_binding.resolution_outcome_id
                ),
            ),
            "obligation": replace(
                first_binding,
                obligation_id=second_binding.obligation_id,
            ),
            "resolved_instance": replace(
                first_binding,
                resolved_measurement_instance=(
                    second_binding.resolved_measurement_instance
                ),
            ),
            "resolved_windows": replace(
                second_binding,
                resolved_measurement_instance=replace(
                    second_binding.resolved_measurement_instance,
                    windows=(
                        first_binding.resolved_measurement_instance.windows
                    ),
                ),
            ),
        }
        for changed_field, tampered_binding in mutations.items():
            with self.subTest(changed_field=changed_field):
                with self.assertRaisesRegex(
                    ValueError,
                    "changes .* authority",
                ):
                    validate_plan_bundle(
                        bundle=replace(
                            split,
                            query_bindings=(
                                (
                                    first_binding
                                    if changed_field == "resolved_windows"
                                    else tampered_binding
                                ),
                                (
                                    tampered_binding
                                    if changed_field == "resolved_windows"
                                    else second_binding
                                ),
                            ),
                        ),
                        case=before_split_case,
                        authority_snapshot=(
                            split.adoption.authority_snapshot
                        ),
                        frame=frame,
                        outcomes=outcomes,
                        admissions=admissions,
                        obligations=obligations,
                    )

    def test_evidence_composition_cardinality_is_validated(self) -> None:
        requirement = make_measurement_design(
            question_id="question-composition-cardinality",
        ).evidence_requirements[0]
        with self.assertRaisesRegex(
            ValueError,
            "cannot exceed required evidence slots",
        ):
            replace(
                requirement,
                required_evidence_type_refs=(
                    "evidence:first",
                    "evidence:second",
                    "evidence:third",
                ),
                composition=EvidenceComposition.AT_LEAST,
                minimum_count=4,
            )
        for invalid_count in (True, 1.5, 0, -1):
            with self.subTest(invalid_count=invalid_count):
                with self.assertRaisesRegex(
                    ValueError,
                    "requires minimum_count",
                ):
                    replace(
                        requirement,
                        required_evidence_type_refs=(
                            "evidence:first",
                            "evidence:second",
                        ),
                        composition=EvidenceComposition.AT_LEAST,
                        minimum_count=invalid_count,
                    )
        for composition in (
            EvidenceComposition.ALL,
            EvidenceComposition.ANY,
        ):
            with self.subTest(composition=composition.value):
                with self.assertRaisesRegex(
                    ValueError,
                    "minimum_count only applies",
                ):
                    replace(
                        requirement,
                        composition=composition,
                        minimum_count=1,
                    )
        requirement_binding = (
            self.bundle.query_bindings[0].requirement_binding
        )
        for invalid_count in (True, 1.0, 0, -1):
            with self.subTest(
                binding_invalid_count=invalid_count,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "binding requires minimum_count",
                ):
                    replace(
                        requirement_binding,
                        required_evidence_type_refs=(
                            "evidence:first",
                            "evidence:second",
                        ),
                        obligation_evidence_type_refs=(
                            "evidence:first",
                        ),
                        composition=EvidenceComposition.AT_LEAST,
                        minimum_count=invalid_count,
                    )
        with self.assertRaisesRegex(
            ValueError,
            "cannot exceed required evidence slots",
        ):
            replace(
                requirement_binding,
                required_evidence_type_refs=(
                    "evidence:first",
                    "evidence:second",
                ),
                obligation_evidence_type_refs=("evidence:first",),
                composition=EvidenceComposition.AT_LEAST,
                minimum_count=3,
            )
        for composition in (
            EvidenceComposition.ALL,
            EvidenceComposition.ANY,
        ):
            with self.subTest(
                binding_composition=composition.value,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "only applies to at_least binding",
                ):
                    replace(
                        requirement_binding,
                        composition=composition,
                        minimum_count=1,
                    )

    def test_executable_and_boundary_obligations_share_one_closed_plan(
        self,
    ) -> None:
        store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = store.open_case(
            case_id="case-mixed-plan",
            thread_id="thread-mixed-plan",
            event_id="event-mixed-plan-open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(store, case)
        design = make_measurement_design(
            question_id=question.question_revision_id,
            source_message_id=question.source_messages[0].message_id,
            source_text=question.source_messages[0].content,
        )
        first_estimand = design.estimands[0]
        first_requirement = design.evidence_requirements[0]
        second_estimand_id = "estimand-mixed-boundary"
        second_scope = replace(
            design.scopes[0],
            scope_id="scope-mixed-boundary",
            predicate_ref="predicate:mixed-boundary-population",
        )
        second_requirement = replace(
            first_requirement,
            evidence_requirement_id="requirement-mixed-boundary",
            target_estimand_ids=(second_estimand_id,),
            scope_id=second_scope.scope_id,
        )
        second_estimand = replace(
            first_estimand,
            estimand_id=second_estimand_id,
            evidence_requirement_ids=(
                second_requirement.evidence_requirement_id,
            ),
            scope_ceiling_id=second_scope.scope_id,
        )
        second_completion = replace(
            design.completion_specs[0],
            completion_spec_id="completion-mixed-boundary",
            target_estimand_ids=(second_estimand_id,),
            required_evidence_requirement_ids=(
                second_requirement.evidence_requirement_id,
            ),
        )
        mixed_design = replace(
            design,
            evidence_requirements=(
                first_requirement,
                second_requirement,
            ),
            completion_specs=(
                design.completion_specs[0],
                second_completion,
            ),
            scopes=(design.scopes[0], second_scope),
            estimands=(first_estimand, second_estimand),
        )
        frame = make_frame(
            case_id=case.case_id,
            question=question,
            frame_id="frame-mixed-plan",
            measurement_design=mixed_design,
        )
        proof_id = record_reviewed_frame(store, frame)
        case = store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-mixed-plan-frame",
            recorded_at=NOW,
        )
        full_request = make_request(
            frame,
            anchor=date(2026, 6, 1),
        )
        short_request = make_request(
            frame,
            anchor=date(2026, 6, 1),
            expected="7",
            observed="6",
            valid="6",
            invalid="0",
            missing="1",
        )
        outcomes, admissions, obligations = (
            record_measurement_authority(
                store=store,
                case=case,
                frame=frame,
                created_at=NOW,
                resolution_requests_by_estimand_id={
                    first_estimand.estimand_id: full_request,
                    second_estimand.estimand_id: short_request,
                },
            )
        )
        bundle = compile_plan_bundle(
            case=case,
            authority_snapshot=store.get_authority_snapshot(
                case.case_id
            ),
            frame=frame,
            outcomes=outcomes,
            admissions=admissions,
            obligations=obligations,
            proposed_tasks=tuple(
                ProposedWorkTask(
                    proposal_task_key=f"close-{index}",
                    business_purpose=(
                        "Close one accepted evidence obligation"
                    ),
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        + (
                            "measurement-evidence.v1"
                            if obligation.execution_disposition.value
                            == "executable"
                            else "boundary-inspection.v1"
                        )
                    ),
                    obligation_ids=(obligation.obligation_id,),
                    depends_on_task_keys=(),
                )
                for index, obligation in enumerate(
                    obligations,
                    start=1,
                )
            ),
            plan_revision_id="plan-mixed",
            revision_number=1,
            prior_plan_revision_id=None,
            created_by_action_id="action-plan-mixed",
            created_at=NOW,
            revision_reason="Close executable and boundary obligations",
        )
        self.assertEqual(len(bundle.plan.tasks), 2)
        self.assertEqual(len(bundle.query_bindings), 1)
        bound_obligation_ids = {
            item.obligation_id for item in bundle.query_bindings
        }
        self.assertEqual(
            len(
                {
                    item.obligation_id
                    for item in obligations
                    if item.obligation_id not in bound_obligation_ids
                }
            ),
            1,
        )
        accepted = store.accept_plan_bundle(
            bundle,
            expected_head_version=case.head_version,
            event_id="event-mixed-plan-accepted",
            recorded_at=NOW,
        )
        self.assertEqual(
            accepted.accepted_plan_revision_id,
            bundle.plan.plan_revision_id,
        )
        self.assertEqual(
            store.list_query_bindings(bundle.plan.plan_revision_id),
            bundle.query_bindings,
        )

    def test_query_binding_preserves_cross_month_window_identity(self) -> None:
        binding = self.bundle.query_bindings[0]
        windows = binding.resolved_measurement_instance.windows
        self.assertEqual(windows[0].actual_start.isoformat(), "2026-06-01")
        self.assertEqual(windows[0].actual_end.isoformat(), "2026-06-07")
        self.assertEqual(windows[1].actual_start.isoformat(), "2026-05-25")
        self.assertEqual(windows[1].actual_end.isoformat(), "2026-05-31")
        self.assertEqual(
            tuple(item.period_offset for item in windows),
            (0, -1),
        )
        self.assertEqual(
            binding.requirement_binding.exposure_id,
            binding.measurement_binding.exposure_id,
        )

    def test_missing_obligation_has_no_accepted_plan_path(self) -> None:
        outcomes = self.store.list_measurement_resolutions(
            self.frame.frame_revision_id
        )
        admissions = tuple(
            self.store.get_measurement_resolution_admission(
                item.resolution_outcome_id
            )
            for item in outcomes
        )
        with self.assertRaisesRegex(ValueError, "cover every obligation"):
            compile_plan_bundle(
                case=self.case,
                authority_snapshot=self.store.get_authority_snapshot(
                    self.case.case_id
                ),
                frame=self.frame,
                outcomes=outcomes,
                admissions=admissions,
                obligations=self.store.list_evidence_obligations(
                    self.frame.frame_revision_id
                ),
                proposed_tasks=(
                    ProposedWorkTask(
                        proposal_task_key="empty-coverage",
                        business_purpose="Attempt incomplete closure",
                        capability_intent_ref=(
                            "waje-vnext://capability-intent/"
                            "measurement-evidence.v1"
                        ),
                        obligation_ids=(content_sha256("unknown"),),
                        depends_on_task_keys=(),
                    ),
                ),
                plan_revision_id="plan-incomplete",
                revision_number=2,
                prior_plan_revision_id=self.bundle.plan.plan_revision_id,
                created_by_action_id="action-plan-incomplete",
                created_at=NOW,
                revision_reason="Attempt incomplete closure",
            )

    def test_capability_intent_cannot_relabel_obligation_disposition(
        self,
    ) -> None:
        outcomes = self.store.list_measurement_resolutions(
            self.frame.frame_revision_id
        )
        admissions = tuple(
            self.store.get_measurement_resolution_admission(
                item.resolution_outcome_id
            )
            for item in outcomes
        )
        obligations = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )
        with self.assertRaisesRegex(
            ValueError,
            "capability intent cannot execute obligation disposition",
        ):
            compile_plan_bundle(
                case=self.case,
                authority_snapshot=self.store.get_authority_snapshot(
                    self.case.case_id
                ),
                frame=self.frame,
                outcomes=outcomes,
                admissions=admissions,
                obligations=obligations,
                proposed_tasks=(
                    ProposedWorkTask(
                        proposal_task_key="wrong-intent",
                        business_purpose=(
                            "Attempt to route executable work as a boundary"
                        ),
                        capability_intent_ref=(
                            "waje-vnext://capability-intent/"
                            "boundary-inspection.v1"
                        ),
                        obligation_ids=tuple(
                            item.obligation_id for item in obligations
                        ),
                        depends_on_task_keys=(),
                    ),
                ),
                plan_revision_id="plan-wrong-intent",
                revision_number=2,
                prior_plan_revision_id=self.bundle.plan.plan_revision_id,
                created_by_action_id="action-plan-wrong-intent",
                created_at=NOW,
                revision_reason="Attempt an invalid capability route",
            )

    def test_capability_intent_registry_and_prerequisites_are_sealed(
        self,
    ) -> None:
        boundary_ref = (
            "waje-vnext://capability-intent/boundary-inspection.v1"
        )
        original_contract = CAPABILITY_INTENT_REGISTRY.get(boundary_ref)
        with self.assertRaises((AttributeError, TypeError)):
            CAPABILITY_INTENT_REGISTRY.contracts += (  # type: ignore[misc]
                replace(
                    original_contract,
                    allowed_execution_dispositions=(
                        original_contract
                        .allowed_execution_dispositions[0],
                    ),
                ),
            )

        outcomes = self.store.list_measurement_resolutions(
            self.frame.frame_revision_id
        )
        admissions = tuple(
            self.store.get_measurement_resolution_admission(
                item.resolution_outcome_id
            )
            for item in outcomes
        )
        obligations = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )
        with self.assertRaisesRegex(
            ValueError,
            "cannot fulfill obligation evidence type",
        ):
            compile_plan_bundle(
                case=self.case,
                authority_snapshot=self.store.get_authority_snapshot(
                    self.case.case_id
                ),
                frame=self.frame,
                outcomes=outcomes,
                admissions=admissions,
                obligations=obligations,
                proposed_tasks=(
                    ProposedWorkTask(
                        proposal_task_key="dead-sensitivity-route",
                        business_purpose=(
                            "Attempt sensitivity without sensitivity authority"
                        ),
                        capability_intent_ref=(
                            "waje-vnext://capability-intent/"
                            "measurement-sensitivity.v1"
                        ),
                        obligation_ids=tuple(
                            item.obligation_id for item in obligations
                        ),
                        depends_on_task_keys=(),
                    ),
                ),
                plan_revision_id="plan-dead-sensitivity-route",
                revision_number=2,
                prior_plan_revision_id=self.bundle.plan.plan_revision_id,
                created_by_action_id="action-dead-sensitivity-route",
                created_at=NOW,
                revision_reason="Attempt a route with no legal action",
            )

    def test_sensitivity_intent_accepts_matching_evidence_authority(
        self,
    ) -> None:
        store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = store.open_case(
            case_id="case-sensitivity-intent",
            thread_id="thread-sensitivity-intent",
            event_id="event-sensitivity-intent-open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(store, case)
        design = make_measurement_design(
            question_id=question.question_revision_id,
            source_message_id=question.source_messages[0].message_id,
            source_text=question.source_messages[0].content,
        )
        sensitivity = SensitivitySpec(
            sensitivity_id="sensitivity:alternative-window",
            changed_node_ids=(design.window_rules[0].window_rule_id,),
            derived_relation_ref="relation:alternative-window",
            expected_evidence_relation_ref=(
                "evidence-relation:robustness"
            ),
        )
        requirement = replace(
            design.evidence_requirements[0],
            required_evidence_type_refs=("evidence:sensitivity",),
        )
        estimand = replace(
            design.estimands[0],
            sensitivity_ids=(sensitivity.sensitivity_id,),
        )
        frame = make_frame(
            case_id=case.case_id,
            question=question,
            frame_id="frame-sensitivity-intent",
            measurement_design=replace(
                design,
                sensitivities=(sensitivity,),
                evidence_requirements=(requirement,),
                estimands=(estimand,),
            ),
        )
        proof_id = record_reviewed_frame(store, frame)
        case = store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-sensitivity-intent-frame",
            recorded_at=NOW,
        )
        outcomes, admissions, obligations = record_measurement_authority(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
        )
        bundle = compile_plan_bundle(
            case=case,
            authority_snapshot=store.get_authority_snapshot(case.case_id),
            frame=frame,
            outcomes=outcomes,
            admissions=admissions,
            obligations=obligations,
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="run-governed-sensitivity",
                    business_purpose="Test an accepted alternative window",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-sensitivity.v1"
                    ),
                    obligation_ids=tuple(
                        item.obligation_id for item in obligations
                    ),
                    depends_on_task_keys=(),
                ),
            ),
            plan_revision_id="plan-sensitivity-intent",
            revision_number=1,
            prior_plan_revision_id=None,
            created_by_action_id="action-sensitivity-intent",
            created_at=NOW,
            revision_reason="Adopt a governed sensitivity obligation",
        )
        accepted = store.accept_plan_bundle(
            bundle,
            expected_head_version=case.head_version,
            event_id="event-sensitivity-intent-plan",
            recorded_at=NOW,
        )
        self.assertEqual(
            accepted.accepted_plan_revision_id,
            bundle.plan.plan_revision_id,
        )
        self.assertEqual(
            bundle.query_bindings[0].measurement_binding.sensitivity_ids,
            (sensitivity.sensitivity_id,),
        )

    def test_query_binding_tamper_fails_exact_replay(self) -> None:
        binding = self.bundle.query_bindings[0]
        tampered = replace(
            binding,
            capability_intent_ref=(
                "waje-vnext://capability-intent/"
                "measurement-sensitivity.v1"
            ),
        )
        with self.assertRaisesRegex(
            ValueError,
            "query binding changes measurement authority",
        ):
            validate_plan_bundle(
                bundle=replace(
                    self.bundle,
                    query_bindings=(tampered,),
                ),
                case=replace(
                    self.case,
                    head_version=self.bundle.adoption.expected_head_version,
                    accepted_plan_revision_id=(
                        self.bundle.plan.prior_plan_revision_id
                    ),
                ),
                authority_snapshot=self.bundle.adoption.authority_snapshot,
                frame=self.frame,
                outcomes=self.store.list_measurement_resolutions(
                    self.frame.frame_revision_id
                ),
                admissions=tuple(
                    self.store.get_measurement_resolution_admission(
                        outcome_id
                    )
                    for outcome_id
                    in self.bundle.adoption.resolution_outcome_ids
                ),
                obligations=self.store.list_evidence_obligations(
                    self.frame.frame_revision_id
                ),
            )

    def test_deep_measurement_authority_mutations_fail_exact_replay(
        self,
    ) -> None:
        binding = self.bundle.query_bindings[0]
        measurement = binding.measurement_binding
        requirement = binding.requirement_binding
        instance = binding.resolved_measurement_instance
        first_window = instance.windows[0]
        mutations = {
            "frame_metric_graph": replace(
                binding,
                frame_content_sha256="f" * 64,
            ),
            "claim_target": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    claim_target_spec_sha256="f" * 64,
                ),
            ),
            "variables": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    variable_ids=(*measurement.variable_ids, "metric:forged"),
                ),
            ),
            "population": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    population_id="population:forged",
                ),
            ),
            "observation_unit": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    observation_unit_id="unit:forged",
                ),
            ),
            "temporal_semantic": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    temporal_semantic_id="time:forged",
                ),
            ),
            "estimator": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    estimator_id="estimator:forged",
                ),
            ),
            "exposure": replace(
                binding,
                requirement_binding=replace(
                    requirement,
                    exposure_id="exposure:forged",
                ),
            ),
            "comparison": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    contrast_id="contrast:forged",
                ),
            ),
            "identification": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    identification_id="identification:forged",
                ),
            ),
            "claim_strength": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    claim_strength_ceiling=ClaimStrengthCeiling.CAUSAL,
                ),
            ),
            "scope_ceiling": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    scope_ceiling_id="scope:forged-ceiling",
                ),
            ),
            "kind_specific_refs": replace(
                binding,
                measurement_binding=replace(
                    measurement,
                    event_ids=("event:forged",),
                    sequence_id="sequence:forged",
                    cohort_risk_set_id="risk-set:forged",
                    reconciliation_id="reconciliation:forged",
                    relationship_id="relationship:forged",
                    eligibility_id="eligibility:forged",
                    alternative_ids=("alternative:forged",),
                    sensitivity_ids=("sensitivity:forged",),
                    falsification_ids=("falsification:forged",),
                    reversal_ids=("reversal:forged",),
                ),
            ),
            "evidence_composition": replace(
                binding,
                requirement_binding=replace(
                    requirement,
                    composition=EvidenceComposition.AT_LEAST,
                    minimum_count=1,
                ),
            ),
            "minimum_strength": replace(
                binding,
                requirement_binding=replace(
                    requirement,
                    minimum_strength=ClaimStrengthCeiling.CAUSAL,
                ),
            ),
            "requirement_scope": replace(
                binding,
                requirement_binding=replace(
                    requirement,
                    scope_id="scope:forged-requirement",
                ),
            ),
            "requirement_boundary": replace(
                binding,
                requirement_binding=replace(
                    requirement,
                    boundary_policy=RequirementBoundaryPolicy.BLOCK,
                    allowed_boundary_codes=(),
                ),
            ),
            "contradiction_policy": replace(
                binding,
                requirement_binding=replace(
                    requirement,
                    contradiction_policy_ref="contradiction:forged",
                ),
            ),
            "linked_checks": replace(
                binding,
                requirement_binding=replace(
                    requirement,
                    linked_falsification_ids=("falsification:forged",),
                    linked_reversal_ids=("reversal:forged",),
                ),
            ),
            "window_rule": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    windows=(
                        replace(
                            first_window,
                            window_rule_id="window:forged",
                        ),
                        *instance.windows[1:],
                    ),
                ),
            ),
            "month_offset": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    windows=(
                        replace(first_window, period_offset=-1),
                        *instance.windows[1:],
                    ),
                ),
            ),
            "timezone": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    context=replace(instance.context, timezone="UTC"),
                ),
            ),
            "business_day_cutoff": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    context=replace(
                        instance.context,
                        business_day_cutoff="05:00:00",
                    ),
                ),
            ),
            "snapshot_release": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    context=replace(
                        instance.context,
                        snapshot_release_ref="snapshot:forged",
                    ),
                ),
            ),
            "exposure_fact": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    windows=(
                        replace(
                            first_window,
                            exposure_facts=(
                                replace(
                                    first_window.exposure_facts[0],
                                    unit_ref="unit:forged-exposure",
                                ),
                            ),
                        ),
                        *instance.windows[1:],
                    ),
                ),
            ),
            "actual_date_range": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    windows=(
                        replace(
                            first_window,
                            actual_start=(
                                first_window.actual_start
                                + timedelta(days=1)
                            ),
                            actual_end=(
                                first_window.actual_end
                                + timedelta(days=1)
                            ),
                            start_instant=(
                                first_window.start_instant
                                + timedelta(days=1)
                            ),
                            end_instant=(
                                first_window.end_instant
                                + timedelta(days=1)
                            ),
                            selected_calendar_dates_sha256="e" * 64,
                            calendar_coverage_receipt_sha256="d" * 64,
                        ),
                        *instance.windows[1:],
                    ),
                ),
            ),
            "calendar_coverage": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    windows=(
                        replace(
                            first_window,
                            observed_calendar_dates_count=6,
                            valid_calendar_dates_count=6,
                            calendar_coverage_receipt_sha256="c" * 64,
                        ),
                        *instance.windows[1:],
                    ),
                ),
            ),
            "exposure_values": replace(
                binding,
                resolved_measurement_instance=replace(
                    instance,
                    windows=(
                        replace(
                            first_window,
                            observed_calendar_dates_count=6,
                            valid_calendar_dates_count=6,
                            calendar_coverage_receipt_sha256="b" * 64,
                            exposure_facts=(
                                replace(
                                    first_window.exposure_facts[0],
                                    observed_exposure_decimal="6",
                                    valid_exposure_decimal="6",
                                    invalid_exposure_decimal="0",
                                    missing_exposure_decimal="1",
                                    coverage_ratio_decimal=(
                                        "0.8571428571428571428571428571"
                                    ),
                                    source_receipt_sha256="a" * 64,
                                ),
                            ),
                        ),
                        *instance.windows[1:],
                    ),
                ),
            ),
        }
        pre_acceptance_case = replace(
            self.case,
            head_version=self.bundle.adoption.expected_head_version,
            accepted_plan_revision_id=(
                self.bundle.plan.prior_plan_revision_id
            ),
        )
        outcomes = self.store.list_measurement_resolutions(
            self.frame.frame_revision_id
        )
        admissions = tuple(
            self.store.get_measurement_resolution_admission(
                outcome_id
            )
            for outcome_id in self.bundle.adoption.resolution_outcome_ids
        )
        obligations = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )
        for label, tampered in mutations.items():
            with self.subTest(field=label):
                with self.assertRaisesRegex(
                    ValueError,
                    "query binding changes measurement authority",
                ):
                    validate_plan_bundle(
                        bundle=replace(
                            self.bundle,
                            query_bindings=(tampered,),
                        ),
                        case=pre_acceptance_case,
                        authority_snapshot=(
                            self.bundle.adoption.authority_snapshot
                        ),
                        frame=self.frame,
                        outcomes=outcomes,
                        admissions=admissions,
                        obligations=obligations,
                    )

    def test_correction_fences_compiled_plan(self) -> None:
        prior = self.bundle.plan
        case = self.store.get_case(self.case.case_id)
        snapshot = self.store.get_authority_snapshot(case.case_id)
        outcomes = self.store.list_measurement_resolutions(
            self.frame.frame_revision_id
        )
        obligations = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )
        bundle = compile_plan_bundle(
            case=case,
            authority_snapshot=snapshot,
            frame=self.frame,
            outcomes=outcomes,
            admissions=tuple(
                self.store.get_measurement_resolution_admission(
                    item.resolution_outcome_id
                )
                for item in outcomes
            ),
            obligations=obligations,
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="replanned-investigation",
                    business_purpose="Reorder the same accepted investigation",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=tuple(
                        item.obligation_id for item in obligations
                    ),
                    depends_on_task_keys=(),
                ),
            ),
            plan_revision_id="plan-after-reorder",
            revision_number=prior.revision_number + 1,
            prior_plan_revision_id=prior.plan_revision_id,
            created_by_action_id="action-plan-after-reorder",
            created_at=NOW,
            revision_reason="Reorder execution",
        )
        payload = {"message": "改成比较另一个业务口径"}
        self.store.append_mailbox_message(
            message_id="message-correction",
            case_id=case.case_id,
            kind=MailboxMessageKind.USER_CORRECTION,
            operation=make_operation(
                operation_id="operation-correction",
                idempotency_key="correction-key",
                payload=payload,
                case_id=case.case_id,
            ),
            payload=payload,
            created_at=NOW,
        )
        with self.assertRaises(StaleHead):
            self.store.accept_plan_bundle(
                bundle,
                expected_head_version=case.head_version,
                event_id="event-stale-plan",
                recorded_at=NOW,
            )

    def test_new_resolution_for_same_frame_must_be_selected_explicitly(
        self,
    ) -> None:
        estimand_id = self.frame.measurement_design.estimands[0].estimand_id
        second_request = make_request(
            self.frame,
            anchor=date(2026, 7, 1),
        )
        second_outcomes, second_admissions, second_obligations = (
            record_measurement_authority(
                store=self.store,
                case=self.case,
                frame=self.frame,
                created_at=NOW,
                resolution_requests_by_estimand_id={
                    estimand_id: second_request,
                },
            )
        )
        all_obligations = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )
        first_outcomes = tuple(
            item
            for item in self.store.list_measurement_resolutions(
                self.frame.frame_revision_id
            )
            if item.resolution_outcome_id
            not in {
                outcome.resolution_outcome_id
                for outcome in second_outcomes
            }
        )
        first_admissions = tuple(
            self.store.get_measurement_resolution_admission(
                item.resolution_outcome_id
            )
            for item in first_outcomes
        )
        second_tasks = (
            ProposedWorkTask(
                proposal_task_key="measure-second-resolution",
                business_purpose=(
                    "Measure the explicitly selected current resolution"
                ),
                capability_intent_ref=(
                    "waje-vnext://capability-intent/"
                    "measurement-evidence.v1"
                ),
                obligation_ids=tuple(
                    item.obligation_id
                    for item in second_obligations
                ),
                depends_on_task_keys=(),
            ),
        )
        event_count = len(self.store.list_events(self.case.case_id))
        with self.assertRaisesRegex(
            ValueError,
            "two outcomes for one estimand",
        ):
            compile_plan_bundle(
                case=self.case,
                authority_snapshot=self.store.get_authority_snapshot(
                    self.case.case_id
                ),
                frame=self.frame,
                outcomes=(*first_outcomes, *second_outcomes),
                admissions=(*first_admissions, *second_admissions),
                obligations=all_obligations,
                proposed_tasks=second_tasks,
                plan_revision_id="plan-ambiguous-resolution",
                revision_number=2,
                prior_plan_revision_id=(
                    self.bundle.plan.plan_revision_id
                ),
                created_by_action_id="action-plan-ambiguous-resolution",
                created_at=NOW,
                revision_reason="Attempt ambiguous resolution adoption",
            )
        with self.assertRaisesRegex(
            ValueError,
            "cover every obligation",
        ):
            compile_plan_bundle(
                case=self.case,
                authority_snapshot=self.store.get_authority_snapshot(
                    self.case.case_id
                ),
                frame=self.frame,
                outcomes=first_outcomes,
                admissions=first_admissions,
                obligations=all_obligations,
                proposed_tasks=second_tasks,
                plan_revision_id="plan-crossed-resolution",
                revision_number=2,
                prior_plan_revision_id=(
                    self.bundle.plan.plan_revision_id
                ),
                created_by_action_id="action-plan-crossed-resolution",
                created_at=NOW,
                revision_reason="Attempt crossed outcome obligations",
            )
        self.assertEqual(
            len(self.store.list_events(self.case.case_id)),
            event_count,
        )
        for rejected_plan_id in (
            "plan-ambiguous-resolution",
            "plan-crossed-resolution",
        ):
            with self.assertRaises(AuthorityNotFound):
                self.store.get_plan(rejected_plan_id)
        bundle = compile_plan_bundle(
            case=self.case,
            authority_snapshot=self.store.get_authority_snapshot(
                self.case.case_id
            ),
            frame=self.frame,
            outcomes=second_outcomes,
            admissions=second_admissions,
            obligations=all_obligations,
            proposed_tasks=second_tasks,
            plan_revision_id="plan-second-resolution",
            revision_number=2,
            prior_plan_revision_id=self.bundle.plan.plan_revision_id,
            created_by_action_id="action-plan-second-resolution",
            created_at=NOW,
            revision_reason="Adopt a newer resolution under the same Frame",
        )
        self.assertEqual(
            bundle.plan.resolution_outcome_ids,
            tuple(
                item.resolution_outcome_id for item in second_outcomes
            ),
        )
        self.assertTrue(
            set(bundle.adoption.obligation_ids).isdisjoint(
                self.bundle.adoption.obligation_ids
            )
        )
        accepted = self.store.accept_plan_bundle(
            bundle,
            expected_head_version=self.case.head_version,
            event_id="event-plan-second-resolution",
            recorded_at=NOW,
        )
        self.assertEqual(
            accepted.accepted_plan_revision_id,
            bundle.plan.plan_revision_id,
        )

    def test_plan_bundle_replay_is_exactly_idempotent(self) -> None:
        event_id = content_sha256(
            {
                "kind": "test-plan-event",
                "plan_revision_id": self.bundle.plan.plan_revision_id,
            }
        )
        replay = self.store.accept_plan_bundle(
            self.bundle,
            expected_head_version=(
                self.bundle.adoption.expected_head_version
            ),
            event_id=event_id,
            recorded_at=NOW,
        )
        self.assertEqual(replay, self.case)
        changed = replace(
            self.bundle,
            plan=replace(
                self.bundle.plan,
                revision_reason="Changed content under the same event",
            ),
        )
        with self.assertRaises(AuthorityConflict):
            self.store.accept_plan_bundle(
                changed,
                expected_head_version=(
                    self.bundle.adoption.expected_head_version
                ),
                event_id=event_id,
                recorded_at=NOW,
            )

    def test_plan_replay_preserves_operation_provenance(self) -> None:
        store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        case = store.open_case(
            case_id="case-plan-operation-replay",
            thread_id="thread-plan-operation-replay",
            event_id="event-plan-operation-open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(store, case)
        frame = make_frame(
            case_id=case.case_id,
            question=question,
            frame_id="frame-plan-operation-replay",
        )
        proof_id = record_reviewed_frame(store, frame)
        case = store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-plan-operation-frame",
            recorded_at=NOW,
        )
        operation = OperationIdentity(
            operation_id="operation-plan-operation-replay",
            idempotency_key="operation-plan-operation-replay-key",
            causation_id="user-turn-plan-operation-replay",
            correlation_id=case.case_id,
            authority_revision=(
                store.get_mailbox_head(case.case_id).authority_epoch
            ),
            payload_sha256=content_sha256(
                {"kind": "plan-operation-replay"}
            ),
        )
        case, bundle = record_plan_bundle(
            store=store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id="plan-operation-replay",
            operation=operation,
        )
        event_id = content_sha256(
            {
                "kind": "test-plan-event",
                "plan_revision_id": bundle.plan.plan_revision_id,
            }
        )
        self.assertEqual(
            store.accept_plan_bundle(
                bundle,
                expected_head_version=(
                    bundle.adoption.expected_head_version
                ),
                event_id=event_id,
                recorded_at=NOW,
                operation=operation,
            ),
            case,
        )
        with self.assertRaisesRegex(
            AuthorityConflict,
            "replay changes bundle content",
        ):
            store.accept_plan_bundle(
                bundle,
                expected_head_version=(
                    bundle.adoption.expected_head_version
                ),
                event_id=event_id,
                recorded_at=NOW,
                operation=None,
            )
        changed_operations = {
            "idempotency_key": replace(
                operation,
                idempotency_key="changed-plan-operation-key",
            ),
            "causation_id": replace(
                operation,
                causation_id="changed-user-turn",
            ),
            "payload_sha256": replace(
                operation,
                payload_sha256=content_sha256(
                    {"kind": "changed-plan-operation"}
                ),
            ),
        }
        for changed_field, changed_operation in (
            changed_operations.items()
        ):
            with self.subTest(changed_field=changed_field):
                with self.assertRaisesRegex(
                    AuthorityConflict,
                    "replay changes bundle content",
                ):
                    store.accept_plan_bundle(
                        bundle,
                        expected_head_version=(
                            bundle.adoption.expected_head_version
                        ),
                        event_id=event_id,
                        recorded_at=NOW,
                        operation=changed_operation,
                    )

    def test_correction_fences_persisted_measurement_derivations(
        self,
    ) -> None:
        outcome = self.store.get_measurement_resolution(
            self.bundle.plan.resolution_outcome_ids[0]
        )
        admission = self.store.get_measurement_resolution_admission(
            outcome.resolution_outcome_id
        )
        obligation = self.store.get_evidence_obligation(
            self.bundle.adoption.obligation_ids[0]
        )
        payload = {"message": "改用另一个业务时间口径"}
        self.store.append_mailbox_message(
            message_id="message-derivation-correction",
            case_id=self.case.case_id,
            kind=MailboxMessageKind.USER_CORRECTION,
            operation=make_operation(
                operation_id="operation-derivation-correction",
                idempotency_key="derivation-correction-key",
                payload=payload,
                case_id=self.case.case_id,
            ),
            payload=payload,
            created_at=NOW,
        )

        with self.assertRaisesRegex(
            StaleHead,
            "measurement derivation authority is stale",
        ):
            self.store.record_measurement_resolution(
                outcome,
                admission=admission,
                expected_head_version=self.case.head_version,
                event_id="event-stale-resolution-after-correction",
            )
        with self.assertRaisesRegex(
            StaleHead,
            "obligation derivation authority is stale",
        ):
            self.store.record_evidence_obligation(
                obligation,
                expected_head_version=self.case.head_version,
                event_id="event-stale-obligation-after-correction",
            )

    def test_correction_fences_stale_outbox_before_enqueue(self) -> None:
        snapshot = self.store.get_authority_snapshot(self.case.case_id)
        payload = {"projection": "refresh"}
        operation = OperationIdentity(
            operation_id="operation-stale-outbox",
            idempotency_key="stale-outbox-key",
            causation_id=self.bundle.plan.plan_revision_id,
            correlation_id=self.case.case_id,
            authority_revision=snapshot.mailbox_authority_epoch,
            payload_sha256=content_sha256(payload),
        )
        message = OutboxMessage(
            outbox_message_id="outbox-stale-after-correction",
            case_id=self.case.case_id,
            source_event_cursor=self.store.list_events(
                self.case.case_id
            )[-1].cursor,
            action_id=None,
            job_kind=AsyncJobKind.PROJECTION,
            operation=operation,
            expected_head_version=snapshot.head_version,
            expected_authority_epoch=snapshot.mailbox_authority_epoch,
            authority_snapshot=snapshot,
            authority_snapshot_sha256=snapshot.content_sha256,
            idempotency_key=operation.idempotency_key,
            destination="projection-worker",
            contract_ref="waje-vnext://projection/refresh.v1",
            payload=payload,
            payload_sha256=content_sha256(payload),
            created_at=NOW,
        )
        correction = {"message": "停止旧投影并重新测量"}
        self.store.append_mailbox_message(
            message_id="message-outbox-correction",
            case_id=self.case.case_id,
            kind=MailboxMessageKind.USER_CORRECTION,
            operation=make_operation(
                operation_id="operation-outbox-correction",
                idempotency_key="outbox-correction-key",
                payload=correction,
                case_id=self.case.case_id,
            ),
            payload=correction,
            created_at=NOW,
        )

        with self.assertRaises(StaleHead):
            self.store.enqueue_outbox(message)

    def test_technical_retry_keeps_sealed_execution_identity(self) -> None:
        binding = self.bundle.query_bindings[0]
        spec = build_conformance_execution_spec(
            query_binding=binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "payment-window.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "payment-window.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "aggregate-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/conformance.v1"
            ),
            created_at=NOW,
        )
        snapshot = self.store.get_authority_snapshot(self.case.case_id)
        first = build_logical_execution_attempt(
            spec=spec,
            authority_snapshot=snapshot,
            attempt_number=1,
            prior_attempt=None,
            retry_reason_code=None,
            requested_at=NOW,
        )
        changed_nonsemantic_state = replace(
            snapshot,
            obligation_state_version=snapshot.obligation_state_version + 1,
            evidence_admission_state_version=(
                snapshot.evidence_admission_state_version + 1
            ),
        )
        second = build_logical_execution_attempt(
            spec=spec,
            authority_snapshot=changed_nonsemantic_state,
            attempt_number=2,
            prior_attempt=first,
            retry_reason_code="provider_timeout",
            requested_at=NOW,
        )
        self.assertEqual(
            first.logical_execution_id,
            second.logical_execution_id,
        )
        self.assertTrue(
            same_business_authority(
                first.authority_snapshot,
                second.authority_snapshot,
            )
        )
        changed_specs = {
            "fixture_ref": replace(
                spec,
                fixture_ref=(
                    "waje-vnext://conformance-fixture/"
                    "payment-window-replacement.v1"
                ),
            ),
            "fixture_content": replace(
                spec,
                fixture_content_sha256=content_sha256(
                    {"fixture": "replacement"}
                ),
            ),
            "result_contract": replace(
                spec,
                result_contract_ref=(
                    "waje-vnext://result-contract/"
                    "replacement-aggregate.v1"
                ),
            ),
            "execution_policy": replace(
                spec,
                execution_policy_ref=(
                    "waje-vnext://execution-policy/"
                    "conformance.replacement.v1"
                ),
            ),
            "query_binding_content": replace(
                spec,
                query_binding_content_sha256=content_sha256(
                    {"query_binding": "replacement"}
                ),
            ),
        }
        for changed_field, changed_spec in changed_specs.items():
            with self.subTest(changed_field=changed_field):
                with self.assertRaisesRegex(
                    ValueError,
                    "cannot change logical execution identity",
                ):
                    build_logical_execution_attempt(
                        spec=changed_spec,
                        authority_snapshot=changed_nonsemantic_state,
                        attempt_number=2,
                        prior_attempt=first,
                        retry_reason_code="provider_timeout",
                        requested_at=NOW,
                    )
        with self.assertRaisesRegex(ValueError, "currently accepted"):
            build_logical_execution_attempt(
                spec=spec,
                authority_snapshot=replace(
                    changed_nonsemantic_state,
                    accepted_plan_revision_id="plan-changed",
                ),
                attempt_number=3,
                prior_attempt=second,
                retry_reason_code="provider_timeout",
                requested_at=NOW,
            )

    def test_repository_rejects_forged_retry_sealed_fields(self) -> None:
        binding = self.bundle.query_bindings[0]
        spec = build_conformance_execution_spec(
            query_binding=binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "repository-retry.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "repository-retry.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "aggregate-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/conformance.v1"
            ),
            created_at=NOW,
        )
        snapshot = self.store.get_authority_snapshot(
            self.case.case_id
        )
        self.store.record_conformance_execution_spec(
            spec,
            expected_authority_snapshot=snapshot,
        )
        initial = build_logical_execution_attempt(
            spec=spec,
            authority_snapshot=snapshot,
            attempt_number=1,
            prior_attempt=None,
            retry_reason_code=None,
            requested_at=NOW,
        )
        self.store.record_logical_execution_attempt(initial)
        retry = build_logical_execution_attempt(
            spec=spec,
            authority_snapshot=snapshot,
            attempt_number=2,
            prior_attempt=initial,
            retry_reason_code="provider_timeout",
            requested_at=NOW,
        )

        def rederive_id(attempt):
            return replace(
                attempt,
                logical_execution_attempt_id=content_sha256(
                    {
                        "kind": "logical-execution-attempt.v1",
                        "logical_execution_id": (
                            attempt.logical_execution_id
                        ),
                        "attempt_number": attempt.attempt_number,
                        "prior_attempt_id": attempt.prior_attempt_id,
                        "retry_reason_code": (
                            attempt.retry_reason_code
                        ),
                    }
                ),
            )

        forged_retries = {
            "prior_attempt_id": rederive_id(
                replace(retry, prior_attempt_id="e" * 64)
            ),
            "task_id": rederive_id(
                replace(retry, task_id="task-forged-retry")
            ),
            "query_binding_content_sha256": rederive_id(
                replace(
                    retry,
                    query_binding_content_sha256="f" * 64,
                )
            ),
            "execution_spec_content_sha256": rederive_id(
                replace(
                    retry,
                    execution_spec_content_sha256="a" * 64,
                )
            ),
        }
        for changed_field, forged_retry in forged_retries.items():
            with self.subTest(changed_field=changed_field):
                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "sealed.*input",
                ):
                    self.store.record_logical_execution_attempt(
                        forged_retry
                    )
                self.assertEqual(
                    self.store.list_logical_execution_attempts(
                        spec.logical_execution_id
                    ),
                    (initial,),
                )
        self.assertEqual(
            self.store.record_logical_execution_attempt(retry),
            retry,
        )

    def test_conformance_spec_cannot_relabel_an_old_plan_binding(
        self,
    ) -> None:
        old_binding = self.bundle.query_bindings[0]
        obligations = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )
        self.case, second_bundle = record_plan_bundle(
            store=self.store,
            case=self.case,
            frame=self.frame,
            created_at=NOW,
            plan_revision_id="plan-2",
            prior_plan=self.bundle.plan,
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="second-plan-task",
                    business_purpose="Reorder the accepted investigation",
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=tuple(
                        item.obligation_id for item in obligations
                    ),
                    depends_on_task_keys=(),
                ),
            ),
        )
        old_spec = build_conformance_execution_spec(
            query_binding=old_binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "payment-window.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "payment-window.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "aggregate-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/conformance.v1"
            ),
            created_at=NOW,
        )
        relabeled = replace(
            old_spec,
            plan_revision_id=second_bundle.plan.plan_revision_id,
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "not system-derived",
        ):
            self.store.record_conformance_execution_spec(
                relabeled,
                expected_authority_snapshot=(
                    self.store.get_authority_snapshot(
                        self.case.case_id
                    )
                ),
            )

    def test_repository_rederives_conformance_ids_and_seals_one_spec(
        self,
    ) -> None:
        binding = self.bundle.query_bindings[0]
        snapshot = self.store.get_authority_snapshot(
            self.case.case_id
        )
        spec = build_conformance_execution_spec(
            query_binding=binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "system-id-proof.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "system-id-proof.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "aggregate-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/conformance.v1"
            ),
            created_at=NOW,
        )
        forged_specs = {
            "logical_execution_id": replace(
                spec,
                logical_execution_id="a" * 64,
            ),
            "execution_spec_id": replace(
                spec,
                conformance_execution_spec_id="b" * 64,
            ),
            "same_logical_changed_input": replace(
                spec,
                conformance_execution_spec_id="c" * 64,
                fixture_ref=(
                    "waje-vnext://conformance-fixture/"
                    "changed-input.v1"
                ),
                fixture_content_sha256=content_sha256(
                    {"fixture": "changed-input.v1"}
                ),
            ),
        }
        for changed_field, forged_spec in forged_specs.items():
            with self.subTest(changed_field=changed_field):
                with self.assertRaisesRegex(
                    InvalidAuthorityTransition,
                    "not system-derived",
                ):
                    self.store.record_conformance_execution_spec(
                        forged_spec,
                        expected_authority_snapshot=snapshot,
                    )

        self.assertEqual(
            self.store.record_conformance_execution_spec(
                spec,
                expected_authority_snapshot=snapshot,
            ),
            spec,
        )
        initial_attempt = build_logical_execution_attempt(
            spec=spec,
            authority_snapshot=snapshot,
            attempt_number=1,
            prior_attempt=None,
            retry_reason_code=None,
            requested_at=NOW,
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "attempt is not system-derived",
        ):
            self.store.record_logical_execution_attempt(
                replace(
                    initial_attempt,
                    logical_execution_attempt_id="d" * 64,
                )
            )
        second_valid_spec = build_conformance_execution_spec(
            query_binding=binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "second-valid-input.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "second-valid-input.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "aggregate-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/conformance.v1"
            ),
            created_at=NOW,
        )
        with self.assertRaisesRegex(
            AuthorityConflict,
            "query binding already has another execution spec",
        ):
            self.store.record_conformance_execution_spec(
                second_valid_spec,
                expected_authority_snapshot=snapshot,
            )

    def test_repository_rejects_old_plan_attempt_with_current_snapshot(
        self,
    ) -> None:
        old_binding = self.bundle.query_bindings[0]
        old_snapshot = self.store.get_authority_snapshot(
            self.case.case_id
        )
        old_spec = build_conformance_execution_spec(
            query_binding=old_binding,
            fixture_ref=(
                "waje-vnext://conformance-fixture/"
                "delayed-attempt.v1"
            ),
            fixture_content_sha256=content_sha256(
                {"fixture": "delayed-attempt.v1"}
            ),
            result_contract_ref=(
                "waje-vnext://result-contract/"
                "aggregate-contrast.v1"
            ),
            execution_policy_ref=(
                "waje-vnext://execution-policy/conformance.v1"
            ),
            created_at=NOW,
        )
        self.store.record_conformance_execution_spec(
            old_spec,
            expected_authority_snapshot=old_snapshot,
        )
        delayed_attempt = build_logical_execution_attempt(
            spec=old_spec,
            authority_snapshot=old_snapshot,
            attempt_number=1,
            prior_attempt=None,
            retry_reason_code=None,
            requested_at=NOW,
        )

        obligations = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )
        outcomes = tuple(
            self.store.get_measurement_resolution(outcome_id)
            for outcome_id in (
                self.bundle.adoption.resolution_outcome_ids
            )
        )
        admissions = tuple(
            self.store.get_measurement_resolution_admission(
                outcome.resolution_outcome_id
            )
            for outcome in outcomes
        )
        self.case, _ = record_plan_bundle(
            store=self.store,
            case=self.case,
            frame=self.frame,
            created_at=NOW + timedelta(minutes=1),
            plan_revision_id="plan-after-delayed-attempt",
            prior_plan=self.bundle.plan,
            proposed_tasks=(
                ProposedWorkTask(
                    proposal_task_key="replacement-plan-task",
                    business_purpose=(
                        "Adopt a replacement investigation ordering"
                    ),
                    capability_intent_ref=(
                        "waje-vnext://capability-intent/"
                        "measurement-evidence.v1"
                    ),
                    obligation_ids=tuple(
                        item.obligation_id for item in obligations
                    ),
                    depends_on_task_keys=(),
                ),
            ),
            measurement_authority=(
                outcomes,
                admissions,
                obligations,
            ),
        )
        current_snapshot = self.store.get_authority_snapshot(
            self.case.case_id
        )
        forged_attempt = replace(
            delayed_attempt,
            authority_snapshot=current_snapshot,
            authority_snapshot_sha256=(
                current_snapshot.content_sha256
            ),
        )
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "changes sealed input",
        ):
            self.store.record_logical_execution_attempt(
                forged_attempt
            )

    def test_forged_obligation_identity_is_rejected(self) -> None:
        obligation = self.store.list_evidence_obligations(
            self.frame.frame_revision_id
        )[0]
        forged = replace(
            obligation,
            field_derivation_proof_sha256="f" * 64,
        )
        with self.assertRaises(InvalidAuthorityTransition):
            self.store.record_evidence_obligation(
                forged,
                expected_head_version=self.case.head_version,
                event_id="event-forged-obligation",
            )


if __name__ == "__main__":
    unittest.main()
