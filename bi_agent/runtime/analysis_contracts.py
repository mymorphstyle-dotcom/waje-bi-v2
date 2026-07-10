from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any, Mapping

from bi_agent.runtime.canonical_values import canonical_thaw


DIMENSION_PRESENCE_POLICIES = frozenset(
    {"paired_required", "sparse_allowed", "zero_filled"}
)


def stable_contract_signature(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContractGap:
    gap_type: str
    gap_id: str
    dataset_id: str = ""
    affected_capabilities: tuple[str, ...] = ()
    affected_claim_types: tuple[str, ...] = ()
    owner: str = "runtime_owner"
    repair_options: tuple[str, ...] = ()
    requires_clarification: bool = False
    diagnostic_context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedWindow:
    window_id: str
    role: str
    label: str
    start_inclusive: str
    end_exclusive: str
    timezone: str
    aggregation: str
    required_complete_days: int
    source_watermark_requirement: str
    membership_policy: str = "allow_overlap"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MetricBinding:
    metric_id: str
    contract_ref: str
    dataset_id: str
    expression: str
    aggregation: str
    required_fields: tuple[str, ...]
    grain: tuple[str, ...]
    numerator_metric: str = ""
    denominator_metric: str = ""
    zero_denominator_policy: str = "null"
    claim_types: tuple[str, ...] = ()
    reconciliation_tolerance: float = 0.0
    reconciliation_strategy: str = "unsupported_non_additive"
    value_semantics: str = "raw_scalar"
    display_format: str = "number"


@dataclass(frozen=True)
class DimensionBinding:
    dimension_id: str
    contract_ref: str
    dataset_id: str
    source_field: str
    allowed_grains: tuple[str, ...]
    null_bucket: str = "Unknown"
    permission_scope: str = "analyst"


@dataclass(frozen=True)
class ResultShape:
    required_fields: tuple[str, ...]
    unique_key: tuple[str, ...]
    grain: tuple[str, ...]
    required_window_ids: tuple[str, ...]
    result_semantics: str = "complete_aggregate"
    dimension_presence_policy: str = "paired_required"


@dataclass(frozen=True)
class ReconciliationBinding:
    reference_query_role_ref: str
    reference_contract_signature: str


@dataclass(frozen=True)
class JoinExpectation:
    cardinality: str
    audit_fields: tuple[str, ...]
    max_duplicate_keys: int
    max_unmatched_rows: int


@dataclass(frozen=True)
class QueryContract:
    query_contract_id: str
    analysis_contract_ref: str
    query_intent: str
    dataset_snapshot_refs: tuple[str, ...]
    metric_bindings: tuple[MetricBinding, ...]
    dimension_bindings: tuple[DimensionBinding, ...]
    window_refs: tuple[str, ...]
    resolved_windows: tuple[ResolvedWindow, ...]
    filters: tuple[Mapping[str, Any], ...]
    result_shape: ResultShape
    completeness_assertions: tuple[str, ...]
    permission_scope: str
    workload_class: str
    contract_signature: str
    query_parameters: Mapping[str, Any] = field(default_factory=dict)
    query_role_ref: str = ""
    reconciliation_binding: ReconciliationBinding | None = None
    join_expectation: JoinExpectation | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_QUERY_CONTRACT_SEMANTIC_FIELDS = (
    "query_intent",
    "dataset_snapshot_refs",
    "metric_bindings",
    "dimension_bindings",
    "window_refs",
    "resolved_windows",
    "filters",
    "result_shape",
    "completeness_assertions",
    "permission_scope",
    "workload_class",
    "query_parameters",
    "reconciliation_binding",
    "join_expectation",
)


def query_contract_semantic_body(
    value: QueryContract | Mapping[str, Any],
) -> dict[str, Any]:
    return {
        field_name: _serialize_contract_value(
            _contract_field(
                value,
                field_name,
                default={} if field_name == "query_parameters" else None,
            )
        )
        for field_name in _QUERY_CONTRACT_SEMANTIC_FIELDS
    }


def query_contract_signature(value: QueryContract | Mapping[str, Any]) -> str:
    return stable_contract_signature(query_contract_semantic_body(value))


def _contract_field(
    value: QueryContract | Mapping[str, Any],
    field_name: str,
    *,
    default: Any,
) -> Any:
    if isinstance(value, Mapping):
        if field_name in value:
            return value[field_name]
        if field_name in {
            "query_parameters",
            "reconciliation_binding",
            "join_expectation",
        }:
            return default
        raise ValueError(f"query_contract_semantic_field_missing:{field_name}")
    return getattr(value, field_name)


def _serialize_contract_value(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): _serialize_contract_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return tuple(_serialize_contract_value(item) for item in value)
    return value


@dataclass(frozen=True)
class CapabilityInputSlot:
    slot_id: str
    query_contract_refs: tuple[str, ...]
    required: bool
    accepted_completeness: tuple[str, ...]
    required_fields: tuple[str, ...]
    required_window_ids: tuple[str, ...]
    validation_query_contract_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityExecutionPlan:
    capability_id: str
    capability_contract_ref: str
    required_input_slots: tuple[CapabilityInputSlot, ...]
    optional_input_slots: tuple[CapabilityInputSlot, ...]
    merge_strategy: str
    minimum_readiness: Mapping[str, Any]
    degradation_policy: Mapping[str, Any]
    supported_evidence_types: tuple[str, ...]
    maximum_claim_strength: str
    analysis_contract_ref: str = ""
    supported_claim_types: tuple[str, ...] = ()
    capability_contract_version: str = ""
    capability_contract_signature: str = ""
    claim_strength_taxonomy_version: str = ""
    maximum_claim_strength_rank: int = -1


@dataclass(frozen=True)
class AnalysisContract:
    analysis_contract_id: str
    contract_version: str
    question_families: tuple[str, ...]
    target_metric_refs: tuple[str, ...]
    claim_intents: tuple[str, ...]
    scope: Mapping[str, Any]
    business_timezone: str
    as_of: str
    resolved_windows: tuple[ResolvedWindow, ...]
    metric_bindings: tuple[MetricBinding, ...]
    dimension_bindings: tuple[DimensionBinding, ...]
    dataset_requirements: tuple[str, ...]
    capability_requirements: tuple[str, ...]
    permission_scope: str
    contract_gaps: tuple[ContractGap, ...] = ()
    clarification_outcome_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QueryResultEnvelope:
    query_contract_ref: str
    query_id: str
    query_hash: str
    result_ref: str
    execution_status: str
    rows_ref: str
    row_count: int
    completeness_report_ref: str
    # Aggregate-only in-process payload. External consumers use rows_ref.
    rows: tuple[Mapping[str, Any], ...] = ()
    observed_schema: Mapping[str, str] = field(default_factory=dict)
    observed_windows: tuple[str, ...] = ()
    observed_grain: tuple[str, ...] = ()
    source_snapshot_refs: tuple[str, ...] = ()
    provider_stats: Mapping[str, Any] = field(default_factory=dict)
    failure_reason: str = ""
    execution_attempt_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = canonical_thaw(self)
        payload.pop("rows")
        return payload


@dataclass(frozen=True)
class CompletenessReport:
    report_ref: str
    query_contract_ref: str
    result_ref: str
    completeness_status: str
    analysis_readiness: str
    assertion_results: tuple[Mapping[str, Any], ...]
    failure_reasons: tuple[str, ...]
    coverage_summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return canonical_thaw(self)
