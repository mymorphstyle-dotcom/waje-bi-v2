"""Schema-epoch 3 measurement authority and derived trust records."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Iterable

from .async_runtime import AuthoritySnapshot
from .canonical import (
    content_sha256,
    require_aware_datetime,
    require_nonempty,
    require_sha256,
)


SCHEMA_EPOCH = 3
IDENTITY_ALGORITHM_VERSION = "measurement-identity.v1"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM_DECISION = "system_decision"


class DecisionObjectiveKind(StrEnum):
    UNDERSTAND = "understand"
    JUDGE = "judge"
    CHOOSE = "choose"
    DEFINE = "define"
    VALIDATE = "validate"


class ClaimTargetKind(StrEnum):
    DEFINITION = "definition"
    DATA_QUALITY_STATE = "data_quality_state"
    POINT_QUANTITY = "point_quantity"
    DISTRIBUTION = "distribution"
    TEMPORAL_PATTERN = "temporal_pattern"
    CONTRAST = "contrast"
    COMPOSITION = "composition"
    ACCOUNTING_DECOMPOSITION = "accounting_decomposition"
    COHORT_OUTCOME = "cohort_outcome"
    FUNNEL_TRANSITION = "funnel_transition"
    ASSOCIATION = "association"
    CAUSAL_EFFECT = "causal_effect"
    DIAGNOSTIC_SET = "diagnostic_set"


class ClaimStrengthCeiling(StrEnum):
    BOUNDARY_ONLY = "boundary_only"
    DESCRIPTIVE = "descriptive"
    ACCOUNTING = "accounting"
    ASSOCIATIONAL = "associational"
    CAUSAL = "causal"


class VariableDataType(StrEnum):
    MONEY = "money"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    CATEGORY = "category"
    TIMESTAMP = "timestamp"
    DURATION = "duration"


class VariableRole(StrEnum):
    OUTCOME = "outcome"
    EXPOSURE = "exposure"
    NUMERATOR = "numerator"
    DENOMINATOR = "denominator"
    DIMENSION = "dimension"
    WEIGHT = "weight"
    COVARIATE = "covariate"
    EVENT_ATTRIBUTE = "event_attribute"


class TimeRole(StrEnum):
    EVENT_TIME = "event_time"
    ACCOUNTING_TIME = "accounting_time"
    INGESTION_TIME = "ingestion_time"
    SNAPSHOT_TIME = "snapshot_time"


class WindowRuleKind(StrEnum):
    ABSOLUTE_INTERVAL = "absolute_interval"
    RELATIVE_CALENDAR = "relative_calendar"
    ROLLING_INTERVAL = "rolling_interval"
    BUSINESS_CALENDAR = "business_calendar"


class CalendarUnit(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    FISCAL_PERIOD = "fiscal_period"


class WindowSelectionKind(StrEnum):
    COMPLETE_PERIOD = "complete_period"
    FIRST_N_CALENDAR_DAYS = "first_n_calendar_days"
    LAST_N_CALENDAR_DAYS = "last_n_calendar_days"
    FIRST_N_VALID_BUSINESS_DAYS = "first_n_valid_business_days"
    LAST_N_VALID_BUSINESS_DAYS = "last_n_valid_business_days"
    ORDINAL_RANGE = "ordinal_range"
    ROLLING_LENGTH = "rolling_length"


class IntervalBoundary(StrEnum):
    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"


class AggregationOrder(StrEnum):
    SUM = "sum"
    MEAN = "mean"
    RATIO_OF_SUMS = "ratio_of_sums"
    MEAN_OF_RATIOS = "mean_of_ratios"
    WEIGHTED_MEAN = "weighted_mean"
    QUANTILE = "quantile"
    DISTRIBUTION = "distribution"
    ACCOUNTING_IDENTITY = "accounting_identity"


class EstimatorFamily(StrEnum):
    TOTAL = "total"
    MEAN = "mean"
    RATE = "rate"
    RATIO = "ratio"
    QUANTILE = "quantile"
    DISTRIBUTION = "distribution"
    ACCOUNTING_BRIDGE = "accounting_bridge"
    ASSOCIATION = "association"
    EFFECT = "effect"


class ExposureBasis(StrEnum):
    CALENDAR = "calendar"
    ELIGIBLE = "eligible"
    OBSERVED = "observed"
    VALID = "valid"
    MISSING_INVALID = "missing_invalid"
    AT_RISK = "at_risk"


class ExposureNormalization(StrEnum):
    NONE = "none"
    PER_EXPOSURE_UNIT = "per_exposure_unit"
    WEIGHTED_BY_EXPOSURE = "weighted_by_exposure"


class MissingExposurePolicy(StrEnum):
    EXCLUDE = "exclude"
    DEGRADE = "degrade"
    BLOCK = "block"
    TREAT_AS_ZERO = "treat_as_zero"


class ContrastOperator(StrEnum):
    DIFFERENCE = "difference"
    RELATIVE_CHANGE = "relative_change"
    RATIO = "ratio"
    INDEX = "index"


class PairingRule(StrEnum):
    UNPAIRED = "unpaired"
    BY_PERIOD = "by_period"
    BY_ENTITY = "by_entity"
    BY_PERIOD_AND_ENTITY = "by_period_and_entity"


class CompletenessPolicy(StrEnum):
    REQUIRE_COMPLETE = "require_complete"
    EXCLUDE_INCOMPLETE = "exclude_incomplete"
    DEGRADE_INCOMPLETE = "degrade_incomplete"
    ALLOW_PARTIAL_WITH_EXPOSURE = "allow_partial_with_exposure"


class IdentificationLevel(StrEnum):
    DESCRIPTIVE = "descriptive"
    ACCOUNTING = "accounting"
    ASSOCIATIONAL = "associational"
    CAUSAL = "causal"


class EvidenceComposition(StrEnum):
    ALL = "all"
    ANY = "any"
    AT_LEAST = "at_least"


class RequirementBoundaryPolicy(StrEnum):
    BLOCK = "block"
    ALLOW_TYPED_BOUNDARY = "allow_typed_boundary"
    ALLOW_CLAIM_DEGRADE = "allow_claim_degrade"


class ResolutionOutcomeKind(StrEnum):
    RESOLVED_INSTANCE = "resolved_instance"
    TYPED_RESOLUTION_BOUNDARY = "typed_resolution_boundary"


class ObligationExecutionDisposition(StrEnum):
    EXECUTABLE = "executable"
    TYPED_BOUNDARY = "typed_boundary"
    BLOCKED = "blocked"


class AmbiguousLocalTimePolicy(StrEnum):
    EARLIEST_FOLD = "earliest_fold"
    LATEST_FOLD = "latest_fold"
    REJECT = "reject"


class ExposureFactSourceKind(StrEnum):
    CONTRACT_CATALOG = "contract_catalog"
    SNAPSHOT_CATALOG = "snapshot_catalog"
    CALENDAR_DERIVATION = "calendar_derivation"


@dataclass(frozen=True, slots=True)
class SourceMessageRef:
    message_id: str
    role: MessageRole
    sequence: int
    content: str
    content_sha256: str

    def __post_init__(self) -> None:
        require_nonempty(self.message_id, "message_id")
        _require_enum(self.role, MessageRole, "role")
        if self.sequence < 1:
            raise ValueError("source message sequence must be positive")
        require_nonempty(self.content, "content")
        require_sha256(self.content_sha256, "content_sha256")
        if content_sha256(self.content) != self.content_sha256:
            raise ValueError("source message content hash does not match")


@dataclass(frozen=True, slots=True)
class SourceMessageSpan:
    span_id: str
    message_id: str
    start_codepoint: int
    end_codepoint: int
    selected_text_sha256: str

    def __post_init__(self) -> None:
        require_nonempty(self.span_id, "span_id")
        require_nonempty(self.message_id, "message_id")
        if self.start_codepoint < 0:
            raise ValueError("span start must be non-negative")
        if self.end_codepoint <= self.start_codepoint:
            raise ValueError("span end must follow span start")
        require_sha256(
            self.selected_text_sha256,
            "selected_text_sha256",
        )


@dataclass(frozen=True, slots=True)
class QuestionRevision:
    question_revision_id: str
    case_id: str
    revision_number: int
    prior_question_revision_id: str | None
    source_messages: tuple[SourceMessageRef, ...]
    explicit_scope_refs: tuple[str, ...]
    explicit_constraint_refs: tuple[str, ...]
    explicit_correction_refs: tuple[str, ...]
    explicit_challenge_refs: tuple[str, ...]
    accepted_clarification_refs: tuple[str, ...]
    acceptance_event_id: str
    accepted_head_version: int
    analysis_cycle_id: str
    created_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for field_name in (
            "question_revision_id",
            "case_id",
            "acceptance_event_id",
            "analysis_cycle_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _validate_revision_lineage(
            self.revision_number,
            self.prior_question_revision_id,
            "question",
        )
        if self.accepted_head_version < 1:
            raise ValueError(
                "accepted question head version must be positive"
            )
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("question revision requires schema epoch 3")
        _require_tuple_of(
            self.source_messages,
            SourceMessageRef,
            "source_messages",
        )
        if not self.source_messages:
            raise ValueError("question revision requires source messages")
        sequences = tuple(item.sequence for item in self.source_messages)
        if tuple(sorted(sequences)) != sequences:
            raise ValueError("question source messages must be ordered")
        if len(sequences) != len(set(sequences)):
            raise ValueError("question source message sequences must be unique")
        _validate_string_tuple_fields(
            self,
            (
                "explicit_scope_refs",
                "explicit_constraint_refs",
                "explicit_correction_refs",
                "explicit_challenge_refs",
                "accepted_clarification_refs",
            ),
        )
        require_aware_datetime(self.created_at, "created_at")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)

    def validate_spans(
        self,
        spans: tuple[SourceMessageSpan, ...],
    ) -> None:
        messages = {
            message.message_id: message.content
            for message in self.source_messages
        }
        _require_unique_ids(
            (span.span_id for span in spans),
            "source span",
        )
        for span in spans:
            content = messages.get(span.message_id)
            if content is None:
                raise ValueError("source span references an unknown message")
            if span.end_codepoint > len(content):
                raise ValueError("source span exceeds message length")
            selected = content[
                span.start_codepoint : span.end_codepoint
            ]
            if content_sha256(selected) != span.selected_text_sha256:
                raise ValueError("source span selected text hash does not match")


@dataclass(frozen=True, slots=True)
class QuestionGrounding:
    grounding_id: str
    question_revision_id: str
    source_spans: tuple[SourceMessageSpan, ...]
    decision_record_ids: tuple[str, ...]
    semantic_contract_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.grounding_id, "grounding_id")
        require_nonempty(
            self.question_revision_id,
            "question_revision_id",
        )
        _require_tuple_of(
            self.source_spans,
            SourceMessageSpan,
            "source_spans",
        )
        _validate_string_tuple_fields(
            self,
            ("decision_record_ids", "semantic_contract_refs"),
        )
        if not self.source_spans and not (
            self.decision_record_ids or self.semantic_contract_refs
        ):
            raise ValueError("question grounding requires replayable support")


@dataclass(frozen=True, slots=True)
class DecisionObjective:
    objective_id: str
    kind: DecisionObjectiveKind
    requested_output_refs: tuple[str, ...]
    excluded_action_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.objective_id, "objective_id")
        _require_enum(self.kind, DecisionObjectiveKind, "kind")
        _validate_string_tuple_fields(
            self,
            ("requested_output_refs", "excluded_action_refs"),
        )
        if not self.requested_output_refs:
            raise ValueError("decision objective requires an output")


@dataclass(frozen=True, slots=True)
class VariableSpec:
    variable_id: str
    concept_ref: str
    data_type: VariableDataType
    unit_ref: str
    role: VariableRole
    expression_contract_ref: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("variable_id", "concept_ref", "unit_ref"):
            require_nonempty(getattr(self, field_name), field_name)
        _require_enum(self.data_type, VariableDataType, "data_type")
        _require_enum(self.role, VariableRole, "role")
        _validate_optional_ref(
            self.expression_contract_ref,
            "expression_contract_ref",
        )


@dataclass(frozen=True, slots=True)
class EventSpec:
    event_id: str
    event_contract_ref: str
    event_time_variable_id: str
    entity_key_variable_ids: tuple[str, ...]
    qualifying_predicate_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "event_contract_ref",
            "event_time_variable_id",
            "qualifying_predicate_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _validate_string_tuple_fields(self, ("entity_key_variable_ids",))
        if not self.entity_key_variable_ids:
            raise ValueError("event requires entity keys")


@dataclass(frozen=True, slots=True)
class PopulationSpec:
    population_id: str
    entity_universe_ref: str
    inclusion_predicate_ref: str
    exclusion_predicate_ref: str | None
    sampling_frame_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "population_id",
            "entity_universe_ref",
            "inclusion_predicate_ref",
            "sampling_frame_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _validate_optional_ref(
            self.exclusion_predicate_ref,
            "exclusion_predicate_ref",
        )


@dataclass(frozen=True, slots=True)
class ObservationUnitSpec:
    observation_unit_id: str
    entity_ref: str
    time_unit: CalendarUnit | None
    grain_ref: str
    dedup_identity_variable_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "observation_unit_id",
            "entity_ref",
            "grain_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.time_unit is not None:
            _require_enum(self.time_unit, CalendarUnit, "time_unit")
        _validate_string_tuple_fields(
            self,
            ("dedup_identity_variable_ids",),
        )
        if not self.dedup_identity_variable_ids:
            raise ValueError("observation unit requires dedup identity")


@dataclass(frozen=True, slots=True)
class MetricExpression:
    metric_expression_id: str
    output_variable_id: str
    numerator_variable_ids: tuple[str, ...]
    denominator_variable_ids: tuple[str, ...]
    aggregation_order: AggregationOrder
    output_unit_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "metric_expression_id",
            "output_variable_id",
            "output_unit_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _require_enum(
            self.aggregation_order,
            AggregationOrder,
            "aggregation_order",
        )
        _validate_string_tuple_fields(
            self,
            ("numerator_variable_ids", "denominator_variable_ids"),
        )
        if not self.numerator_variable_ids:
            raise ValueError("metric expression requires a numerator")
        ratio_orders = {
            AggregationOrder.RATIO_OF_SUMS,
            AggregationOrder.MEAN_OF_RATIOS,
        }
        if (
            self.aggregation_order in ratio_orders
            and not self.denominator_variable_ids
        ):
            raise ValueError("ratio metric requires a denominator")


@dataclass(frozen=True, slots=True)
class TemporalSemanticSpec:
    temporal_semantic_id: str
    primary_time_role: TimeRole
    time_variable_id: str
    prohibited_substitute_roles: tuple[TimeRole, ...]

    def __post_init__(self) -> None:
        require_nonempty(
            self.temporal_semantic_id,
            "temporal_semantic_id",
        )
        require_nonempty(self.time_variable_id, "time_variable_id")
        _require_enum(
            self.primary_time_role,
            TimeRole,
            "primary_time_role",
        )
        _require_tuple_of(
            self.prohibited_substitute_roles,
            TimeRole,
            "prohibited_substitute_roles",
        )
        if self.primary_time_role in self.prohibited_substitute_roles:
            raise ValueError("primary time role cannot be prohibited")


@dataclass(frozen=True, slots=True)
class WindowRuleSpec:
    window_rule_id: str
    rule_kind: WindowRuleKind
    anchor_ref: str
    calendar_unit: CalendarUnit
    period_offset: int
    selection_kind: WindowSelectionKind
    selection_count: int | None
    ordinal_start: int | None
    ordinal_end: int | None
    absolute_start: date | None
    absolute_end: date | None
    start_boundary: IntervalBoundary
    end_boundary: IntervalBoundary
    pairing_key_ref: str
    selection_rationale_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "window_rule_id",
            "anchor_ref",
            "pairing_key_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _require_enum(self.rule_kind, WindowRuleKind, "rule_kind")
        _require_enum(self.calendar_unit, CalendarUnit, "calendar_unit")
        _require_enum(
            self.selection_kind,
            WindowSelectionKind,
            "selection_kind",
        )
        _require_enum(
            self.start_boundary,
            IntervalBoundary,
            "start_boundary",
        )
        _require_enum(
            self.end_boundary,
            IntervalBoundary,
            "end_boundary",
        )
        _validate_string_tuple_fields(
            self,
            ("selection_rationale_refs",),
        )
        count_selections = {
            WindowSelectionKind.FIRST_N_CALENDAR_DAYS,
            WindowSelectionKind.LAST_N_CALENDAR_DAYS,
            WindowSelectionKind.FIRST_N_VALID_BUSINESS_DAYS,
            WindowSelectionKind.LAST_N_VALID_BUSINESS_DAYS,
            WindowSelectionKind.ROLLING_LENGTH,
        }
        if self.selection_kind in count_selections:
            if self.selection_count is None or self.selection_count < 1:
                raise ValueError("window selection requires positive count")
        elif self.selection_count is not None:
            raise ValueError("window selection count is not applicable")
        if self.selection_kind is WindowSelectionKind.ORDINAL_RANGE:
            if (
                self.ordinal_start is None
                or self.ordinal_end is None
                or self.ordinal_start < 1
                or self.ordinal_end < self.ordinal_start
            ):
                raise ValueError("ordinal window range is invalid")
        elif self.ordinal_start is not None or self.ordinal_end is not None:
            raise ValueError("ordinal fields are not applicable")
        if self.rule_kind is WindowRuleKind.ABSOLUTE_INTERVAL:
            if (
                self.absolute_start is None
                or self.absolute_end is None
                or self.absolute_end < self.absolute_start
            ):
                raise ValueError("absolute window interval is invalid")
            if self.period_offset != 0:
                raise ValueError("absolute window cannot carry period offset")
        elif self.absolute_start is not None or self.absolute_end is not None:
            raise ValueError("absolute dates require absolute interval rule")


@dataclass(frozen=True, slots=True)
class ExposureSpec:
    exposure_id: str
    basis: ExposureBasis
    unit_ref: str
    normalization: ExposureNormalization
    aggregation_order: AggregationOrder
    zero_policy: MissingExposurePolicy
    missing_policy: MissingExposurePolicy
    minimum_coverage_ratio: str
    comparability_rule_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "exposure_id",
            "unit_ref",
            "comparability_rule_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _require_enum(self.basis, ExposureBasis, "basis")
        _require_enum(
            self.normalization,
            ExposureNormalization,
            "normalization",
        )
        _require_enum(
            self.aggregation_order,
            AggregationOrder,
            "aggregation_order",
        )
        _require_enum(
            self.zero_policy,
            MissingExposurePolicy,
            "zero_policy",
        )
        _require_enum(
            self.missing_policy,
            MissingExposurePolicy,
            "missing_policy",
        )
        _require_decimal_ratio(
            self.minimum_coverage_ratio,
            "minimum_coverage_ratio",
        )


@dataclass(frozen=True, slots=True)
class EstimatorSpec:
    estimator_id: str
    family: EstimatorFamily
    metric_expression_id: str
    exposure_id: str | None
    weight_variable_id: str | None
    aggregation_order: AggregationOrder
    uncertainty_method_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "estimator_id",
            "metric_expression_id",
            "uncertainty_method_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _require_enum(self.family, EstimatorFamily, "family")
        _require_enum(
            self.aggregation_order,
            AggregationOrder,
            "aggregation_order",
        )
        _validate_optional_ref(self.exposure_id, "exposure_id")
        _validate_optional_ref(
            self.weight_variable_id,
            "weight_variable_id",
        )
        if (
            self.family in {EstimatorFamily.RATE, EstimatorFamily.RATIO}
            and self.exposure_id is None
        ):
            raise ValueError("rate or ratio estimator requires exposure")


@dataclass(frozen=True, slots=True)
class ContrastOperandSpec:
    operand_id: str
    role: str
    window_rule_id: str
    population_id: str | None

    def __post_init__(self) -> None:
        for field_name in ("operand_id", "role", "window_rule_id"):
            require_nonempty(getattr(self, field_name), field_name)
        _validate_optional_ref(self.population_id, "population_id")


@dataclass(frozen=True, slots=True)
class ContrastSpec:
    contrast_id: str
    operands: tuple[ContrastOperandSpec, ...]
    operator: ContrastOperator
    direction_from_operand_id: str
    direction_to_operand_id: str
    pairing_rule: PairingRule

    def __post_init__(self) -> None:
        for field_name in (
            "contrast_id",
            "direction_from_operand_id",
            "direction_to_operand_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _require_tuple_of(
            self.operands,
            ContrastOperandSpec,
            "operands",
        )
        if len(self.operands) < 2:
            raise ValueError("contrast requires at least two operands")
        operand_ids = tuple(item.operand_id for item in self.operands)
        _require_unique_ids(operand_ids, "contrast operand")
        if (
            self.direction_from_operand_id not in operand_ids
            or self.direction_to_operand_id not in operand_ids
            or self.direction_from_operand_id
            == self.direction_to_operand_id
        ):
            raise ValueError("contrast direction must bind distinct operands")
        _require_enum(self.operator, ContrastOperator, "operator")
        _require_enum(self.pairing_rule, PairingRule, "pairing_rule")


@dataclass(frozen=True, slots=True)
class EligibilitySpec:
    eligibility_id: str
    completeness_policy: CompletenessPolicy
    minimum_coverage_ratio: str
    missingness_contract_ref: str
    exclusion_reason_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.eligibility_id, "eligibility_id")
        require_nonempty(
            self.missingness_contract_ref,
            "missingness_contract_ref",
        )
        _require_enum(
            self.completeness_policy,
            CompletenessPolicy,
            "completeness_policy",
        )
        _require_decimal_ratio(
            self.minimum_coverage_ratio,
            "minimum_coverage_ratio",
        )
        _validate_string_tuple_fields(self, ("exclusion_reason_refs",))


@dataclass(frozen=True, slots=True)
class SequenceSpec:
    sequence_id: str
    ordered_event_ids: tuple[str, ...]
    entity_continuity_ref: str
    transition_timeout_ref: str
    denominator_dynamics_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "sequence_id",
            "entity_continuity_ref",
            "transition_timeout_ref",
            "denominator_dynamics_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _validate_string_tuple_fields(self, ("ordered_event_ids",))
        if len(self.ordered_event_ids) < 2:
            raise ValueError("sequence requires at least two events")


@dataclass(frozen=True, slots=True)
class CohortRiskSetSpec:
    cohort_risk_set_id: str
    entry_event_id: str
    time_origin_ref: str
    horizon_ref: str
    at_risk_rule_ref: str
    censoring_rule_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class ReconciliationSpec:
    reconciliation_id: str
    identity_contract_ref: str
    allocation_rule_ref: str
    residual_variable_id: str
    tolerance_decimal: str

    def __post_init__(self) -> None:
        for field_name in (
            "reconciliation_id",
            "identity_contract_ref",
            "allocation_rule_ref",
            "residual_variable_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _require_nonnegative_decimal(
            self.tolerance_decimal,
            "tolerance_decimal",
        )


@dataclass(frozen=True, slots=True)
class RelationshipSpec:
    relationship_id: str
    exposure_variable_ids: tuple[str, ...]
    outcome_variable_ids: tuple[str, ...]
    confounder_variable_ids: tuple[str, ...]
    adjustment_contract_ref: str
    temporal_order_contract_ref: str

    def __post_init__(self) -> None:
        require_nonempty(self.relationship_id, "relationship_id")
        require_nonempty(
            self.adjustment_contract_ref,
            "adjustment_contract_ref",
        )
        require_nonempty(
            self.temporal_order_contract_ref,
            "temporal_order_contract_ref",
        )
        _validate_string_tuple_fields(
            self,
            (
                "exposure_variable_ids",
                "outcome_variable_ids",
                "confounder_variable_ids",
            ),
        )
        if not self.exposure_variable_ids or not self.outcome_variable_ids:
            raise ValueError("relationship requires exposure and outcome")


@dataclass(frozen=True, slots=True)
class IdentificationSpec:
    identification_id: str
    level: IdentificationLevel
    assumption_refs: tuple[str, ...]
    counterfactual_ref: str | None
    positivity_ref: str | None
    consistency_ref: str | None
    interference_ref: str | None

    def __post_init__(self) -> None:
        require_nonempty(self.identification_id, "identification_id")
        _require_enum(self.level, IdentificationLevel, "level")
        _validate_string_tuple_fields(self, ("assumption_refs",))
        for field_name in (
            "counterfactual_ref",
            "positivity_ref",
            "consistency_ref",
            "interference_ref",
        ):
            _validate_optional_ref(getattr(self, field_name), field_name)
        if self.level is IdentificationLevel.CAUSAL:
            required = (
                self.counterfactual_ref,
                self.positivity_ref,
                self.consistency_ref,
                self.interference_ref,
            )
            if not self.assumption_refs or any(item is None for item in required):
                raise ValueError(
                    "causal identification requires complete assumptions"
                )


@dataclass(frozen=True, slots=True)
class AssumptionSpec:
    assumption_id: str
    statement_ref: str
    support_refs: tuple[str, ...]
    risk_level: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(self, ("support_refs",))


@dataclass(frozen=True, slots=True)
class AlternativeSpec:
    alternative_id: str
    hypothesis_ref: str
    test_requirement_ids: tuple[str, ...]
    disposition_policy_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(self, ("test_requirement_ids",))
        if not self.test_requirement_ids:
            raise ValueError("alternative requires a test")


@dataclass(frozen=True, slots=True)
class SensitivitySpec:
    sensitivity_id: str
    changed_node_ids: tuple[str, ...]
    derived_relation_ref: str
    expected_evidence_relation_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(self, ("changed_node_ids",))
        if not self.changed_node_ids:
            raise ValueError("sensitivity requires changed nodes")


@dataclass(frozen=True, slots=True)
class FalsificationSpec:
    falsification_id: str
    observable_condition_ref: str
    evidence_requirement_ids: tuple[str, ...]
    disposition_policy_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(
            self,
            ("evidence_requirement_ids",),
        )
        if not self.evidence_requirement_ids:
            raise ValueError("falsification requires evidence")


@dataclass(frozen=True, slots=True)
class ReversalSpec:
    reversal_id: str
    result_condition_ref: str
    affected_estimand_ids: tuple[str, ...]
    direction_change_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(self, ("affected_estimand_ids",))
        if not self.affected_estimand_ids:
            raise ValueError("reversal requires affected estimands")


@dataclass(frozen=True, slots=True)
class ScopeExpression:
    scope_id: str
    entity_universe_ref: str
    dimension_domain_refs: tuple[str, ...]
    time_window_rule_ids: tuple[str, ...]
    predicate_ref: str
    grain_ref: str
    unit_ref: str
    aggregation_path_ref: str
    population_or_risk_set_ref: str | None
    data_version_boundary_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "scope_id",
            "entity_universe_ref",
            "predicate_ref",
            "grain_ref",
            "unit_ref",
            "aggregation_path_ref",
            "data_version_boundary_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _validate_string_tuple_fields(
            self,
            ("dimension_domain_refs", "time_window_rule_ids"),
        )
        _validate_optional_ref(
            self.population_or_risk_set_ref,
            "population_or_risk_set_ref",
        )


@dataclass(frozen=True, slots=True)
class EvidenceRequirementSpec:
    evidence_requirement_id: str
    target_estimand_ids: tuple[str, ...]
    required_evidence_type_refs: tuple[str, ...]
    composition: EvidenceComposition
    minimum_count: int | None
    minimum_strength: ClaimStrengthCeiling
    scope_id: str
    exposure_id: str | None
    contradiction_policy_ref: str
    boundary_policy: RequirementBoundaryPolicy
    allowed_boundary_codes: tuple[str, ...]
    linked_falsification_ids: tuple[str, ...]
    linked_reversal_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_requirement_id",
            "scope_id",
            "contradiction_policy_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _validate_string_tuple_fields(
            self,
            (
                "target_estimand_ids",
                "required_evidence_type_refs",
                "allowed_boundary_codes",
                "linked_falsification_ids",
                "linked_reversal_ids",
            ),
        )
        if not self.target_estimand_ids:
            raise ValueError("evidence requirement requires estimands")
        if not self.required_evidence_type_refs:
            raise ValueError("evidence requirement requires evidence types")
        _require_enum(
            self.composition,
            EvidenceComposition,
            "composition",
        )
        _require_enum(
            self.minimum_strength,
            ClaimStrengthCeiling,
            "minimum_strength",
        )
        _require_enum(
            self.boundary_policy,
            RequirementBoundaryPolicy,
            "boundary_policy",
        )
        _validate_optional_ref(self.exposure_id, "exposure_id")
        if self.composition is EvidenceComposition.AT_LEAST:
            if (
                type(self.minimum_count) is not int
                or self.minimum_count < 1
            ):
                raise ValueError("at_least composition requires minimum_count")
            if self.minimum_count > len(self.required_evidence_type_refs):
                raise ValueError(
                    "minimum_count cannot exceed required evidence slots"
                )
        elif self.minimum_count is not None:
            raise ValueError("minimum_count only applies to at_least")
        if (
            self.boundary_policy
            is RequirementBoundaryPolicy.ALLOW_TYPED_BOUNDARY
            and not self.allowed_boundary_codes
        ):
            raise ValueError("typed boundary policy requires allowed codes")
        if (
            self.boundary_policy is RequirementBoundaryPolicy.BLOCK
            and self.allowed_boundary_codes
        ):
            raise ValueError("blocking boundary policy cannot allow codes")


@dataclass(frozen=True, slots=True)
class EpistemicCompletionSpec:
    completion_spec_id: str
    target_estimand_ids: tuple[str, ...]
    required_evidence_requirement_ids: tuple[str, ...]
    success_policy_ref: str
    degrade_policy_ref: str
    stop_policy_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(
            self,
            (
                "target_estimand_ids",
                "required_evidence_requirement_ids",
            ),
        )
        if not self.target_estimand_ids:
            raise ValueError("completion spec requires estimands")
        if not self.required_evidence_requirement_ids:
            raise ValueError("completion spec requires evidence requirements")


@dataclass(frozen=True, slots=True)
class DefinitionTargetSpec:
    defined_concept_ref: str
    definition_contract_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class DataQualityTargetSpec:
    assessed_object_ref: str
    quality_predicate_refs: tuple[str, ...]
    decision_rule_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(self, ("quality_predicate_refs",))
        if not self.quality_predicate_refs:
            raise ValueError("data-quality target requires predicates")


@dataclass(frozen=True, slots=True)
class PointQuantityTargetSpec:
    quantity_operator_ref: str
    scalar_result_contract_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class DistributionTargetSpec:
    distribution_operator_ref: str
    support_contract_ref: str
    statistic_parameter_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(self, ("statistic_parameter_refs",))
        if not self.statistic_parameter_refs:
            raise ValueError(
                "distribution target requires statistic parameters"
            )


@dataclass(frozen=True, slots=True)
class TemporalPatternTargetSpec:
    cadence_ref: str
    pattern_operator_ref: str
    minimum_cycle_count: int

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        if self.minimum_cycle_count < 2:
            raise ValueError(
                "temporal-pattern target requires at least two cycles"
            )


@dataclass(frozen=True, slots=True)
class ContrastTargetSpec:
    contrast_id: str
    effect_scale_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class CompositionTargetSpec:
    whole_variable_id: str
    component_variable_ids: tuple[str, ...]
    completeness_rule_ref: str
    exclusivity_rule_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(self, ("component_variable_ids",))
        if len(self.component_variable_ids) < 2:
            raise ValueError(
                "composition target requires at least two components"
            )
        if self.whole_variable_id in self.component_variable_ids:
            raise ValueError("composition whole cannot be a component")


@dataclass(frozen=True, slots=True)
class AccountingDecompositionTargetSpec:
    reconciliation_id: str
    residual_policy_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class CohortOutcomeTargetSpec:
    cohort_risk_set_id: str
    horizon_ref: str
    maturity_policy_ref: str
    censoring_policy_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class FunnelTransitionTargetSpec:
    sequence_id: str
    stage_order_ref: str
    transition_denominator_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class AssociationTargetSpec:
    relationship_id: str
    association_measure_ref: str
    adjustment_set_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class CausalEffectTargetSpec:
    relationship_id: str
    identification_id: str
    causal_contrast_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)


@dataclass(frozen=True, slots=True)
class DiagnosticSetTargetSpec:
    member_estimand_ids: tuple[str, ...]
    ranking_rule_ref: str
    joint_stop_rule_ref: str

    def __post_init__(self) -> None:
        _validate_nonempty_dataclass_strings(self)
        _validate_string_tuple_fields(self, ("member_estimand_ids",))
        if len(self.member_estimand_ids) < 2:
            raise ValueError(
                "diagnostic-set target requires at least two members"
            )


type ClaimTargetSpec = (
    DefinitionTargetSpec
    | DataQualityTargetSpec
    | PointQuantityTargetSpec
    | DistributionTargetSpec
    | TemporalPatternTargetSpec
    | ContrastTargetSpec
    | CompositionTargetSpec
    | AccountingDecompositionTargetSpec
    | CohortOutcomeTargetSpec
    | FunnelTransitionTargetSpec
    | AssociationTargetSpec
    | CausalEffectTargetSpec
    | DiagnosticSetTargetSpec
)


CLAIM_TARGET_SPEC_TYPES: Mapping[
    ClaimTargetKind,
    type[ClaimTargetSpec],
] = {
    ClaimTargetKind.DEFINITION: DefinitionTargetSpec,
    ClaimTargetKind.DATA_QUALITY_STATE: DataQualityTargetSpec,
    ClaimTargetKind.POINT_QUANTITY: PointQuantityTargetSpec,
    ClaimTargetKind.DISTRIBUTION: DistributionTargetSpec,
    ClaimTargetKind.TEMPORAL_PATTERN: TemporalPatternTargetSpec,
    ClaimTargetKind.CONTRAST: ContrastTargetSpec,
    ClaimTargetKind.COMPOSITION: CompositionTargetSpec,
    ClaimTargetKind.ACCOUNTING_DECOMPOSITION: (
        AccountingDecompositionTargetSpec
    ),
    ClaimTargetKind.COHORT_OUTCOME: CohortOutcomeTargetSpec,
    ClaimTargetKind.FUNNEL_TRANSITION: FunnelTransitionTargetSpec,
    ClaimTargetKind.ASSOCIATION: AssociationTargetSpec,
    ClaimTargetKind.CAUSAL_EFFECT: CausalEffectTargetSpec,
    ClaimTargetKind.DIAGNOSTIC_SET: DiagnosticSetTargetSpec,
}


@dataclass(frozen=True, slots=True)
class EstimandSpec:
    estimand_id: str
    claim_target_kind: ClaimTargetKind
    claim_target_spec: ClaimTargetSpec
    variable_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    population_id: str | None
    observation_unit_id: str | None
    temporal_semantic_id: str | None
    estimator_id: str | None
    exposure_id: str | None
    contrast_id: str | None
    sequence_id: str | None
    cohort_risk_set_id: str | None
    reconciliation_id: str | None
    relationship_id: str | None
    eligibility_id: str | None
    identification_id: str | None
    evidence_requirement_ids: tuple[str, ...]
    alternative_ids: tuple[str, ...]
    sensitivity_ids: tuple[str, ...]
    falsification_ids: tuple[str, ...]
    reversal_ids: tuple[str, ...]
    scope_ceiling_id: str
    claim_strength_ceiling: ClaimStrengthCeiling

    def __post_init__(self) -> None:
        require_nonempty(self.estimand_id, "estimand_id")
        require_nonempty(self.scope_ceiling_id, "scope_ceiling_id")
        _require_enum(
            self.claim_target_kind,
            ClaimTargetKind,
            "claim_target_kind",
        )
        expected_target_type = CLAIM_TARGET_SPEC_TYPES[
            self.claim_target_kind
        ]
        if not isinstance(self.claim_target_spec, expected_target_type):
            raise TypeError(
                "claim_target_spec does not match claim_target_kind"
            )
        _require_enum(
            self.claim_strength_ceiling,
            ClaimStrengthCeiling,
            "claim_strength_ceiling",
        )
        _validate_string_tuple_fields(
            self,
            (
                "variable_ids",
                "event_ids",
                "evidence_requirement_ids",
                "alternative_ids",
                "sensitivity_ids",
                "falsification_ids",
                "reversal_ids",
            ),
        )
        for field_name in (
            "population_id",
            "observation_unit_id",
            "temporal_semantic_id",
            "estimator_id",
            "exposure_id",
            "contrast_id",
            "sequence_id",
            "cohort_risk_set_id",
            "reconciliation_id",
            "relationship_id",
            "eligibility_id",
            "identification_id",
        ):
            _validate_optional_ref(getattr(self, field_name), field_name)
        if not self.evidence_requirement_ids:
            raise ValueError("estimand requires evidence requirements")


@dataclass(frozen=True, slots=True)
class MeasurementDesign:
    question_grounding: QuestionGrounding
    decision_objective: DecisionObjective
    variables: tuple[VariableSpec, ...]
    events: tuple[EventSpec, ...]
    populations: tuple[PopulationSpec, ...]
    observation_units: tuple[ObservationUnitSpec, ...]
    metric_expressions: tuple[MetricExpression, ...]
    temporal_semantics: tuple[TemporalSemanticSpec, ...]
    window_rules: tuple[WindowRuleSpec, ...]
    exposures: tuple[ExposureSpec, ...]
    estimators: tuple[EstimatorSpec, ...]
    contrasts: tuple[ContrastSpec, ...]
    eligibilities: tuple[EligibilitySpec, ...]
    sequences: tuple[SequenceSpec, ...]
    cohort_risk_sets: tuple[CohortRiskSetSpec, ...]
    reconciliations: tuple[ReconciliationSpec, ...]
    relationships: tuple[RelationshipSpec, ...]
    identifications: tuple[IdentificationSpec, ...]
    assumptions: tuple[AssumptionSpec, ...]
    alternatives: tuple[AlternativeSpec, ...]
    sensitivities: tuple[SensitivitySpec, ...]
    falsifications: tuple[FalsificationSpec, ...]
    reversals: tuple[ReversalSpec, ...]
    scopes: tuple[ScopeExpression, ...]
    evidence_requirements: tuple[EvidenceRequirementSpec, ...]
    completion_specs: tuple[EpistemicCompletionSpec, ...]
    estimands: tuple[EstimandSpec, ...]

    def __post_init__(self) -> None:
        typed_collections: tuple[tuple[str, tuple[object, ...], type], ...] = (
            ("variables", self.variables, VariableSpec),
            ("events", self.events, EventSpec),
            ("populations", self.populations, PopulationSpec),
            ("observation_units", self.observation_units, ObservationUnitSpec),
            ("metric_expressions", self.metric_expressions, MetricExpression),
            ("temporal_semantics", self.temporal_semantics, TemporalSemanticSpec),
            ("window_rules", self.window_rules, WindowRuleSpec),
            ("exposures", self.exposures, ExposureSpec),
            ("estimators", self.estimators, EstimatorSpec),
            ("contrasts", self.contrasts, ContrastSpec),
            ("eligibilities", self.eligibilities, EligibilitySpec),
            ("sequences", self.sequences, SequenceSpec),
            ("cohort_risk_sets", self.cohort_risk_sets, CohortRiskSetSpec),
            ("reconciliations", self.reconciliations, ReconciliationSpec),
            ("relationships", self.relationships, RelationshipSpec),
            ("identifications", self.identifications, IdentificationSpec),
            ("assumptions", self.assumptions, AssumptionSpec),
            ("alternatives", self.alternatives, AlternativeSpec),
            ("sensitivities", self.sensitivities, SensitivitySpec),
            ("falsifications", self.falsifications, FalsificationSpec),
            ("reversals", self.reversals, ReversalSpec),
            ("scopes", self.scopes, ScopeExpression),
            (
                "evidence_requirements",
                self.evidence_requirements,
                EvidenceRequirementSpec,
            ),
            ("completion_specs", self.completion_specs, EpistemicCompletionSpec),
            ("estimands", self.estimands, EstimandSpec),
        )
        for name, values, expected_type in typed_collections:
            _require_tuple_of(values, expected_type, name)
        if not self.estimands:
            raise ValueError("measurement design requires estimands")
        if not self.evidence_requirements:
            raise ValueError("measurement design requires evidence requirements")
        if not self.completion_specs:
            raise ValueError("measurement design requires completion specs")
        self._validate_graph()

    def _validate_graph(self) -> None:
        node_index: dict[str, object] = {}
        for node in _measurement_nodes(self):
            node_id = _node_id(node)
            if node_id in node_index:
                raise ValueError("measurement node IDs must be globally unique")
            node_index[node_id] = node
        known_ids = set(node_index)
        contract_like_refs = set(
            self.question_grounding.semantic_contract_refs
        )
        for node in _measurement_nodes(self):
            for field in fields(node):
                value = getattr(node, field.name)
                if field.name.endswith("_id") and value is not None:
                    if field.name in {
                        _node_id_field(node),
                        "question_revision_id",
                    }:
                        continue
                    if value not in known_ids:
                        raise ValueError(
                            "{} references unknown node {}".format(
                                type(node).__name__,
                                value,
                            )
                        )
                if field.name.endswith("_ids") and isinstance(value, tuple):
                    for reference in value:
                        if (
                            reference not in known_ids
                            and reference not in contract_like_refs
                            and not field.name.endswith("_record_ids")
                        ):
                            raise ValueError(
                                "{} references unknown node {}".format(
                                    type(node).__name__,
                                    reference,
                                )
                            )
        estimand_ids = {item.estimand_id for item in self.estimands}
        requirement_ids = {
            item.evidence_requirement_id
            for item in self.evidence_requirements
        }
        for requirement in self.evidence_requirements:
            if not set(requirement.target_estimand_ids) <= estimand_ids:
                raise ValueError(
                    "evidence requirement targets unknown estimand"
                )
        for completion in self.completion_specs:
            if not set(completion.target_estimand_ids) <= estimand_ids:
                raise ValueError("completion targets unknown estimand")
            if not set(
                completion.required_evidence_requirement_ids
            ) <= requirement_ids:
                raise ValueError(
                    "completion references unknown evidence requirement"
                )
        for estimand in self.estimands:
            if not set(estimand.evidence_requirement_ids) <= requirement_ids:
                raise ValueError(
                    "estimand references unknown evidence requirement"
                )
            for requirement_id in estimand.evidence_requirement_ids:
                requirement = next(
                    item
                    for item in self.evidence_requirements
                    if item.evidence_requirement_id == requirement_id
                )
                if estimand.estimand_id not in (
                    requirement.target_estimand_ids
                ):
                    raise ValueError(
                        "estimand and evidence requirement must target "
                        "each other"
                    )
            _validate_estimand_shape(estimand)
            if not any(
                estimand.estimand_id in completion.target_estimand_ids
                for completion in self.completion_specs
            ):
                raise ValueError("estimand has no epistemic completion path")


@dataclass(frozen=True, slots=True)
class AnalysisFrameRevision:
    frame_revision_id: str
    case_id: str
    question_revision_id: str
    revision_number: int
    prior_frame_revision_id: str | None
    created_by_action_id: str
    created_at: datetime
    revision_reason_ref: str
    measurement_design: MeasurementDesign
    semantic_measurement_ids: tuple[str, ...]
    authority_binding_ids: tuple[str, ...]
    identity_algorithm_version: str = IDENTITY_ALGORITHM_VERSION
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        for field_name in (
            "frame_revision_id",
            "case_id",
            "question_revision_id",
            "created_by_action_id",
            "revision_reason_ref",
            "identity_algorithm_version",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        _validate_revision_lineage(
            self.revision_number,
            self.prior_frame_revision_id,
            "frame",
        )
        require_aware_datetime(self.created_at, "created_at")
        if not isinstance(self.measurement_design, MeasurementDesign):
            raise TypeError("measurement_design must be MeasurementDesign")
        if (
            self.measurement_design.question_grounding.question_revision_id
            != self.question_revision_id
        ):
            raise ValueError("frame grounding must bind its question revision")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("analysis frame requires schema epoch 3")
        if self.identity_algorithm_version != IDENTITY_ALGORITHM_VERSION:
            raise ValueError("analysis frame identity algorithm is unsupported")
        _validate_digest_tuple(
            self.semantic_measurement_ids,
            "semantic_measurement_ids",
        )
        _validate_digest_tuple(
            self.authority_binding_ids,
            "authority_binding_ids",
        )
        expected_count = len(self.measurement_design.estimands)
        if (
            len(self.semantic_measurement_ids) != expected_count
            or len(self.authority_binding_ids) != expected_count
        ):
            raise ValueError("frame requires one identity per estimand")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    as_of_instant: datetime
    timezone: str
    business_day_cutoff: str
    ambiguous_local_time_policy: AmbiguousLocalTimePolicy
    calendar_version_ref: str
    holiday_version_ref: str | None
    fiscal_version_ref: str | None
    data_contract_version_ref: str
    snapshot_release_ref: str
    coverage_watermark_ref: str
    late_arrival_policy_ref: str

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of_instant, "as_of_instant")
        for field_name in (
            "timezone",
            "business_day_cutoff",
            "calendar_version_ref",
            "data_contract_version_ref",
            "snapshot_release_ref",
            "coverage_watermark_ref",
            "late_arrival_policy_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        try:
            parsed_cutoff = time.fromisoformat(self.business_day_cutoff)
        except ValueError as exc:
            raise ValueError(
                "business_day_cutoff must be an ISO local time"
            ) from exc
        if parsed_cutoff.tzinfo is not None:
            raise ValueError("business_day_cutoff must be a local wall time")
        _require_enum(
            self.ambiguous_local_time_policy,
            AmbiguousLocalTimePolicy,
            "ambiguous_local_time_policy",
        )
        _validate_optional_ref(
            self.holiday_version_ref,
            "holiday_version_ref",
        )
        _validate_optional_ref(
            self.fiscal_version_ref,
            "fiscal_version_ref",
        )


@dataclass(frozen=True, slots=True)
class ResolvedExposureFact:
    exposure_id: str
    basis: ExposureBasis
    unit_ref: str
    expected_exposure_decimal: str
    observed_exposure_decimal: str
    valid_exposure_decimal: str
    invalid_exposure_decimal: str
    missing_exposure_decimal: str
    coverage_ratio_decimal: str
    at_risk_exposure_decimal: str | None
    source_kind: ExposureFactSourceKind
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("exposure_id", "unit_ref"):
            require_nonempty(getattr(self, field_name), field_name)
        _require_enum(self.basis, ExposureBasis, "basis")
        _require_enum(
            self.source_kind,
            ExposureFactSourceKind,
            "source_kind",
        )
        require_sha256(
            self.source_receipt_sha256,
            "source_receipt_sha256",
        )
        for field_name in (
            "expected_exposure_decimal",
            "observed_exposure_decimal",
            "valid_exposure_decimal",
            "invalid_exposure_decimal",
            "missing_exposure_decimal",
            "coverage_ratio_decimal",
        ):
            _require_nonnegative_decimal(
                getattr(self, field_name),
                field_name,
            )
        from decimal import Decimal

        expected = Decimal(self.expected_exposure_decimal)
        observed = Decimal(self.observed_exposure_decimal)
        valid = Decimal(self.valid_exposure_decimal)
        invalid = Decimal(self.invalid_exposure_decimal)
        missing = Decimal(self.missing_exposure_decimal)
        coverage = Decimal(self.coverage_ratio_decimal)
        if not valid <= observed <= expected:
            raise ValueError(
                "resolved exposure must satisfy valid <= observed <= expected"
            )
        if invalid != observed - valid:
            raise ValueError(
                "invalid exposure must equal observed minus valid"
            )
        if missing != expected - observed:
            raise ValueError(
                "missing exposure must equal expected minus observed"
            )
        expected_coverage = valid / expected if expected else Decimal("0")
        if coverage != expected_coverage:
            raise ValueError(
                "coverage ratio must equal valid divided by expected"
            )
        if coverage > 1:
            raise ValueError("coverage ratio cannot exceed one")
        if self.at_risk_exposure_decimal is not None:
            _require_nonnegative_decimal(
                self.at_risk_exposure_decimal,
                "at_risk_exposure_decimal",
            )
            if Decimal(self.at_risk_exposure_decimal) > expected:
                raise ValueError(
                    "at-risk exposure cannot exceed expected exposure"
                )
        if (
            self.basis is ExposureBasis.AT_RISK
            and self.at_risk_exposure_decimal is None
        ):
            raise ValueError("at-risk basis requires at-risk exposure")


@dataclass(frozen=True, slots=True)
class ResolvedWindow:
    operand_id: str
    window_rule_id: str
    anchor_date: date
    period_offset: int
    actual_start: date
    actual_end: date
    start_instant: datetime
    end_instant: datetime
    elapsed_seconds: int
    actual_calendar_days: int
    selected_calendar_dates_count: int
    observed_calendar_dates_count: int
    valid_calendar_dates_count: int
    selected_calendar_dates_sha256: str
    calendar_coverage_receipt_sha256: str
    exposure_facts: tuple[ResolvedExposureFact, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.operand_id, "operand_id")
        require_nonempty(self.window_rule_id, "window_rule_id")
        if self.actual_end < self.actual_start:
            raise ValueError("resolved window end precedes start")
        require_aware_datetime(self.start_instant, "start_instant")
        require_aware_datetime(self.end_instant, "end_instant")
        if self.end_instant <= self.start_instant:
            raise ValueError("resolved instant interval must be positive")
        from datetime import UTC

        expected_elapsed = int(
            (
                self.end_instant.astimezone(UTC)
                - self.start_instant.astimezone(UTC)
            ).total_seconds()
        )
        if self.elapsed_seconds != expected_elapsed:
            raise ValueError(
                "elapsed seconds do not match resolved instant interval"
            )
        expected_days = (self.actual_end - self.actual_start).days + 1
        if self.actual_calendar_days != expected_days:
            raise ValueError("actual calendar days do not match date interval")
        if not 1 <= self.selected_calendar_dates_count <= (
            self.actual_calendar_days
        ):
            raise ValueError(
                "selected calendar date count must fit the actual interval"
            )
        if not (
            0
            <= self.valid_calendar_dates_count
            <= self.observed_calendar_dates_count
            <= self.selected_calendar_dates_count
        ):
            raise ValueError(
                "calendar coverage must satisfy valid <= observed <= selected"
            )
        require_sha256(
            self.selected_calendar_dates_sha256,
            "selected_calendar_dates_sha256",
        )
        require_sha256(
            self.calendar_coverage_receipt_sha256,
            "calendar_coverage_receipt_sha256",
        )
        _require_tuple_of(
            self.exposure_facts,
            ResolvedExposureFact,
            "exposure_facts",
        )
        exposure_ids = tuple(
            fact.exposure_id for fact in self.exposure_facts
        )
        _require_unique_ids(exposure_ids, "resolved exposure")


@dataclass(frozen=True, slots=True)
class ResolvedMeasurementInstance:
    resolution_id: str
    semantic_measurement_id: str
    authority_binding_id: str
    frame_revision_id: str
    estimand_id: str
    context: ResolutionContext
    target_period_ref: str
    windows: tuple[ResolvedWindow, ...]
    expected_scope_id: str
    expected_grain_ref: str
    expected_unit_ref: str
    expected_exposure_id: str | None
    eligibility_id: str | None
    resolver_contract_ref: str
    resolver_input_bundle_sha256: str
    field_derivation_proof_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "frame_revision_id",
            "estimand_id",
            "target_period_ref",
            "expected_scope_id",
            "expected_grain_ref",
            "expected_unit_ref",
            "resolver_contract_ref",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        require_sha256(self.resolution_id, "resolution_id")
        require_sha256(
            self.semantic_measurement_id,
            "semantic_measurement_id",
        )
        require_sha256(self.authority_binding_id, "authority_binding_id")
        require_sha256(
            self.resolver_input_bundle_sha256,
            "resolver_input_bundle_sha256",
        )
        require_sha256(
            self.field_derivation_proof_sha256,
            "field_derivation_proof_sha256",
        )
        if not isinstance(self.context, ResolutionContext):
            raise TypeError("context must be ResolutionContext")
        _require_tuple_of(self.windows, ResolvedWindow, "windows")
        _validate_optional_ref(
            self.expected_exposure_id,
            "expected_exposure_id",
        )
        _validate_optional_ref(self.eligibility_id, "eligibility_id")


@dataclass(frozen=True, slots=True)
class TypedResolutionBoundary:
    boundary_code: str
    boundary_policy_ref: str
    failed_requirement_ids: tuple[str, ...]
    failed_contract_refs: tuple[str, ...]
    inspection_evidence_refs: tuple[str, ...]
    allowed_claim_ceiling: ClaimStrengthCeiling
    derivation_proof_sha256: str

    def __post_init__(self) -> None:
        require_nonempty(self.boundary_code, "boundary_code")
        require_nonempty(self.boundary_policy_ref, "boundary_policy_ref")
        _validate_string_tuple_fields(
            self,
            (
                "failed_requirement_ids",
                "failed_contract_refs",
                "inspection_evidence_refs",
            ),
        )
        if not self.failed_requirement_ids:
            raise ValueError("resolution boundary requires failed requirements")
        _require_enum(
            self.allowed_claim_ceiling,
            ClaimStrengthCeiling,
            "allowed_claim_ceiling",
        )
        require_sha256(
            self.derivation_proof_sha256,
            "derivation_proof_sha256",
        )


@dataclass(frozen=True, slots=True)
class RequirementResolutionBoundary:
    evidence_requirement_id: str
    boundary_code: str
    boundary_policy_ref: str
    failed_contract_refs: tuple[str, ...]
    inspection_evidence_refs: tuple[str, ...]
    allowed_claim_ceiling: ClaimStrengthCeiling
    derivation_proof_sha256: str

    def __post_init__(self) -> None:
        require_nonempty(
            self.evidence_requirement_id,
            "evidence_requirement_id",
        )
        require_nonempty(self.boundary_code, "boundary_code")
        require_nonempty(self.boundary_policy_ref, "boundary_policy_ref")
        _validate_string_tuple_fields(
            self,
            ("failed_contract_refs", "inspection_evidence_refs"),
        )
        if not self.failed_contract_refs:
            raise ValueError(
                "requirement boundary requires failed contracts"
            )
        if not self.inspection_evidence_refs:
            raise ValueError(
                "requirement boundary requires inspection evidence"
            )
        _require_enum(
            self.allowed_claim_ceiling,
            ClaimStrengthCeiling,
            "allowed_claim_ceiling",
        )
        require_sha256(
            self.derivation_proof_sha256,
            "derivation_proof_sha256",
        )


@dataclass(frozen=True, slots=True)
class MeasurementDerivationAuthority:
    """Business authority under which measurement facts were derived."""

    case_id: str
    mailbox_authority_epoch: int
    accepted_question_revision_id: str
    accepted_frame_revision_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "case_id",
            "accepted_question_revision_id",
            "accepted_frame_revision_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        if self.mailbox_authority_epoch < 0:
            raise ValueError(
                "mailbox_authority_epoch must be non-negative"
            )

    @classmethod
    def from_authority_snapshot(
        cls,
        snapshot: AuthoritySnapshot,
    ) -> MeasurementDerivationAuthority:
        if snapshot.accepted_question_revision_id is None:
            raise ValueError(
                "measurement derivation requires an accepted question"
            )
        if snapshot.accepted_frame_revision_id is None:
            raise ValueError(
                "measurement derivation requires an accepted Frame"
            )
        return cls(
            case_id=snapshot.case_id,
            mailbox_authority_epoch=snapshot.mailbox_authority_epoch,
            accepted_question_revision_id=(
                snapshot.accepted_question_revision_id
            ),
            accepted_frame_revision_id=snapshot.accepted_frame_revision_id,
        )

    def matches(self, snapshot: AuthoritySnapshot) -> bool:
        try:
            current = self.from_authority_snapshot(snapshot)
        except ValueError:
            return False
        return self == current

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class MeasurementResolutionOutcome:
    resolution_outcome_id: str
    case_id: str
    question_revision_id: str
    frame_revision_id: str
    estimand_id: str
    semantic_measurement_id: str
    authority_binding_id: str
    derivation_authority: MeasurementDerivationAuthority
    kind: ResolutionOutcomeKind
    resolved_instance: ResolvedMeasurementInstance | None
    boundary: TypedResolutionBoundary | None
    requirement_boundaries: tuple[RequirementResolutionBoundary, ...]
    created_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        require_sha256(
            self.resolution_outcome_id,
            "resolution_outcome_id",
        )
        for field_name in (
            "case_id",
            "question_revision_id",
            "frame_revision_id",
            "estimand_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        require_sha256(
            self.semantic_measurement_id,
            "semantic_measurement_id",
        )
        require_sha256(self.authority_binding_id, "authority_binding_id")
        if not isinstance(
            self.derivation_authority,
            MeasurementDerivationAuthority,
        ):
            raise TypeError(
                "derivation_authority must be MeasurementDerivationAuthority"
            )
        if (
            self.derivation_authority.case_id != self.case_id
            or self.derivation_authority.accepted_question_revision_id
            != self.question_revision_id
            or self.derivation_authority.accepted_frame_revision_id
            != self.frame_revision_id
        ):
            raise ValueError(
                "derivation authority does not bind outcome authority"
            )
        _require_enum(self.kind, ResolutionOutcomeKind, "kind")
        _require_tuple_of(
            self.requirement_boundaries,
            RequirementResolutionBoundary,
            "requirement_boundaries",
        )
        requirement_ids = tuple(
            item.evidence_requirement_id
            for item in self.requirement_boundaries
        )
        _require_unique_ids(
            requirement_ids,
            "requirement resolution boundary",
        )
        if (
            self.kind is ResolutionOutcomeKind.RESOLVED_INSTANCE
            and (
                self.resolved_instance is None
                or self.boundary is not None
            )
        ):
            raise ValueError("resolved outcome requires only an instance")
        if (
            self.kind is ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY
            and (
                self.boundary is None
                or self.resolved_instance is not None
                or self.requirement_boundaries
            )
        ):
            raise ValueError("boundary outcome requires only a boundary")
        require_aware_datetime(self.created_at, "created_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("resolution outcome requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class ResolvedEvidenceObligation:
    obligation_id: str
    case_id: str
    frame_revision_id: str
    estimand_id: str
    evidence_requirement_id: str
    evidence_requirement_sha256: str
    evidence_type_refs: tuple[str, ...]
    resolution_outcome_id: str
    derivation_authority: MeasurementDerivationAuthority
    execution_disposition: ObligationExecutionDisposition
    boundary_code: str | None
    closure_definition_sha256: str
    field_derivation_proof_sha256: str
    created_at: datetime
    schema_epoch: int = SCHEMA_EPOCH

    def __post_init__(self) -> None:
        require_sha256(self.obligation_id, "obligation_id")
        for field_name in (
            "case_id",
            "frame_revision_id",
            "estimand_id",
            "evidence_requirement_id",
            "resolution_outcome_id",
        ):
            require_nonempty(getattr(self, field_name), field_name)
        for field_name in (
            "evidence_requirement_sha256",
            "closure_definition_sha256",
            "field_derivation_proof_sha256",
        ):
            require_sha256(getattr(self, field_name), field_name)
        if not isinstance(
            self.derivation_authority,
            MeasurementDerivationAuthority,
        ):
            raise TypeError(
                "derivation_authority must be MeasurementDerivationAuthority"
            )
        if (
            self.derivation_authority.case_id != self.case_id
            or self.derivation_authority.accepted_frame_revision_id
            != self.frame_revision_id
        ):
            raise ValueError(
                "derivation authority does not bind obligation authority"
            )
        _validate_string_tuple_fields(self, ("evidence_type_refs",))
        if not self.evidence_type_refs:
            raise ValueError(
                "obligation requires at least one evidence type"
            )
        if len(self.evidence_type_refs) != 1:
            raise ValueError(
                "resolved obligation must own one evidence type slot"
            )
        _require_enum(
            self.execution_disposition,
            ObligationExecutionDisposition,
            "execution_disposition",
        )
        if (
            self.execution_disposition
            is ObligationExecutionDisposition.EXECUTABLE
        ):
            if self.boundary_code is not None:
                raise ValueError(
                    "executable obligation cannot carry a boundary"
                )
        elif self.boundary_code is None:
            raise ValueError(
                "boundary obligation requires a boundary code"
            )
        if self.boundary_code is not None:
            require_nonempty(self.boundary_code, "boundary_code")
        require_aware_datetime(self.created_at, "created_at")
        if self.schema_epoch != SCHEMA_EPOCH:
            raise ValueError("obligation requires schema epoch 3")

    @property
    def content_sha256(self) -> str:
        return content_sha256(self)


def _validate_estimand_shape(estimand: EstimandSpec) -> None:
    definition_like = {
        ClaimTargetKind.DEFINITION,
        ClaimTargetKind.DATA_QUALITY_STATE,
    }
    if estimand.claim_target_kind not in definition_like:
        required = (
            estimand.population_id,
            estimand.observation_unit_id,
            estimand.estimator_id,
            estimand.eligibility_id,
            estimand.identification_id,
        )
        if any(item is None for item in required):
            raise ValueError(
                "{} estimand is missing a material measurement node".format(
                    estimand.claim_target_kind.value
                )
            )
    time_targets = {
        ClaimTargetKind.TEMPORAL_PATTERN,
        ClaimTargetKind.CONTRAST,
        ClaimTargetKind.COHORT_OUTCOME,
        ClaimTargetKind.FUNNEL_TRANSITION,
        ClaimTargetKind.CAUSAL_EFFECT,
    }
    if (
        estimand.claim_target_kind in time_targets
        and estimand.temporal_semantic_id is None
    ):
        raise ValueError("time-dependent estimand requires time semantics")
    if (
        estimand.claim_target_kind is ClaimTargetKind.CONTRAST
        and estimand.contrast_id is None
    ):
        raise ValueError("contrast estimand requires contrast")
    if (
        estimand.claim_target_kind
        is ClaimTargetKind.ACCOUNTING_DECOMPOSITION
        and estimand.reconciliation_id is None
    ):
        raise ValueError("decomposition requires reconciliation")
    if (
        estimand.claim_target_kind is ClaimTargetKind.COHORT_OUTCOME
        and estimand.cohort_risk_set_id is None
    ):
        raise ValueError("cohort estimand requires risk set")
    if (
        estimand.claim_target_kind is ClaimTargetKind.FUNNEL_TRANSITION
        and estimand.sequence_id is None
    ):
        raise ValueError("funnel estimand requires sequence")
    if estimand.claim_target_kind in {
        ClaimTargetKind.ASSOCIATION,
        ClaimTargetKind.CAUSAL_EFFECT,
    } and estimand.relationship_id is None:
        raise ValueError("relationship estimand requires relationship")
    if (
        estimand.claim_target_kind is ClaimTargetKind.CAUSAL_EFFECT
        and estimand.claim_strength_ceiling
        is not ClaimStrengthCeiling.CAUSAL
    ):
        raise ValueError("causal target requires causal claim ceiling")


def _measurement_nodes(design: MeasurementDesign) -> tuple[object, ...]:
    result: list[object] = []
    for field in fields(design):
        value = getattr(design, field.name)
        if isinstance(value, tuple):
            for node in value:
                result.append(node)
                result.extend(getattr(node, "operands", ()))
    return tuple(result)


def _node_id_field(node: object) -> str:
    candidates = [
        field.name
        for field in fields(node)
        if field.name.endswith("_id")
        and not field.name.endswith("_revision_id")
    ]
    if not candidates:
        raise TypeError(
            "{} is not a measurement node".format(type(node).__name__)
        )
    return candidates[0]


def _node_id(node: object) -> str:
    return str(getattr(node, _node_id_field(node)))


def _validate_revision_lineage(
    revision_number: int,
    prior_revision_id: str | None,
    label: str,
) -> None:
    if revision_number < 1:
        raise ValueError("{} revision number must be positive".format(label))
    if revision_number == 1 and prior_revision_id is not None:
        raise ValueError(
            "first {} revision cannot have a prior revision".format(label)
        )
    if revision_number > 1 and not prior_revision_id:
        raise ValueError(
            "later {} revisions require a prior revision".format(label)
        )


def _require_enum(value: object, enum_type: type, field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(
            "{} must be {}".format(field_name, enum_type.__name__)
        )


def _require_tuple_of(
    values: tuple[object, ...],
    expected_type: type,
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    for index, value in enumerate(values):
        if not isinstance(value, expected_type):
            raise TypeError(
                "{}[{}] must be {}".format(
                    field_name,
                    index,
                    expected_type.__name__,
                )
            )


def _validate_string_tuple_fields(
    value: object,
    field_names: Iterable[str],
) -> None:
    for field_name in field_names:
        members = getattr(value, field_name)
        if not isinstance(members, tuple):
            raise TypeError("{} must be a tuple".format(field_name))
        _require_unique_ids(members, field_name)
        for member in members:
            require_nonempty(member, field_name)


def _require_unique_ids(values: Iterable[str], label: str) -> None:
    members = tuple(values)
    if len(members) != len(set(members)):
        raise ValueError("{} values must be unique".format(label))


def _validate_optional_ref(value: str | None, field_name: str) -> None:
    if value is not None:
        require_nonempty(value, field_name)


def _validate_nonempty_dataclass_strings(value: object) -> None:
    if not is_dataclass(value):
        raise TypeError("expected dataclass")
    for field in fields(value):
        member = getattr(value, field.name)
        if isinstance(member, str):
            require_nonempty(member, field.name)


def _require_nonnegative_decimal(value: str, field_name: str) -> None:
    require_nonempty(value, field_name)
    try:
        from decimal import Decimal, InvalidOperation

        decimal_value = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(
            "{} must be a canonical decimal string".format(field_name)
        ) from error
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError("{} must be finite and non-negative".format(field_name))
    canonical = format(decimal_value, "f")
    if decimal_value == 0:
        canonical = "0"
    elif "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical != value:
        raise ValueError(
            "{} must use canonical decimal representation".format(field_name)
        )


def _require_decimal_ratio(value: str, field_name: str) -> None:
    _require_nonnegative_decimal(value, field_name)
    from decimal import Decimal

    if Decimal(value) > 1:
        raise ValueError("{} cannot exceed 1".format(field_name))


def _validate_digest_tuple(
    values: tuple[str, ...],
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise TypeError("{} must be a tuple".format(field_name))
    if not values:
        raise ValueError("{} cannot be empty".format(field_name))
    _require_unique_ids(values, field_name)
    for value in values:
        require_sha256(value, field_name)
