from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, Sequence

from bi_agent.runtime.analysis_contract_compiler import (
    AnalysisCompileOutcome,
    compile_analysis_contract,
    expand_dynamic_dimension_queries,
)
from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CompletenessFailureClass,
    ContractGap,
    QueryContract,
    QueryResultEnvelope,
    ResolvedWindow,
    completeness_report_failure_classes,
)
from bi_agent.runtime.capability_execution import (
    BoundCapabilityInput,
    bind_capability_inputs,
)
from bi_agent.runtime.capability_task_adapter import (
    ExpectedCapabilityGap,
    TaskRuntimeInputs,
    TaskScopedCapabilityInput,
    builtin_capability_adapter_registry,
)
from bi_agent.runtime.capability_authority import CapabilityFailure
from bi_agent.runtime.clickhouse_query_compiler import (
    validate_clickhouse_query_contract,
)
from bi_agent.runtime.dataset_catalog import DatasetCatalog
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    canonical_digest,
    canonical_value,
)
from bi_agent.runtime.event_window_derivation import (
    EventWindowDerivationError,
    derive_event_window_set,
    validate_event_window_derivation_policy,
)
from bi_agent.runtime.dimension_combination_derivation import (
    DimensionCombinationDerivationError,
    derive_dimension_combinations,
    validate_dimension_combination_policy,
)
from bi_agent.runtime.durable_call_journal import (
    DurableCallJournal,
    DurableCallJournalError,
    DurableCallSpec,
)
from bi_agent.runtime.formula_graph import (
    formula_metric_ids,
    load_formula_graph,
)
from bi_agent.runtime.plan_authority import (
    AnalysisAxis,
    AuthorityContext,
    CapabilityTask,
    PlanRevision,
)
from bi_agent.runtime.query_completeness import (
    validate_query_result,
    validate_query_set,
)
from bi_agent.runtime.query_ir import (
    QueryBundle,
    compile_capability_query_route,
)
from bi_agent.runtime.runtime_persistence import CapabilitySettlementAuthority
from bi_agent.runtime.single_authority import (
    DecisionLedger,
    DecisionRecord,
    IntentRevision,
)
from bi_agent.runtime.temporal_comparison import (
    EffectiveTemporalComparison,
    TemporalComparisonContractError,
    resolve_rolling_window_strategy,
    validate_calendar_partition_role_frame,
)
from bi_agent.runtime.window_metric_evidence import (
    WindowMetricEvidenceError,
    validate_event_window_metric_authority,
)
from bi_agent.capabilities.candidate_dimension_screen import (
    candidate_dimension_screen,
)


class AuthoritativeTaskInputContractError(ValueError):
    pass


_CONTRACT_GAP_TYPES = frozenset(
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


@dataclass(frozen=True)
class _TaskPayloadContractGap(Exception):
    gap_type: str
    limitation_ref: str
    business_boundary: str


@dataclass(frozen=True)
class _TaskQueryDisposition:
    expected_gap: ExpectedCapabilityGap | None
    failure_status: str | None
    failure: CapabilityFailure | None
    result_refs: tuple[str, ...]
    report_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        has_gap = self.expected_gap is not None
        has_failure = self.failure_status is not None or self.failure is not None
        if has_gap == has_failure or (self.failure_status is None) != (
            self.failure is None
        ):
            raise ValueError("task_query_disposition_invalid")


@dataclass(frozen=True)
class MaterializedAuthoritativeTaskInputs:
    task_inputs: TaskRuntimeInputs
    settlement_authority: CapabilitySettlementAuthority
    accepted_query_attempt_refs: tuple[str, ...]
    query_bundle: QueryBundle | None = None
    performance_observations: tuple[Mapping[str, Any], ...] = ()

    def resolve_task_input(
        self,
        plan_revision_id: str,
        task_id: str,
    ) -> TaskScopedCapabilityInput | None:
        return self.task_inputs.resolve_task_input(plan_revision_id, task_id)


@dataclass(frozen=True)
class AuthoritativeTaskInputMaterializer:
    analysis_runtime: Any
    attempt_journal: DurableCallJournal
    query_bundle: QueryBundle | None = None

    def materialize(
        self,
        *,
        plan_revision: PlanRevision,
        intent_revision: IntentRevision,
        decision_ledger: DecisionLedger,
        authority_context: AuthorityContext,
    ) -> MaterializedAuthoritativeTaskInputs:
        performance_observations: list[dict[str, Any]] = []
        plan, intent, context = _validate_authority_bundle(
            plan_revision=plan_revision,
            intent_revision=intent_revision,
            decision_ledger=decision_ledger,
            authority_context=authority_context,
        )
        if not isinstance(self.attempt_journal, DurableCallJournal):
            raise AuthoritativeTaskInputContractError(
                "authoritative_query_attempt_journal_invalid"
            )
        # Adapter availability is part of Phase 3 admission. A plan may not
        # enter physical execution with a capability that has no typed adapter.
        builtin_capability_adapter_registry().validate_plan(plan)
        runtime = self.analysis_runtime
        registry = _runtime_service(runtime, "registry")
        _validate_pinned_runtime_registry(context, registry)
        executor = _runtime_service(runtime, "executor")
        release_resolver = _runtime_service(runtime, "release_resolver")
        evidence_resolver = _runtime_service(runtime, "evidence_resolver")
        rows_loader = _runtime_service(runtime, "rows_loader")
        evidence_writer = _runtime_service(runtime, "evidence_writer")
        catalog_for_authority_context = getattr(
            runtime,
            "catalog_for_authority_context",
            None,
        )
        if not callable(catalog_for_authority_context):
            raise AuthoritativeTaskInputContractError(
                "authoritative_runtime_catalog_service_missing"
            )
        catalog = catalog_for_authority_context(context)
        if not isinstance(catalog, DatasetCatalog):
            raise AuthoritativeTaskInputContractError(
                "authoritative_runtime_catalog_invalid"
            )

        capability_ids = tuple(
            dict.fromkeys(task.capability_id for task in plan.capability_tasks)
        )
        compile_material = _compile_material(
            plan=plan,
            intent=intent,
            registry=registry,
            query_bundle=self.query_bundle,
        )
        started = perf_counter()
        outcome = compile_analysis_contract(
            run_id=plan.plan_revision_id,
            proposal=compile_material,
            accepted_capabilities=capability_ids,
            catalog=catalog,
            registry=registry,
            temporal_authority=plan.temporal_authority,
            as_of=_parse_actual_as_of(context.actual_as_of),
            release_resolver=release_resolver,
        )
        performance_observations.append(
            _performance_observation(
                stage="contract_compile",
                operation="compile_analysis_contract",
                started=started,
                input_value=compile_material,
            )
        )
        _validate_compile_outcome(
            outcome=outcome,
            plan=plan,
            context=context,
            catalog=catalog,
            capability_ids=capability_ids,
            registry=registry,
        )

        snapshots = {item.snapshot_ref: item for item in catalog.snapshots()}
        (
            query_results,
            completeness_reports,
            accepted_query_attempt_refs,
        ) = _execute_query_contract_batch(
            contracts=outcome.query_contracts,
            outcome=outcome,
            plan=plan,
            snapshots=snapshots,
            executor=executor,
            release_resolver=release_resolver,
            attempt_journal=self.attempt_journal,
            evidence_writer=evidence_writer,
            registry=registry,
            performance_observations=performance_observations,
        )
        query_results = list(query_results)
        completeness_reports = list(completeness_reports)
        accepted_query_attempt_refs = list(accepted_query_attempt_refs)
        if query_results:
            started = perf_counter()
            completeness_reports = list(
                validate_query_set(
                    tuple(outcome.query_contracts),
                    tuple(query_results),
                    tuple(completeness_reports),
                    evidence_writer=evidence_writer,
                )
            )
            performance_observations.append(
                _performance_observation(
                    stage="query_set_validation",
                    operation="validate_query_set",
                    started=started,
                    input_value={
                        "query_contract_refs": tuple(
                            item.query_contract_id for item in outcome.query_contracts
                        ),
                        "result_refs": tuple(item.result_ref for item in query_results),
                    },
                )
            )

        dynamic_derivations: dict[str, Mapping[str, Any]] = {}
        for capability_id in capability_ids:
            capability_contract = registry.capability_inputs(capability_id)
            raw_policy = capability_contract.get(
                "dynamic_dimension_combination_policy"
            )
            if raw_policy is None:
                continue
            try:
                policy = validate_dimension_combination_policy(raw_policy)
                derivation = _derive_dynamic_dimension_queries(
                    plan=plan,
                    intent=intent,
                    outcome=outcome,
                    capability_id=capability_id,
                    source_dependency=policy["source_dependency"],
                    policy=policy,
                    query_results=query_results,
                    completeness_reports=completeness_reports,
                    evidence_resolver=evidence_resolver,
                    rows_loader=rows_loader,
                    evidence_writer=evidence_writer,
                    release_resolver=release_resolver,
                    registry=registry,
                )
            except DimensionCombinationDerivationError as exc:
                raise AuthoritativeTaskInputContractError(
                    f"authoritative_dynamic_dimension_derivation_failed:{exc}"
                ) from exc
            dynamic_derivations[capability_id] = derivation
            selected_combinations = tuple(
                tuple(item["dimension_ids"])
                for item in derivation["selected_combinations"]
            )
            if not selected_combinations:
                continue
            previous_query_refs = {
                item.query_contract_id for item in outcome.query_contracts
            }
            outcome = expand_dynamic_dimension_queries(
                outcome,
                run_id=plan.plan_revision_id,
                capability_id=capability_id,
                selected_combinations=selected_combinations,
                proposal=compile_material,
                snapshots=tuple(snapshots.values()),
                registry=registry,
                temporal_authority=plan.temporal_authority,
            )
            additions = tuple(
                item
                for item in outcome.query_contracts
                if item.query_contract_id not in previous_query_refs
            )
            (
                added_results,
                added_reports,
                added_attempt_refs,
            ) = _execute_query_contract_batch(
                contracts=additions,
                outcome=outcome,
                plan=plan,
                snapshots=snapshots,
                executor=executor,
                release_resolver=release_resolver,
                attempt_journal=self.attempt_journal,
                evidence_writer=evidence_writer,
                registry=registry,
                performance_observations=performance_observations,
            )
            query_results.extend(added_results)
            completeness_reports.extend(added_reports)
            accepted_query_attempt_refs.extend(added_attempt_refs)
            started = perf_counter()
            completeness_reports = list(
                validate_query_set(
                    tuple(outcome.query_contracts),
                    tuple(query_results),
                    tuple(completeness_reports),
                    evidence_writer=evidence_writer,
                )
            )
            performance_observations.append(
                _performance_observation(
                    stage="dynamic_query_set_validation",
                    operation=capability_id,
                    started=started,
                    input_value={
                        "selected_combinations": selected_combinations,
                        "query_contract_refs": tuple(
                            item.query_contract_id for item in additions
                        ),
                    },
                )
            )
        _validate_compile_outcome(
            outcome=outcome,
            plan=plan,
            context=context,
            catalog=catalog,
            capability_ids=capability_ids,
            registry=registry,
        )

        result_by_query = {item.query_contract_ref: item for item in query_results}
        report_by_query = {
            item.query_contract_ref: item for item in completeness_reports
        }
        execution_plan_by_capability = {
            item.capability_id: item for item in outcome.capability_plans
        }
        query_by_ref = {
            item.query_contract_id: item for item in outcome.query_contracts
        }
        gaps_by_capability = _contract_gaps_by_capability(
            outcome.analysis_contract.contract_gaps,
            capability_ids,
        )
        bound_by_capability: dict[str, BoundCapabilityInput] = {}
        for capability_id in capability_ids:
            execution_plan = execution_plan_by_capability[capability_id]
            started = perf_counter()
            bound_by_capability[capability_id] = bind_capability_inputs(
                execution_plan,
                results=result_by_query,
                reports=report_by_query,
                evidence_resolver=evidence_resolver,
                rows_loader=rows_loader,
                evidence_writer=evidence_writer,
                runtime_registry=registry,
                release_resolver=release_resolver,
            )
            performance_observations.append(
                _performance_observation(
                    stage="capability_input_binding",
                    operation=capability_id,
                    started=started,
                    input_value={
                        "query_contract_refs": tuple(
                            ref
                            for slot in (
                                *execution_plan.required_input_slots,
                                *execution_plan.optional_input_slots,
                            )
                            for ref in slot.query_contract_refs
                        ),
                        "result_refs": tuple(result_by_query),
                        "report_refs": tuple(report_by_query),
                    },
                )
            )

        scoped_inputs = []
        for task in plan.capability_tasks:
            execution_plan = execution_plan_by_capability[task.capability_id]
            bound = bound_by_capability[task.capability_id]
            capability_gaps = gaps_by_capability.get(task.capability_id, ())
            if bound.status == "blocked":
                disposition = _task_query_disposition(
                    task=task,
                    execution_plan=execution_plan,
                    results=result_by_query,
                    reports=report_by_query,
                )
                if disposition is not None and disposition.failure is not None:
                    status = disposition.failure_status
                    failure = disposition.failure
                    if status is None:
                        raise EvidenceIntegrityError(
                            "authoritative_task_query_failure_status_missing"
                        )
                    scoped_inputs.append(
                        TaskScopedCapabilityInput.create(
                            plan_revision_id=plan.plan_revision_id,
                            task_id=task.task_id,
                            authority_context_ref=context.authority_context_ref,
                            binding_record_ref=None,
                            data_contract_state=(
                                "invalid"
                                if status == "integrity_failed"
                                else "unavailable"
                            ),
                            maximum_claim_strength=(
                                execution_plan.maximum_claim_strength
                            ),
                            scope_ref=_scope_ref(intent),
                            payload={},
                            result_refs=disposition.result_refs,
                            completeness_report_refs=disposition.report_refs,
                            limitation_refs=("failure:" + failure.content_digest,),
                            expected_gap=None,
                            terminal_failure_status=status,
                            terminal_failure=failure,
                            services={},
                        )
                    )
                    continue
                if disposition is not None and disposition.expected_gap is not None:
                    expected_gap = disposition.expected_gap
                    scoped_inputs.append(
                        TaskScopedCapabilityInput.create(
                            plan_revision_id=plan.plan_revision_id,
                            task_id=task.task_id,
                            authority_context_ref=context.authority_context_ref,
                            binding_record_ref=None,
                            data_contract_state=expected_gap.data_contract_state,
                            maximum_claim_strength=(
                                execution_plan.maximum_claim_strength
                            ),
                            scope_ref=_scope_ref(intent),
                            payload={},
                            result_refs=disposition.result_refs,
                            completeness_report_refs=disposition.report_refs,
                            limitation_refs=(expected_gap.limitation_ref,),
                            expected_gap=expected_gap,
                            terminal_failure_status=None,
                            terminal_failure=None,
                            services={},
                        )
                    )
                    continue
                dynamic_derivation = dynamic_derivations.get(task.capability_id)
                if (
                    dynamic_derivation is not None
                    and not dynamic_derivation.get("selected_combinations")
                ):
                    expected_gap = ExpectedCapabilityGap.create(
                        gap_type="unsupported_scope",
                        limitation_ref=(
                            "dynamic_dimension_combination:"
                            "no_admissible_combination"
                        ),
                        data_contract_state="contract_backed",
                        business_boundary=(
                            "候选维度不足，或所有组合超出层级与查询成本边界，"
                            "本次不执行联合归因。"
                        ),
                        retryability="replan_required",
                    )
                    scoped_inputs.append(
                        TaskScopedCapabilityInput.create(
                            plan_revision_id=plan.plan_revision_id,
                            task_id=task.task_id,
                            authority_context_ref=context.authority_context_ref,
                            binding_record_ref=None,
                            data_contract_state=expected_gap.data_contract_state,
                            maximum_claim_strength=(
                                execution_plan.maximum_claim_strength
                            ),
                            scope_ref=_scope_ref(intent),
                            payload={},
                            result_refs=tuple(
                                dynamic_derivation.get("source_result_refs") or ()
                            ),
                            completeness_report_refs=(),
                            limitation_refs=(expected_gap.limitation_ref,),
                            expected_gap=expected_gap,
                            terminal_failure_status=None,
                            terminal_failure=None,
                            services={},
                        )
                    )
                    continue
                expected_gap = _expected_gap(
                    task=task,
                    gaps=capability_gaps,
                )
                if expected_gap is None:
                    raise EvidenceIntegrityError(
                        "authoritative_capability_binding_blocked:"
                        f"{task.capability_id}:" + ",".join(bound.reasons)
                    )
                scoped_inputs.append(
                    TaskScopedCapabilityInput.create(
                        plan_revision_id=plan.plan_revision_id,
                        task_id=task.task_id,
                        authority_context_ref=context.authority_context_ref,
                        binding_record_ref=None,
                        data_contract_state=expected_gap.data_contract_state,
                        maximum_claim_strength=(execution_plan.maximum_claim_strength),
                        scope_ref=_scope_ref(intent),
                        payload={},
                        result_refs=(),
                        completeness_report_refs=(),
                        limitation_refs=_gap_limitation_refs(capability_gaps),
                        expected_gap=expected_gap,
                        terminal_failure_status=None,
                        terminal_failure=None,
                        services={},
                    )
                )
                continue
            if bound.status not in {"ready", "degraded"}:
                raise EvidenceIntegrityError(
                    "authoritative_capability_binding_status_invalid:"
                    f"{task.capability_id}:{bound.status}"
                )
            if not bound.binding_manifest_ref:
                raise EvidenceIntegrityError(
                    "authoritative_capability_binding_record_missing:"
                    f"{task.capability_id}"
                )
            try:
                payload = _task_payload(
                    plan=plan,
                    task=task,
                    intent=intent,
                    bound=bound,
                    bound_by_capability=bound_by_capability,
                    execution_plan=execution_plan,
                    query_by_ref=query_by_ref,
                    result_by_query=result_by_query,
                    report_by_query=report_by_query,
                    registry=registry,
                )
            except _TaskPayloadContractGap as gap:
                expected_gap = ExpectedCapabilityGap.create(
                    gap_type=gap.gap_type,
                    limitation_ref=gap.limitation_ref,
                    data_contract_state=gap.gap_type,
                    business_boundary=gap.business_boundary,
                    retryability="replan_required",
                )
                scoped_inputs.append(
                    TaskScopedCapabilityInput.create(
                        plan_revision_id=plan.plan_revision_id,
                        task_id=task.task_id,
                        authority_context_ref=context.authority_context_ref,
                        binding_record_ref=None,
                        data_contract_state=expected_gap.data_contract_state,
                        maximum_claim_strength=(execution_plan.maximum_claim_strength),
                        scope_ref=_scope_ref(intent),
                        payload={},
                        result_refs=_dedupe(
                            (*bound.result_refs, *bound.validation_result_refs)
                        ),
                        completeness_report_refs=_dedupe(
                            (
                                *bound.completeness_report_refs,
                                *bound.validation_completeness_report_refs,
                            )
                        ),
                        limitation_refs=(gap.limitation_ref,),
                        expected_gap=expected_gap,
                        terminal_failure_status=None,
                        terminal_failure=None,
                        services={},
                    )
                )
                continue
            scoped_inputs.append(
                TaskScopedCapabilityInput.create(
                    plan_revision_id=plan.plan_revision_id,
                    task_id=task.task_id,
                    authority_context_ref=context.authority_context_ref,
                    binding_record_ref=bound.binding_manifest_ref,
                    data_contract_state=(
                        "complete" if bound.status == "ready" else "partial"
                    ),
                    maximum_claim_strength=bound.maximum_claim_strength,
                    scope_ref=_scope_ref(intent),
                    payload=payload,
                    result_refs=_dedupe(
                        (
                            *bound.result_refs,
                            *bound.validation_result_refs,
                            *_dependency_bound_refs(
                                plan=plan,
                                task=task,
                                bound_by_capability=bound_by_capability,
                                field="result_refs",
                            ),
                            *_dependency_bound_refs(
                                plan=plan,
                                task=task,
                                bound_by_capability=bound_by_capability,
                                field="validation_result_refs",
                            ),
                        )
                    ),
                    completeness_report_refs=_dedupe(
                        (
                            *bound.completeness_report_refs,
                            *bound.validation_completeness_report_refs,
                            *_dependency_bound_refs(
                                plan=plan,
                                task=task,
                                bound_by_capability=bound_by_capability,
                                field="completeness_report_refs",
                            ),
                            *_dependency_bound_refs(
                                plan=plan,
                                task=task,
                                bound_by_capability=bound_by_capability,
                                field="validation_completeness_report_refs",
                            ),
                        )
                    ),
                    limitation_refs=_dedupe(
                        (
                            *bound.reasons,
                            *_applicable_bound_gap_limitation_refs(
                                capability_gaps,
                                bound=bound,
                                query_by_ref=query_by_ref,
                            ),
                        )
                    ),
                    expected_gap=None,
                    terminal_failure_status=None,
                    terminal_failure=None,
                    services={
                        "bound_capability_input": bound,
                        "evidence_resolver": evidence_resolver,
                        "rows_loader": rows_loader,
                        "runtime_registry": registry,
                        "release_resolver": release_resolver,
                    },
                )
            )
        task_inputs = TaskRuntimeInputs.create(tuple(scoped_inputs))
        binding_refs = tuple(
            dict.fromkeys(
                item.binding_record_ref
                for item in scoped_inputs
                if item.binding_record_ref is not None
            )
        )
        settlement_authority = CapabilitySettlementAuthority.from_resolver(
            run_id=plan.run_attempt_id,
            analysis_contract=outcome.analysis_contract,
            query_contracts=outcome.query_contracts,
            binding_refs=binding_refs,
            evidence_resolver=evidence_resolver,
        )
        return MaterializedAuthoritativeTaskInputs(
            task_inputs=task_inputs,
            settlement_authority=settlement_authority,
            accepted_query_attempt_refs=tuple(sorted(set(accepted_query_attempt_refs))),
            query_bundle=self.query_bundle,
            performance_observations=tuple(performance_observations),
        )


def _execute_query_contract_batch(
    *,
    contracts: Sequence[QueryContract],
    outcome: AnalysisCompileOutcome,
    plan: PlanRevision,
    snapshots: Mapping[str, Any],
    executor: Any,
    release_resolver: Any,
    attempt_journal: DurableCallJournal,
    evidence_writer: Any,
    registry: Any,
    performance_observations: list[dict[str, Any]],
) -> tuple[tuple[Any, ...], tuple[Any, ...], tuple[str, ...]]:
    query_results = []
    completeness_reports = []
    accepted_attempt_refs = []
    for contract in contracts:
        contract_snapshots = {
            ref: snapshots[ref] for ref in contract.dataset_snapshot_refs
        }
        query_input = {
            "query_contract": contract.to_dict(),
            "dataset_snapshots": tuple(
                item.to_dict() for item in contract_snapshots.values()
            ),
        }
        started = perf_counter()
        validate_clickhouse_query_contract(
            contract,
            contract_snapshots,
            registry=registry,
            release_resolver=release_resolver,
        )
        performance_observations.append(
            _performance_observation(
                stage="query_contract_validation",
                operation=contract.query_contract_id,
                started=started,
                input_value=query_input,
            )
        )
        owner_task = _query_owner_task(
            plan=plan,
            outcome=outcome,
            contract=contract,
        )
        started = perf_counter()
        result, accepted_attempt_ref = _execute_journaled_query(
            plan=plan,
            task=owner_task,
            contract=contract,
            snapshots=contract_snapshots,
            executor=executor,
            release_resolver=release_resolver,
            attempt_journal=attempt_journal,
        )
        performance_observations.append(
            _performance_observation(
                stage="query_execution",
                operation=contract.query_contract_id,
                started=started,
                input_value=query_input,
            )
        )
        started = perf_counter()
        report = validate_query_result(
            contract,
            result,
            tuple(contract_snapshots.values()),
            evidence_writer=evidence_writer,
            release_resolver=release_resolver,
        )
        performance_observations.append(
            _performance_observation(
                stage="query_result_validation",
                operation=contract.query_contract_id,
                started=started,
                input_value=result.to_dict(),
            )
        )
        query_results.append(result)
        completeness_reports.append(report)
        accepted_attempt_refs.append(accepted_attempt_ref)
    return (
        tuple(query_results),
        tuple(completeness_reports),
        tuple(accepted_attempt_refs),
    )


def _derive_dynamic_dimension_queries(
    *,
    plan: PlanRevision,
    intent: IntentRevision,
    outcome: AnalysisCompileOutcome,
    capability_id: str,
    source_dependency: str,
    policy: Mapping[str, Any],
    query_results: Sequence[Any],
    completeness_reports: Sequence[Any],
    evidence_resolver: Any,
    rows_loader: Any,
    evidence_writer: Any,
    release_resolver: Any,
    registry: Any,
) -> Mapping[str, Any]:
    dynamic_tasks = tuple(
        task
        for task in plan.capability_tasks
        if task.capability_id == capability_id
    )
    if len(dynamic_tasks) != 1:
        raise DimensionCombinationDerivationError(
            "dynamic_dimension_task_cardinality_invalid"
        )
    dynamic_task = dynamic_tasks[0]
    task_by_id = {task.task_id: task for task in plan.capability_tasks}
    dependency_tasks = tuple(
        task_by_id[item] for item in dynamic_task.dependency_task_ids
    )
    source_tasks = tuple(
        task for task in dependency_tasks if task.capability_id == source_dependency
    )
    if len(source_tasks) != 1:
        raise DimensionCombinationDerivationError(
            "dynamic_dimension_source_dependency_invalid"
        )
    source_task = source_tasks[0]
    execution_plan_by_capability = {
        item.capability_id: item for item in outcome.capability_plans
    }
    source_plan = execution_plan_by_capability.get(source_dependency)
    if source_plan is None:
        raise DimensionCombinationDerivationError(
            "dynamic_dimension_source_plan_missing"
        )
    result_by_query = {
        item.query_contract_ref: item for item in query_results
    }
    report_by_query = {
        item.query_contract_ref: item for item in completeness_reports
    }
    bound = bind_capability_inputs(
        source_plan,
        results=result_by_query,
        reports=report_by_query,
        evidence_resolver=evidence_resolver,
        rows_loader=rows_loader,
        evidence_writer=evidence_writer,
        runtime_registry=registry,
        release_resolver=release_resolver,
    )
    if bound.status not in {"ready", "degraded"}:
        return {
            "schema_version": "derived-dimension-combinations.v1",
            "source_dependency": source_dependency,
            "candidate_pool": (),
            "selected_combinations": (),
            "excluded_combinations": (
                {
                    "dimension_ids": (),
                    "reason": "source_dependency_unready",
                },
            ),
            "estimated_cells_total": 0,
            "policy": dict(policy),
            "source_result_refs": tuple(bound.result_refs),
        }
    query_by_ref = {
        item.query_contract_id: item for item in outcome.query_contracts
    }
    payload = _task_payload(
        plan=plan,
        task=source_task,
        intent=intent,
        bound=bound,
        execution_plan=source_plan,
        query_by_ref=query_by_ref,
        result_by_query=result_by_query,
        report_by_query=report_by_query,
        registry=registry,
    )
    try:
        evidence = candidate_dimension_screen(
            **dict(payload),
            result_refs=tuple(bound.result_refs),
        )
        derivation = derive_dimension_combinations(
            evidence.typed_payload,
            dimension_metadata=payload["dimension_metadata"],
            policy=policy,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DimensionCombinationDerivationError(
            "dynamic_dimension_source_evidence_invalid"
        ) from exc
    return {
        **derivation,
        "source_result_refs": tuple(bound.result_refs),
    }


def materialize_authoritative_task_inputs(
    *,
    plan_revision: PlanRevision,
    intent_revision: IntentRevision,
    decision_ledger: DecisionLedger,
    authority_context: AuthorityContext,
    analysis_runtime: Any,
    attempt_journal: DurableCallJournal,
    query_bundle: QueryBundle | None = None,
) -> MaterializedAuthoritativeTaskInputs:
    return AuthoritativeTaskInputMaterializer(
        analysis_runtime,
        attempt_journal,
        query_bundle,
    ).materialize(
        plan_revision=plan_revision,
        intent_revision=intent_revision,
        decision_ledger=decision_ledger,
        authority_context=authority_context,
    )


def _performance_observation(
    *,
    stage: str,
    operation: str,
    started: float,
    input_value: Any,
) -> dict[str, Any]:
    encoded = json.dumps(
        canonical_value(input_value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        "stage": stage,
        "operation": operation,
        "duration_ms": round((perf_counter() - started) * 1000, 6),
        "input_bytes": len(encoded),
    }


def _execute_journaled_query(
    *,
    plan: PlanRevision,
    task: CapabilityTask,
    contract: QueryContract,
    snapshots: Mapping[str, Any],
    executor: Any,
    release_resolver: Any,
    attempt_journal: DurableCallJournal,
) -> tuple[QueryResultEnvelope, str]:
    if (
        not isinstance(plan, PlanRevision)
        or not isinstance(task, CapabilityTask)
        or task not in plan.capability_tasks
        or not isinstance(contract, QueryContract)
        or not isinstance(snapshots, Mapping)
        or set(snapshots) != set(contract.dataset_snapshot_refs)
        or not callable(getattr(executor, "execute", None))
        or not callable(getattr(executor, "accept_durable_result", None))
        or not isinstance(attempt_journal, DurableCallJournal)
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_query_call_contract_invalid"
        )
    input_payload = {
        "query_contract": contract.to_dict(),
        "dataset_snapshots": tuple(
            snapshots[ref].to_dict() for ref in sorted(contract.dataset_snapshot_refs)
        ),
    }
    spec = DurableCallSpec.create(
        run_attempt_id=plan.run_attempt_id,
        intent_revision_id=plan.intent_revision_id,
        plan_revision_id=plan.plan_revision_id,
        task_id=task.task_id,
        stage_name="execute_capability_dag",
        call_kind="query",
        operation_name=contract.query_contract_id,
        input_ref="query-contract:sha256:" + contract.contract_signature,
        input_payload=input_payload,
    )
    claim = attempt_journal.claim(spec)
    if claim.replayed:
        result = _query_result_from_journal(claim.output_payload)
        accepted_attempt_ref = claim.attempt.attempt_ref
    else:
        try:
            current_result = executor.execute(
                contract,
                snapshots,
                execution_attempt_ref=claim.attempt.attempt_ref,
                release_resolver=release_resolver,
            )
            if not isinstance(current_result, QueryResultEnvelope):
                raise AuthoritativeTaskInputContractError(
                    "authoritative_query_result_invalid"
                )
        except Exception as exc:
            attempt_journal.fail(
                claim.attempt,
                failure_code=type(exc).__name__,
            )
            raise
        completion = attempt_journal.succeed(
            claim.attempt,
            {"query_result": canonical_value(current_result)},
        )
        if completion.disposition != "accepted" or completion.acceptance is None:
            raise DurableCallJournalError("call_success_orphaned")
        result = _query_result_from_journal(completion.output_payload)
        accepted_attempt_ref = completion.acceptance.accepted_attempt_ref
    if result.execution_attempt_ref != accepted_attempt_ref:
        raise AuthoritativeTaskInputContractError(
            "authoritative_query_attempt_ref_mismatch"
        )
    accepted = executor.accept_durable_result(contract, snapshots, result)
    if accepted != result:
        raise AuthoritativeTaskInputContractError(
            "authoritative_query_durable_result_mismatch"
        )
    return result, accepted_attempt_ref


def _query_result_from_journal(
    payload: Mapping[str, Any] | None,
) -> QueryResultEnvelope:
    if not isinstance(payload, Mapping) or set(payload) != {"query_result"}:
        raise AuthoritativeTaskInputContractError(
            "authoritative_query_journal_output_invalid"
        )
    raw = payload["query_result"]
    if not isinstance(raw, Mapping) or set(raw) != set(
        QueryResultEnvelope.__dataclass_fields__
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_query_journal_output_invalid"
        )
    restored = _restore_canonical_value(raw)
    try:
        return QueryResultEnvelope(
            **{
                **restored,
                "rows": tuple(dict(item) for item in restored["rows"]),
                "observed_schema": dict(restored["observed_schema"]),
                "observed_windows": tuple(restored["observed_windows"]),
                "observed_grain": tuple(restored["observed_grain"]),
                "source_snapshot_refs": tuple(restored["source_snapshot_refs"]),
                "provider_stats": dict(restored["provider_stats"]),
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthoritativeTaskInputContractError(
            "authoritative_query_journal_output_invalid"
        ) from exc


def _restore_canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {"$decimal"}:
            try:
                return Decimal(str(value["$decimal"]))
            except InvalidOperation as exc:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_query_journal_output_invalid"
                ) from exc
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(str(value["$datetime"]))
        if set(value) == {"$date"}:
            return date.fromisoformat(str(value["$date"]))
        return {str(key): _restore_canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_restore_canonical_value(item) for item in value)
    return value


def _query_owner_task(
    *,
    plan: PlanRevision,
    outcome: AnalysisCompileOutcome,
    contract: QueryContract,
) -> CapabilityTask:
    consuming_capabilities = {
        capability_plan.capability_id
        for capability_plan in outcome.capability_plans
        if any(
            contract.query_contract_id
            in {
                *slot.query_contract_refs,
                *slot.validation_query_contract_refs,
            }
            for slot in (
                *capability_plan.required_input_slots,
                *capability_plan.optional_input_slots,
            )
        )
    }
    candidates = tuple(
        task
        for task in plan.capability_tasks
        if task.capability_id in consuming_capabilities
    )
    if not candidates:
        raise AuthoritativeTaskInputContractError(
            "authoritative_query_owner_task_missing"
        )
    return min(candidates, key=lambda item: item.task_id)


def _runtime_service(runtime: Any, name: str) -> Any:
    value = getattr(runtime, name, None)
    if value is None:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_runtime_service_missing:{name}"
        )
    return value


def _validate_pinned_runtime_registry(
    context: AuthorityContext,
    registry: Any,
) -> None:
    expected_version = context.contract_versions.get("runtime_bindings")
    expected_digest = context.contract_versions.get("runtime_bindings_digest")
    if (
        type(expected_version) is not str
        or not expected_version
        or type(expected_digest) is not str
        or len(expected_digest) != 64
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_runtime_registry_authority_missing"
        )
    actual_version = getattr(registry, "contract_version", None)
    try:
        actual_digest = registry.source_payload_digest
    except (AttributeError, ValueError) as exc:
        raise AuthoritativeTaskInputContractError(
            "authoritative_runtime_registry_digest_unavailable"
        ) from exc
    if actual_version != expected_version or actual_digest != expected_digest:
        raise AuthoritativeTaskInputContractError(
            "authoritative_runtime_registry_drift"
        )


def _validate_authority_bundle(
    *,
    plan_revision: PlanRevision,
    intent_revision: IntentRevision,
    decision_ledger: DecisionLedger,
    authority_context: AuthorityContext,
) -> tuple[PlanRevision, IntentRevision, AuthorityContext]:
    if not isinstance(plan_revision, PlanRevision):
        raise AuthoritativeTaskInputContractError("authoritative_plan_invalid")
    if not isinstance(intent_revision, IntentRevision):
        raise AuthoritativeTaskInputContractError("authoritative_intent_invalid")
    if not isinstance(decision_ledger, DecisionLedger):
        raise AuthoritativeTaskInputContractError("authoritative_ledger_invalid")
    if not isinstance(authority_context, AuthorityContext):
        raise AuthoritativeTaskInputContractError("authoritative_context_invalid")
    plan = PlanRevision.from_dict(plan_revision.to_dict())
    intent = IntentRevision.from_dict(intent_revision.to_dict())
    context = AuthorityContext.from_dict(authority_context.to_dict())
    ledger = DecisionLedger()
    for record in decision_ledger.records:
        ledger = ledger.append(DecisionRecord.from_dict(record.to_dict()))
    active = ledger.active_records()
    active_refs = tuple(item.decision_id for item in active)
    if (
        plan.run_attempt_id != intent.run_attempt_id
        or plan.run_attempt_id != context.run_attempt_id
        or plan.intent_revision_id != intent.intent_revision_id
        or plan.authority_context_ref != context.authority_context_ref
        or plan.decision_refs != active_refs
        or dict(plan.contract_versions) != dict(context.contract_versions)
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_execution_authority_mismatch"
        )
    if any(item.intent_revision_id != intent.intent_revision_id for item in active):
        raise AuthoritativeTaskInputContractError(
            "authoritative_execution_decision_stale"
        )
    for slot in intent.ambiguity_slots:
        if slot["materiality"] != "material":
            continue
        decision = ledger.active_for_slot(str(slot["slot_id"]))
        if decision is None or decision.status not in {"inferred", "user_confirmed"}:
            raise AuthoritativeTaskInputContractError(
                f"authoritative_material_decision_unresolved:{slot['slot_id']}"
            )
    return plan, intent, context


def _compile_material(
    *,
    plan: PlanRevision,
    intent: IntentRevision,
    registry: Any,
    query_bundle: QueryBundle | None = None,
) -> Mapping[str, Any]:
    goal_ids = tuple(str(item["goal_id"]) for item in intent.goal_bindings)
    goal_obligation = getattr(registry, "analysis_goal_obligation", None)
    merged_goal_ids = tuple(
        dict.fromkeys(
            merged_goal_id
            for goal_id in goal_ids
            for merged_goal_id in (
                goal_obligation(goal_id).get("merged_goal_refs", ())
                if callable(goal_obligation)
                else ()
            )
        )
    )
    question_families = tuple(
        dict.fromkeys(
            registry.analysis_goal_question_family_ref(goal_id)
            for goal_id in (*goal_ids, *merged_goal_ids)
        )
    )
    if query_bundle is not None and (
        not isinstance(query_bundle, QueryBundle)
        or query_bundle.plan_revision_id != plan.plan_revision_id
        or query_bundle.intent_revision_id != intent.intent_revision_id
        or query_bundle.stage != "compiled"
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_query_bundle_mismatch"
        )
    filters = intent.scope.get("filters")
    if (
        isinstance(filters, (str, bytes))
        or not isinstance(filters, Sequence)
        or any(not isinstance(item, Mapping) for item in filters)
    ):
        raise AuthoritativeTaskInputContractError("authoritative_scope_filters_invalid")
    scope_type = intent.scope.get("scope_type")
    if not isinstance(scope_type, str) or not scope_type:
        raise AuthoritativeTaskInputContractError("authoritative_scope_type_invalid")
    approved_filter_fields = set(registry.all_customer_safe_filter_fields)
    unapproved_filter_fields = sorted(
        {
            str(item.get("field") or "")
            for item in filters
            if str(item.get("field") or "") not in approved_filter_fields
        }
    )
    if unapproved_filter_fields:
        raise AuthoritativeTaskInputContractError(
            "authoritative_scope_filter_field_unapproved:"
            + ",".join(unapproved_filter_fields)
        )
    planned_metric_refs = tuple(
        dict.fromkeys(
            metric_ref
            for axis in plan.analysis_axes
            for metric_ref in axis.metric_refs
            if metric_ref not in set(intent.target_metric_refs)
        )
    )
    planned_dimension_refs = tuple(
        dict.fromkeys(
            dimension_ref
            for axis in plan.analysis_axes
            for dimension_ref in axis.dimension_refs
        )
    )
    planned_context_source_refs = tuple(
        dict.fromkeys(
            source_ref
            for axis in plan.analysis_axes
            for source_ref in axis.context_source_refs
        )
    )
    executable_query_nodes = (
        tuple(
            node
            for node in query_bundle.query_nodes
            if node.status != "degraded"
        )
        if query_bundle is not None
        else ()
    )
    if query_bundle is None:
        metric_refs = planned_metric_refs
        dimension_refs = planned_dimension_refs
        context_source_refs = planned_context_source_refs
    else:
        metric_refs = tuple(
            dict.fromkeys(
                metric_ref
                for node in executable_query_nodes
                for metric_ref in node.metric_refs
                if metric_ref not in set(intent.target_metric_refs)
            )
        )
        dimension_refs = tuple(
            dict.fromkeys(
                dimension_ref
                for node in executable_query_nodes
                for dimension_ref in node.dimension_refs
            )
        )
        context_source_refs = tuple(
            dict.fromkeys(
                source_ref
                for node in executable_query_nodes
                for source_ref in node.context_source_refs
            )
        )
    claim_intents = tuple(
        dict.fromkeys(item.claim_kind for item in plan.claim_obligations)
    )
    required_claims = tuple(
        dict.fromkeys(
            item.claim_kind
            for item in plan.claim_obligations
            if item.role == "user_required"
        )
    )
    candidate_claims = tuple(
        item for item in claim_intents if item not in set(required_claims)
    )
    roles = {}
    for capability_id in dict.fromkeys(
        task.capability_id for task in plan.capability_tasks
    ):
        tasks = tuple(
            item
            for item in plan.capability_tasks
            if item.capability_id == capability_id
        )
        roles[capability_id] = {
            "analysis_role": (
                "required"
                if any(
                    edge["required"] for task in tasks for edge in task.obligation_edges
                )
                else "auxiliary"
            ),
            "sources": tuple(item.task_id for item in tasks),
        }
    material = {
        "question_families": question_families,
        "target_metrics": intent.target_metric_refs,
        "requested_components": metric_refs,
        "requested_dimensions": dimension_refs,
        "requested_context_sources": context_source_refs,
        "claim_intents": claim_intents,
        "required_claim_intents": required_claims,
        "candidate_claim_intents": candidate_claims,
        "context_window_specs": tuple(
            spec.to_dict() for spec in plan.context_window_specs
        ),
        "scope": {"type": scope_type},
        "filters": tuple(dict(item) for item in filters),
        "grain": (
            query_bundle.aggregation_grain
            if query_bundle is not None
            else "window_id"
        ),
        "capability_roles": roles,
        "query_ir": (
            tuple(node.to_dict() for node in query_bundle.query_nodes)
            if query_bundle is not None
            else ()
        ),
    }
    return material


def _parse_actual_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthoritativeTaskInputContractError(
            "authoritative_actual_as_of_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise AuthoritativeTaskInputContractError("authoritative_actual_as_of_invalid")
    return parsed


def _validate_compile_outcome(
    *,
    outcome: AnalysisCompileOutcome,
    plan: PlanRevision,
    context: AuthorityContext,
    catalog: DatasetCatalog,
    capability_ids: tuple[str, ...],
    registry: Any,
) -> None:
    if not isinstance(outcome, AnalysisCompileOutcome):
        raise AuthoritativeTaskInputContractError(
            "authoritative_compile_outcome_invalid"
        )
    compiled_capabilities = tuple(
        item.capability_id for item in outcome.capability_plans
    )
    if (
        outcome.analysis_contract.capability_requirements != capability_ids
        or compiled_capabilities != capability_ids
        or len(compiled_capabilities) != len(set(compiled_capabilities))
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_compiled_capability_set_mismatch"
        )
    if plan.resolved_window_refs != plan.temporal_authority.resolved_window_refs:
        raise AuthoritativeTaskInputContractError(
            "authoritative_plan_window_refs_mismatch"
        )
    _validate_compiled_windows(
        outcome.analysis_contract.resolved_windows,
        plan=plan,
        registry=registry,
    )
    analysis_windows = {
        item.window_id: item for item in outcome.analysis_contract.resolved_windows
    }
    if len(analysis_windows) != len(outcome.analysis_contract.resolved_windows):
        raise AuthoritativeTaskInputContractError(
            "authoritative_compiled_window_duplicated"
        )
    snapshots = {item.snapshot_ref: item for item in catalog.snapshots()}
    coverage = {str(item["dataset_id"]): item for item in context.dataset_coverage}
    used_by_dataset: dict[str, set[str]] = {}
    query_refs: set[str] = set()
    for contract in outcome.query_contracts:
        if contract.query_contract_id in query_refs:
            raise AuthoritativeTaskInputContractError(
                "authoritative_query_contract_ref_duplicated"
            )
        query_refs.add(contract.query_contract_id)
        if tuple(item.window_id for item in contract.resolved_windows) != tuple(
            contract.window_refs
        ):
            raise AuthoritativeTaskInputContractError(
                "authoritative_query_window_refs_mismatch"
            )
        for window in contract.resolved_windows:
            if analysis_windows.get(window.window_id) != window:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_query_window_drift"
                )
        for snapshot_ref in contract.dataset_snapshot_refs:
            snapshot = snapshots.get(snapshot_ref)
            if snapshot is None:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_query_snapshot_missing"
                )
            item = coverage.get(snapshot.dataset_id)
            if (
                item is None
                or snapshot_ref not in set(item["snapshot_refs"])
                or snapshot_ref not in set(context.snapshot_refs)
                or snapshot.release_ref != item["release_ref"]
                or snapshot.release_ref not in set(context.release_refs)
            ):
                raise AuthoritativeTaskInputContractError(
                    "authoritative_query_snapshot_authority_mismatch"
                )
            used_by_dataset.setdefault(snapshot.dataset_id, set()).add(snapshot_ref)
    for dataset_id, used_refs in used_by_dataset.items():
        if used_refs != set(coverage[dataset_id]["snapshot_refs"]):
            raise AuthoritativeTaskInputContractError(
                "authoritative_query_snapshot_closure_mismatch"
            )
    known_query_refs = set(query_refs)
    for capability_plan in outcome.capability_plans:
        referenced = {
            ref
            for slot in (
                *capability_plan.required_input_slots,
                *capability_plan.optional_input_slots,
            )
            for ref in (
                *slot.query_contract_refs,
                *slot.validation_query_contract_refs,
            )
        }
        if referenced - known_query_refs:
            raise AuthoritativeTaskInputContractError(
                "authoritative_capability_query_ref_unknown"
            )


def _validate_compiled_windows(
    windows: Sequence[ResolvedWindow],
    *,
    plan: PlanRevision,
    registry: Any,
) -> None:
    targets = tuple(item for item in windows if item.role == "target")
    baselines = tuple(item for item in windows if item.role == "baseline")
    references = tuple(item for item in windows if item.role == "reference")
    if len(targets) + len(baselines) + len(references) != len(windows):
        raise AuthoritativeTaskInputContractError(
            "authoritative_compiled_window_role_invalid"
        )
    if len(targets) != 1 or targets[0].window_id != "target_day":
        raise AuthoritativeTaskInputContractError(
            "authoritative_target_window_mismatch"
        )
    _validate_compiled_temporal_window(
        targets[0],
        plan.temporal_authority.target_window,
        expected_window_id="target_day",
    )
    authority_baseline = plan.temporal_authority.baseline_window
    if authority_baseline is None:
        expected_baseline_ids: tuple[str, ...] = ()
    elif plan.temporal_authority.baseline_ids:
        expected_baseline_ids = plan.temporal_authority.baseline_ids
    else:
        expected_baseline_ids = ("baseline_window",)
    if tuple(item.window_id for item in baselines) != expected_baseline_ids:
        raise AuthoritativeTaskInputContractError(
            "authoritative_baseline_window_mismatch"
        )
    if authority_baseline is not None:
        if len(baselines) != 1:
            raise AuthoritativeTaskInputContractError(
                "authoritative_baseline_window_mismatch"
            )
        _validate_compiled_temporal_window(
            baselines[0],
            authority_baseline,
            expected_window_id=expected_baseline_ids[0],
        )
    specs_by_capability = {
        spec.capability_id: spec for spec in plan.context_window_specs
    }
    if len(references) != len(specs_by_capability):
        raise AuthoritativeTaskInputContractError(
            "authoritative_reference_window_closure_mismatch"
        )
    observed_capabilities: set[str] = set()
    for window in references:
        if len(window.capability_refs) != 1:
            raise AuthoritativeTaskInputContractError(
                "authoritative_reference_window_owner_mismatch"
            )
        capability_id = window.capability_refs[0]
        spec = specs_by_capability.get(capability_id)
        if spec is None or capability_id in observed_capabilities:
            raise AuthoritativeTaskInputContractError(
                "authoritative_reference_window_owner_mismatch"
            )
        observed_capabilities.add(capability_id)
        contract = registry.capability_inputs(capability_id)
        policy = contract.get("context_window_policy")
        if not isinstance(policy, Mapping):
            raise AuthoritativeTaskInputContractError(
                "authoritative_reference_window_contract_missing"
            )
        execution_default = policy.get("execution_default")
        binding = contract.get("task_input_binding")
        expected_execution = (
            dict(execution_default) if isinstance(execution_default, Mapping) else None
        )
        expected_relation = policy.get("relation")
        if spec.relation == "evaluation_range":
            query_route = compile_capability_query_route(
                capability_id=capability_id,
                capability_contract=contract,
                temporal_authority=plan.temporal_authority,
            )
            if (
                plan.temporal_authority.mode != "calendar_partition"
                or query_route.get("adapter_kind")
                != "daily_observation_frame"
                or plan.temporal_authority.target_window.start is None
                or plan.temporal_authority.target_window.end is None
            ):
                raise AuthoritativeTaskInputContractError(
                    "authoritative_reference_window_policy_mismatch"
                )
            try:
                observation_days = (
                    date.fromisoformat(
                        plan.temporal_authority.target_window.end
                    )
                    - date.fromisoformat(
                        plan.temporal_authority.target_window.start
                    )
                ).days + 1
            except ValueError as exc:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_reference_window_policy_mismatch"
                ) from exc
            expected_relation = "evaluation_range"
            expected_execution = {
                "unit": "day",
                "count": observation_days,
            }
        elif isinstance(binding, Mapping) and binding.get("pattern_mode") == "rolling":
            bounds = policy.get("count_bounds")
            day_bounds = bounds.get("day") if isinstance(bounds, Mapping) else None
            try:
                strategy = resolve_rolling_window_strategy(
                    plan.temporal_authority,
                    parameters=binding.get("parameters"),
                    maximum_context_days=(
                        day_bounds[1]
                        if isinstance(day_bounds, list) and len(day_bounds) == 2
                        else None
                    ),
                )
            except (TemporalComparisonContractError, TypeError) as exc:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_reference_window_contract_invalid"
                ) from exc
            expected_execution = {"unit": "day", "count": strategy.context_days}
        if expected_relation != spec.relation or expected_execution != {
            "unit": spec.unit,
            "count": spec.count,
        }:
            raise AuthoritativeTaskInputContractError(
                "authoritative_reference_window_policy_mismatch"
            )
        expected_window_id = (
            f"context__{capability_id}__{spec.relation}__{spec.count}_{spec.unit}"
        )
        if window.window_id != expected_window_id:
            raise AuthoritativeTaskInputContractError(
                "authoritative_reference_window_policy_mismatch"
            )
        try:
            required_complete_days = (
                date.fromisoformat(window.end_exclusive)
                - date.fromisoformat(window.start_inclusive)
            ).days
        except ValueError as exc:
            raise AuthoritativeTaskInputContractError(
                "authoritative_reference_window_policy_mismatch"
            ) from exc
        if (
            required_complete_days <= 0
            or window.required_complete_days != required_complete_days
            or (spec.unit == "day" and required_complete_days != spec.count)
        ):
            raise AuthoritativeTaskInputContractError(
                "authoritative_reference_window_policy_mismatch"
            )
    if observed_capabilities != set(specs_by_capability):
        raise AuthoritativeTaskInputContractError(
            "authoritative_reference_window_closure_mismatch"
        )


def _validate_compiled_temporal_window(
    window: ResolvedWindow,
    authority_window: Any,
    *,
    expected_window_id: str,
) -> None:
    if authority_window.start is None or authority_window.end is None:
        raise AuthoritativeTaskInputContractError(
            "authoritative_temporal_window_unresolved"
        )
    try:
        start = date.fromisoformat(authority_window.start)
        end = date.fromisoformat(authority_window.end)
    except ValueError as exc:
        raise AuthoritativeTaskInputContractError(
            "authoritative_temporal_window_invalid"
        ) from exc
    required_days = (end - start).days + 1
    aggregation = authority_window.aggregation
    if (
        not isinstance(aggregation, str)
        or not aggregation
        or window.window_id != expected_window_id
        or window.role != authority_window.role
        or window.start_inclusive != start.isoformat()
        or window.end_exclusive != (end + timedelta(days=1)).isoformat()
        or window.required_complete_days != required_days
        or window.source_watermark_requirement != end.isoformat()
        or window.aggregation != aggregation
    ):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_temporal_window_drift:{authority_window.role}"
        )


def _contract_gaps_by_capability(
    gaps: Sequence[ContractGap],
    capability_ids: Sequence[str],
) -> dict[str, tuple[ContractGap, ...]]:
    result: dict[str, list[ContractGap]] = {item: [] for item in capability_ids}
    for gap in gaps:
        affected = set(gap.affected_capabilities)
        targets = (
            tuple(capability_ids)
            if "analysis_contract" in affected
            else tuple(item for item in capability_ids if item in affected)
        )
        for capability_id in targets:
            result[capability_id].append(gap)
    return {
        key: tuple(sorted(value, key=lambda item: item.gap_id))
        for key, value in result.items()
    }


def _task_query_disposition(
    *,
    task: CapabilityTask,
    execution_plan: CapabilityExecutionPlan,
    results: Mapping[str, Any],
    reports: Mapping[str, Any],
) -> _TaskQueryDisposition | None:
    query_refs = _dedupe(
        tuple(
            ref
            for slot in (
                *execution_plan.required_input_slots,
                *execution_plan.optional_input_slots,
            )
            for ref in (
                *slot.query_contract_refs,
                *slot.validation_query_contract_refs,
            )
        )
    )
    missing_authority_refs = []
    failed_results = []
    unready_reports = []
    for query_ref in query_refs:
        result = results.get(query_ref)
        report = reports.get(query_ref)
        if result is None or report is None:
            missing_authority_refs.append(query_ref)
            continue
        if result.execution_status != "succeeded":
            if result.execution_status not in {"blocked", "failed"}:
                raise EvidenceIntegrityError(
                    "authoritative_query_execution_status_invalid"
                )
            failed_results.append(result)
            continue
        if (
            report.analysis_readiness != "ready"
            or report.completeness_status
            not in set(
                execution_plan.minimum_readiness.get("accepted_completeness", ())
            )
        ):
            unready_reports.append(report)
    if not missing_authority_refs and not failed_results and not unready_reports:
        return None

    report_classes = {
        item.report_ref: completeness_report_failure_classes(item)
        for item in unready_reports
    }
    observed_classes = {
        failure_class
        for classes in report_classes.values()
        for failure_class in classes
    }
    integrity_classes = {
        CompletenessFailureClass.AUTHORITY_INTEGRITY.value,
        CompletenessFailureClass.SCHEMA_INTEGRITY.value,
        CompletenessFailureClass.RESULT_CONSISTENCY.value,
        CompletenessFailureClass.RECONCILIATION_PENDING.value,
    }
    technical_classes = {
        CompletenessFailureClass.EXECUTION_TECHNICAL.value,
        CompletenessFailureClass.PROVIDER_TRUNCATION.value,
    }
    boundary_classes = {
        CompletenessFailureClass.RECONCILIATION.value,
        CompletenessFailureClass.ANALYTICAL_QUALITY.value,
    }
    availability_classes = {
        CompletenessFailureClass.EMPTY_RESULT.value,
        CompletenessFailureClass.AVAILABILITY.value,
        CompletenessFailureClass.FRESHNESS.value,
    }
    known_classes = (
        integrity_classes | technical_classes | boundary_classes | availability_classes
    )
    if observed_classes - known_classes or (unready_reports and not observed_classes):
        raise EvidenceIntegrityError("authoritative_completeness_failure_class_invalid")

    result_refs = _dedupe(
        (
            *(item.result_ref for item in failed_results),
            *(item.result_ref for item in unready_reports),
        )
    )
    report_refs = _dedupe(
        (
            *(item.completeness_report_ref for item in failed_results),
            *(item.report_ref for item in unready_reports),
        )
    )
    detail_payload = {
        "task_id": task.task_id,
        "capability_id": task.capability_id,
        "missing_authority_refs": tuple(missing_authority_refs),
        "query_failures": tuple(
            {
                "query_contract_ref": item.query_contract_ref,
                "result_ref": item.result_ref,
                "execution_status": item.execution_status,
                "failure_reason": item.failure_reason,
            }
            for item in failed_results
        ),
        "completeness_failures": tuple(
            {
                "query_contract_ref": item.query_contract_ref,
                "report_ref": item.report_ref,
                "completeness_status": item.completeness_status,
                "analysis_readiness": item.analysis_readiness,
                "failure_classes": report_classes[item.report_ref],
            }
            for item in unready_reports
        ),
    }
    integrity_failure = bool(
        missing_authority_refs
        or observed_classes & integrity_classes
        or any(item.execution_status == "blocked" for item in failed_results)
    )
    technical_failure = bool(
        observed_classes & technical_classes
        or any(item.execution_status == "failed" for item in failed_results)
    )
    if not integrity_failure and not technical_failure:
        if observed_classes & boundary_classes:
            gap_type = "contract_partial"
            retryability = "replan_required"
        elif CompletenessFailureClass.FRESHNESS.value in observed_classes:
            gap_type = "dataset_snapshot_unavailable_as_of"
            retryability = "same_input"
        elif observed_classes & availability_classes:
            gap_type = "window_data_unavailable"
            retryability = "same_input"
        else:
            raise EvidenceIntegrityError("authoritative_query_disposition_unclassified")
        expected_gap = ExpectedCapabilityGap.create(
            gap_type=gap_type,
            limitation_ref=("limitation:sha256:" + canonical_digest(detail_payload)),
            data_contract_state=gap_type,
            business_boundary=f"{task.capability_id}_evidence_unavailable",
            retryability=retryability,
        )
        return _TaskQueryDisposition(
            expected_gap=expected_gap,
            failure_status=None,
            failure=None,
            result_refs=result_refs,
            report_refs=report_refs,
        )

    status = "integrity_failed" if integrity_failure else "technical_failed"
    failure_kind = (
        "query_authority_invalid"
        if missing_authority_refs
        else "query_execution_blocked"
        if any(item.execution_status == "blocked" for item in failed_results)
        else "query_result_contract_invalid"
        if observed_classes & integrity_classes
        else "query_result_truncated"
        if CompletenessFailureClass.PROVIDER_TRUNCATION.value in observed_classes
        else "query_execution_failed"
    )
    failure = CapabilityFailure.create(
        layer="query",
        kind=failure_kind,
        scope="task",
        affected_refs=_dedupe(
            (
                task.task_id,
                *task.supports_obligation_ids,
                *missing_authority_refs,
                *(item.query_contract_ref for item in failed_results),
                *(item.query_contract_ref for item in unready_reports),
            )
        ),
        integrity_level="task",
        retryability=(
            "replan_required"
            if integrity_failure
            or CompletenessFailureClass.PROVIDER_TRUNCATION.value in observed_classes
            else "same_input"
        ),
        user_actionable=False,
        business_boundary=f"{task.capability_id}_evidence_unpublishable",
        technical_detail_ref=(
            "task-query-failure:sha256:" + canonical_digest(detail_payload)
        ),
    )
    return _TaskQueryDisposition(
        expected_gap=None,
        failure_status=status,
        failure=failure,
        result_refs=result_refs,
        report_refs=report_refs,
    )


def _expected_gap(
    *,
    task: CapabilityTask,
    gaps: Sequence[ContractGap],
) -> ExpectedCapabilityGap | None:
    if not gaps:
        input_gaps = tuple(
            item
            for item in task.execution_policy["input_states"]
            if item["availability"] in {"missing_contract", "unavailable"}
        )
        if not input_gaps:
            return None
        gap_type = (
            "missing_contract"
            if any(item["availability"] == "missing_contract" for item in input_gaps)
            else "source_unbound"
        )
        limitation_refs = tuple(
            str(item["limitation_ref"]) for item in input_gaps if item["limitation_ref"]
        )
        limitation_ref = (
            limitation_refs[0]
            if len(limitation_refs) == 1
            else "limitation:sha256:"
            + canonical_digest(
                {
                    "capability_id": task.capability_id,
                    "limitation_refs": limitation_refs,
                }
            )
        )
        return ExpectedCapabilityGap.create(
            gap_type=gap_type,
            limitation_ref=limitation_ref,
            data_contract_state=gap_type,
            business_boundary=f"{task.capability_id}_evidence_unavailable",
            retryability="replan_required",
        )
    gap_types = tuple(
        item.gap_type if item.gap_type in _CONTRACT_GAP_TYPES else "contract_partial"
        for item in gaps
    )
    gap_type = gap_types[0] if len(set(gap_types)) == 1 else "contract_partial"
    return ExpectedCapabilityGap.create(
        gap_type=gap_type,
        limitation_ref="limitation:sha256:"
        + canonical_digest(
            {
                "capability_id": task.capability_id,
                "gap_ids": tuple(item.gap_id for item in gaps),
            }
        ),
        data_contract_state=gap_type,
        business_boundary=f"{task.capability_id}_evidence_unavailable",
        retryability="replan_required",
    )


def _gap_limitation_refs(gaps: Sequence[ContractGap]) -> tuple[str, ...]:
    return tuple(f"contract-gap:{item.gap_id}" for item in gaps)


def _applicable_bound_gap_limitation_refs(
    gaps: Sequence[ContractGap],
    *,
    bound: BoundCapabilityInput,
    query_by_ref: Mapping[str, QueryContract],
) -> tuple[str, ...]:
    """Keep only contract gaps that constrain the source selected for this binding."""
    query_refs = _dedupe(
        (*bound.query_contract_refs, *bound.validation_query_contract_refs)
    )
    try:
        contracts = tuple(query_by_ref[ref] for ref in query_refs)
    except KeyError as exc:
        raise EvidenceIntegrityError(
            "authoritative_bound_query_contract_missing"
        ) from exc
    selected_dataset_ids = {
        binding.dataset_id
        for contract in contracts
        for binding in (*contract.metric_bindings, *contract.dimension_bindings)
    }
    applicable = tuple(
        gap
        for gap in gaps
        if not gap.dataset_id or gap.dataset_id in selected_dataset_ids
        if not (
            gap.diagnostic_context.get("resolution_mode")
            == "current_window_reconciliation"
            and gap.diagnostic_context.get("resolver_capability_id")
            == bound.capability_id
        )
    )
    return _gap_limitation_refs(applicable)


def _scope_ref(intent: IntentRevision) -> str:
    return "scope:sha256:" + canonical_digest(intent.scope)


def _task_payload(
    *,
    plan: PlanRevision,
    task: CapabilityTask,
    intent: IntentRevision,
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    query_by_ref: Mapping[str, QueryContract],
    result_by_query: Mapping[str, Any],
    report_by_query: Mapping[str, Any],
    registry: Any,
    bound_by_capability: Mapping[str, BoundCapabilityInput] | None = None,
) -> Mapping[str, Any]:
    axis = _task_axis(plan, task)
    metric_id = _single_target_metric(axis, intent)
    capability_id = task.capability_id
    rows = _bound_rows(bound)
    contracts = _bound_primary_contracts(bound, query_by_ref)
    binding = _task_input_binding(registry, capability_id)
    payload_kind = _binding_string(binding, "payload_kind", capability_id)
    fields = _binding_mapping(binding, "fields", capability_id, required=False)
    parameters = _binding_mapping(binding, "parameters", capability_id, required=False)
    if payload_kind == "event_window_metric_comparison":
        return _event_window_metric_comparison_payload(
            plan=plan,
            task=task,
            bound=bound,
            bound_by_capability=bound_by_capability or {},
            execution_plan=execution_plan,
            contracts=contracts,
            metric_id=metric_id,
            binding=binding,
            capability_id=capability_id,
            temporal_authority=plan.temporal_authority,
            dynamic_event_window_policy=registry.capability_inputs(
                capability_id
            ).get("dynamic_event_window_policy"),
        )
    if payload_kind == "window_metric_comparison":
        contract = _single_query_family(
            contracts, _binding_query_family(binding, "primary", capability_id)
        )
        _require_query_metric(contract, metric_id, capability_id)
        return {
            "contract": contract,
            "rows": _rows_for_contract(bound, execution_plan, contract),
            "metric_id": metric_id,
            "primary_baseline_window_id": _comparison_baseline_window_id(
                contract
            ),
        }
    if payload_kind == "multi_metric_window_comparison":
        contract = _single_query_family(
            contracts, _binding_query_family(binding, "primary", capability_id)
        )
        metric_ids = tuple(
            item.metric_id for item in contract.metric_bindings
        )
        required_metric_ids = tuple(
            str(item)
            for item in registry.capability_inputs(capability_id).get(
                "required_metrics", ()
            )
        )
        if (
            not metric_ids
            or not required_metric_ids
            or not set(required_metric_ids) <= set(metric_ids)
        ):
            raise _TaskPayloadContractGap(
                gap_type="missing_contract",
                limitation_ref=(
                    "limitation:sha256:"
                    + canonical_digest(
                        {
                            "capability_id": capability_id,
                            "query_contract_ref": contract.query_contract_id,
                            "required_metric_ids": required_metric_ids,
                            "bound_metric_ids": metric_ids,
                        }
                    )
                ),
                business_boundary=(
                    f"{capability_id}_multi_metric_input_unavailable"
                ),
            )
        return {
            "contract": contract,
            "rows": _rows_for_contract(bound, execution_plan, contract),
            "metric_ids": required_metric_ids,
            "primary_baseline_window_id": _comparison_baseline_window_id(
                contract
            ),
        }
    if payload_kind == "formula_graph":
        return _formula_payload(
            bound=bound,
            execution_plan=execution_plan,
            contracts=contracts,
            metric_id=metric_id,
            baseline_id=_comparison_baseline_window_id(
                _single_query_family(
                    contracts,
                    _binding_query_family(binding, "primary", capability_id),
                )
            ),
            registry=registry,
            binding=binding,
            capability_id=capability_id,
        )
    if payload_kind == "funnel_decomposition":
        return _funnel_decomposition_payload(
            bound=bound,
            execution_plan=execution_plan,
            contracts=contracts,
            binding=binding,
            capability_id=capability_id,
        )
    if payload_kind == "candidate_dimension_screen":
        rows_by_dimension = _rows_by_dimension(bound, execution_plan, query_by_ref)
        group_key = _binding_string(fields, "group_key", capability_id)
        target_group = _binding_string(fields, "target_group", capability_id)
        baseline_group = _binding_string(fields, "baseline_group", capability_id)
        amount_key = _binding_string(fields, "amount_key", capability_id)
        order_key = _binding_string(fields, "order_key", capability_id)
        user_key = _binding_string(fields, "user_key", capability_id)
        for contract in contracts:
            _require_query_fields(
                contract,
                (group_key, amount_key, order_key, user_key),
                capability_id,
            )
        return {
            "rows_by_dimension": rows_by_dimension,
            "overall_by_group": _overall_by_group(
                rows_by_dimension,
                metric_id=amount_key,
                group_key=group_key,
                target_group=target_group,
                baseline_group=baseline_group,
            ),
            "complete_dimensions": tuple(rows_by_dimension),
            "dimension_labels": {
                item: str(registry.dimension(item).get("business_name") or item)
                for item in rows_by_dimension
            },
            "dimension_metadata": {
                item: registry.dimension(item) for item in rows_by_dimension
            },
            "group_key": group_key,
            "target_group": target_group,
            "baseline_group": baseline_group,
            "amount_key": amount_key,
            "order_key": order_key,
            "user_key": user_key,
            **dict(parameters),
        }
    if payload_kind == "payment_outcome_comparison":
        rows_by_dimension = _rows_by_dimension(
            bound,
            execution_plan,
            query_by_ref,
        )
        group_key = _binding_string(fields, "group_key", capability_id)
        window_id_key = _binding_string(fields, "window_id_key", capability_id)
        target_group = _binding_string(fields, "target_group", capability_id)
        baseline_group = _binding_string(fields, "baseline_group", capability_id)
        terminal_orders_key = _binding_string(
            fields, "terminal_orders_key", capability_id
        )
        successful_orders_key = _binding_string(
            fields, "successful_orders_key", capability_id
        )
        not_paid_orders_key = _binding_string(
            fields, "not_paid_orders_key", capability_id
        )
        success_rate_key = _binding_string(
            fields, "success_rate_key", capability_id
        )
        for contract in contracts:
            _require_query_fields(
                contract,
                (
                    group_key,
                    window_id_key,
                    terminal_orders_key,
                    successful_orders_key,
                    not_paid_orders_key,
                    success_rate_key,
                ),
                capability_id,
            )
        return {
            "rows_by_dimension": rows_by_dimension,
            "group_key": group_key,
            "window_id_key": window_id_key,
            "target_group": target_group,
            "baseline_group": baseline_group,
            "terminal_orders_key": terminal_orders_key,
            "successful_orders_key": successful_orders_key,
            "not_paid_orders_key": not_paid_orders_key,
            "success_rate_key": success_rate_key,
            "dimension_labels": {
                item: str(registry.dimension(item).get("business_name") or item)
                for item in rows_by_dimension
            },
        }
    if payload_kind == "dimension_distribution":
        rows_by_dimension = _rows_by_dimension(bound, execution_plan, query_by_ref)
        group_key = _binding_string(fields, "group_key", capability_id)
        target_group = _binding_string(fields, "target_group", capability_id)
        baseline_group = _binding_string(fields, "baseline_group", capability_id)
        for contract in contracts:
            _require_query_fields(contract, (group_key, metric_id), capability_id)
        return {
            "rows_by_dimension": rows_by_dimension,
            "metric_id": metric_id,
            "group_key": group_key,
            "target_group": target_group,
            "baseline_group": baseline_group,
            "dimension_paths": _dimension_paths(rows_by_dimension, registry),
        }
    if payload_kind == "segment_contribution":
        rows_by_dimension = _rows_by_dimension(bound, execution_plan, query_by_ref)
        segment_key = _binding_string(fields, "segment_key", capability_id)
        group_key = _binding_string(fields, "group_key", capability_id)
        target_group = _binding_string(fields, "target_group", capability_id)
        baseline_group = _binding_string(fields, "baseline_group", capability_id)
        amount_key = _binding_string(fields, "amount_key", capability_id)
        for contract in contracts:
            _require_query_fields(contract, (group_key, amount_key), capability_id)
        return {
            "rows": _normalized_independent_dimension_rows(
                rows_by_dimension, output_key=segment_key
            ),
            "segment_key": segment_key,
            "group_key": group_key,
            "target_group": target_group,
            "baseline_group": baseline_group,
            "amount_key": amount_key,
        }
    if payload_kind == "joint_attribution":
        group_key = _binding_string(fields, "group_key", capability_id)
        target_group = _binding_string(fields, "target_group", capability_id)
        baseline_group = _binding_string(fields, "baseline_group", capability_id)
        amount_key = _binding_string(fields, "amount_key", capability_id)
        analyses = []
        for contract in contracts:
            dimension_ids = tuple(
                item.dimension_id for item in contract.dimension_bindings
            )
            if len(dimension_ids) < 2:
                continue
            _require_query_fields(
                contract, (group_key, amount_key, *dimension_ids), capability_id
            )
            analyses.append(
                {
                    "query_contract_ref": contract.query_contract_id,
                    "rows": _rows_for_contract(bound, execution_plan, contract),
                    "dimension_keys": dimension_ids,
                }
            )
        if not analyses:
            raise _TaskPayloadContractGap(
                gap_type="missing_contract",
                limitation_ref=(
                    "limitation:sha256:"
                    + canonical_digest(
                        {
                            "capability_id": capability_id,
                            "required_dimension_count": 2,
                            "query_contract_refs": tuple(
                                contract.query_contract_id
                                for contract in contracts
                            ),
                        }
                    )
                ),
                business_boundary=(
                    f"{capability_id}_joint_query_inputs_unavailable"
                ),
            )
        return {
            "analyses": tuple(analyses),
            "group_key": group_key,
            "target_group": target_group,
            "baseline_group": baseline_group,
            "amount_key": amount_key,
            **dict(parameters),
        }
    if payload_kind == "user_mix_contribution":
        required = tuple(
            _binding_string(fields, key, capability_id)
            for key in (
                "segment_key",
                "mix_key",
                "group_key",
                "amount_key",
                "users_key",
            )
        )
        contract = _single_query_family_with_fields(
            contracts,
            _binding_query_family(binding, "primary", capability_id),
            required,
            capability_id,
        )
        return {
            "rows": _rows_for_contract(bound, execution_plan, contract),
            **{
                key: _binding_string(fields, key, capability_id)
                for key in (
                    "segment_key",
                    "mix_key",
                    "group_key",
                    "amount_key",
                    "users_key",
                )
            },
            **dict(parameters),
        }
    if payload_kind == "high_value_user_contribution":
        contract = _single_query_family(
            contracts, _binding_query_family(binding, "primary", capability_id)
        )
        quantile = contract.query_parameters.get("threshold_quantile")
        if isinstance(quantile, bool) or not isinstance(quantile, (int, float)):
            raise AuthoritativeTaskInputContractError(
                "authoritative_high_value_threshold_contract_invalid"
            )
        group_key = _binding_string(fields, "group_key", capability_id)
        total_amount_key = _binding_string(fields, "total_amount_key", capability_id)
        high_value_amount_key = _binding_string(
            fields, "high_value_amount_key", capability_id
        )
        high_value_users_key = _binding_string(
            fields, "high_value_users_key", capability_id
        )
        threshold_key = _binding_string(fields, "threshold_key", capability_id)
        _require_query_fields(
            contract,
            (
                group_key,
                total_amount_key,
                high_value_amount_key,
                high_value_users_key,
                threshold_key,
            ),
            capability_id,
        )
        window_aggregations = {
            window.aggregation for window in contract.resolved_windows
        }
        if window_aggregations == {"mean_of_complete_days"}:
            high_value_users_aggregation = "mean_per_complete_day"
        elif window_aggregations and window_aggregations.issubset(
            {"daily_total", "sum_of_complete_days"}
        ):
            high_value_users_aggregation = "window_distinct_count"
        else:
            raise AuthoritativeTaskInputContractError(
                "authoritative_high_value_user_count_aggregation_ambiguous"
            )
        return {
            "rows": _rows_for_contract(bound, execution_plan, contract),
            "threshold_policy": {"type": "top_percentile", "value": quantile},
            "high_value_users_aggregation": high_value_users_aggregation,
            "group_key": group_key,
            "total_amount_key": total_amount_key,
            "high_value_amount_key": high_value_amount_key,
            "high_value_users_key": high_value_users_key,
            "threshold_key": threshold_key,
        }
    if payload_kind == "metric_timeseries":
        contract = _single_query_family(
            contracts, _binding_query_family(binding, "primary", capability_id)
        )
        time_key = _binding_string(fields, "time_key", capability_id)
        window_id_key = _binding_string(fields, "window_id_key", capability_id)
        window_role_key = _binding_string(fields, "window_role_key", capability_id)
        _require_query_fields(
            contract,
            (metric_id, time_key, window_id_key, window_role_key),
            capability_id,
        )
        return {
            "rows": _rows_for_contract(bound, execution_plan, contract),
            "metric_id": metric_id,
            "time_key": time_key,
            "window_id_key": window_id_key,
            "window_role_key": window_role_key,
        }
    if payload_kind == "change_point_scan":
        contract = _single_query_family(
            contracts, _binding_query_family(binding, "primary", capability_id)
        )
        time_key = _binding_string(fields, "time_key", capability_id)
        value_key = _binding_string(fields, "value_key", capability_id)
        _require_query_metric(contract, metric_id, capability_id)
        _require_query_fields(contract, (time_key, value_key), capability_id)
        _require_exact_binding_keys(
            parameters,
            (
                "min_total_samples",
                "min_segment_samples",
                "min_relative_level_shift",
                "min_standardized_level_shift",
                "max_candidates",
            ),
            capability_id=capability_id,
            field="parameters",
        )
        return {
            "rows": _deduplicated_metric_timeseries_rows(
                _rows_for_contract(bound, execution_plan, contract),
                time_key=time_key,
                value_key=value_key,
            ),
            "time_key": time_key,
            "value_key": value_key,
            **dict(parameters),
        }
    if payload_kind == "metric_coverage_profile":
        return _metric_coverage_profile_payload(
            bound=bound,
            execution_plan=execution_plan,
            contracts=contracts,
            result_by_query=result_by_query,
            report_by_query=report_by_query,
            metric_id=metric_id,
            binding=binding,
            capability_id=capability_id,
        )
    if payload_kind == "market_channel_context":
        return _market_channel_context_payload(
            bound=bound,
            execution_plan=execution_plan,
            contracts=contracts,
            result_by_query=result_by_query,
            report_by_query=report_by_query,
            metric_id=metric_id,
            binding=binding,
            capability_id=capability_id,
        )
    if payload_kind == "source_reconciliation":
        return _source_reconciliation_payload(
            bound=bound,
            execution_plan=execution_plan,
            contracts=contracts,
            result_by_query=result_by_query,
            metric_id=metric_id,
            binding=binding,
            capability_id=capability_id,
        )
    if payload_kind == "pattern":
        return _pattern_payload(
            capability_id=capability_id,
            rows=rows,
            contracts=contracts,
            metric_id=metric_id,
            binding=binding,
            temporal_authority=plan.temporal_authority,
        )
    if payload_kind == "data_quality":
        return {
            "rows": rows,
            "required_fields": tuple(
                dict.fromkeys(
                    field
                    for contract in contracts
                    for field in contract.result_shape.required_fields
                )
            ),
        }
    if payload_kind == "event_evidence":
        payload = {"events": rows, **dict(parameters)}
        if plan.temporal_authority.mode == "event_relative":
            payload.update(_event_temporal_identity(plan.temporal_authority))
        elif plan.temporal_authority.mode == "calendar_partition":
            dynamic_policy = _dependent_event_window_policy(
                plan=plan,
                source_task=task,
                registry=registry,
            )
            if dynamic_policy is not None:
                try:
                    payload["event_window_set"] = derive_event_window_set(
                        rows,
                        temporal_authority=plan.temporal_authority,
                        policy=dynamic_policy,
                    )
                except EventWindowDerivationError as exc:
                    raise AuthoritativeTaskInputContractError(
                        f"authoritative_event_window_derivation_failed:{exc}"
                    ) from exc
        return payload
    if payload_kind == "cross_source_association":
        return _cross_source_association_payload(
            bound=bound,
            execution_plan=execution_plan,
            contracts=contracts,
            binding=binding,
            capability_id=capability_id,
        )
    if payload_kind == "cross_source_panel_association":
        return _cross_source_panel_payload(
            bound=bound,
            execution_plan=execution_plan,
            contracts=contracts,
            binding=binding,
            capability_id=capability_id,
            registry=registry,
        )
    if payload_kind == "outlier_scan":
        contract = _single_query_family(
            contracts, _binding_query_family(binding, "primary", capability_id)
        )
        value_key = _binding_string(fields, "value_key", capability_id)
        period_key = _binding_string(fields, "period_key", capability_id)
        group_key = _binding_string(fields, "group_key", capability_id)
        target_group = _binding_string(fields, "target_group", capability_id)
        reference_group = _binding_string(fields, "reference_group", capability_id)
        _require_query_fields(
            contract,
            (value_key, period_key, group_key),
            capability_id,
        )
        return {
            "rows": _rows_for_contract(bound, execution_plan, contract),
            "value_key": value_key,
            "period_key": period_key,
            "group_key": group_key,
            "target_group": target_group,
            "reference_group": reference_group,
            **dict(parameters),
        }
    if payload_kind == "outlier_contribution":
        contract = _single_query_family(
            contracts, _binding_query_family(binding, "primary", capability_id)
        )
        period_key = _binding_string(fields, "period_key", capability_id)
        group_key = _binding_string(fields, "group_key", capability_id)
        amount_key = _binding_string(fields, "amount_key", capability_id)
        _require_query_fields(
            contract,
            (period_key, group_key, metric_id),
            capability_id,
        )
        return {
            "rows": tuple(
                {
                    **dict(item),
                    amount_key: item.get(metric_id),
                }
                for item in _rows_for_contract(bound, execution_plan, contract)
            ),
            "period_key": period_key,
            "period_grain": _binding_string(fields, "period_grain", capability_id),
            "group_key": group_key,
            "target_group": _binding_string(fields, "target_group", capability_id),
            "baseline_group": _binding_string(fields, "baseline_group", capability_id),
            "amount_key": amount_key,
            **dict(parameters),
        }
    raise AuthoritativeTaskInputContractError(
        f"authoritative_task_payload_kind_unknown:{capability_id}:{payload_kind}"
    )


def _task_input_binding(
    registry: Any,
    capability_id: str,
) -> Mapping[str, Any]:
    contract = registry.capability_inputs(capability_id)
    binding = contract.get("task_input_binding")
    if not isinstance(binding, Mapping):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_missing:{capability_id}"
        )
    return binding


def _binding_mapping(
    source: Mapping[str, Any],
    key: str,
    capability_id: str,
    *,
    required: bool = True,
) -> Mapping[str, Any]:
    value = source.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, Mapping) or (required and not value):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:{key}"
        )
    return value


def _binding_string(
    source: Mapping[str, Any],
    key: str,
    capability_id: str,
) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:{key}"
        )
    return value


def _binding_sequence(
    source: Mapping[str, Any],
    key: str,
    capability_id: str,
) -> tuple[str, ...]:
    value = source.get(key)
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or not value
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:{key}"
        )
    return tuple(value)


def _binding_query_family(
    binding: Mapping[str, Any],
    role: str,
    capability_id: str,
) -> str:
    families = _binding_mapping(binding, "query_families", capability_id)
    return _binding_string(families, role, capability_id)


def _query_output_fields(contract: QueryContract) -> frozenset[str]:
    return frozenset(
        (
            *contract.result_shape.required_fields,
            *(item.metric_id for item in contract.metric_bindings),
            *(item.dimension_id for item in contract.dimension_bindings),
        )
    )


def _require_query_metric(
    contract: QueryContract,
    metric_id: str,
    capability_id: str,
) -> None:
    if metric_id not in {item.metric_id for item in contract.metric_bindings}:
        _raise_task_payload_gap(
            capability_id=capability_id,
            missing_fields=(metric_id,),
            contract=contract,
        )


def _require_query_fields(
    contract: QueryContract,
    fields: Sequence[str],
    capability_id: str,
) -> None:
    missing = tuple(
        item
        for item in dict.fromkeys(fields)
        if item not in _query_output_fields(contract)
    )
    if missing:
        _raise_task_payload_gap(
            capability_id=capability_id,
            missing_fields=missing,
            contract=contract,
        )


def _raise_task_payload_gap(
    *,
    capability_id: str,
    missing_fields: Sequence[str],
    contract: QueryContract,
) -> None:
    limitation_ref = "limitation:sha256:" + canonical_digest(
        {
            "capability_id": capability_id,
            "query_contract_ref": contract.query_contract_id,
            "missing_fields": tuple(missing_fields),
        }
    )
    raise _TaskPayloadContractGap(
        gap_type="missing_contract",
        limitation_ref=limitation_ref,
        business_boundary=f"{capability_id}_input_contract_unavailable",
    )


def _task_axis(plan: PlanRevision, task: CapabilityTask) -> AnalysisAxis:
    refs = set(task.normalized_input_refs)
    matches = tuple(
        axis for axis in plan.analysis_axes if axis.analysis_axis_ref in refs
    )
    if len(matches) != 1:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_axis_cardinality_invalid:{task.task_id}"
        )
    return matches[0]


def _single_target_metric(
    axis: AnalysisAxis,
    intent: IntentRevision,
) -> str:
    metrics = tuple(
        item
        for item in axis.target_metric_refs
        if item in set(intent.target_metric_refs)
    )
    if len(metrics) != 1:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_target_metric_cardinality_invalid:{axis.axis_id}"
        )
    return metrics[0]


def _bound_rows(bound: BoundCapabilityInput) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        dict(row) for slot_rows in bound.rows_by_slot.values() for row in slot_rows
    )


def _dependency_bound_refs(
    *,
    plan: PlanRevision,
    task: CapabilityTask,
    bound_by_capability: Mapping[str, BoundCapabilityInput],
    field: str,
) -> tuple[str, ...]:
    task_by_id = {item.task_id: item for item in plan.capability_tasks}
    output: list[str] = []
    for dependency_task_id in task.dependency_task_ids:
        dependency_task = task_by_id.get(dependency_task_id)
        if dependency_task is None:
            raise AuthoritativeTaskInputContractError(
                "authoritative_dynamic_event_dependency_invalid"
            )
        dependency_bound = bound_by_capability.get(
            dependency_task.capability_id
        )
        if dependency_bound is None or not hasattr(dependency_bound, field):
            raise AuthoritativeTaskInputContractError(
                "authoritative_dynamic_event_dependency_input_missing"
            )
        output.extend(str(item) for item in getattr(dependency_bound, field))
    return tuple(output)


def _bound_primary_contracts(
    bound: BoundCapabilityInput,
    query_by_ref: Mapping[str, QueryContract],
) -> tuple[QueryContract, ...]:
    try:
        return tuple(query_by_ref[item] for item in bound.query_contract_refs)
    except KeyError as exc:
        raise EvidenceIntegrityError(
            "authoritative_bound_query_contract_missing"
        ) from exc


def _single_query_family(
    contracts: Sequence[QueryContract], family: str
) -> QueryContract:
    matches = tuple(item for item in contracts if item.query_intent == family)
    if len(matches) != 1:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_query_family_cardinality_invalid:{family}"
        )
    return matches[0]


def _single_query_family_with_fields(
    contracts: Sequence[QueryContract],
    family: str,
    fields: Sequence[str],
    capability_id: str,
) -> QueryContract:
    family_contracts = tuple(item for item in contracts if item.query_intent == family)
    matches = tuple(
        item
        for item in family_contracts
        if set(fields) <= set(_query_output_fields(item))
    )
    if len(matches) == 1:
        return matches[0]
    if not matches and family_contracts:
        _raise_task_payload_gap(
            capability_id=capability_id,
            missing_fields=tuple(fields),
            contract=family_contracts[0],
        )
    raise AuthoritativeTaskInputContractError(
        f"authoritative_query_family_field_binding_invalid:{capability_id}:{family}"
    )


def _rows_for_contract(
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    contract: QueryContract,
) -> tuple[Mapping[str, Any], ...]:
    matches = tuple(
        slot.slot_id
        for slot in (
            *execution_plan.required_input_slots,
            *execution_plan.optional_input_slots,
        )
        if tuple(slot.query_contract_refs) == (contract.query_contract_id,)
    )
    if len(matches) != 1 or matches[0] not in bound.rows_by_slot:
        raise EvidenceIntegrityError(
            f"authoritative_bound_query_slot_mismatch:{contract.query_contract_id}"
        )
    return tuple(dict(item) for item in bound.rows_by_slot[matches[0]])


def _query_family_contracts(
    contracts: Sequence[QueryContract],
    family: str,
) -> tuple[QueryContract, ...]:
    matches = tuple(item for item in contracts if item.query_intent == family)
    if not matches:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_query_family_missing:{family}"
        )
    return matches


def _require_exact_binding_keys(
    value: Mapping[str, Any],
    expected: Sequence[str],
    *,
    capability_id: str,
    field: str,
) -> None:
    if set(value) != set(expected):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:{field}"
        )


def _deduplicated_metric_timeseries_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    time_key: str,
    value_key: str,
) -> tuple[Mapping[str, Any], ...]:
    by_observation: dict[tuple[str, str], tuple[Decimal, Mapping[str, Any]]] = {}
    for row in rows:
        if time_key not in row or value_key not in row:
            raise EvidenceIntegrityError("authoritative_metric_timeseries_row_invalid")
        identity = (type(row[time_key]).__name__, repr(row[time_key]))
        try:
            value = Decimal(str(row[value_key]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                "authoritative_metric_timeseries_value_invalid"
            ) from exc
        if not value.is_finite():
            raise EvidenceIntegrityError(
                "authoritative_metric_timeseries_value_invalid"
            )
        previous = by_observation.get(identity)
        if previous is not None:
            if previous[0] != value:
                raise EvidenceIntegrityError(
                    "authoritative_metric_timeseries_overlap_mismatch"
                )
            continue
        by_observation[identity] = (value, dict(row))
    return tuple(item[1] for item in by_observation.values())


def _metric_coverage_profile_payload(
    *,
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    contracts: Sequence[QueryContract],
    result_by_query: Mapping[str, Any],
    report_by_query: Mapping[str, Any],
    metric_id: str,
    binding: Mapping[str, Any],
    capability_id: str,
) -> Mapping[str, Any]:
    fields = _binding_mapping(binding, "fields", capability_id)
    parameters = _binding_mapping(binding, "parameters", capability_id)
    _require_exact_binding_keys(
        fields,
        (
            "value_key",
            "result_ref_key",
            "window_id_key",
            "observation_key",
            "source_row_count_key",
        ),
        capability_id=capability_id,
        field="fields",
    )
    _require_exact_binding_keys(
        parameters,
        ("coverage_records_source",),
        capability_id=capability_id,
        field="parameters",
    )
    if parameters["coverage_records_source"] != "query_contract_and_completeness":
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:"
            "parameters.coverage_records_source"
        )
    value_key = _binding_string(fields, "value_key", capability_id)
    result_ref_key = _binding_string(fields, "result_ref_key", capability_id)
    window_id_key = _binding_string(fields, "window_id_key", capability_id)
    observation_key = _binding_string(fields, "observation_key", capability_id)
    source_row_count_key = _binding_string(
        fields, "source_row_count_key", capability_id
    )
    family = _binding_query_family(binding, "primary", capability_id)
    entries = []
    seen_datasets: set[str] = set()
    for contract in _query_family_contracts(contracts, family):
        metric_binding = _single_contract_metric_binding(
            contract, metric_id=metric_id, capability_id=capability_id
        )
        _require_query_fields(
            contract,
            (
                value_key,
                window_id_key,
                observation_key,
                source_row_count_key,
            ),
            capability_id,
        )
        if metric_binding.dataset_id in seen_datasets:
            raise AuthoritativeTaskInputContractError(
                "authoritative_metric_coverage_dataset_duplicate"
            )
        result, report = _query_result_and_report(
            contract,
            result_by_query=result_by_query,
            report_by_query=report_by_query,
        )
        coverage_summary = report.coverage_summary
        if not isinstance(coverage_summary, Mapping):
            raise EvidenceIntegrityError(
                "authoritative_metric_coverage_summary_invalid"
            )
        day_counts = coverage_summary.get("window_day_counts")
        if not isinstance(day_counts, Mapping):
            raise EvidenceIntegrityError(
                "authoritative_metric_coverage_window_counts_invalid"
            )
        windows = []
        for window in contract.resolved_windows:
            observed_days = day_counts.get(window.window_id, 0)
            if type(observed_days) is not int or observed_days < 0:
                raise EvidenceIntegrityError(
                    "authoritative_metric_coverage_window_counts_invalid"
                )
            windows.append(
                {
                    "window_id": window.window_id,
                    "required_days": window.required_complete_days,
                    "observed_days": observed_days,
                }
            )
        source_rows = _rows_for_contract(bound, execution_plan, contract)
        normalized_rows = []
        for row in source_rows:
            if result_ref_key in row:
                raise EvidenceIntegrityError(
                    "authoritative_metric_coverage_result_ref_collision"
                )
            normalized_rows.append({**dict(row), result_ref_key: result.result_ref})
        entries.append(
            (
                metric_binding.dataset_id,
                tuple(normalized_rows),
                {
                    "result_ref": result.result_ref,
                    "dataset_id": metric_binding.dataset_id,
                    "snapshot_refs": tuple(result.source_snapshot_refs),
                    "completeness_report_ref": report.report_ref,
                    "completeness_status": report.completeness_status,
                    "analysis_readiness": report.analysis_readiness,
                    "windows": tuple(windows),
                },
            )
        )
        seen_datasets.add(metric_binding.dataset_id)
    entries.sort(key=lambda item: item[0])
    return {
        "rows": tuple(row for _, rows, _ in entries for row in rows),
        "metric_id": metric_id,
        "value_key": value_key,
        "result_ref_key": result_ref_key,
        "window_id_key": window_id_key,
        "observation_key": observation_key,
        "source_row_count_key": source_row_count_key,
        "coverage_records": tuple(record for _, _, record in entries),
    }


def _market_channel_context_payload(
    *,
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    contracts: Sequence[QueryContract],
    result_by_query: Mapping[str, Any],
    report_by_query: Mapping[str, Any],
    metric_id: str,
    binding: Mapping[str, Any],
    capability_id: str,
) -> Mapping[str, Any]:
    fields = _binding_mapping(binding, "fields", capability_id)
    parameters = _binding_mapping(binding, "parameters", capability_id)
    _require_exact_binding_keys(
        fields,
        ("channel_key", "window_id_key", "observation_key"),
        capability_id=capability_id,
        field="fields",
    )
    _require_exact_binding_keys(
        parameters,
        (
            "value_key_source",
            "required_window_presence",
            "completeness_source",
        ),
        capability_id=capability_id,
        field="parameters",
    )
    if (
        parameters["value_key_source"] != "requested_metric"
        or parameters["completeness_source"] != "bound_input"
    ):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:parameters"
        )
    required_window_presence = parameters["required_window_presence"]
    if required_window_presence not in {"all", "reconciled_zero_fill"}:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:"
            "parameters.required_window_presence"
        )
    contract = _single_query_family(
        contracts, _binding_query_family(binding, "primary", capability_id)
    )
    channel_key = _binding_string(fields, "channel_key", capability_id)
    window_id_key = _binding_string(fields, "window_id_key", capability_id)
    observation_key = _binding_string(fields, "observation_key", capability_id)
    _require_query_metric(contract, metric_id, capability_id)
    _require_query_fields(
        contract,
        (metric_id, channel_key, window_id_key, observation_key),
        capability_id,
    )
    result, report = _query_result_and_report(
        contract,
        result_by_query=result_by_query,
        report_by_query=report_by_query,
    )
    reconciliation_status = _overall_channel_reconciliation_status(report)
    return {
        "rows": _rows_for_contract(bound, execution_plan, contract),
        "metric_id": metric_id,
        "value_key": metric_id,
        "channel_key": channel_key,
        "window_id_key": window_id_key,
        "observation_key": observation_key,
        "required_window_ids": tuple(contract.window_refs),
        "required_window_presence": required_window_presence,
        "completeness_records": (
            {
                "result_ref": result.result_ref,
                "completeness_report_ref": report.report_ref,
                "completeness_status": report.completeness_status,
                "analysis_readiness": report.analysis_readiness,
                "reconciliation_status": reconciliation_status,
            },
        ),
    }


def _source_reconciliation_payload(
    *,
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    contracts: Sequence[QueryContract],
    result_by_query: Mapping[str, Any],
    metric_id: str,
    binding: Mapping[str, Any],
    capability_id: str,
) -> Mapping[str, Any]:
    fields = _binding_mapping(binding, "fields", capability_id)
    parameters = _binding_mapping(binding, "parameters", capability_id)
    _require_exact_binding_keys(
        fields,
        ("join_keys", "value_key", "window_id_key", "window_role_key"),
        capability_id=capability_id,
        field="fields",
    )
    _require_exact_binding_keys(
        parameters,
        (
            "authoritative_source_id",
            "bounded_change_residual_share",
            "bounded_window_relative_tolerance",
            "context_only_resolution",
            "hard_observation_relative_limit",
            "partition_source_id",
            "reconciliation_contract",
            "required_source_count",
            "strategy_source",
            "tolerance_source",
        ),
        capability_id=capability_id,
        field="parameters",
    )
    if (
        parameters["required_source_count"] != 2
        or parameters["tolerance_source"] != "metric_contract"
        or parameters["strategy_source"] != "metric_contract"
        or parameters["context_only_resolution"]
        != "current_window_reconciliation"
    ):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:parameters"
        )
    join_keys = _binding_sequence(fields, "join_keys", capability_id)
    value_key = _binding_string(fields, "value_key", capability_id)
    window_id_key = _binding_string(fields, "window_id_key", capability_id)
    window_role_key = _binding_string(fields, "window_role_key", capability_id)
    if window_id_key not in join_keys:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:window_id_key"
        )
    policy = {
        "contract_id": _binding_string(
            parameters, "reconciliation_contract", capability_id
        ),
        "authoritative_source_id": _binding_string(
            parameters, "authoritative_source_id", capability_id
        ),
        "partition_source_id": _binding_string(
            parameters, "partition_source_id", capability_id
        ),
        "window_id_key": window_id_key,
        "window_role_key": window_role_key,
        "bounded_window_relative_tolerance": _binding_bounded_ratio(
            parameters,
            "bounded_window_relative_tolerance",
            capability_id=capability_id,
        ),
        "bounded_change_residual_share": _binding_bounded_ratio(
            parameters,
            "bounded_change_residual_share",
            capability_id=capability_id,
        ),
        "hard_observation_relative_limit": _binding_bounded_ratio(
            parameters,
            "hard_observation_relative_limit",
            capability_id=capability_id,
        ),
    }
    if (
        policy["authoritative_source_id"] == policy["partition_source_id"]
        or policy["bounded_window_relative_tolerance"]
        > policy["hard_observation_relative_limit"]
    ):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:parameters"
        )
    family_contracts = _query_family_contracts(
        contracts, _binding_query_family(binding, "primary", capability_id)
    )
    if len(family_contracts) != 2:
        raise AuthoritativeTaskInputContractError(
            "authoritative_source_reconciliation_source_count_invalid"
        )
    sources = []
    metric_contracts = set()
    source_ids = set()
    for contract in family_contracts:
        metric_binding = _single_contract_metric_binding(
            contract, metric_id=metric_id, capability_id=capability_id
        )
        _require_query_fields(
            contract,
            (*join_keys, window_role_key, value_key),
            capability_id,
        )
        if metric_binding.dataset_id in source_ids:
            raise AuthoritativeTaskInputContractError(
                "authoritative_source_reconciliation_source_duplicate"
            )
        tolerance = Decimal(str(metric_binding.reconciliation_tolerance))
        if not tolerance.is_finite() or tolerance < 0:
            raise AuthoritativeTaskInputContractError(
                "authoritative_source_reconciliation_tolerance_invalid"
            )
        strategy = metric_binding.reconciliation_strategy
        if not isinstance(strategy, str) or not strategy:
            raise AuthoritativeTaskInputContractError(
                "authoritative_source_reconciliation_strategy_invalid"
            )
        metric_contracts.add((metric_binding.contract_ref, tolerance, strategy))
        result = result_by_query.get(contract.query_contract_id)
        if result is None:
            raise EvidenceIntegrityError("authoritative_query_result_missing")
        sources.append(
            {
                "source_id": metric_binding.dataset_id,
                "result_ref": result.result_ref,
                "metric_contract_ref": metric_binding.contract_ref,
                "reconciliation_tolerance": tolerance,
                "reconciliation_strategy": strategy,
                "rows": _rows_for_contract(bound, execution_plan, contract),
            }
        )
        source_ids.add(metric_binding.dataset_id)
    if len(metric_contracts) != 1:
        raise AuthoritativeTaskInputContractError(
            "authoritative_source_reconciliation_metric_contract_inconsistent"
        )
    metric_contract_ref, tolerance, strategy = next(iter(metric_contracts))
    if not metric_contract_ref:
        raise AuthoritativeTaskInputContractError(
            "authoritative_source_reconciliation_metric_contract_invalid"
        )
    sources.sort(key=lambda item: item["source_id"])
    if {source["source_id"] for source in sources} != {
        policy["authoritative_source_id"],
        policy["partition_source_id"],
    }:
        raise AuthoritativeTaskInputContractError(
            "authoritative_source_reconciliation_policy_source_mismatch"
        )
    return {
        "sources": tuple(sources),
        "join_keys": join_keys,
        "value_key": value_key,
        "reconciliation_tolerance": tolerance,
        "reconciliation_strategy": strategy,
        "reconciliation_policy": policy,
    }


def _binding_bounded_ratio(
    value: Mapping[str, Any],
    field: str,
    *,
    capability_id: str,
) -> Decimal:
    try:
        normalized = Decimal(str(value[field]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:{field}"
        ) from exc
    if not normalized.is_finite() or normalized < 0 or normalized > 1:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:{field}"
        )
    return normalized


def _single_contract_metric_binding(
    contract: QueryContract,
    *,
    metric_id: str,
    capability_id: str,
) -> Any:
    matches = tuple(
        item for item in contract.metric_bindings if item.metric_id == metric_id
    )
    if len(matches) != 1:
        _raise_task_payload_gap(
            capability_id=capability_id,
            missing_fields=(metric_id,),
            contract=contract,
        )
    return matches[0]


def _query_result_and_report(
    contract: QueryContract,
    *,
    result_by_query: Mapping[str, Any],
    report_by_query: Mapping[str, Any],
) -> tuple[Any, Any]:
    result = result_by_query.get(contract.query_contract_id)
    report = report_by_query.get(contract.query_contract_id)
    if result is None or report is None:
        raise EvidenceIntegrityError("authoritative_query_result_or_report_missing")
    if (
        result.query_contract_ref != contract.query_contract_id
        or report.query_contract_ref != contract.query_contract_id
        or report.result_ref != result.result_ref
        or report.report_ref != result.completeness_report_ref
    ):
        raise EvidenceIntegrityError("authoritative_query_result_report_link_mismatch")
    return result, report


def _overall_channel_reconciliation_status(report: Any) -> str:
    assertions = tuple(
        item
        for item in report.assertion_results
        if isinstance(item, Mapping)
        and item.get("assertion") == "overall_channel_reconciliation"
    )
    if len(assertions) != 1:
        raise EvidenceIntegrityError(
            "authoritative_channel_reconciliation_assertion_missing"
        )
    assertion = assertions[0]
    if assertion.get("passed") is True:
        return "passed"
    details = assertion.get("details")
    if isinstance(details, Mapping) and details.get("status") == "pending":
        return "pending"
    return "failed"


def _baseline_window_id(contracts: Sequence[QueryContract]) -> str:
    baseline_ids = tuple(
        dict.fromkeys(
            window.window_id
            for contract in contracts
            for window in contract.resolved_windows
            if window.role == "baseline"
        )
    )
    if len(baseline_ids) != 1:
        raise AuthoritativeTaskInputContractError(
            "authoritative_baseline_window_cardinality_invalid"
        )
    return baseline_ids[0]


def _calendar_partition_role_frame(
    contract: QueryContract,
) -> Mapping[str, Any] | None:
    raw = contract.query_parameters.get("calendar_partition_role_frame")
    if raw is None:
        return None
    try:
        return validate_calendar_partition_role_frame(raw)
    except TemporalComparisonContractError as exc:
        raise AuthoritativeTaskInputContractError(
            "authoritative_calendar_partition_role_frame_invalid"
        ) from exc


def _comparison_baseline_window_id(contract: QueryContract) -> str:
    if _calendar_partition_role_frame(contract) is None:
        return _baseline_window_id((contract,))
    targets = tuple(
        window for window in contract.resolved_windows if window.role == "target"
    )
    if len(targets) != 1:
        raise AuthoritativeTaskInputContractError(
            "authoritative_calendar_partition_window_cardinality_invalid"
        )
    return f"{targets[0].window_id}:partition:baseline"


def _formula_payload(
    *,
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    contracts: Sequence[QueryContract],
    metric_id: str,
    baseline_id: str,
    registry: Any,
    binding: Mapping[str, Any],
    capability_id: str,
) -> Mapping[str, Any]:
    contract = _single_query_family(
        contracts, _binding_query_family(binding, "primary", capability_id)
    )
    rows = _rows_for_contract(bound, execution_plan, contract)
    metric_contract_ref = str(registry.metric(metric_id).get("contract_ref") or "")
    contract_path_text = metric_contract_ref.split("|", 1)[0].split("#", 1)[0]
    if "@" in contract_path_text:
        contract_path_text = contract_path_text.rsplit("@", 1)[0]
    if not contract_path_text:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_formula_contract_ref_missing:{metric_id}"
        )
    contract_path = Path(contract_path_text)
    if not contract_path.is_absolute():
        contract_path = Path(__file__).resolve().parents[2] / contract_path
    graph = load_formula_graph(contract_path)
    if graph.metric_id != metric_id:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_formula_metric_contract_mismatch:{metric_id}"
        )
    capability_contract = registry.capability_inputs(capability_id)
    path_id = capability_contract.get("formula_path_id")
    if not isinstance(path_id, str) or not path_id or path_id != path_id.strip():
        raise AuthoritativeTaskInputContractError(
            f"authoritative_formula_path_contract_missing:{capability_id}"
        )
    path = graph.path(path_id)
    available_metrics = {binding.metric_id for binding in contract.metric_bindings}
    path_metrics = formula_metric_ids(path.runtime_ast)
    factor_ids = tuple(item for item in path_metrics if item != metric_id)
    if (
        metric_id not in path_metrics
        or not factor_ids
        or not set(factor_ids) <= available_metrics
    ):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_formula_path_unbound:{capability_id}:{path_id}"
        )
    aggregate_result = (
        contract.result_shape.result_semantics == "complete_window_aggregate"
    )
    partition_frame = _calendar_partition_role_frame(contract)
    if partition_frame is not None:
        targets = tuple(
            item for item in contract.resolved_windows if item.role == "target"
        )
        expected_baseline_id = _comparison_baseline_window_id(contract)
        if (
            len(targets) != 1
            or baseline_id != expected_baseline_id
            or not aggregate_result
        ):
            raise AuthoritativeTaskInputContractError(
                "authoritative_formula_window_cardinality_invalid"
            )

        def value_for(role: str, selected_metric: str) -> Decimal:
            return _partition_role_metric_value(
                rows,
                contract=contract,
                role=role,
                metric_id=selected_metric,
            )

    else:
        target_windows = tuple(
            item for item in contract.resolved_windows if item.role == "target"
        )
        baseline_windows = tuple(
            item for item in contract.resolved_windows if item.window_id == baseline_id
        )
        if len(target_windows) != 1 or len(baseline_windows) != 1:
            raise AuthoritativeTaskInputContractError(
                "authoritative_formula_window_cardinality_invalid"
            )

        def value_for(role: str, selected_metric: str) -> Decimal:
            window = (
                target_windows[0] if role == "target" else baseline_windows[0]
            )
            return _window_metric_value(
                rows,
                window,
                selected_metric,
                aggregate_result=aggregate_result,
            )

    target_metrics = {
        item: value_for("target", item)
        for item in factor_ids
    }
    baseline_metrics = {
        item: value_for("baseline", item)
        for item in factor_ids
    }
    return {
        "formula_path_id": path.path_id,
        "formula_contract_ref": metric_contract_ref,
        "formula_ast": path.runtime_ast,
        "baseline_metrics": baseline_metrics,
        "target_metrics": target_metrics,
        "factor_metric_ids": factor_ids,
        "factor_groupings": tuple(
            {
                "grouping_id": grouping.grouping_id,
                "method": grouping.method,
                "groups": tuple(
                    {
                        "factor_id": group.factor_id,
                        "member_metric_ids": group.member_metric_ids,
                    }
                    for group in grouping.groups
                ),
            }
            for grouping in path.contribution_groupings
        ),
        "observed_baseline": value_for("baseline", metric_id),
        "observed_target": value_for("target", metric_id),
        "absolute_tolerance": path.absolute_tolerance,
        "relative_tolerance": path.relative_tolerance,
    }


def _funnel_decomposition_payload(
    *,
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    contracts: Sequence[QueryContract],
    binding: Mapping[str, Any],
    capability_id: str,
) -> Mapping[str, Any]:
    contract = _single_query_family(
        contracts, _binding_query_family(binding, "primary", capability_id)
    )
    fields = _binding_mapping(binding, "fields", capability_id)
    parameters = _binding_mapping(binding, "parameters", capability_id)
    _require_exact_binding_keys(
        fields,
        ("window_id_key", "window_role_key", "stages"),
        capability_id=capability_id,
        field="fields",
    )
    _require_exact_binding_keys(
        parameters,
        (
            "source_grain",
            "lifetime_first_payment_supported",
            "rate_reconciliation_tolerance",
        ),
        capability_id=capability_id,
        field="parameters",
    )
    raw_stages = fields.get("stages")
    if (
        isinstance(raw_stages, (str, bytes))
        or not isinstance(raw_stages, Sequence)
        or not raw_stages
        or any(
            not isinstance(item, Mapping)
            or set(item)
            != {
                "stage_id",
                "numerator_metric",
                "denominator_metric",
                "rate_metric",
            }
            for item in raw_stages
        )
    ):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:stages"
        )
    tolerance = parameters.get("rate_reconciliation_tolerance")
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not 0 <= float(tolerance) <= 1
        or type(parameters.get("source_grain")) is not str
        or not parameters["source_grain"]
        or type(parameters.get("lifetime_first_payment_supported")) is not bool
    ):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:parameters"
        )
    rows = _rows_for_contract(bound, execution_plan, contract)
    aggregate_result = (
        contract.result_shape.result_semantics == "complete_window_aggregate"
    )
    partition_frame = _calendar_partition_role_frame(contract)
    if partition_frame is not None:
        targets = tuple(
            item for item in contract.resolved_windows if item.role == "target"
        )
        if len(targets) != 1 or not aggregate_result:
            raise AuthoritativeTaskInputContractError(
                "authoritative_funnel_window_cardinality_invalid"
            )
        target_window_ref = f"{targets[0].window_id}:partition:target"
        baseline_window_ref = _comparison_baseline_window_id(contract)

        def value_for(role: str, selected_metric: str) -> Decimal:
            return _partition_role_metric_value(
                rows,
                contract=contract,
                role=role,
                metric_id=selected_metric,
            )

    else:
        target_windows = tuple(
            item for item in contract.resolved_windows if item.role == "target"
        )
        baseline_id = _baseline_window_id((contract,))
        baseline_windows = tuple(
            item for item in contract.resolved_windows
            if item.window_id == baseline_id
        )
        if len(target_windows) != 1 or len(baseline_windows) != 1:
            raise AuthoritativeTaskInputContractError(
                "authoritative_funnel_window_cardinality_invalid"
            )
        target_window_ref = target_windows[0].window_id
        baseline_window_ref = baseline_windows[0].window_id

        def value_for(role: str, selected_metric: str) -> Decimal:
            window = (
                target_windows[0] if role == "target" else baseline_windows[0]
            )
            return _window_metric_value(
                rows,
                window,
                selected_metric,
                aggregate_result=aggregate_result,
            )

    stages = []
    for raw_stage in raw_stages:
        stage = {str(key): str(value) for key, value in raw_stage.items()}
        for metric_id in (
            stage["numerator_metric"],
            stage["denominator_metric"],
            stage["rate_metric"],
        ):
            _require_query_metric(contract, metric_id, capability_id)
        values: dict[str, Any] = {}
        for prefix in ("target", "baseline"):
            numerator = value_for(prefix, stage["numerator_metric"])
            denominator = value_for(prefix, stage["denominator_metric"])
            reported_rate = value_for(prefix, stage["rate_metric"])
            recomputed_rate = None if denominator == 0 else numerator / denominator
            reconciled = (
                recomputed_rate is None
                and reported_rate is None
                or recomputed_rate is not None
                and reported_rate is not None
                and abs(float(recomputed_rate) - float(reported_rate))
                <= float(tolerance)
            )
            values.update(
                {
                    f"{prefix}_numerator": numerator,
                    f"{prefix}_denominator": denominator,
                    f"{prefix}_rate": reported_rate,
                    f"{prefix}_recomputed_rate": recomputed_rate,
                    f"{prefix}_reconciled": reconciled,
                }
            )
        stages.append({**stage, **values})
    return {
        "contract_id": "new-user-funnel-decomposition.v1",
        "source_grain": parameters["source_grain"],
        "lifetime_first_payment_supported": parameters[
            "lifetime_first_payment_supported"
        ],
        "target_window_ref": target_window_ref,
        "baseline_window_ref": baseline_window_ref,
        "stages": tuple(stages),
    }


def _window_metric_value(
    rows: Sequence[Mapping[str, Any]],
    window: ResolvedWindow,
    metric_id: str,
    *,
    aggregate_result: bool = False,
) -> Decimal:
    values = []
    observation_keys = set()
    for row in rows:
        if row.get("window_id") != window.window_id:
            continue
        observation_key = str(row.get("observation_key") or "")
        if not observation_key or observation_key in observation_keys:
            raise EvidenceIntegrityError(
                f"authoritative_window_observation_invalid:{window.window_id}"
            )
        observation_keys.add(observation_key)
        try:
            value = Decimal(str(row[metric_id]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(
                f"authoritative_window_metric_invalid:{window.window_id}:{metric_id}"
            ) from exc
        if not value.is_finite():
            raise EvidenceIntegrityError(
                f"authoritative_window_metric_invalid:{window.window_id}:{metric_id}"
            )
        values.append(value)
    if aggregate_result:
        if len(values) != 1 or observation_keys != {window.window_id}:
            raise EvidenceIntegrityError(
                f"authoritative_window_aggregate_invalid:{window.window_id}:{metric_id}"
            )
        return values[0]
    if len(values) != window.required_complete_days:
        raise EvidenceIntegrityError(
            f"authoritative_window_metric_incomplete:{window.window_id}:{metric_id}"
        )
    if window.aggregation == "daily_total":
        if len(values) != 1:
            raise EvidenceIntegrityError(
                f"authoritative_window_daily_total_invalid:{window.window_id}"
            )
        return values[0]
    if window.aggregation == "sum_of_complete_days":
        return sum(values, Decimal(0))
    if window.aggregation == "mean_of_complete_days":
        return sum(values, Decimal(0)) / Decimal(len(values))
    if window.aggregation == "daily_series":
        raise AuthoritativeTaskInputContractError(
            f"authoritative_formula_series_window_unsupported:{window.window_id}"
        )
    raise AuthoritativeTaskInputContractError(
        f"authoritative_window_aggregation_unsupported:{window.aggregation}"
    )


def _partition_role_metric_value(
    rows: Sequence[Mapping[str, Any]],
    *,
    contract: QueryContract,
    role: str,
    metric_id: str,
) -> Decimal:
    if role not in {"target", "baseline"}:
        raise AuthoritativeTaskInputContractError(
            "authoritative_calendar_partition_role_invalid"
        )
    targets = tuple(
        item for item in contract.resolved_windows if item.role == "target"
    )
    matches = tuple(
        row
        for row in rows
        if row.get("window_role") == role
        and len(targets) == 1
        and row.get("window_id") == targets[0].window_id
        and row.get("observation_key") == targets[0].window_id
    )
    if len(matches) != 1:
        raise EvidenceIntegrityError(
            f"authoritative_partition_role_aggregate_invalid:{role}:{metric_id}"
        )
    try:
        value = Decimal(str(matches[0][metric_id]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(
            f"authoritative_partition_role_metric_invalid:{role}:{metric_id}"
        ) from exc
    if not value.is_finite():
        raise EvidenceIntegrityError(
            f"authoritative_partition_role_metric_invalid:{role}:{metric_id}"
        )
    return value


def _rows_by_dimension(
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    query_by_ref: Mapping[str, QueryContract],
) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
    output: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for slot in (
        *execution_plan.required_input_slots,
        *execution_plan.optional_input_slots,
    ):
        if slot.slot_id not in bound.rows_by_slot:
            continue
        if len(slot.query_contract_refs) != 1:
            raise EvidenceIntegrityError(
                f"authoritative_dimension_query_cardinality_invalid:{slot.slot_id}"
            )
        contract = query_by_ref[slot.query_contract_refs[0]]
        if len(contract.dimension_bindings) != 1:
            raise AuthoritativeTaskInputContractError(
                f"authoritative_independent_dimension_contract_invalid:{slot.slot_id}"
            )
        dimension_id = contract.dimension_bindings[0].dimension_id
        if dimension_id in output:
            raise EvidenceIntegrityError(
                f"authoritative_dimension_query_duplicated:{dimension_id}"
            )
        output[dimension_id] = tuple(
            dict(item) for item in bound.rows_by_slot[slot.slot_id]
        )
    if not output:
        raise AuthoritativeTaskInputContractError(
            "authoritative_dimension_rows_missing"
        )
    return output


def _overall_by_group(
    rows_by_dimension: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    metric_id: str,
    group_key: str,
    target_group: str,
    baseline_group: str,
) -> Mapping[str, Decimal]:
    totals_by_dimension: dict[str, dict[str, Decimal]] = {}
    for dimension_id, rows in rows_by_dimension.items():
        totals: dict[str, Decimal] = {}
        for row in rows:
            role = str(row.get(group_key) or "")
            if role not in {target_group, baseline_group}:
                continue
            try:
                value = Decimal(str(row[metric_id]))
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise EvidenceIntegrityError(
                    f"authoritative_dimension_metric_invalid:{dimension_id}:{metric_id}"
                ) from exc
            totals[role] = totals.get(role, Decimal(0)) + value
        totals_by_dimension[dimension_id] = totals
    first = next(iter(totals_by_dimension.values()))
    if set(first) != {target_group, baseline_group} or any(
        item != first for item in totals_by_dimension.values()
    ):
        raise EvidenceIntegrityError(
            "authoritative_dimension_overall_reconciliation_mismatch"
        )
    return first


def _dimension_paths(
    rows_by_dimension: Mapping[str, Any], registry: Any
) -> Mapping[str, tuple[str, ...]]:
    paths = {}
    for dimension_id in rows_by_dimension:
        current = dimension_id
        lineage = [current]
        seen = {current}
        while True:
            parent = str(registry.dimension(current).get("parent_dimension") or "")
            if not parent:
                break
            if parent in seen:
                raise AuthoritativeTaskInputContractError(
                    f"authoritative_dimension_hierarchy_cycle:{dimension_id}"
                )
            lineage.insert(0, parent)
            seen.add(parent)
            current = parent
        paths[dimension_id] = tuple(lineage)
    return paths


def _normalized_independent_dimension_rows(
    rows_by_dimension: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    output_key: str,
) -> tuple[Mapping[str, Any], ...]:
    normalized = []
    for dimension_id, rows in rows_by_dimension.items():
        for row in rows:
            member = row.get(dimension_id)
            if member in (None, ""):
                continue
            normalized.append(
                {
                    **dict(row),
                    output_key: f"{dimension_id}:{member}",
                }
            )
    if not normalized:
        raise AuthoritativeTaskInputContractError(
            "authoritative_dimension_members_missing"
        )
    return tuple(normalized)


def _cross_source_association_payload(
    *,
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    contracts: Sequence[QueryContract],
    binding: Mapping[str, Any],
    capability_id: str,
) -> Mapping[str, Any]:
    outcome = _single_query_family(
        contracts, _binding_query_family(binding, "outcome", capability_id)
    )
    candidate = _single_query_family(
        contracts, _binding_query_family(binding, "candidate", capability_id)
    )
    fields = _binding_mapping(binding, "fields", capability_id)
    target_key = _binding_string(fields, "target_key", capability_id)
    time_key = _binding_string(fields, "time_key", capability_id)
    candidate_keys = _binding_sequence(binding, "candidate_keys", capability_id)
    join_keys = _binding_sequence(binding, "join_keys", capability_id)
    _require_query_fields(outcome, (*join_keys, target_key, time_key), capability_id)
    _require_query_fields(
        candidate, (*join_keys, *candidate_keys, time_key), capability_id
    )
    rows = _merge_query_rows(
        (
            _capability_context_rows(
                _rows_for_contract(bound, execution_plan, outcome),
                resolved_windows=outcome.resolved_windows,
                capability_id=capability_id,
            ),
            _capability_context_rows(
                _rows_for_contract(bound, execution_plan, candidate),
                resolved_windows=candidate.resolved_windows,
                capability_id=capability_id,
            ),
        ),
        join_keys=join_keys,
        capability_id=capability_id,
    )
    return {
        "rows": rows,
        "target_key": target_key,
        "candidate_keys": candidate_keys,
        "time_key": time_key,
        **dict(_binding_mapping(binding, "parameters", capability_id, required=False)),
    }


def _cross_source_panel_payload(
    *,
    bound: BoundCapabilityInput,
    execution_plan: CapabilityExecutionPlan,
    contracts: Sequence[QueryContract],
    binding: Mapping[str, Any],
    capability_id: str,
    registry: Any,
) -> Mapping[str, Any]:
    outcome = _single_query_family(
        contracts, _binding_query_family(binding, "outcome", capability_id)
    )
    candidate = _single_query_family(
        contracts, _binding_query_family(binding, "candidate", capability_id)
    )
    fields = _binding_mapping(binding, "fields", capability_id)
    time_key = _binding_string(fields, "time_key", capability_id)
    panel_key = _binding_string(fields, "panel_key", capability_id)
    hypothesis = _binding_mapping(binding, "hypothesis", capability_id)
    outcome_key = _binding_string(hypothesis, "outcome_key", capability_id)
    candidate_key = _binding_string(hypothesis, "candidate_key", capability_id)
    transform = _binding_string(hypothesis, "transform", capability_id)
    lag = hypothesis.get("lag")
    if type(lag) is not int or lag < 0:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_task_input_binding_invalid:{capability_id}:hypothesis.lag"
        )
    join_keys = _binding_sequence(binding, "join_keys", capability_id)
    _require_query_fields(
        outcome, (*join_keys, time_key, panel_key, outcome_key), capability_id
    )
    _require_query_fields(
        candidate,
        (*join_keys, time_key, panel_key, candidate_key),
        capability_id,
    )
    capability_contract = registry.capability_inputs(capability_id)
    alignment = capability_contract.get("channel_alignment_policy")
    if not isinstance(alignment, Mapping):
        raise AuthoritativeTaskInputContractError(
            f"authoritative_panel_alignment_contract_missing:{capability_id}"
        )
    mapping_authority_status = _binding_string(
        alignment, "review_status", capability_id
    )
    rows, mapping_coverage, mapping_coverage_basis = _align_cross_source_panel_rows(
        _capability_context_rows(
            _rows_for_contract(bound, execution_plan, outcome),
            resolved_windows=outcome.resolved_windows,
            capability_id=capability_id,
        ),
        _capability_context_rows(
            _rows_for_contract(bound, execution_plan, candidate),
            resolved_windows=candidate.resolved_windows,
            capability_id=capability_id,
        ),
        join_keys=join_keys,
        capability_id=capability_id,
    )
    return {
        "rows": rows,
        "time_key": time_key,
        "panel_key": panel_key,
        "hypothesis": {
            "outcome_key": outcome_key,
            "candidate_key": candidate_key,
            "transform": transform,
            "lag": lag,
        },
        "mapping_authority_status": mapping_authority_status,
        "mapping_coverage": mapping_coverage,
        "mapping_coverage_basis": mapping_coverage_basis,
        **dict(_binding_mapping(binding, "parameters", capability_id, required=False)),
    }


def _capability_context_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    resolved_windows: Sequence[ResolvedWindow],
    capability_id: str,
) -> tuple[Mapping[str, Any], ...]:
    context_window_ids = tuple(
        window.window_id
        for window in resolved_windows
        if capability_id in window.capability_refs
    )
    if len(context_window_ids) != 1:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_capability_context_window_invalid:{capability_id}"
        )
    context_window_id = context_window_ids[0]
    return tuple(row for row in rows if row.get("window_id") == context_window_id)


def _align_cross_source_panel_rows(
    outcome_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    join_keys: Sequence[str],
    capability_id: str,
) -> tuple[tuple[Mapping[str, Any], ...], float, Mapping[str, Any]]:
    indexes: list[dict[tuple[Any, ...], Mapping[str, Any]]] = []
    for rows in (outcome_rows, candidate_rows):
        index: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for row in rows:
            key = tuple(row.get(field) for field in join_keys)
            if any(item in (None, "") for item in key):
                raise EvidenceIntegrityError(
                    f"authoritative_cross_source_join_key_missing:{capability_id}"
                )
            try:
                duplicated = key in index
            except TypeError as exc:
                raise EvidenceIntegrityError(
                    f"authoritative_cross_source_join_key_invalid:{capability_id}"
                ) from exc
            if duplicated:
                raise EvidenceIntegrityError(
                    f"authoritative_cross_source_join_key_duplicated:{capability_id}"
                )
            index[key] = row
        indexes.append(index)

    matched_keys = set(indexes[0]) & set(indexes[1])
    merged: list[Mapping[str, Any]] = []
    for key in sorted(matched_keys, key=canonical_digest):
        item: dict[str, Any] = {}
        for index in indexes:
            for field, value in index[key].items():
                if field in item and item[field] != value:
                    raise EvidenceIntegrityError(
                        "authoritative_cross_source_field_conflict:"
                        f"{capability_id}:{field}"
                    )
                item[field] = value
        merged.append(item)

    matched_count = len(matched_keys)
    source_names = ("outcome", "candidate")
    coverage_basis: dict[str, Mapping[str, Any]] = {}
    source_coverages: list[float] = []
    for source_name, index in zip(source_names, indexes, strict=True):
        total_count = len(index)
        coverage = matched_count / total_count if total_count else 0.0
        source_coverages.append(coverage)
        coverage_basis[source_name] = {
            "total_cells": total_count,
            "matched_cells": matched_count,
            "coverage": coverage,
        }
    mapping_coverage = min(source_coverages)
    return tuple(merged), mapping_coverage, coverage_basis


def _merge_query_rows(
    row_sets: Sequence[Sequence[Mapping[str, Any]]],
    *,
    join_keys: Sequence[str],
    capability_id: str,
) -> tuple[Mapping[str, Any], ...]:
    indexes: list[dict[tuple[Any, ...], Mapping[str, Any]]] = []
    all_keys: set[tuple[Any, ...]] = set()
    for rows in row_sets:
        index: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for row in rows:
            key = tuple(row.get(field) for field in join_keys)
            if any(item in (None, "") for item in key):
                raise EvidenceIntegrityError(
                    f"authoritative_cross_source_join_key_missing:{capability_id}"
                )
            try:
                duplicated = key in index
            except TypeError as exc:
                raise EvidenceIntegrityError(
                    f"authoritative_cross_source_join_key_invalid:{capability_id}"
                ) from exc
            if duplicated:
                raise EvidenceIntegrityError(
                    f"authoritative_cross_source_join_key_duplicated:{capability_id}"
                )
            index[key] = row
            all_keys.add(key)
        indexes.append(index)
    merged = []
    for key in sorted(all_keys, key=canonical_digest):
        item: dict[str, Any] = {}
        for index in indexes:
            row = index.get(key)
            if row is None:
                continue
            for field, value in row.items():
                if field in item and item[field] != value:
                    raise EvidenceIntegrityError(
                        "authoritative_cross_source_field_conflict:"
                        f"{capability_id}:{field}"
                    )
                item[field] = value
        merged.append(item)
    if not merged:
        raise EvidenceIntegrityError(
            f"authoritative_cross_source_rows_empty:{capability_id}"
        )
    return tuple(merged)


def _pattern_payload(
    *,
    capability_id: str,
    rows: tuple[Mapping[str, Any], ...],
    contracts: Sequence[QueryContract],
    metric_id: str,
    binding: Mapping[str, Any],
    temporal_authority: EffectiveTemporalComparison,
) -> Mapping[str, Any]:
    mode = _binding_string(binding, "pattern_mode", capability_id)
    fields = _binding_mapping(binding, "fields", capability_id)
    parameters = _binding_mapping(binding, "parameters", capability_id, required=False)
    query_family = _binding_query_family(binding, "primary", capability_id)
    contract = _single_query_family(contracts, query_family)
    if mode == "rolling":
        observation_key = _binding_string(fields, "observation_key", capability_id)
        window_role_key = _binding_string(fields, "window_role_key", capability_id)
        target_role = _binding_string(fields, "target_role", capability_id)
        baseline_role = _binding_string(fields, "baseline_role", capability_id)
        context_role = _binding_string(fields, "context_role", capability_id)
        value_key = _binding_string(fields, "value_key", capability_id)
        _require_query_fields(
            contract,
            (observation_key, window_role_key, metric_id),
            capability_id,
        )
        try:
            rolling_strategy = resolve_rolling_window_strategy(
                temporal_authority,
                parameters=parameters,
            )
        except TemporalComparisonContractError as exc:
            raise AuthoritativeTaskInputContractError(
                "authoritative_rolling_parameters_invalid"
            ) from exc
        capability_parameters = rolling_strategy.capability_parameters()
        if len({target_role, baseline_role, context_role}) != 3:
            raise AuthoritativeTaskInputContractError(
                "authoritative_rolling_window_roles_invalid"
            )
        series_by_day: dict[date, Mapping[str, Any]] = {}
        target_days: set[date] = set()
        context_days: set[date] = set()
        for item in rows:
            role = item.get(window_role_key)
            if role not in {target_role, context_role}:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_rolling_window_role_invalid"
                )
            raw_observation = item.get(observation_key)
            if type(raw_observation) is not str:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_rolling_observation_invalid"
                )
            try:
                observed_on = date.fromisoformat(raw_observation)
            except ValueError as exc:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_rolling_observation_invalid"
                ) from exc
            if observed_on in series_by_day:
                raise AuthoritativeTaskInputContractError(
                    "authoritative_rolling_observation_duplicate"
                )
            series_by_day[observed_on] = {
                observation_key: raw_observation,
                value_key: item.get(metric_id),
            }
            (target_days if role == target_role else context_days).add(observed_on)
        if not target_days or not context_days or max(context_days) >= min(target_days):
            raise AuthoritativeTaskInputContractError(
                "authoritative_rolling_context_window_invalid"
            )
        rolling_step_days = capability_parameters["rolling_step_days"]
        if (
            isinstance(rolling_step_days, bool)
            or not isinstance(rolling_step_days, int)
            or rolling_step_days <= 0
        ):
            raise AuthoritativeTaskInputContractError(
                "authoritative_rolling_parameters_invalid"
            )
        ordered_series = tuple(
            series_by_day[observed_on] for observed_on in sorted(series_by_day)
        )
        baseline_series = (
            ordered_series[:-rolling_step_days]
            if len(ordered_series) > rolling_step_days
            else ()
        )
        target_series = ordered_series[rolling_step_days:]
        normalized_rows = tuple(
            {
                **dict(item),
                window_role_key: role,
            }
            for role, series in (
                (target_role, target_series),
                (baseline_role, baseline_series),
            )
            for item in series
        )
        return {
            "rows": normalized_rows,
            "observation_key": observation_key,
            "window_role_key": window_role_key,
            "target_role": target_role,
            "baseline_role": baseline_role,
            "value_key": value_key,
            **capability_parameters,
        }
    if mode == "intra_period":
        window_role_key = _binding_string(fields, "window_role_key", capability_id)
        phase_key = _binding_string(fields, "phase_key", capability_id)
        observation_key = _binding_string(fields, "observation_key", capability_id)
        period_key = _binding_string(fields, "period_key", capability_id)
        value_key = _binding_string(fields, "value_key", capability_id)
        _require_query_fields(
            contract,
            (window_role_key, phase_key, observation_key, metric_id),
            capability_id,
        )
        target_phases, baseline_phases = _calendar_partition_members(
            temporal_authority,
            expected_field="month_phase",
        )
        aggregation = _calendar_partition_aggregation(temporal_authority)
        alignment = _calendar_partition_alignment(temporal_authority)
        if any(item.get(window_role_key) != "target" for item in rows):
            raise AuthoritativeTaskInputContractError(
                "authoritative_calendar_partition_window_role_invalid"
            )
        return {
            "rows": tuple(
                {
                    **dict(item),
                    period_key: str(item.get(observation_key) or "")[:7],
                    value_key: item.get(metric_id),
                }
                for item in rows
            ),
            "period_key": period_key,
            "group_key": phase_key,
            "target_phases": target_phases,
            "baseline_phases": baseline_phases,
            **dict(parameters),
            "aggregation": aggregation,
            **alignment,
        }
    if mode == "weekly":
        window_role_key = _binding_string(fields, "window_role_key", capability_id)
        week_key = _binding_string(fields, "week_key", capability_id)
        weekday_key = _binding_string(fields, "weekday_key", capability_id)
        value_key = _binding_string(fields, "value_key", capability_id)
        _require_query_fields(
            contract,
            (window_role_key, week_key, weekday_key, metric_id),
            capability_id,
        )
        target_weekdays, baseline_weekdays = _calendar_partition_members(
            temporal_authority,
            expected_field="iso_weekday",
        )
        aggregation = _calendar_partition_aggregation(temporal_authority)
        alignment = _calendar_partition_alignment(temporal_authority)
        if any(item.get(window_role_key) != "target" for item in rows):
            raise AuthoritativeTaskInputContractError(
                "authoritative_calendar_partition_window_role_invalid"
            )
        return {
            "rows": tuple(
                {**dict(item), value_key: item.get(metric_id)} for item in rows
            ),
            "week_key": week_key,
            "weekday_key": weekday_key,
            "target_weekdays": target_weekdays,
            "baseline_weekdays": baseline_weekdays,
            **dict(parameters),
            "aggregation": aggregation,
            **alignment,
        }
    raise AuthoritativeTaskInputContractError(
        f"authoritative_pattern_payload_contract_missing:{capability_id}"
    )


def _event_temporal_identity(
    temporal_authority: EffectiveTemporalComparison,
) -> dict[str, str]:
    if (
        not isinstance(temporal_authority, EffectiveTemporalComparison)
        or temporal_authority.mode != "event_relative"
        or not temporal_authority.event_ref
        or temporal_authority.baseline_window is None
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_event_temporal_authority_invalid"
        )
    return {
        "event_ref": temporal_authority.event_ref,
        "temporal_authority_ref": temporal_authority.authority_ref,
    }


def _dependent_event_window_policy(
    *,
    plan: PlanRevision,
    source_task: CapabilityTask,
    registry: Any,
) -> Mapping[str, Any] | None:
    candidates: list[Mapping[str, Any]] = []
    for task in plan.capability_tasks:
        if source_task.task_id not in set(task.dependency_task_ids):
            continue
        contract = registry.capability_inputs(task.capability_id)
        raw_policy = contract.get("dynamic_event_window_policy")
        if raw_policy is None:
            continue
        try:
            policy = validate_event_window_derivation_policy(
                raw_policy,
                expected_source_dependency=source_task.capability_id,
            )
        except EventWindowDerivationError as exc:
            raise AuthoritativeTaskInputContractError(
                "authoritative_dynamic_event_window_policy_invalid"
            ) from exc
        candidates.append(policy)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise AuthoritativeTaskInputContractError(
            "authoritative_dynamic_event_window_policy_ambiguous"
        )
    return candidates[0]


def _event_window_metric_comparison_payload(
    *,
    plan: PlanRevision | None = None,
    task: CapabilityTask | None = None,
    bound: BoundCapabilityInput,
    bound_by_capability: Mapping[str, BoundCapabilityInput] | None = None,
    execution_plan: CapabilityExecutionPlan,
    contracts: Sequence[QueryContract],
    metric_id: str,
    binding: Mapping[str, Any],
    capability_id: str,
    temporal_authority: EffectiveTemporalComparison,
    dynamic_event_window_policy: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    contract = _single_query_family(
        contracts,
        _binding_query_family(binding, "primary", capability_id),
    )
    _require_query_metric(contract, metric_id, capability_id)
    if metric_id == "event_count":
        raise AuthoritativeTaskInputContractError(
            "authoritative_event_window_metric_forbidden"
        )
    if temporal_authority.mode == "calendar_partition":
        if plan is None or task is None:
            raise AuthoritativeTaskInputContractError(
                "authoritative_dynamic_event_window_plan_missing"
            )
        try:
            dynamic_policy = validate_event_window_derivation_policy(
                dynamic_event_window_policy,
            )
        except EventWindowDerivationError as exc:
            raise AuthoritativeTaskInputContractError(
                "authoritative_dynamic_event_window_policy_invalid"
            ) from exc
        dependency_tasks = tuple(
            candidate
            for candidate in plan.capability_tasks
            if candidate.task_id in set(task.dependency_task_ids)
        )
        source_dependency = str(dynamic_policy["source_dependency"])
        if (
            len(dependency_tasks) != 1
            or dependency_tasks[0].capability_id != source_dependency
        ):
            raise AuthoritativeTaskInputContractError(
                "authoritative_dynamic_event_dependency_invalid"
            )
        dependency_bound = (bound_by_capability or {}).get(source_dependency)
        if dependency_bound is None:
            raise AuthoritativeTaskInputContractError(
                "authoritative_dynamic_event_dependency_input_missing"
            )
        if dependency_bound.status not in {"ready", "degraded"}:
            limitation_ref = (
                dependency_bound.reasons[0]
                if dependency_bound.reasons
                else "limitation:dynamic_event_dependency_unavailable"
            )
            raise _TaskPayloadContractGap(
                gap_type="source_unbound",
                limitation_ref=limitation_ref,
                business_boundary=(
                    f"{capability_id}_event_discovery_unavailable"
                ),
            )
        try:
            event_window_set = derive_event_window_set(
                _bound_rows(dependency_bound),
                temporal_authority=temporal_authority,
                policy=dynamic_policy,
            )
        except EventWindowDerivationError as exc:
            raise AuthoritativeTaskInputContractError(
                f"authoritative_event_window_derivation_failed:{exc}"
            ) from exc
        return {
            "contract": contract,
            "rows": _rows_for_contract(bound, execution_plan, contract),
            "metric_id": metric_id,
            "derivation_policy": dynamic_policy,
            "event_window_set": event_window_set,
        }
    identity = _event_temporal_identity(temporal_authority)
    baseline_window_id = _baseline_window_id((contract,))
    try:
        validate_event_window_metric_authority(
            contract,
            temporal_authority,
            primary_baseline_window_id=baseline_window_id,
        )
    except WindowMetricEvidenceError as exc:
        raise AuthoritativeTaskInputContractError(
            f"authoritative_event_window_authority_mismatch:{exc.code}"
        ) from exc
    return {
        "contract": contract,
        "rows": _rows_for_contract(bound, execution_plan, contract),
        "metric_id": metric_id,
        "primary_baseline_window_id": baseline_window_id,
        **identity,
    }


def _calendar_partition_members(
    temporal_authority: EffectiveTemporalComparison,
    *,
    expected_field: str,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    partition = temporal_authority.calendar_partition
    if (
        temporal_authority.mode != "calendar_partition"
        or not isinstance(partition, Mapping)
        or partition.get("partition_field") != expected_field
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_calendar_partition_mismatch"
        )
    target_members = partition.get("target_members")
    baseline_members = partition.get("baseline_members")
    if (
        isinstance(target_members, (str, bytes))
        or not isinstance(target_members, Sequence)
        or not target_members
        or isinstance(baseline_members, (str, bytes))
        or not isinstance(baseline_members, Sequence)
        or not baseline_members
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_calendar_partition_members_invalid"
        )
    targets = tuple(target_members)
    baselines = tuple(baseline_members)
    if (
        len(targets) != len(set(targets))
        or len(baselines) != len(set(baselines))
        or set(targets).intersection(baselines)
    ):
        raise AuthoritativeTaskInputContractError(
            "authoritative_calendar_partition_members_invalid"
        )
    return targets, baselines


def _calendar_partition_aggregation(
    temporal_authority: EffectiveTemporalComparison,
) -> str:
    partition = temporal_authority.calendar_partition
    aggregation = (
        partition.get("aggregation") if isinstance(partition, Mapping) else None
    )
    if aggregation not in {"sum_of_complete_days", "mean_of_complete_days"}:
        raise AuthoritativeTaskInputContractError(
            "authoritative_calendar_partition_aggregation_invalid"
        )
    return str(aggregation)


def _calendar_partition_alignment(
    temporal_authority: EffectiveTemporalComparison,
) -> Mapping[str, str]:
    partition = temporal_authority.calendar_partition
    baseline_class = (
        partition.get("baseline_class")
        if isinstance(partition, Mapping)
        else None
    )
    period_grain = (
        partition.get("period_grain")
        if isinstance(partition, Mapping)
        else None
    )
    if baseline_class not in {
        "custom_control_window",
        "prior_period",
        "same_month_phase",
    } or period_grain not in {"month", "week", "year"}:
        raise AuthoritativeTaskInputContractError(
            "authoritative_calendar_partition_alignment_invalid"
        )
    return {
        "baseline_class": str(baseline_class),
        "period_grain": str(period_grain),
    }


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


__all__ = (
    "AuthoritativeTaskInputContractError",
    "AuthoritativeTaskInputMaterializer",
    "materialize_authoritative_task_inputs",
)
