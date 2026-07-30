from __future__ import annotations

from datetime import UTC, datetime, timedelta

from waje_vnext.domain.async_runtime import (
    MailboxMessageKind,
    OperationIdentity,
)
from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerClaim,
    AnswerStatus,
    AnswerVersion,
    ClaimVerifierStatus,
    EvidenceRecord,
    EvidenceStrength,
    EvidenceType,
    ReviewerObjection,
    ReviewerObjectionStatus,
    ReviewerSeverity,
    WorkPlanRevision,
    WorkTask,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.identity import build_analysis_frame_revision
from waje_vnext.domain.measurement import (
    AggregationOrder,
    CalendarUnit,
    ClaimStrengthCeiling,
    ClaimTargetKind,
    CompletenessPolicy,
    ContrastOperandSpec,
    ContrastOperator,
    ContrastSpec,
    DecisionObjective,
    DecisionObjectiveKind,
    EpistemicCompletionSpec,
    EstimandSpec,
    EstimatorFamily,
    EstimatorSpec,
    EvidenceComposition,
    EvidenceRequirementSpec,
    ExposureBasis,
    ExposureNormalization,
    ExposureSpec,
    IdentificationLevel,
    IdentificationSpec,
    IntervalBoundary,
    MeasurementDesign,
    MessageRole,
    MetricExpression,
    MissingExposurePolicy,
    ObservationUnitSpec,
    PairingRule,
    PopulationSpec,
    QuestionGrounding,
    QuestionRevision,
    RequirementBoundaryPolicy,
    ScopeExpression,
    SourceMessageRef,
    SourceMessageSpan,
    TemporalSemanticSpec,
    TimeRole,
    VariableDataType,
    VariableRole,
    VariableSpec,
    WindowRuleKind,
    WindowRuleSpec,
    WindowSelectionKind,
    EligibilitySpec,
)


NOW = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
QUESTION_TEXT = "比较目标月月初与上一个月月末的日均付费金额。"


def make_operation(
    *,
    operation_id: str = "operation-question",
    idempotency_key: str = "question-message-key",
    payload: dict[str, object] | None = None,
    case_id: str = "case-1",
) -> OperationIdentity:
    message_payload = payload or {"message": QUESTION_TEXT}
    return OperationIdentity(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        causation_id="user-turn-1",
        correlation_id=case_id,
        authority_revision=0,
        payload_sha256=content_sha256(message_payload),
    )


def make_question(
    *,
    revision_number: int = 1,
    question_id: str | None = None,
    prior_id: str | None = None,
    accepted_head_version: int = 1,
    event_id: str = "event-question",
    analysis_cycle_id: str | None = None,
    message_id: str = "message-1",
    message_sequence: int = 1,
    text: str = QUESTION_TEXT,
    case_id: str = "case-1",
) -> QuestionRevision:
    if case_id != "case-1":
        question_id = question_id or f"{case_id}:question:{revision_number}"
        event_id = (
            f"{case_id}:event:question:{revision_number}"
            if event_id == "event-question"
            else event_id
        )
        message_id = (
            f"{case_id}:message:{message_sequence}"
            if message_id == "message-1"
            else message_id
        )
    return QuestionRevision(
        question_revision_id=question_id
        or f"question-{revision_number}",
        case_id=case_id,
        revision_number=revision_number,
        prior_question_revision_id=prior_id,
        source_messages=(
            SourceMessageRef(
                message_id=message_id,
                role=MessageRole.USER,
                sequence=message_sequence,
                content=text,
                content_sha256=content_sha256(text),
            ),
        ),
        explicit_scope_refs=("source:message-1:target-month",),
        explicit_constraint_refs=(),
        explicit_correction_refs=(),
        explicit_challenge_refs=(),
        accepted_clarification_refs=(),
        acceptance_event_id=event_id,
        accepted_head_version=accepted_head_version,
        analysis_cycle_id=analysis_cycle_id
        or f"{case_id}:cycle:{revision_number}",
        created_at=NOW + timedelta(minutes=revision_number),
    )


def make_measurement_design(
    *,
    question_id: str = "question-1",
    left_period_offset: int = 0,
    right_period_offset: int = -1,
    window_days: int = 7,
    exposure_normalization: ExposureNormalization = (
        ExposureNormalization.PER_EXPOSURE_UNIT
    ),
    node_prefix: str = "",
    include_source_span: bool = True,
    source_message_id: str = "message-1",
    source_text: str = QUESTION_TEXT,
) -> MeasurementDesign:
    def node(name: str) -> str:
        return f"{node_prefix}{name}"

    span_start = source_text.index("目标月")
    span_end = len(source_text) - 1
    source_span = SourceMessageSpan(
        span_id=node("span-comparison"),
        message_id=source_message_id,
        start_codepoint=span_start,
        end_codepoint=span_end,
        selected_text_sha256=content_sha256(
            source_text[span_start:span_end]
        ),
    )
    paid_amount = VariableSpec(
        variable_id=node("variable-paid-amount"),
        concept_ref="metric:paid_amount:v1",
        data_type=VariableDataType.MONEY,
        unit_ref="currency:CNY",
        role=VariableRole.OUTCOME,
        expression_contract_ref="metric:paid_amount:v1",
    )
    period_key = VariableSpec(
        variable_id=node("variable-calendar-day"),
        concept_ref="dimension:calendar_day:v1",
        data_type=VariableDataType.TIMESTAMP,
        unit_ref="calendar:business-day",
        role=VariableRole.DIMENSION,
        expression_contract_ref=None,
    )
    population = PopulationSpec(
        population_id=node("population-all-valid-payments"),
        entity_universe_ref="population:all-valid-payments:v1",
        inclusion_predicate_ref="predicate:valid-paid-order:v1",
        exclusion_predicate_ref="predicate:fraud-or-refund:v1",
        sampling_frame_ref="sampling:full-population:v1",
    )
    observation = ObservationUnitSpec(
        observation_unit_id=node("observation-business-day"),
        entity_ref="entity:business-day",
        time_unit=CalendarUnit.DAY,
        grain_ref="grain:business-day",
        dedup_identity_variable_ids=(period_key.variable_id,),
    )
    metric = MetricExpression(
        metric_expression_id=node("metric-daily-paid-amount"),
        output_variable_id=paid_amount.variable_id,
        numerator_variable_ids=(paid_amount.variable_id,),
        denominator_variable_ids=(),
        aggregation_order=AggregationOrder.SUM,
        output_unit_ref="currency:CNY",
    )
    temporal = TemporalSemanticSpec(
        temporal_semantic_id=node("time-accounting-day"),
        primary_time_role=TimeRole.ACCOUNTING_TIME,
        time_variable_id=period_key.variable_id,
        prohibited_substitute_roles=(
            TimeRole.INGESTION_TIME,
            TimeRole.SNAPSHOT_TIME,
        ),
    )
    left_window = WindowRuleSpec(
        window_rule_id=node("window-target-month-start"),
        rule_kind=WindowRuleKind.RELATIVE_CALENDAR,
        anchor_ref="anchor:target-month",
        calendar_unit=CalendarUnit.MONTH,
        period_offset=left_period_offset,
        selection_kind=WindowSelectionKind.FIRST_N_CALENDAR_DAYS,
        selection_count=window_days,
        ordinal_start=None,
        ordinal_end=None,
        absolute_start=None,
        absolute_end=None,
        start_boundary=IntervalBoundary.INCLUSIVE,
        end_boundary=IntervalBoundary.INCLUSIVE,
        pairing_key_ref="pairing:target-month",
        selection_rationale_refs=("decision:comparable-seven-day-windows",),
    )
    right_window = WindowRuleSpec(
        window_rule_id=node("window-previous-month-end"),
        rule_kind=WindowRuleKind.RELATIVE_CALENDAR,
        anchor_ref="anchor:target-month",
        calendar_unit=CalendarUnit.MONTH,
        period_offset=right_period_offset,
        selection_kind=WindowSelectionKind.LAST_N_CALENDAR_DAYS,
        selection_count=window_days,
        ordinal_start=None,
        ordinal_end=None,
        absolute_start=None,
        absolute_end=None,
        start_boundary=IntervalBoundary.INCLUSIVE,
        end_boundary=IntervalBoundary.INCLUSIVE,
        pairing_key_ref="pairing:target-month",
        selection_rationale_refs=("decision:comparable-seven-day-windows",),
    )
    exposure = ExposureSpec(
        exposure_id=node("exposure-valid-observed-day"),
        basis=ExposureBasis.VALID,
        unit_ref="unit:valid-observed-day",
        normalization=exposure_normalization,
        aggregation_order=AggregationOrder.RATIO_OF_SUMS,
        zero_policy=MissingExposurePolicy.BLOCK,
        missing_policy=MissingExposurePolicy.DEGRADE,
        minimum_coverage_ratio="0.9",
        comparability_rule_ref="comparability:valid-day-exposure:v1",
    )
    estimator = EstimatorSpec(
        estimator_id=node("estimator-paid-amount-per-valid-day"),
        family=EstimatorFamily.RATE,
        metric_expression_id=metric.metric_expression_id,
        exposure_id=exposure.exposure_id,
        weight_variable_id=None,
        aggregation_order=AggregationOrder.RATIO_OF_SUMS,
        uncertainty_method_ref="uncertainty:paired-period-bootstrap:v1",
    )
    left_operand = ContrastOperandSpec(
        operand_id=node("operand-target-month-start"),
        role="left",
        window_rule_id=left_window.window_rule_id,
        population_id=population.population_id,
    )
    right_operand = ContrastOperandSpec(
        operand_id=node("operand-previous-month-end"),
        role="right",
        window_rule_id=right_window.window_rule_id,
        population_id=population.population_id,
    )
    contrast = ContrastSpec(
        contrast_id=node("contrast-month-start-vs-prior-month-end"),
        operands=(left_operand, right_operand),
        operator=ContrastOperator.DIFFERENCE,
        direction_from_operand_id=right_operand.operand_id,
        direction_to_operand_id=left_operand.operand_id,
        pairing_rule=PairingRule.BY_PERIOD,
    )
    eligibility = EligibilitySpec(
        eligibility_id=node("eligibility-complete-paired-windows"),
        completeness_policy=CompletenessPolicy.DEGRADE_INCOMPLETE,
        minimum_coverage_ratio="0.9",
        missingness_contract_ref="missingness:calendar-day-coverage:v1",
        exclusion_reason_refs=("exclusion:unreleased-period",),
    )
    identification = IdentificationSpec(
        identification_id=node("identification-descriptive"),
        level=IdentificationLevel.DESCRIPTIVE,
        assumption_refs=("assumption:payment-contract-stable",),
        counterfactual_ref=None,
        positivity_ref=None,
        consistency_ref=None,
        interference_ref=None,
    )
    scope = ScopeExpression(
        scope_id=node("scope-paired-windows"),
        entity_universe_ref=population.entity_universe_ref,
        dimension_domain_refs=("dimension:all-channels",),
        time_window_rule_ids=(
            left_window.window_rule_id,
            right_window.window_rule_id,
        ),
        predicate_ref=population.inclusion_predicate_ref,
        grain_ref=observation.grain_ref,
        unit_ref="currency:CNY-per-valid-observed-day",
        aggregation_path_ref="aggregation:ratio-of-sums",
        population_or_risk_set_ref=population.population_id,
        data_version_boundary_ref="release:resolved-at-runtime",
    )
    estimand_id = node("estimand-window-contrast")
    requirement_id = node("requirement-window-contrast")
    requirement = EvidenceRequirementSpec(
        evidence_requirement_id=requirement_id,
        target_estimand_ids=(estimand_id,),
        required_evidence_type_refs=("evidence:descriptive-contrast",),
        composition=EvidenceComposition.ALL,
        minimum_count=None,
        minimum_strength=ClaimStrengthCeiling.DESCRIPTIVE,
        scope_id=scope.scope_id,
        exposure_id=exposure.exposure_id,
        contradiction_policy_ref="contradiction:surface-and-degrade:v1",
        boundary_policy=RequirementBoundaryPolicy.ALLOW_TYPED_BOUNDARY,
        allowed_boundary_codes=(
            "incomplete_period",
            "insufficient_valid_exposure",
        ),
        linked_falsification_ids=(),
        linked_reversal_ids=(),
    )
    estimand = EstimandSpec(
        estimand_id=estimand_id,
        claim_target_kind=ClaimTargetKind.CONTRAST,
        variable_ids=(paid_amount.variable_id,),
        event_ids=(),
        population_id=population.population_id,
        observation_unit_id=observation.observation_unit_id,
        temporal_semantic_id=temporal.temporal_semantic_id,
        estimator_id=estimator.estimator_id,
        exposure_id=exposure.exposure_id,
        contrast_id=contrast.contrast_id,
        sequence_id=None,
        cohort_risk_set_id=None,
        reconciliation_id=None,
        relationship_id=None,
        eligibility_id=eligibility.eligibility_id,
        identification_id=identification.identification_id,
        evidence_requirement_ids=(requirement_id,),
        alternative_ids=(),
        sensitivity_ids=(),
        falsification_ids=(),
        reversal_ids=(),
        scope_ceiling_id=scope.scope_id,
        claim_strength_ceiling=ClaimStrengthCeiling.DESCRIPTIVE,
    )
    completion = EpistemicCompletionSpec(
        completion_spec_id=node("completion-window-contrast"),
        target_estimand_ids=(estimand_id,),
        required_evidence_requirement_ids=(requirement_id,),
        success_policy_ref="completion:all-required-evidence-valid:v1",
        degrade_policy_ref="degrade:typed-boundary:v1",
        stop_policy_ref="stop:no-lawful-comparison:v1",
    )
    return MeasurementDesign(
        question_grounding=QuestionGrounding(
            grounding_id=node("grounding-question"),
            question_revision_id=question_id,
            source_spans=(source_span,) if include_source_span else (),
            decision_record_ids=(),
            semantic_contract_refs=(
                "metric:paid_amount:v1",
                "dimension:calendar_day:v1",
            ),
        ),
        decision_objective=DecisionObjective(
            objective_id=node("objective-compare-windows"),
            kind=DecisionObjectiveKind.UNDERSTAND,
            requested_output_refs=("output:contrast-direction-and-size",),
            excluded_action_refs=("action:automatic-business-change",),
        ),
        variables=(paid_amount, period_key),
        events=(),
        populations=(population,),
        observation_units=(observation,),
        metric_expressions=(metric,),
        temporal_semantics=(temporal,),
        window_rules=(left_window, right_window),
        exposures=(exposure,),
        estimators=(estimator,),
        contrasts=(contrast,),
        eligibilities=(eligibility,),
        sequences=(),
        cohort_risk_sets=(),
        reconciliations=(),
        relationships=(),
        identifications=(identification,),
        assumptions=(),
        alternatives=(),
        sensitivities=(),
        falsifications=(),
        reversals=(),
        scopes=(scope,),
        evidence_requirements=(requirement,),
        completion_specs=(completion,),
        estimands=(estimand,),
    )


def make_frame(
    *,
    revision_number: int = 1,
    frame_id: str | None = None,
    prior_id: str | None = None,
    action_id: str | None = None,
    question: QuestionRevision | None = None,
    measurement_design: MeasurementDesign | None = None,
    case_id: str = "case-1",
) -> AnalysisFrameRevision:
    bound_question = question or make_question(case_id=case_id)
    design = measurement_design or make_measurement_design(
        question_id=bound_question.question_revision_id,
        source_message_id=bound_question.source_messages[0].message_id,
        source_text=bound_question.source_messages[0].content,
    )
    return build_analysis_frame_revision(
        question=bound_question,
        frame_revision_id=frame_id or f"frame-{revision_number}",
        case_id=case_id,
        revision_number=revision_number,
        prior_frame_revision_id=prior_id,
        created_by_action_id=action_id
        or f"action-frame-{revision_number}",
        created_at=NOW
        + timedelta(minutes=60 * (revision_number - 1) + 1),
        revision_reason_ref="reason:define-current-measurement",
        measurement_design=design,
    )


def accept_initial_question(store, case):
    payload = {"message": QUESTION_TEXT}
    message_id = (
        "message-1"
        if case.case_id == "case-1"
        else f"{case.case_id}:message:1"
    )
    store.append_mailbox_message(
        message_id=message_id,
        case_id=case.case_id,
        kind=MailboxMessageKind.USER_MESSAGE,
        operation=make_operation(
            operation_id=f"{case.case_id}:operation:question",
            idempotency_key=f"{case.case_id}:question-message-key",
            payload=payload,
            case_id=case.case_id,
        ),
        payload=payload,
        created_at=NOW,
    )
    question = make_question(case_id=case.case_id)
    case = store.accept_question(
        question,
        expected_head_version=case.head_version,
        event_id=question.acceptance_event_id,
        recorded_at=question.created_at,
    )
    return case, question


def make_plan(
    *,
    frame_id: str = "frame-1",
    revision_number: int = 1,
    plan_id: str | None = None,
    prior_id: str | None = None,
    action_id: str | None = None,
    case_id: str = "case-1",
) -> WorkPlanRevision:
    return WorkPlanRevision(
        plan_revision_id=plan_id or f"plan-{revision_number}",
        case_id=case_id,
        frame_revision_id=frame_id,
        revision_number=revision_number,
        prior_plan_revision_id=prior_id,
        created_by_action_id=action_id or f"action-plan-{revision_number}",
        created_at=NOW
        + timedelta(minutes=60 * (revision_number - 1) + 11),
        revision_reason="Investigate the accepted frame",
        tasks=(
            WorkTask(
                task_id="task-pattern",
                business_purpose="Measure the accepted window contrast",
                capability_intent="descriptive contrast with valid exposure",
                target_claim_ids=("claim-pattern",),
                depends_on_task_ids=(),
                success_conditions=("Accepted estimand evidence is measured",),
                stop_conditions=("Coverage is insufficient",),
            ),
        ),
    )


def make_evidence(
    *,
    evidence_id: str = "evidence-1",
    frame_id: str = "frame-1",
    plan_id: str = "plan-1",
    payload: dict[str, object] | None = None,
    case_id: str = "case-1",
) -> EvidenceRecord:
    inline_payload = payload or {
        "left_daily_amount": "120.0",
        "right_daily_amount": "100.0",
        "left_valid_days": 7,
        "right_valid_days": 7,
    }
    return EvidenceRecord(
        evidence_record_id=evidence_id,
        case_id=case_id,
        frame_revision_id=frame_id,
        plan_revision_id=plan_id,
        task_id="task-pattern",
        capability_name="descriptive_contrast",
        query_spec_ref="query-spec-1",
        semantic_contract_refs=("metric:paid_amount:v1",),
        snapshot_release_ref="release-2026-07-29",
        grain="business_day",
        evidence_type=EvidenceType.DESCRIPTIVE,
        strength=EvidenceStrength.QUANTIFIED,
        business_summary="The accepted normalized contrast was measured",
        limitations=("Descriptive evidence does not identify a mechanism",),
        provenance={
            "query_spec_ref": "query-spec-1",
            "snapshot_release_ref": "release-2026-07-29",
        },
        payload_sha256=content_sha256(inline_payload),
        inline_payload=inline_payload,
        result_handle=None,
        created_at=NOW + timedelta(minutes=20),
    )


def make_answer(
    *,
    status: AnswerStatus = AnswerStatus.PROVISIONAL,
    answer_id: str = "answer-1",
    frame_id: str = "frame-1",
    plan_id: str = "plan-1",
    evidence_id: str = "evidence-1",
    version_number: int = 1,
    prior_id: str | None = None,
    unresolved: tuple[str, ...] = (),
    verifier_status: ClaimVerifierStatus = ClaimVerifierStatus.ACCEPTED,
    case_id: str = "case-1",
) -> AnswerVersion:
    claims = (
        AnswerClaim(
            claim_id="claim-pattern",
            statement="The accepted normalized left window is higher",
            applicability="Accepted frame and release-2026-07-29",
            evidence_record_ids=(evidence_id,),
            boundary_ref=None,
            limitations=("Mechanism remains unproven",),
            verifier_status=verifier_status,
            reviewer_objection_ids=(),
        ),
    )
    return AnswerVersion(
        answer_version_id=answer_id,
        case_id=case_id,
        frame_revision_id=frame_id,
        plan_revision_id=plan_id,
        version_number=version_number,
        prior_answer_version_id=prior_id,
        status=status,
        claims=claims,
        narrative_markdown="The accepted normalized comparison is provisional.",
        verifier_policy_version="answer-verifier.v1",
        unresolved_blocking_objection_ids=unresolved,
        settlement_fingerprint=None,
        created_by_action_id="action-answer-1",
        created_at=NOW
        + timedelta(minutes=60 * (version_number - 1) + 30),
    )


def make_objection(
    *,
    objection_id: str = "objection-1",
    revision_number: int = 1,
    prior_id: str | None = None,
    status: ReviewerObjectionStatus = ReviewerObjectionStatus.OPEN,
    answer_id: str = "answer-1",
    case_id: str = "case-1",
) -> ReviewerObjection:
    resolved = status is not ReviewerObjectionStatus.OPEN
    created_at = NOW + timedelta(minutes=30 + revision_number)
    return ReviewerObjection(
        objection_id=objection_id,
        objection_key="claim-pattern:causal-overreach",
        revision_number=revision_number,
        prior_objection_id=prior_id,
        case_id=case_id,
        answer_version_id=answer_id,
        claim_id="claim-pattern",
        severity=ReviewerSeverity.BLOCKING,
        status=status,
        risk_type="claim_strength",
        evidence_gap="Descriptive evidence cannot support a causal mechanism",
        requested_action="Limit the claim to the measured association",
        disposition_note=(
            "Claim language now matches the descriptive evidence"
            if resolved
            else None
        ),
        created_at=created_at,
        resolved_at=(
            created_at + timedelta(minutes=1) if resolved else None
        ),
    )
