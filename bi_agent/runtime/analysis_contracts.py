from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from hashlib import sha256
import json
import math
from typing import Any, Mapping

from bi_agent.runtime.canonical_values import canonical_thaw


DIMENSION_PRESENCE_POLICIES = frozenset(
    {"paired_required", "sparse_allowed", "zero_filled"}
)


def canonical_exact_additive_count(value: Any) -> int | None:
    """Return the exact integer represented by a reviewed count scalar."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return None
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value)
    return None


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
    capability_refs: tuple[str, ...] = ()

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


def analysis_contract_semantic_body(value: "AnalysisContract" | Mapping[str, Any]) -> dict[str, Any]:
    payload = asdict(value) if isinstance(value, AnalysisContract) else dict(value)
    return {
        str(key): _serialize_contract_value(item)
        for key, item in payload.items()
        if key not in {"analysis_contract_id", "contract_signature"}
    }


def analysis_contract_signature(value: "AnalysisContract" | Mapping[str, Any]) -> str:
    return stable_contract_signature(analysis_contract_semantic_body(value))


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
    contract_gaps: tuple[ContractGap, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analysis_contract_from_dict(value: Mapping[str, Any]) -> AnalysisContract:
    """Rehydrate the complete canonical analysis contract without coercion."""
    item = _strict_mapping(value, path="analysis_contract")
    _require_exact_keys(
        item,
        tuple(AnalysisContract.__dataclass_fields__),
        path="analysis_contract",
    )
    contract = AnalysisContract(
        analysis_contract_id=_strict_string(
            item["analysis_contract_id"], path="analysis_contract.analysis_contract_id"
        ),
        contract_version=_strict_string(
            item["contract_version"], path="analysis_contract.contract_version"
        ),
        question_families=_strict_string_sequence(
            item["question_families"], path="analysis_contract.question_families"
        ),
        target_metric_refs=_strict_string_sequence(
            item["target_metric_refs"], path="analysis_contract.target_metric_refs"
        ),
        claim_intents=_strict_string_sequence(
            item["claim_intents"], path="analysis_contract.claim_intents"
        ),
        scope=dict(_strict_mapping(item["scope"], path="analysis_contract.scope")),
        business_timezone=_strict_string(
            item["business_timezone"], path="analysis_contract.business_timezone"
        ),
        as_of=_strict_string(item["as_of"], path="analysis_contract.as_of"),
        resolved_windows=tuple(
            _resolved_window_from_dict(raw, index=index)
            for index, raw in enumerate(
                _strict_sequence(
                    item["resolved_windows"], path="analysis_contract.resolved_windows"
                )
            )
        ),
        metric_bindings=tuple(
            _metric_binding_from_dict(raw, index=index)
            for index, raw in enumerate(
                _strict_sequence(
                    item["metric_bindings"], path="analysis_contract.metric_bindings"
                )
            )
        ),
        dimension_bindings=tuple(
            _dimension_binding_from_dict(raw, index=index)
            for index, raw in enumerate(
                _strict_sequence(
                    item["dimension_bindings"],
                    path="analysis_contract.dimension_bindings",
                )
            )
        ),
        dataset_requirements=_strict_string_sequence(
            item["dataset_requirements"],
            path="analysis_contract.dataset_requirements",
        ),
        capability_requirements=_strict_string_sequence(
            item["capability_requirements"],
            path="analysis_contract.capability_requirements",
        ),
        contract_gaps=tuple(
            _contract_gap_from_dict(raw, index=index)
            for index, raw in enumerate(
                _strict_sequence(
                    item["contract_gaps"], path="analysis_contract.contract_gaps"
                )
            )
        ),
    )
    for name, values in (
        ("resolved_windows", tuple(item.window_id for item in contract.resolved_windows)),
        (
            "metric_bindings",
            tuple(
                (item.metric_id, item.dataset_id)
                for item in contract.metric_bindings
            ),
        ),
        (
            "dimension_bindings",
            tuple(
                (item.dimension_id, item.dataset_id)
                for item in contract.dimension_bindings
            ),
        ),
        ("dataset_requirements", contract.dataset_requirements),
        ("capability_requirements", contract.capability_requirements),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"analysis_contract.{name}:duplicate")
    return contract


def _resolved_window_from_dict(value: Any, *, index: int) -> ResolvedWindow:
    path = f"analysis_contract.resolved_windows[{index}]"
    item = _strict_mapping(value, path=path)
    _require_exact_keys(item, tuple(ResolvedWindow.__dataclass_fields__), path=path)
    return ResolvedWindow(
        window_id=_strict_string(item["window_id"], path=f"{path}.window_id"),
        role=_strict_string(item["role"], path=f"{path}.role"),
        label=_strict_string(item["label"], path=f"{path}.label"),
        start_inclusive=_strict_string(
            item["start_inclusive"], path=f"{path}.start_inclusive"
        ),
        end_exclusive=_strict_string(
            item["end_exclusive"], path=f"{path}.end_exclusive"
        ),
        timezone=_strict_string(item["timezone"], path=f"{path}.timezone"),
        aggregation=_strict_string(
            item["aggregation"], path=f"{path}.aggregation"
        ),
        required_complete_days=_strict_int(
            item["required_complete_days"], path=f"{path}.required_complete_days"
        ),
        source_watermark_requirement=_strict_string(
            item["source_watermark_requirement"],
            path=f"{path}.source_watermark_requirement",
        ),
        membership_policy=_strict_string(
            item["membership_policy"], path=f"{path}.membership_policy"
        ),
        capability_refs=_strict_string_sequence(
            item["capability_refs"], path=f"{path}.capability_refs"
        ),
    )


def _metric_binding_from_dict(value: Any, *, index: int) -> MetricBinding:
    path = f"analysis_contract.metric_bindings[{index}]"
    item = _strict_mapping(value, path=path)
    _require_exact_keys(item, tuple(MetricBinding.__dataclass_fields__), path=path)
    return MetricBinding(
        metric_id=_strict_string(item["metric_id"], path=f"{path}.metric_id"),
        contract_ref=_strict_string(item["contract_ref"], path=f"{path}.contract_ref"),
        dataset_id=_strict_string(item["dataset_id"], path=f"{path}.dataset_id"),
        expression=_strict_string(item["expression"], path=f"{path}.expression"),
        aggregation=_strict_string(item["aggregation"], path=f"{path}.aggregation"),
        required_fields=_strict_string_sequence(
            item["required_fields"], path=f"{path}.required_fields"
        ),
        grain=_strict_string_sequence(item["grain"], path=f"{path}.grain"),
        numerator_metric=_strict_string(
            item["numerator_metric"], path=f"{path}.numerator_metric", allow_empty=True
        ),
        denominator_metric=_strict_string(
            item["denominator_metric"],
            path=f"{path}.denominator_metric",
            allow_empty=True,
        ),
        zero_denominator_policy=_strict_string(
            item["zero_denominator_policy"], path=f"{path}.zero_denominator_policy"
        ),
        claim_types=_strict_string_sequence(
            item["claim_types"], path=f"{path}.claim_types"
        ),
        reconciliation_tolerance=_strict_number(
            item["reconciliation_tolerance"],
            path=f"{path}.reconciliation_tolerance",
        ),
        reconciliation_strategy=_strict_string(
            item["reconciliation_strategy"],
            path=f"{path}.reconciliation_strategy",
        ),
        value_semantics=_strict_string(
            item["value_semantics"], path=f"{path}.value_semantics"
        ),
        display_format=_strict_string(
            item["display_format"], path=f"{path}.display_format"
        ),
    )


def _dimension_binding_from_dict(value: Any, *, index: int) -> DimensionBinding:
    path = f"analysis_contract.dimension_bindings[{index}]"
    item = _strict_mapping(value, path=path)
    _require_exact_keys(item, tuple(DimensionBinding.__dataclass_fields__), path=path)
    return DimensionBinding(
        dimension_id=_strict_string(
            item["dimension_id"], path=f"{path}.dimension_id"
        ),
        contract_ref=_strict_string(item["contract_ref"], path=f"{path}.contract_ref"),
        dataset_id=_strict_string(item["dataset_id"], path=f"{path}.dataset_id"),
        source_field=_strict_string(item["source_field"], path=f"{path}.source_field"),
        allowed_grains=_strict_string_sequence(
            item["allowed_grains"], path=f"{path}.allowed_grains"
        ),
        null_bucket=_strict_string(item["null_bucket"], path=f"{path}.null_bucket"),
    )


def _contract_gap_from_dict(value: Any, *, index: int) -> ContractGap:
    path = f"analysis_contract.contract_gaps[{index}]"
    item = _strict_mapping(value, path=path)
    _require_exact_keys(item, tuple(ContractGap.__dataclass_fields__), path=path)
    if type(item["requires_clarification"]) is not bool:
        raise TypeError(f"{path}.requires_clarification:expected_boolean")
    return ContractGap(
        gap_type=_strict_string(item["gap_type"], path=f"{path}.gap_type"),
        gap_id=_strict_string(item["gap_id"], path=f"{path}.gap_id"),
        dataset_id=_strict_string(
            item["dataset_id"], path=f"{path}.dataset_id", allow_empty=True
        ),
        affected_capabilities=_strict_string_sequence(
            item["affected_capabilities"], path=f"{path}.affected_capabilities"
        ),
        affected_claim_types=_strict_string_sequence(
            item["affected_claim_types"], path=f"{path}.affected_claim_types"
        ),
        owner=_strict_string(item["owner"], path=f"{path}.owner"),
        repair_options=_strict_string_sequence(
            item["repair_options"], path=f"{path}.repair_options"
        ),
        requires_clarification=item["requires_clarification"],
        diagnostic_context=dict(
            _strict_mapping(
                item["diagnostic_context"], path=f"{path}.diagnostic_context"
            )
        ),
    )


def _strict_mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError(f"{path}:expected_mapping")
    return value


def _strict_sequence(value: Any, *, path: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{path}:expected_sequence")
    return tuple(value)


def _strict_string_sequence(value: Any, *, path: str) -> tuple[str, ...]:
    items = _strict_sequence(value, path=path)
    if any(type(item) is not str or not item for item in items):
        raise TypeError(f"{path}:expected_nonempty_strings")
    return items


def _strict_string(value: Any, *, path: str, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise TypeError(f"{path}:expected_string")
    return value


def _strict_int(value: Any, *, path: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{path}:expected_integer")
    return value


def _strict_number(value: Any, *, path: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{path}:expected_number")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: tuple[str, ...], *, path: str
) -> None:
    if set(value) != set(expected):
        missing = sorted(set(expected) - set(value))
        unknown = sorted(set(value) - set(expected))
        raise ValueError(f"{path}:keys_invalid:missing={missing}:unknown={unknown}")


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
