from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from bi_agent.capabilities import EvidenceEnvelope, make_evidence_envelope
from bi_agent.capabilities.candidate_dimension_screen import (
    candidate_dimension_screen,
)
from bi_agent.capabilities.change_point_scan import change_point_scan
from bi_agent.capabilities.cross_source_association import cross_source_association
from bi_agent.capabilities.cross_source_panel_association import (
    cross_source_panel_association,
)
from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.event_evidence import (
    EVENT_PRESENCE_EVIDENCE_CONTRACT,
    event_evidence,
)
from bi_agent.capabilities.high_value_user_contribution import (
    high_value_user_contribution,
)
from bi_agent.capabilities.joint_attribution import joint_attribution
from bi_agent.capabilities.market_channel_context import market_channel_context
from bi_agent.capabilities.metric_coverage_profile import metric_coverage_profile
from bi_agent.capabilities.metric_timeseries import metric_timeseries
from bi_agent.capabilities.outlier_contribution import outlier_contribution
from bi_agent.capabilities.outlier_scan import outlier_scan
from bi_agent.capabilities.payment_outcome_compare import payment_outcome_compare
from bi_agent.capabilities.pattern_scan import PatternScanResult, scan_pattern
from bi_agent.capabilities.segment_contribution import segment_contribution
from bi_agent.capabilities.segment_distribution import (
    segment_breakdown,
    segment_shift_compare,
)
from bi_agent.capabilities.source_reconciliation import source_reconciliation
from bi_agent.capabilities.user_mix_contribution import user_mix_contribution
from bi_agent.runtime.capability_authority import (
    CapabilityAdapterOutput,
    CapabilityAttempt,
    CapabilityEvidence,
    CapabilityFailure,
)
from bi_agent.runtime.evidence_authority import canonical_digest
from bi_agent.runtime.event_window_derivation import (
    EventWindowDerivationError,
    validate_event_window_derivation_policy,
    validate_event_window_set,
)
from bi_agent.runtime.evidence_taxonomy import (
    EvidenceTaxonomyContractError,
    publication_evidence_kind,
)
from bi_agent.runtime.formula_graph import (
    decompose_formula_change,
    validate_formula_contribution_groupings,
)
from bi_agent.runtime.plan_authority import CapabilityTask, PlanRevision
from bi_agent.runtime.window_metric_evidence import (
    aggregate_derived_event_window_set,
    aggregate_window_metric_comparison,
    validate_event_window_metric_authority,
)


class CapabilityTaskAdapterContractError(ValueError):
    pass


EXPECTED_GAP_TYPES = frozenset(
    {
        "contract_absent",
        "contract_partial",
        "dataset_snapshot_unavailable_as_of",
        "missing_contract",
        "permission_blocked",
        "source_unbound",
        "unsupported_grain",
        "unsupported_scope",
        "window_data_unavailable",
    }
)
_UNAVAILABLE_PRIMITIVE_EVIDENCE_TYPES = frozenset({"insufficient_evidence"})
_TRUST_BOUNDARY_WORDING_LIMITS = frozenset(
    {"context_only", "degraded", "supported", "trust_boundary"}
)
_EVENT_WINDOW_EVIDENCE_CONTRACT = "event-window-metric-comparison.v1"
_EVENT_WINDOW_CLAIM_RECORD_LIMIT = 20


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CapabilityTaskAdapterContractError(error)
    return value


def _string_tuple(
    value: Any,
    error: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CapabilityTaskAdapterContractError(error)
    normalized = tuple(_required_string(item, error) for item in value)
    if not allow_empty and not normalized:
        raise CapabilityTaskAdapterContractError(error)
    if len(normalized) != len(set(normalized)):
        raise CapabilityTaskAdapterContractError(error)
    return normalized


def _freeze_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CapabilityTaskAdapterContractError(
                    "task_runtime_payload_key_invalid"
                )
            normalized[key] = _freeze_payload(item)
        return MappingProxyType(normalized)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)
    if isinstance(value, (set, frozenset)):
        raise CapabilityTaskAdapterContractError("task_runtime_payload_set_unsupported")
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class ExpectedCapabilityGap:
    gap_type: str
    limitation_ref: str
    data_contract_state: str
    business_boundary: str
    retryability: str

    @classmethod
    def create(
        cls,
        *,
        gap_type: str,
        limitation_ref: str,
        data_contract_state: str,
        business_boundary: str,
        retryability: str,
    ) -> "ExpectedCapabilityGap":
        if gap_type not in EXPECTED_GAP_TYPES:
            raise CapabilityTaskAdapterContractError(
                "expected_capability_gap_type_invalid"
            )
        if retryability not in {"never", "same_input", "replan_required"}:
            raise CapabilityTaskAdapterContractError(
                "expected_capability_gap_retryability_invalid"
            )
        return cls(
            gap_type=gap_type,
            limitation_ref=_required_string(
                limitation_ref,
                "expected_capability_gap_limitation_ref_invalid",
            ),
            data_contract_state=_required_string(
                data_contract_state,
                "expected_capability_gap_data_contract_state_invalid",
            ),
            business_boundary=_required_string(
                business_boundary,
                "expected_capability_gap_business_boundary_invalid",
            ),
            retryability=retryability,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "gap_type": self.gap_type,
            "limitation_ref": self.limitation_ref,
            "data_contract_state": self.data_contract_state,
            "business_boundary": self.business_boundary,
            "retryability": self.retryability,
        }


@dataclass(frozen=True)
class TaskScopedCapabilityInput:
    plan_revision_id: str
    task_id: str
    authority_context_ref: str
    binding_record_ref: str | None
    data_contract_state: str
    maximum_claim_strength: str
    scope_ref: str
    payload: Mapping[str, Any]
    result_refs: tuple[str, ...]
    completeness_report_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    expected_gap: ExpectedCapabilityGap | None
    terminal_failure_status: str | None
    terminal_failure: CapabilityFailure | None
    services: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        *,
        plan_revision_id: str,
        task_id: str,
        authority_context_ref: str,
        binding_record_ref: str | None,
        data_contract_state: str,
        maximum_claim_strength: str,
        scope_ref: str,
        payload: Mapping[str, Any],
        result_refs: Sequence[str],
        completeness_report_refs: Sequence[str],
        limitation_refs: Sequence[str],
        expected_gap: ExpectedCapabilityGap | None,
        terminal_failure_status: str | None = None,
        terminal_failure: CapabilityFailure | Mapping[str, Any] | None = None,
        services: Mapping[str, Any],
    ) -> "TaskScopedCapabilityInput":
        if not isinstance(payload, Mapping):
            raise CapabilityTaskAdapterContractError("task_runtime_payload_invalid")
        if not isinstance(services, Mapping) or any(
            not isinstance(key, str) or not key for key in services
        ):
            raise CapabilityTaskAdapterContractError("task_runtime_services_invalid")
        if expected_gap is not None and not isinstance(
            expected_gap, ExpectedCapabilityGap
        ):
            raise CapabilityTaskAdapterContractError(
                "task_runtime_expected_gap_invalid"
            )
        if terminal_failure is not None and not isinstance(
            terminal_failure, CapabilityFailure
        ):
            terminal_failure = CapabilityFailure.from_dict(terminal_failure)
        if terminal_failure_status is not None and terminal_failure_status not in {
            "integrity_failed",
            "technical_failed",
        }:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_terminal_failure_status_invalid"
            )
        if (terminal_failure_status is None) != (terminal_failure is None):
            raise CapabilityTaskAdapterContractError(
                "task_runtime_terminal_failure_incomplete"
            )
        if expected_gap is not None and terminal_failure is not None:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_terminal_states_conflict"
            )
        if (expected_gap is not None or terminal_failure is not None) and payload:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_terminal_payload_forbidden"
            )
        if binding_record_ref is not None:
            binding_record_ref = _required_string(
                binding_record_ref, "task_runtime_binding_ref_invalid"
            )
        return cls(
            plan_revision_id=_required_string(
                plan_revision_id, "task_runtime_plan_revision_id_invalid"
            ),
            task_id=_required_string(task_id, "task_runtime_task_id_invalid"),
            authority_context_ref=_required_string(
                authority_context_ref,
                "task_runtime_authority_context_ref_invalid",
            ),
            binding_record_ref=binding_record_ref,
            data_contract_state=_required_string(
                data_contract_state, "task_runtime_data_contract_state_invalid"
            ),
            maximum_claim_strength=_required_string(
                maximum_claim_strength,
                "task_runtime_maximum_claim_strength_invalid",
            ),
            scope_ref=_required_string(scope_ref, "task_runtime_scope_ref_invalid"),
            payload=_freeze_payload(payload),
            result_refs=_string_tuple(result_refs, "task_runtime_result_refs_invalid"),
            completeness_report_refs=_string_tuple(
                completeness_report_refs,
                "task_runtime_completeness_refs_invalid",
            ),
            limitation_refs=_string_tuple(
                limitation_refs, "task_runtime_limitation_refs_invalid"
            ),
            expected_gap=expected_gap,
            terminal_failure_status=terminal_failure_status,
            terminal_failure=terminal_failure,
            services=MappingProxyType(dict(services)),
        )


@runtime_checkable
class TaskRuntimeInputResolver(Protocol):
    def resolve_task_input(
        self,
        plan_revision_id: str,
        task_id: str,
    ) -> TaskScopedCapabilityInput: ...


@dataclass(frozen=True)
class TaskRuntimeInputs:
    _by_task_id: Mapping[str, TaskScopedCapabilityInput]

    @classmethod
    def create(
        cls,
        inputs: Sequence[TaskScopedCapabilityInput],
    ) -> "TaskRuntimeInputs":
        if isinstance(inputs, (str, bytes)) or not isinstance(inputs, Sequence):
            raise CapabilityTaskAdapterContractError("task_runtime_inputs_invalid")
        by_task_id: dict[str, TaskScopedCapabilityInput] = {}
        for item in inputs:
            if not isinstance(item, TaskScopedCapabilityInput):
                raise CapabilityTaskAdapterContractError(
                    "task_runtime_input_type_invalid"
                )
            if item.task_id in by_task_id:
                raise CapabilityTaskAdapterContractError(
                    f"task_runtime_input_duplicated:{item.task_id}"
                )
            by_task_id[item.task_id] = item
        return cls(_by_task_id=MappingProxyType(by_task_id))

    def resolve_task_input(
        self,
        plan_revision_id: str,
        task_id: str,
    ) -> TaskScopedCapabilityInput:
        item = self._by_task_id.get(task_id)
        if item is None:
            raise CapabilityTaskAdapterContractError(
                f"task_runtime_input_missing:{task_id}"
            )
        if item.plan_revision_id != plan_revision_id:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_input_plan_revision_mismatch"
            )
        return item


CapabilityTaskAdapter = Callable[
    [
        PlanRevision,
        CapabilityTask,
        CapabilityAttempt,
        TaskScopedCapabilityInput,
    ],
    CapabilityAdapterOutput,
]


@dataclass(frozen=True)
class CapabilityAdapterRegistration:
    capability_id: str
    adapter: CapabilityTaskAdapter

    def __post_init__(self) -> None:
        _required_string(self.capability_id, "adapter_capability_id_invalid")
        if not callable(self.adapter):
            raise CapabilityTaskAdapterContractError("adapter_callable_invalid")


class CapabilityTaskAdapterRegistry:
    def __init__(
        self,
        registrations: Sequence[CapabilityAdapterRegistration],
    ) -> None:
        if isinstance(registrations, (str, bytes)) or not isinstance(
            registrations, Sequence
        ):
            raise CapabilityTaskAdapterContractError("adapter_registrations_invalid")
        by_capability: dict[str, CapabilityTaskAdapter] = {}
        for registration in registrations:
            if not isinstance(registration, CapabilityAdapterRegistration):
                raise CapabilityTaskAdapterContractError(
                    "adapter_registration_type_invalid"
                )
            if registration.capability_id in by_capability:
                raise CapabilityTaskAdapterContractError(
                    f"adapter_duplicated:{registration.capability_id}"
                )
            by_capability[registration.capability_id] = registration.adapter
        self._by_capability = MappingProxyType(by_capability)

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_capability))

    def validate_plan(self, plan_revision: PlanRevision) -> None:
        if not isinstance(plan_revision, PlanRevision) or not plan_revision.executable:
            raise CapabilityTaskAdapterContractError("adapter_plan_invalid")
        missing = tuple(
            sorted(
                {
                    task.capability_id
                    for task in plan_revision.capability_tasks
                    if task.capability_id not in self._by_capability
                }
            )
        )
        if missing:
            raise CapabilityTaskAdapterContractError(
                "adapter_missing:" + ",".join(missing)
            )

    def bind(
        self,
        plan_revision: PlanRevision,
        runtime_input: TaskRuntimeInputResolver,
    ) -> Callable[[CapabilityTask, CapabilityAttempt], CapabilityAdapterOutput]:
        self.validate_plan(plan_revision)
        if not isinstance(runtime_input, TaskRuntimeInputResolver):
            raise CapabilityTaskAdapterContractError(
                "task_runtime_input_resolver_invalid"
            )
        task_by_id = {task.task_id: task for task in plan_revision.capability_tasks}

        def execute(
            task: CapabilityTask,
            attempt: CapabilityAttempt,
        ) -> CapabilityAdapterOutput:
            active_task = task_by_id.get(getattr(task, "task_id", ""))
            if active_task is None or active_task != task:
                raise CapabilityTaskAdapterContractError(
                    "adapter_task_not_in_active_plan"
                )
            _validate_attempt(plan_revision, active_task, attempt)
            scoped_input = runtime_input.resolve_task_input(
                plan_revision.plan_revision_id,
                active_task.task_id,
            )
            _validate_scoped_input(plan_revision, active_task, scoped_input)
            if scoped_input.expected_gap is not None:
                return _expected_gap_output(active_task, scoped_input.expected_gap)
            if scoped_input.terminal_failure is not None:
                return _terminal_failure_output(active_task, scoped_input)
            adapter = self._by_capability[active_task.capability_id]
            output = adapter(
                plan_revision,
                active_task,
                attempt,
                scoped_input,
            )
            if not isinstance(output, CapabilityAdapterOutput):
                raise CapabilityTaskAdapterContractError("adapter_output_type_invalid")
            unexpected_obligations = set(output.affected_obligation_ids) - set(
                active_task.supports_obligation_ids
            )
            if unexpected_obligations:
                raise CapabilityTaskAdapterContractError(
                    "adapter_output_obligation_scope_invalid"
                )
            return output

        return execute


def _validate_attempt(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    attempt: CapabilityAttempt,
) -> None:
    if not isinstance(attempt, CapabilityAttempt):
        raise CapabilityTaskAdapterContractError("adapter_attempt_invalid")
    expected = CapabilityAttempt.create(
        plan_revision,
        task,
        execution_attempt=attempt.execution_attempt,
    )
    if attempt != expected:
        raise CapabilityTaskAdapterContractError("adapter_attempt_authority_mismatch")


def _validate_scoped_input(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    scoped_input: TaskScopedCapabilityInput,
) -> None:
    if not isinstance(scoped_input, TaskScopedCapabilityInput):
        raise CapabilityTaskAdapterContractError("task_runtime_input_type_invalid")
    if (
        scoped_input.plan_revision_id != plan_revision.plan_revision_id
        or scoped_input.task_id != task.task_id
        or scoped_input.authority_context_ref != plan_revision.authority_context_ref
        or task.authority_context_ref != plan_revision.authority_context_ref
    ):
        raise CapabilityTaskAdapterContractError(
            "task_runtime_input_authority_mismatch"
        )
    if (
        scoped_input.expected_gap is None
        and scoped_input.terminal_failure is None
        and scoped_input.binding_record_ref is None
    ):
        raise CapabilityTaskAdapterContractError("task_runtime_binding_ref_required")
    bound = scoped_input.services.get("bound_capability_input")
    if bound is not None and (
        getattr(bound, "maximum_claim_strength", None)
        != scoped_input.maximum_claim_strength
    ):
        raise CapabilityTaskAdapterContractError(
            "task_runtime_claim_ceiling_binding_mismatch"
        )


def _expected_gap_output(
    task: CapabilityTask,
    gap: ExpectedCapabilityGap,
) -> CapabilityAdapterOutput:
    return CapabilityAdapterOutput.create(
        status="unavailable",
        output_payload={"expected_gap": gap.to_dict()},
        evidence=(),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=(gap.limitation_ref,),
        retryability=gap.retryability,
    )


def _terminal_failure_output(
    task: CapabilityTask,
    scoped_input: TaskScopedCapabilityInput,
) -> CapabilityAdapterOutput:
    failure = scoped_input.terminal_failure
    status = scoped_input.terminal_failure_status
    if failure is None or status not in {"integrity_failed", "technical_failed"}:
        raise CapabilityTaskAdapterContractError(
            "task_runtime_terminal_failure_incomplete"
        )
    return CapabilityAdapterOutput.create(
        status=status,
        output_payload={"terminal_failure": failure.to_dict()},
        evidence=(),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=scoped_input.limitation_refs,
        retryability=failure.retryability,
        failure=failure,
    )


def builtin_capability_adapter_registry() -> CapabilityTaskAdapterRegistry:
    registrations = (
        CapabilityAdapterRegistration(
            "compare_periods", _window_metric_comparison_adapter
        ),
        CapabilityAdapterRegistration(
            "market_health_compare", _multi_metric_window_comparison_adapter
        ),
        CapabilityAdapterRegistration(
            "post_payment_behavior_compare", _window_metric_comparison_adapter
        ),
        CapabilityAdapterRegistration(
            "post_payment_tier_behavior",
            _primitive_adapter(segment_breakdown, hierarchy_paths=True),
        ),
        CapabilityAdapterRegistration(
            "market_channel_context", _primitive_adapter(market_channel_context)
        ),
        CapabilityAdapterRegistration(
            "source_reconciliation", _primitive_adapter(source_reconciliation)
        ),
        CapabilityAdapterRegistration(
            "compare_period_phases", _pattern_adapter("intra_period")
        ),
        CapabilityAdapterRegistration(
            "rolling_window_compare", _pattern_adapter("rolling")
        ),
        CapabilityAdapterRegistration(
            "weekday_calendar_compare", _pattern_adapter("weekly")
        ),
        CapabilityAdapterRegistration(
            "event_window_compare", _event_window_metric_comparison_adapter
        ),
        CapabilityAdapterRegistration(
            "internal_operation_event_window_compare",
            _event_window_metric_comparison_adapter,
        ),
        CapabilityAdapterRegistration("formula_decompose", _formula_graph_adapter),
        CapabilityAdapterRegistration(
            "funnel_decompose", _funnel_decomposition_adapter
        ),
        CapabilityAdapterRegistration(
            "candidate_dimension_screen",
            _primitive_adapter(candidate_dimension_screen, hierarchy_paths=True),
        ),
        CapabilityAdapterRegistration(
            "payment_outcome_compare",
            _primitive_adapter(payment_outcome_compare),
        ),
        CapabilityAdapterRegistration(
            "data_quality_profile", _primitive_adapter(data_quality_check)
        ),
        CapabilityAdapterRegistration(
            "metric_coverage_profile", _primitive_adapter(metric_coverage_profile)
        ),
        CapabilityAdapterRegistration(
            "metric_timeseries", _primitive_adapter(metric_timeseries)
        ),
        CapabilityAdapterRegistration("event_evidence", _event_evidence_adapter),
        CapabilityAdapterRegistration(
            "internal_operation_event_evidence", _event_evidence_adapter
        ),
        CapabilityAdapterRegistration(
            "cross_source_association",
            _primitive_adapter(cross_source_association),
        ),
        CapabilityAdapterRegistration(
            "cross_source_panel_association",
            _primitive_adapter(cross_source_panel_association),
        ),
        CapabilityAdapterRegistration(
            "segment_contribution", _primitive_adapter(segment_contribution)
        ),
        CapabilityAdapterRegistration(
            "segment_breakdown",
            _primitive_adapter(segment_breakdown, hierarchy_paths=True),
        ),
        CapabilityAdapterRegistration(
            "segment_shift_compare",
            _primitive_adapter(segment_shift_compare, hierarchy_paths=True),
        ),
        CapabilityAdapterRegistration(
            "user_mix_contribution", _primitive_adapter(user_mix_contribution)
        ),
        CapabilityAdapterRegistration(
            "high_value_user_contribution",
            _primitive_adapter(high_value_user_contribution),
        ),
        CapabilityAdapterRegistration(
            "joint_attribution", _joint_attribution_adapter
        ),
        CapabilityAdapterRegistration("outlier_scan", _primitive_adapter(outlier_scan)),
        CapabilityAdapterRegistration(
            "outlier_contribution", _primitive_adapter(outlier_contribution)
        ),
        CapabilityAdapterRegistration(
            "change_point_scan", _primitive_adapter(change_point_scan)
        ),
    )
    return CapabilityTaskAdapterRegistry(registrations)


def _primitive_adapter(
    primitive: Callable[..., EvidenceEnvelope],
    *,
    hierarchy_paths: bool = False,
) -> CapabilityTaskAdapter:
    def execute(
        plan_revision: PlanRevision,
        task: CapabilityTask,
        _attempt: CapabilityAttempt,
        scoped_input: TaskScopedCapabilityInput,
    ) -> CapabilityAdapterOutput:
        kwargs = dict(scoped_input.payload)
        if "result_refs" in kwargs:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_payload_result_refs_forbidden"
            )
        envelope = primitive(**kwargs, result_refs=scoped_input.result_refs)
        return _envelope_output(
            plan_revision,
            task,
            scoped_input,
            envelope,
            hierarchy_paths=hierarchy_paths,
        )

    return execute


def _joint_attribution_adapter(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    _attempt: CapabilityAttempt,
    scoped_input: TaskScopedCapabilityInput,
) -> CapabilityAdapterOutput:
    kwargs = dict(scoped_input.payload)
    analyses = kwargs.pop("analyses", ())
    if (
        isinstance(analyses, (str, bytes))
        or not isinstance(analyses, Sequence)
        or not analyses
    ):
        raise CapabilityTaskAdapterContractError(
            "task_runtime_joint_analyses_invalid"
        )
    observations = []
    limitations = []
    for analysis in analyses:
        if not isinstance(analysis, Mapping):
            raise CapabilityTaskAdapterContractError(
                "task_runtime_joint_analysis_invalid"
            )
        query_contract_ref = _required_string(
            analysis.get("query_contract_ref"),
            "task_runtime_joint_query_contract_ref_invalid",
        )
        dimension_keys = _string_tuple(
            analysis.get("dimension_keys"),
            "task_runtime_joint_dimension_keys_invalid",
            allow_empty=False,
        )
        if len(dimension_keys) < 2:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_joint_dimension_count_invalid"
            )
        rows = analysis.get("rows")
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise CapabilityTaskAdapterContractError(
                "task_runtime_joint_rows_invalid"
            )
        envelope = joint_attribution(
            rows=tuple(rows),
            dimension_keys=dimension_keys,
            **kwargs,
            result_refs=(),
        )
        limitations.extend(envelope.limitations)
        observations.append(
            {
                "query_contract_ref": query_contract_ref,
                "dimension_keys": dimension_keys,
                "evidence_type": envelope.evidence_type,
                "strength": envelope.strength,
                "wording_limit": envelope.wording_limit,
                "typed_payload": _plain(envelope.typed_payload),
                "limitations": tuple(envelope.limitations),
            }
        )
    supported = tuple(
        item
        for item in observations
        if item["evidence_type"] == "accounting_contribution"
    )
    if not supported:
        envelope = make_evidence_envelope(
            task.capability_id,
            evidence_type="insufficient_evidence",
            strength="low",
            wording_limit="insufficient",
            typed_payload={
                "evidence_contract": "multi-query-joint-attribution.v1",
                "analysis_count": len(observations),
                "supported_analysis_count": 0,
                "analyses": tuple(observations),
            },
            limitations=tuple(dict.fromkeys(limitations)),
            result_refs=scoped_input.result_refs,
        )
    else:
        if len(supported) != len(observations):
            limitations.append("partial_joint_dimension_coverage")
        envelope = make_evidence_envelope(
            task.capability_id,
            evidence_type="accounting_contribution",
            strength="medium",
            wording_limit="candidate",
            typed_payload={
                "evidence_contract": "multi-query-joint-attribution.v1",
                "analysis_count": len(observations),
                "supported_analysis_count": len(supported),
                "analyses": tuple(observations),
                "claim_boundary": (
                    "联合维度结果用于定位候选贡献组合，不能单独证明因果关系。"
                ),
            },
            limitations=tuple(dict.fromkeys(limitations)),
            result_refs=scoped_input.result_refs,
        )
    return _envelope_output(
        plan_revision,
        task,
        scoped_input,
        envelope,
        hierarchy_paths=False,
    )


def _pattern_adapter(pattern_family: str) -> CapabilityTaskAdapter:
    def execute(
        plan_revision: PlanRevision,
        task: CapabilityTask,
        _attempt: CapabilityAttempt,
        scoped_input: TaskScopedCapabilityInput,
    ) -> CapabilityAdapterOutput:
        kwargs = dict(scoped_input.payload)
        supplied_family = kwargs.pop("pattern_family", pattern_family)
        if supplied_family != pattern_family:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_pattern_family_mismatch"
            )
        if "result_refs" in kwargs or "evidence_ref" in kwargs:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_payload_provenance_forbidden"
            )
        result = scan_pattern(
            **kwargs,
            pattern_family=pattern_family,
            result_refs=scoped_input.result_refs,
            evidence_ref=f"pattern:{task.task_id}",
        )
        return _envelope_output(
            plan_revision,
            task,
            scoped_input,
            result,
            hierarchy_paths=False,
        )

    return execute


def _window_metric_comparison_adapter(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    _attempt: CapabilityAttempt,
    scoped_input: TaskScopedCapabilityInput,
) -> CapabilityAdapterOutput:
    kwargs = dict(scoped_input.payload)
    comparison = aggregate_window_metric_comparison(**kwargs)
    envelope = make_evidence_envelope(
        task.capability_id,
        evidence_type="observed_comparison",
        strength="directional",
        wording_limit="comparative",
        numeric_facts={
            "target_value": comparison.target.value,
            "baseline_value": comparison.primary_baseline.value,
        },
        typed_payload=comparison.to_payload(),
        limitations=(),
        result_refs=scoped_input.result_refs,
        evidence_ref=f"window-comparison:{task.task_id}",
    )
    return _envelope_output(
        plan_revision,
        task,
        scoped_input,
        envelope,
        hierarchy_paths=False,
    )


def _multi_metric_window_comparison_adapter(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    _attempt: CapabilityAttempt,
    scoped_input: TaskScopedCapabilityInput,
) -> CapabilityAdapterOutput:
    kwargs = dict(scoped_input.payload)
    metric_ids = tuple(kwargs.pop("metric_ids", ()))
    if not metric_ids or any(
        not isinstance(metric_id, str) or not metric_id
        for metric_id in metric_ids
    ):
        raise CapabilityTaskAdapterContractError(
            "task_runtime_multi_metric_ids_invalid"
        )
    comparisons = tuple(
        aggregate_window_metric_comparison(
            **kwargs,
            metric_id=metric_id,
        )
        for metric_id in metric_ids
    )
    numeric_facts = {
        fact_key: fact_value
        for comparison in comparisons
        for fact_key, fact_value in (
            (f"{comparison.metric_id}_target_value", comparison.target.value),
            (
                f"{comparison.metric_id}_baseline_value",
                comparison.primary_baseline.value,
            ),
        )
    }
    envelope = make_evidence_envelope(
        task.capability_id,
        evidence_type="observed_comparison",
        strength="directional",
        wording_limit="comparative",
        numeric_facts=numeric_facts,
        typed_payload={
            "comparison_set_contract": "multi-metric-window-comparison.v1",
            "metric_ids": metric_ids,
            "comparisons": tuple(
                comparison.to_payload() for comparison in comparisons
            ),
        },
        limitations=(),
        result_refs=scoped_input.result_refs,
        evidence_ref=f"multi-window-comparison:{task.task_id}",
    )
    return _envelope_output(
        plan_revision,
        task,
        scoped_input,
        envelope,
        hierarchy_paths=False,
    )


def _event_evidence_adapter(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    _attempt: CapabilityAttempt,
    scoped_input: TaskScopedCapabilityInput,
) -> CapabilityAdapterOutput:
    kwargs = dict(scoped_input.payload)
    if "result_refs" in kwargs:
        raise CapabilityTaskAdapterContractError(
            "task_runtime_payload_result_refs_forbidden"
        )
    temporal_authority = plan_revision.temporal_authority
    supplied_event_ref = kwargs.get("event_ref")
    supplied_authority_ref = kwargs.get("temporal_authority_ref")
    supplied_event_window_set = kwargs.get("event_window_set")
    if temporal_authority.mode == "event_relative":
        if (
            supplied_event_ref != temporal_authority.event_ref
            or supplied_authority_ref != temporal_authority.authority_ref
        ):
            raise CapabilityTaskAdapterContractError(
                "task_runtime_event_temporal_authority_mismatch"
            )
    elif supplied_event_ref is not None or supplied_authority_ref is not None:
        raise CapabilityTaskAdapterContractError(
            "task_runtime_event_temporal_authority_unexpected"
        )
    if supplied_event_window_set is not None:
        if temporal_authority.mode != "calendar_partition":
            raise CapabilityTaskAdapterContractError(
                "task_runtime_dynamic_event_authority_unexpected"
            )
        try:
            policy = validate_event_window_derivation_policy(
                supplied_event_window_set.get("derivation_policy"),
                expected_source_dependency=task.capability_id,
            )
            validate_event_window_set(
                supplied_event_window_set,
                temporal_authority=temporal_authority,
                policy=policy,
            )
        except (AttributeError, EventWindowDerivationError) as exc:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_dynamic_event_authority_mismatch"
            ) from exc
    envelope = event_evidence(
        **kwargs,
        result_refs=scoped_input.result_refs,
    )
    if (
        temporal_authority.mode == "event_relative"
        and envelope.evidence_type != "insufficient_evidence"
        and envelope.typed_payload.get("evidence_contract")
        != EVENT_PRESENCE_EVIDENCE_CONTRACT
    ):
        raise CapabilityTaskAdapterContractError(
            "task_runtime_event_evidence_contract_mismatch"
        )
    if (
        supplied_event_window_set is not None
        and envelope.evidence_type != "insufficient_evidence"
        and envelope.typed_payload.get("evidence_contract")
        != EVENT_PRESENCE_EVIDENCE_CONTRACT
    ):
        raise CapabilityTaskAdapterContractError(
            "task_runtime_event_evidence_contract_mismatch"
        )
    return _envelope_output(
        plan_revision,
        task,
        scoped_input,
        envelope,
        hierarchy_paths=False,
    )


def _event_window_metric_comparison_adapter(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    _attempt: CapabilityAttempt,
    scoped_input: TaskScopedCapabilityInput,
) -> CapabilityAdapterOutput:
    kwargs = dict(scoped_input.payload)
    if set(kwargs) == {
        "contract",
        "rows",
        "metric_id",
        "derivation_policy",
        "event_window_set",
    }:
        temporal_authority = plan_revision.temporal_authority
        registry = scoped_input.services.get("runtime_registry")
        try:
            capability_contract = registry.capability_inputs(task.capability_id)
            expected_policy = validate_event_window_derivation_policy(
                capability_contract.get("dynamic_event_window_policy"),
            )
            supplied_policy = validate_event_window_derivation_policy(
                kwargs.pop("derivation_policy"),
            )
            if expected_policy != supplied_policy:
                raise EventWindowDerivationError(
                    "event_window_derivation_policy_mismatch"
                )
            comparison_set = aggregate_derived_event_window_set(
                kwargs.pop("contract"),
                kwargs.pop("rows"),
                metric_id=kwargs.pop("metric_id"),
                event_window_set=kwargs.pop("event_window_set"),
                temporal_authority=temporal_authority,
                derivation_policy=supplied_policy,
            )
        except (
            AttributeError,
            EventWindowDerivationError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise CapabilityTaskAdapterContractError(
                "task_runtime_dynamic_event_window_invalid"
            ) from exc
        comparisons = tuple(comparison_set["comparisons"])
        if not comparisons:
            envelope = make_evidence_envelope(
                task.capability_id,
                evidence_type="insufficient_evidence",
                strength="insufficient",
                wording_limit="insufficient",
                typed_payload={
                    "event_ref": comparison_set["event_ref"],
                    "temporal_authority_ref": comparison_set[
                        "temporal_authority_ref"
                    ],
                    "source_temporal_authority_ref": comparison_set[
                        "source_temporal_authority_ref"
                    ],
                    "metric": comparison_set["metric"],
                    "event_occurrence_count": 0,
                    "excluded_occurrence_counts": comparison_set[
                        "excluded_occurrence_counts"
                    ],
                    "business_readout": "已发现活动，但评估范围内没有完整的活动前后窗口。",
                    "claim_boundary": "没有完整前后窗口时不能判断活动与付费表现的方向关系。",
                },
                limitations=("no_complete_event_comparison_window",),
                result_refs=scoped_input.result_refs,
            )
            return _envelope_output(
                plan_revision,
                task,
                scoped_input,
                envelope,
                hierarchy_paths=False,
            )
        higher_count = sum(
            item["direction"] == "higher" for item in comparisons
        )
        lower_count = sum(item["direction"] == "lower" for item in comparisons)
        unchanged_count = len(comparisons) - higher_count - lower_count
        ranked_comparisons = tuple(
            sorted(
                comparisons,
                key=lambda item: (
                    item.get("relative_change") is not None,
                    abs(item.get("relative_change") or 0),
                    abs(item.get("absolute_change") or 0),
                    str(item.get("occurrence_ref") or ""),
                ),
                reverse=True,
            )
        )
        displayed_comparisons = ranked_comparisons[
            :_EVENT_WINDOW_CLAIM_RECORD_LIMIT
        ]
        claim_material_summary = {
            "projection_kind": "claim_material_summary",
            "evidence_contract": _EVENT_WINDOW_EVIDENCE_CONTRACT,
            "event_ref": comparison_set["event_ref"],
            "temporal_authority_ref": comparison_set[
                "temporal_authority_ref"
            ],
            "source_temporal_authority_ref": comparison_set[
                "source_temporal_authority_ref"
            ],
            "metric": comparison_set["metric"],
            "event_occurrence_count": len(comparisons),
            "post_event_higher_count": higher_count,
            "post_event_lower_count": lower_count,
            "post_event_unchanged_count": unchanged_count,
            "displayed_comparison_count": len(displayed_comparisons),
            "omitted_comparison_count": (
                len(comparisons) - len(displayed_comparisons)
            ),
            "comparison_record_limit": _EVENT_WINDOW_CLAIM_RECORD_LIMIT,
            "comparison_selection_policy": (
                "largest_absolute_relative_then_absolute_change"
            ),
            "material_comparisons": displayed_comparisons,
            "excluded_occurrence_counts": comparison_set[
                "excluded_occurrence_counts"
            ],
            "interpretation_contract": comparison_set[
                "interpretation_contract"
            ],
            "causal_interpretation_allowed": False,
        }
        typed_payload = {
            "evidence_contract": _EVENT_WINDOW_EVIDENCE_CONTRACT,
            "event_ref": comparison_set["event_ref"],
            "temporal_authority_ref": comparison_set[
                "temporal_authority_ref"
            ],
            "source_temporal_authority_ref": comparison_set[
                "source_temporal_authority_ref"
            ],
            "metric_comparisons": comparison_set,
            "interpretation_contract": comparison_set[
                "interpretation_contract"
            ],
            "claim_material_observations": (claim_material_summary,),
            "causal_interpretation_allowed": False,
        }
        envelope = make_evidence_envelope(
            task.capability_id,
            evidence_type="observed_comparison",
            strength="directional",
            wording_limit="candidate_non_causal",
            numeric_facts={
                "event_occurrence_count": len(comparisons),
                "post_event_higher_count": higher_count,
                "post_event_lower_count": lower_count,
                "post_event_unchanged_count": unchanged_count,
            },
            typed_payload=typed_payload,
            limitations=("event_window_comparison_is_non_causal",),
            result_refs=scoped_input.result_refs,
            evidence_ref=f"event-window-comparison:{task.task_id}",
        )
        return _envelope_output(
            plan_revision,
            task,
            scoped_input,
            envelope,
            hierarchy_paths=False,
        )
    expected_fields = {
        "contract",
        "rows",
        "metric_id",
        "primary_baseline_window_id",
        "event_ref",
        "temporal_authority_ref",
    }
    if set(kwargs) != expected_fields:
        raise CapabilityTaskAdapterContractError(
            "task_runtime_event_window_payload_shape_invalid"
        )
    temporal_authority = plan_revision.temporal_authority
    event_ref = kwargs.pop("event_ref")
    temporal_authority_ref = kwargs.pop("temporal_authority_ref")
    if (
        temporal_authority.mode != "event_relative"
        or not temporal_authority.event_ref
        or temporal_authority.baseline_window is None
        or event_ref != temporal_authority.event_ref
        or temporal_authority_ref != temporal_authority.authority_ref
    ):
        raise CapabilityTaskAdapterContractError(
            "task_runtime_event_window_authority_mismatch"
        )
    if kwargs["metric_id"] == "event_count":
        raise CapabilityTaskAdapterContractError(
            "task_runtime_event_window_metric_forbidden"
        )
    validate_event_window_metric_authority(
        kwargs["contract"],
        temporal_authority,
        primary_baseline_window_id=kwargs["primary_baseline_window_id"],
    )
    comparison = aggregate_window_metric_comparison(**kwargs)
    changes = comparison.changes(
        comparison.target,
        comparison.primary_baseline,
    )
    comparison_payload = comparison.to_payload()
    claim_material_summary = {
        "projection_kind": "claim_material_summary",
        "evidence_contract": _EVENT_WINDOW_EVIDENCE_CONTRACT,
        "event_ref": event_ref,
        "temporal_authority_ref": temporal_authority_ref,
        "metric_comparison": comparison_payload,
        "interpretation_contract": comparison_payload["interpretation_contract"],
        "causal_interpretation_allowed": False,
    }
    typed_payload = {
        "evidence_contract": _EVENT_WINDOW_EVIDENCE_CONTRACT,
        "event_ref": event_ref,
        "temporal_authority_ref": temporal_authority_ref,
        "metric_comparison": comparison_payload,
        "interpretation_contract": comparison_payload["interpretation_contract"],
        "claim_material_observations": (claim_material_summary,),
        "causal_interpretation_allowed": False,
    }
    envelope = make_evidence_envelope(
        task.capability_id,
        evidence_type="observed_comparison",
        strength="directional",
        wording_limit="candidate_non_causal",
        numeric_facts={
            "target_value": comparison.target.value,
            "baseline_value": comparison.primary_baseline.value,
            **changes,
        },
        typed_payload=typed_payload,
        limitations=("event_window_comparison_is_non_causal",),
        result_refs=scoped_input.result_refs,
        evidence_ref=f"event-window-comparison:{task.task_id}",
    )
    return _envelope_output(
        plan_revision,
        task,
        scoped_input,
        envelope,
        hierarchy_paths=False,
    )


def _formula_graph_adapter(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    _attempt: CapabilityAttempt,
    scoped_input: TaskScopedCapabilityInput,
) -> CapabilityAdapterOutput:
    kwargs = dict(scoped_input.payload)
    formula_path_id = _required_string(
        kwargs.pop("formula_path_id", None),
        "task_runtime_formula_path_id_missing",
    )
    formula_contract_ref = _required_string(
        kwargs.pop("formula_contract_ref", None),
        "task_runtime_formula_contract_ref_missing",
    )
    factor_metric_ids = kwargs.get("factor_metric_ids")
    factor_groupings = validate_formula_contribution_groupings(
        kwargs.pop("factor_groupings", ()),
        factor_metric_ids=factor_metric_ids,
    )
    decomposition = decompose_formula_change(
        kwargs.pop("formula_ast"),
        factor_groupings=factor_groupings,
        **kwargs,
    )
    payload = {
        "formula_path_id": formula_path_id,
        "formula_contract_ref": formula_contract_ref,
        "interpretation_contract": {
            "contract_id": "formula-accounting-decomposition-interpretation.v2",
            "analysis_role": "accounting_decomposition",
            "ranking_scope": "within_formula_decomposition_components",
            "ranking_subject": "formula_component",
            "ranking_measure": "absolute_contribution",
            "ranking_order": "absolute_contribution_descending",
            "additivity": {
                "contribution": "reconciles_to_contribution_total",
                "contribution_share": ("sums_to_one_when_contribution_total_nonzero"),
            },
            "contribution_semantics": {
                "contribution": "signed_accounting_component_change",
                "contribution_share": ("signed_share_of_contribution_total"),
            },
            "contribution_share_denominator": ("decomposition.contribution_total"),
            "contribution_share_range": "unbounded_signed",
            "zero_contribution_total_policy": ("contribution_share_unavailable"),
            "factor_hierarchy": {
                "leaf_decomposition": "decomposition.contributions",
                "grouped_decompositions": "decomposition.grouped_decompositions",
                "grouping_method": (
                    "independent_grouped_shapley_over_contract_declared_partition"
                ),
                "cross_level_additivity": "forbidden",
                "comparison_rule": (
                    "compare_factors_only_within_the_same_grouping_id"
                ),
                "groupings": [
                    {
                        "grouping_id": grouping.grouping_id,
                        "method": grouping.method,
                        "factors": [
                            {
                                "factor_ref": group.factor_id,
                                "member_metric_refs": list(group.member_metric_ids),
                            }
                            for group in grouping.groups
                        ],
                    }
                    for grouping in factor_groupings
                ],
            },
            "dimension_localization_relationship": (
                "co_report_only_no_shared_rank_sum_or_share"
            ),
            "causal_interpretation": "forbidden",
        },
        "decomposition": asdict(decomposition),
    }
    if decomposition.status == "missing":
        limitations = tuple(
            dict.fromkeys(
                (
                    *(
                        f"missing-metric:{item}"
                        for item in decomposition.missing_metric_ids
                    ),
                    *(
                        f"missing-dimension:{item}"
                        for item in decomposition.missing_dimension_ids
                    ),
                    *(
                        (f"formula:{decomposition.reason}",)
                        if decomposition.reason
                        else ()
                    ),
                )
            )
        )
        return CapabilityAdapterOutput.create(
            status="unavailable",
            output_payload=payload,
            evidence=(),
            affected_obligation_ids=task.supports_obligation_ids,
            limitation_refs=limitations,
            retryability="replan_required",
        )
    if decomposition.status != "reconciled":
        kind = (
            "formula_reconciliation_mismatch"
            if decomposition.status == "mismatch"
            else f"formula_{decomposition.status}"
        )
        failure = CapabilityFailure.create(
            layer="capability",
            kind=kind,
            scope="task",
            affected_refs=(task.task_id, *task.supports_obligation_ids),
            integrity_level="task",
            retryability="replan_required",
            user_actionable=False,
            business_boundary="formula_contribution_unpublishable",
            technical_detail_ref=("formula-decomposition:" + canonical_digest(payload)),
        )
        return CapabilityAdapterOutput.create(
            status="integrity_failed",
            output_payload=payload,
            evidence=(),
            affected_obligation_ids=task.supports_obligation_ids,
            limitation_refs=(f"formula:{decomposition.status}",),
            retryability="replan_required",
            failure=failure,
        )
    _validate_bound_evidence_type(
        scoped_input,
        "accounting_contribution",
    )
    evidence = CapabilityEvidence.create(
        evidence_ref=f"evidence:{task.task_id}:formula",
        binding_record_ref=scoped_input.binding_record_ref,
        execution_state="available",
        evidence_kind="derived",
        data_contract_state=scoped_input.data_contract_state,
        supported_claim_kinds=_supported_claim_kinds(
            plan_revision,
            task,
            scoped_input,
        ),
        evidence_strength="reconciled",
        maximum_claim_strength=scoped_input.maximum_claim_strength,
        observation_facts=(payload,),
        scope=scoped_input.scope_ref,
        window_refs=plan_revision.resolved_window_refs,
        dimension_path=(),
        limitation_refs=scoped_input.limitation_refs,
        result_refs=scoped_input.result_refs,
        completeness_report_refs=scoped_input.completeness_report_refs,
        hierarchy_qualified=False,
    )
    return CapabilityAdapterOutput.create(
        status="succeeded",
        output_payload=payload,
        evidence=(evidence,),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=scoped_input.limitation_refs,
        retryability="never",
    )


def _funnel_decomposition_adapter(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    _attempt: CapabilityAttempt,
    scoped_input: TaskScopedCapabilityInput,
) -> CapabilityAdapterOutput:
    payload = dict(scoped_input.payload)
    expected_fields = {
        "contract_id",
        "source_grain",
        "lifetime_first_payment_supported",
        "target_window_ref",
        "baseline_window_ref",
        "stages",
    }
    raw_stages = payload.get("stages")
    if (
        set(payload) != expected_fields
        or payload.get("contract_id") != "new-user-funnel-decomposition.v1"
        or payload.get("source_grain") != "dashboard_daily"
        or payload.get("lifetime_first_payment_supported") is not False
        or isinstance(raw_stages, (str, bytes))
        or not isinstance(raw_stages, Sequence)
        or not raw_stages
        or any(not isinstance(item, Mapping) for item in raw_stages)
    ):
        raise CapabilityTaskAdapterContractError(
            "task_runtime_funnel_payload_invalid"
        )
    stages = tuple(dict(item) for item in raw_stages)
    if any(
        item.get("target_reconciled") is not True
        or item.get("baseline_reconciled") is not True
        for item in stages
    ):
        return CapabilityAdapterOutput.create(
            status="unavailable",
            output_payload={
                "contract_id": payload["contract_id"],
                "rate_reconciliation": "mismatch",
            },
            evidence=(),
            affected_obligation_ids=task.supports_obligation_ids,
            limitation_refs=("funnel-rate-reconciliation-mismatch",),
            retryability="replan_required",
        )
    numeric_facts: dict[str, Any] = {}
    typed_stages = []
    for item in stages:
        stage_id = _required_string(
            item.get("stage_id"), "task_runtime_funnel_stage_id_invalid"
        )
        stage_payload = dict(item)
        target_rate = stage_payload.get("target_rate")
        baseline_rate = stage_payload.get("baseline_rate")
        for field in (
            "target_numerator",
            "baseline_numerator",
            "target_denominator",
            "baseline_denominator",
            "target_rate",
            "baseline_rate",
        ):
            value = stage_payload.get(field)
            if value is not None:
                numeric_facts[f"{stage_id}_{field}"] = value
        stage_payload["rate_delta"] = (
            None
            if target_rate is None or baseline_rate is None
            else float(target_rate) - float(baseline_rate)
        )
        if stage_payload["rate_delta"] is not None:
            numeric_facts[f"{stage_id}_rate_delta"] = stage_payload["rate_delta"]
        typed_stages.append(stage_payload)
    typed_payload = {
        **payload,
        "stages": tuple(typed_stages),
        "interpretation_contract": {
            "contract_id": "funnel-comparison-interpretation.v1",
            "analysis_role": "window_funnel_comparison",
            "rate_semantics": "ratio_recomputed_from_window_sums",
            "cross_stage_additivity": "forbidden",
            "lifetime_first_payment_inference": "forbidden",
            "causal_interpretation": "forbidden",
        },
    }
    envelope = make_evidence_envelope(
        task.capability_id,
        evidence_type="observed_comparison",
        strength="directional",
        wording_limit="comparative",
        numeric_facts=numeric_facts,
        typed_payload=typed_payload,
        limitations=("dashboard_daily_funnel_not_lifetime_cohort",),
        result_refs=scoped_input.result_refs,
        evidence_ref=f"funnel-decomposition:{task.task_id}",
    )
    return _envelope_output(
        plan_revision,
        task,
        scoped_input,
        envelope,
        hierarchy_paths=False,
    )


def _envelope_output(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    scoped_input: TaskScopedCapabilityInput,
    envelope: EvidenceEnvelope | PatternScanResult,
    *,
    hierarchy_paths: bool,
) -> CapabilityAdapterOutput:
    if not isinstance(envelope, (EvidenceEnvelope, PatternScanResult)):
        raise CapabilityTaskAdapterContractError("primitive_evidence_envelope_invalid")
    typed_payload = _plain(envelope.typed_payload)
    numeric_facts = _plain(getattr(envelope, "numeric_facts", {}))
    capability_limitations = tuple(
        dict.fromkeys((*scoped_input.limitation_refs, *envelope.limitations))
    )
    result_refs = tuple(
        dict.fromkeys((*scoped_input.result_refs, *envelope.result_refs))
    )
    output_payload = {
        "evidence_type": envelope.evidence_type,
        "strength": envelope.strength,
        "wording_limit": envelope.wording_limit,
        "numeric_facts": numeric_facts,
        "typed_payload": typed_payload,
    }
    if envelope.evidence_type in _UNAVAILABLE_PRIMITIVE_EVIDENCE_TYPES:
        continuation = _ready_continuation_contract(typed_payload)
        if continuation is not None:
            continuation_limitations = tuple(
                dict.fromkeys(
                    (*capability_limitations, "public_claim_support_unavailable")
                )
            )
            return CapabilityAdapterOutput.create(
                status="succeeded",
                output_payload=output_payload,
                evidence=(
                    CapabilityEvidence.create(
                        evidence_ref=f"evidence:{task.task_id}:continuation",
                        binding_record_ref=scoped_input.binding_record_ref,
                        execution_state="available",
                        evidence_kind="boundary",
                        data_contract_state=scoped_input.data_contract_state,
                        supported_claim_kinds=(),
                        evidence_strength="low",
                        maximum_claim_strength=scoped_input.maximum_claim_strength,
                        observation_facts=(
                            {"continuation_contract": continuation},
                        ),
                        scope=scoped_input.scope_ref,
                        window_refs=plan_revision.resolved_window_refs,
                        dimension_path=(),
                        limitation_refs=continuation_limitations,
                        result_refs=result_refs,
                        completeness_report_refs=scoped_input.completeness_report_refs,
                        hierarchy_qualified=False,
                    ),
                ),
                affected_obligation_ids=task.supports_obligation_ids,
                limitation_refs=continuation_limitations,
                retryability="never",
            )
        return CapabilityAdapterOutput.create(
            status="unavailable",
            output_payload=output_payload,
            evidence=(),
            affected_obligation_ids=task.supports_obligation_ids,
            limitation_refs=capability_limitations,
            retryability="replan_required",
        )
    _validate_bound_evidence_type(scoped_input, envelope.evidence_type)
    evidence_kind = _publishable_evidence_kind(envelope)
    numeric_observations = tuple(
        {"name": str(key), "value": value}
        for key, value in sorted(numeric_facts.items())
    )
    interpretation_contract = typed_payload.get("interpretation_contract")
    if interpretation_contract is not None and (
        not isinstance(interpretation_contract, Mapping) or not interpretation_contract
    ):
        raise CapabilityTaskAdapterContractError(
            "primitive_interpretation_contract_invalid"
        )
    declared_observations = typed_payload.get("claim_material_observations")
    if declared_observations is not None:
        if (
            isinstance(declared_observations, (str, bytes))
            or not isinstance(declared_observations, Sequence)
            or not declared_observations
            or len(declared_observations) > 32
            or any(
                not isinstance(item, Mapping) or not item
                for item in declared_observations
            )
        ):
            raise CapabilityTaskAdapterContractError(
                "primitive_claim_material_observations_invalid"
            )
        contract_observations = tuple(
            _plain(item) for item in declared_observations
        )
    else:
        contract_observations = (
            (typed_payload,)
            if isinstance(typed_payload, Mapping)
            and "evidence_contract" in typed_payload
            else (
                ({"interpretation_contract": interpretation_contract},)
                if interpretation_contract is not None
                else ()
            )
        )
    main_observations = (*contract_observations, *numeric_observations)
    if not main_observations:
        main_observations = (typed_payload,)
    evidence: list[CapabilityEvidence] = [
        CapabilityEvidence.create(
            evidence_ref=f"evidence:{task.task_id}:main",
            binding_record_ref=scoped_input.binding_record_ref,
            execution_state="available",
            evidence_kind=evidence_kind,
            data_contract_state=scoped_input.data_contract_state,
            supported_claim_kinds=_supported_claim_kinds(
                plan_revision,
                task,
                scoped_input,
            ),
            evidence_strength=envelope.strength,
            maximum_claim_strength=scoped_input.maximum_claim_strength,
            observation_facts=main_observations,
            scope=scoped_input.scope_ref,
            window_refs=plan_revision.resolved_window_refs,
            dimension_path=(),
            limitation_refs=capability_limitations,
            result_refs=result_refs,
            completeness_report_refs=scoped_input.completeness_report_refs,
            hierarchy_qualified=False,
        )
    ]
    if hierarchy_paths:
        for finding in typed_payload.get("dimension_findings", ()):
            if (
                not isinstance(finding, Mapping)
                or finding.get("evidence_state") != "verified"
            ):
                continue
            dimension_path = tuple(
                str(item)
                for item in (
                    finding.get("dimension_path")
                    or (finding.get("dimension_id") or finding.get("dimension"),)
                )
                if str(item)
            )
            if not dimension_path:
                raise CapabilityTaskAdapterContractError(
                    "qualified_hierarchy_dimension_path_missing"
                )
            finding_limitations = _string_tuple(
                finding.get("limitation_refs", ()),
                "qualified_hierarchy_limitation_refs_invalid",
            )
            qualified_limitations = tuple(
                dict.fromkeys((*capability_limitations, *finding_limitations))
            )
            finding_observation = dict(finding)
            if interpretation_contract is not None:
                existing_contract = finding_observation.get("interpretation_contract")
                if (
                    existing_contract is not None
                    and existing_contract != interpretation_contract
                ):
                    raise CapabilityTaskAdapterContractError(
                        "hierarchy_interpretation_contract_conflict"
                    )
                finding_observation["interpretation_contract"] = interpretation_contract
            evidence.append(
                CapabilityEvidence.create(
                    evidence_ref=(
                        f"evidence:{task.task_id}:hierarchy:"
                        + canonical_digest(finding)[:16]
                    ),
                    binding_record_ref=scoped_input.binding_record_ref,
                    execution_state="available",
                    evidence_kind=evidence_kind,
                    data_contract_state=scoped_input.data_contract_state,
                    supported_claim_kinds=_supported_claim_kinds(
                        plan_revision,
                        task,
                        scoped_input,
                    ),
                    evidence_strength=envelope.strength,
                    maximum_claim_strength=scoped_input.maximum_claim_strength,
                    observation_facts=(finding_observation,),
                    scope=scoped_input.scope_ref,
                    window_refs=plan_revision.resolved_window_refs,
                    dimension_path=dimension_path,
                    limitation_refs=qualified_limitations,
                    result_refs=result_refs,
                    completeness_report_refs=(scoped_input.completeness_report_refs),
                    hierarchy_qualified=True,
                )
            )
    return CapabilityAdapterOutput.create(
        status="succeeded",
        output_payload=output_payload,
        evidence=tuple(evidence),
        affected_obligation_ids=task.supports_obligation_ids,
        limitation_refs=capability_limitations,
        retryability="never",
    )


def _ready_continuation_contract(
    typed_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    continuation = typed_payload.get("continuation_contract")
    if continuation is None:
        return None
    if not isinstance(continuation, Mapping) or set(continuation) != {
        "state",
        "purpose",
        "material_ref",
        "material_count",
        "claim_support",
    }:
        raise CapabilityTaskAdapterContractError(
            "primitive_continuation_contract_invalid"
        )
    if continuation.get("state") != "ready":
        return None
    purpose = continuation.get("purpose")
    material_ref = continuation.get("material_ref")
    material_count = continuation.get("material_count")
    if (
        not isinstance(purpose, str)
        or not purpose
        or purpose != purpose.strip()
        or not isinstance(material_ref, str)
        or not material_ref
        or material_ref != material_ref.strip()
        or type(material_count) is not int
        or material_count <= 0
        or continuation.get("claim_support") != "none"
    ):
        raise CapabilityTaskAdapterContractError(
            "primitive_continuation_contract_invalid"
        )
    material = typed_payload.get(material_ref)
    if (
        isinstance(material, (str, bytes))
        or not isinstance(material, Sequence)
        or len(material) != material_count
        or any(not isinstance(item, Mapping) or not item for item in material)
    ):
        raise CapabilityTaskAdapterContractError(
            "primitive_continuation_material_invalid"
        )
    return _plain(continuation)


def _supported_claim_kinds(
    plan_revision: PlanRevision,
    task: CapabilityTask,
    scoped_input: TaskScopedCapabilityInput,
) -> tuple[str, ...]:
    bound = scoped_input.services.get("bound_capability_input")
    contract_claim_kinds = getattr(bound, "supported_claim_types", None)
    if contract_claim_kinds is not None:
        return _string_tuple(
            contract_claim_kinds,
            "adapter_capability_claim_kinds_invalid",
            allow_empty=False,
        )
    obligations = {item.obligation_id: item for item in plan_revision.claim_obligations}
    try:
        return tuple(
            dict.fromkeys(
                obligations[obligation_id].claim_kind
                for obligation_id in task.supports_obligation_ids
            )
        )
    except KeyError as exc:
        raise CapabilityTaskAdapterContractError(
            "adapter_task_obligation_unknown"
        ) from exc


def _publishable_evidence_kind(
    envelope: EvidenceEnvelope | PatternScanResult,
) -> str:
    evidence_type = envelope.evidence_type
    if (
        not isinstance(evidence_type, str)
        or not evidence_type
        or evidence_type != evidence_type.strip()
    ):
        raise CapabilityTaskAdapterContractError("primitive_evidence_type_invalid")
    try:
        evidence_kind = publication_evidence_kind(evidence_type)
    except EvidenceTaxonomyContractError as exc:
        raise CapabilityTaskAdapterContractError(
            f"primitive_evidence_type_unsupported:{evidence_type}"
        ) from exc
    if evidence_type == "trust_boundary" and (
        envelope.strength != "trust_boundary"
        or envelope.wording_limit not in _TRUST_BOUNDARY_WORDING_LIMITS
    ):
        raise CapabilityTaskAdapterContractError(
            "primitive_trust_boundary_claim_ceiling_invalid"
        )
    return evidence_kind


def _validate_bound_evidence_type(
    scoped_input: TaskScopedCapabilityInput,
    evidence_type: str,
) -> None:
    bound = scoped_input.services.get("bound_capability_input")
    if bound is None:
        return
    declared = getattr(bound, "supported_evidence_types", None)
    if (
        isinstance(declared, (str, bytes))
        or not isinstance(declared, Sequence)
        or evidence_type not in declared
    ):
        raise CapabilityTaskAdapterContractError(
            f"primitive_evidence_type_not_declared:{evidence_type}"
        )


__all__ = (
    "CapabilityAdapterRegistration",
    "CapabilityTaskAdapterContractError",
    "CapabilityTaskAdapterRegistry",
    "ExpectedCapabilityGap",
    "TaskRuntimeInputResolver",
    "TaskRuntimeInputs",
    "TaskScopedCapabilityInput",
    "builtin_capability_adapter_registry",
)
