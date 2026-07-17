from dataclasses import replace
from datetime import datetime

import pytest

from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
    validate_capability_binding_plan_semantics,
)
from bi_agent.runtime.capability_execution import bind_capability_inputs
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
from bi_agent.runtime.dataset_catalog import (
    build_dataset_release_authority_record,
    DatasetCatalog,
    DatasetSnapshot,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.evidence_authority import (
    CompletenessRecord,
    RuntimeEvidenceAuthority,
    _record_completeness,
    canonical_digest,
)
from bi_agent.runtime.query_completeness import validate_query_result
from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
from bi_agent.capabilities import candidate_dimension_screen
from bi_agent.capabilities.driver_decomposition import driver_decomposition


RUNTIME_BINDINGS = "contracts/runtime/clickhouse-analysis-bindings.yaml"


def _paid_order_snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_ref="snapshot:paid_order_success:dimension-screen",
        dataset_id="paid_order_success",
        physical_table="paid_order_success__dimension_screen",
        watermark="2026-07-04",
        schema_fingerprint="schema:paid-order-dimension-screen",
        schema_fields=(
            "business_date_lagos",
            "paid_amount_ngn",
            "user_id",
            "order_id",
            "channel",
            "payment_method",
            "region",
            "device_brand",
            "device_model",
            "is_first_payment",
        ),
        contract_ref="contracts/sources/paid-order-detail.source.yaml@0.1",
        loaded_at="2026-06-03T00:00:00+00:00",
        status="active",
    )


def _released_paid_order_catalog():
    snapshot = _paid_order_snapshot()
    logical_snapshot_id = "paid-order-dimension-screen"
    load_revision = "paid-order-dimension-screen:sha256:test"
    release_ref = dataset_snapshot_release_ref(
        logical_snapshot_id,
        load_revision,
        (snapshot.snapshot_ref,),
    )
    signed = replace(
        snapshot,
        logical_snapshot_id=logical_snapshot_id,
        load_revision=load_revision,
        snapshot_id=logical_snapshot_id,
        release_ref=release_ref,
        rows_content_hash="d" * 64,
    )
    record = build_dataset_release_authority_record(
        ({**signed.to_dict(), "requires_release": True},)
    )
    signed = replace(signed, authority_record_ref=record.authority_record_ref)

    class Resolver:
        def resolve_dataset_release(self, requested_ref):
            if requested_ref != record.release_ref:
                raise KeyError(requested_ref)
            return record

    resolver = Resolver()
    return DatasetCatalog((signed,), release_resolver=resolver), resolver


def test_candidate_dimension_screen_contract_is_independent_and_sample_aware():
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)

    screen = registry.capability_inputs("candidate_dimension_screen")

    assert screen["query_families"] == ["dimension_contribution_scan"]
    assert screen["required_metrics"] == [
        "paid_amount",
        "paid_orders",
        "paid_users",
    ]
    assert (
        registry.query_shape("dimension_contribution_scan")[
            "dimension_presence_policy"
        ]
        == "sparse_allowed"
    )
    assert registry.diagnostic_obligation("factor_topk")[
        "required_capabilities"
    ] == ["candidate_dimension_screen"]


def test_paid_amount_candidate_dimensions_have_distinct_business_meanings():
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)

    assert {
        dimension_id: registry.dimension(dimension_id)["business_name"]
        for dimension_id in (
            "channel",
            "payment_method",
            "region",
            "device_brand",
            "device_model",
        )
    } == {
        "channel": "渠道",
        "payment_method": "支付方式",
        "region": "地区",
        "device_brand": "设备品牌",
        "device_model": "设备型号",
    }
    assert all(
        registry.dimension(dimension_id)["automatic_screening"] == "allowed"
        for dimension_id in (
            "channel",
            "payment_method",
            "region",
            "device_brand",
            "device_model",
        )
    )
    assert registry.dimension("region")["output_policy"] == "aggregate_only"
    assert registry.dimension("device_brand")["hierarchy_id"] == "device_environment"
    assert registry.dimension("device_brand")["hierarchy_level"] == "brand"
    assert registry.dimension("device_model")["hierarchy_id"] == "device_environment"
    assert registry.dimension("device_model")["hierarchy_level"] == "model"
    assert registry.dimension("device_model")["parent_dimension"] == "device_brand"


def test_candidate_dimension_screen_compiles_one_query_per_dimension():
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)
    catalog, release_resolver = _released_paid_order_catalog()
    dimensions = (
        "channel",
        "payment_method",
        "region",
        "device_brand",
        "device_model",
    )

    outcome = compile_analysis_contract(
        run_id="run-candidate-dimension-screen",
        proposal={
            "question_families": ["paid_amount_change_explanation"],
            "target_metrics": ["paid_amount"],
            "requested_dimensions": list(dimensions),
            "baselines": ["previous_day"],
            "claim_intents": ["segment_contribution_or_mix_shift"],
            "scope": {"type": "full_sample"},
        },
        accepted_capabilities=("candidate_dimension_screen",),
        catalog=catalog,
        registry=registry,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        release_resolver=release_resolver,
    )

    dimension_queries = tuple(
        query for query in outcome.query_contracts if query.dimension_bindings
    )
    assert len(dimension_queries) == len(dimensions)
    assert {
        tuple(binding.dimension_id for binding in query.dimension_bindings)
        for query in dimension_queries
    } == {(dimension_id,) for dimension_id in dimensions}
    assert all(
        {binding.metric_id for binding in query.metric_bindings}
        == {"paid_amount", "paid_orders", "paid_users"}
        for query in dimension_queries
    )
    assert len(outcome.capability_plans) == 1
    assert len(outcome.capability_plans[0].required_input_slots) == len(dimensions)


def test_shared_dimension_total_validation_survives_binding_chain_reordering():
    context = _shared_dimension_validation_context()

    bound = bind_capability_inputs(
        context["plan"],
        results=context["results"],
        reports=context["reports"],
        evidence_authority=context["authority"],
        runtime_registry=context["registry"],
        release_resolver=context["release_resolver"],
    )

    assert (bound.status, bound.reasons) == ("ready", ())
    assert len(bound.query_contract_refs) == 2
    assert len(bound.validation_query_contract_refs) == 1
    binding = context["authority"].resolve_capability_binding(
        bound.binding_manifest_ref
    )
    chain = validate_authoritative_query_chain(
        binding,
        resolver=context["authority"],
        rows_loader=context["authority"].rows_loader,
        runtime_registry=context["registry"],
        release_resolver=context["release_resolver"],
    )

    assert len(chain.primary_results) == 2
    assert len(chain.validation_results) == 1
    assert all(report.completeness_status == "complete" for report in chain.primary_reports)
    assert chain.validation_reports[0].completeness_status == "complete"


def test_shared_dimension_query_set_membership_change_fails_closed():
    context = _shared_dimension_validation_context()
    bound = bind_capability_inputs(
        context["plan"],
        results=context["results"],
        reports=context["reports"],
        evidence_authority=context["authority"],
        runtime_registry=context["registry"],
        release_resolver=context["release_resolver"],
    )
    binding = context["authority"].resolve_capability_binding(
        bound.binding_manifest_ref
    )
    original = context["authority"].resolve_completeness(
        binding.completeness_record_refs[0]
    )
    report_payload = dict(original.report_payload)
    coverage = dict(report_payload["coverage_summary"])
    members = tuple(coverage["query_set_contract_refs"])
    assert len(members) == 3
    coverage["query_set_contract_refs"] = members[:-1]
    report_payload["coverage_summary"] = coverage
    report_digest = canonical_digest(report_payload)
    changed = CompletenessRecord(
        record_ref=f"completeness-record:{original.report_ref}:{report_digest}",
        report_ref=original.report_ref,
        query_contract_ref=original.query_contract_ref,
        result_ref=original.result_ref,
        report_digest=report_digest,
        report_payload=report_payload,
    )
    forged = _replace_binding_completeness_record(
        binding,
        original_record_ref=original.record_ref,
        changed=changed,
    )

    class Resolver:
        def resolve_query_execution(self, result_ref):
            return context["authority"].resolve_query_execution(result_ref)

        def resolve_query_execution_record(self, record_ref):
            return context["authority"].resolve_query_execution_record(record_ref)

        def resolve_rows(self, rows_ref):
            return context["authority"].resolve_rows(rows_ref)

        def resolve_rows_record(self, record_ref):
            return context["authority"].resolve_rows_record(record_ref)

        def resolve_snapshot(self, snapshot_ref):
            return context["authority"].resolve_snapshot(snapshot_ref)

        def resolve_completeness(self, record_ref):
            if record_ref == changed.record_ref:
                return changed
            return context["authority"].resolve_completeness(record_ref)

        def resolve_capability_binding(self, binding_ref):
            return context["authority"].resolve_capability_binding(binding_ref)

    with pytest.raises(
        AuthoritativeQueryChainError,
        match="completeness_report_recomputation_mismatch",
    ):
        validate_authoritative_query_chain(
            forged,
            resolver=Resolver(),
            rows_loader=context["authority"].rows_loader,
            runtime_registry=context["registry"],
            release_resolver=context["release_resolver"],
        )


def test_degraded_at_least_one_dimension_binding_validates_only_persisted_subset():
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)
    catalog, release_resolver = _released_paid_order_catalog()
    outcome = compile_analysis_contract(
        run_id="run-candidate-dimension-subset",
        proposal={
            "question_families": ["paid_amount_change_explanation"],
            "target_metrics": ["paid_amount"],
            "requested_dimensions": ["channel", "region"],
            "baselines": ["previous_day"],
            "claim_intents": ["segment_contribution_or_mix_shift"],
            "scope": {"type": "full_sample"},
        },
        accepted_capabilities=("candidate_dimension_screen",),
        catalog=catalog,
        registry=registry,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        release_resolver=release_resolver,
    )
    plan = outcome.capability_plans[0]
    channel_query = next(
        query
        for query in outcome.query_contracts
        if tuple(
            binding.dimension_id for binding in query.dimension_bindings
        )
        == ("channel",)
    )
    reconciliation = channel_query.reconciliation_binding
    assert reconciliation is not None
    total_query = next(
        query
        for query in outcome.query_contracts
        if query.query_role_ref == reconciliation.reference_query_role_ref
        and query.contract_signature
        == reconciliation.reference_contract_signature
    )

    def provenance(prefix, query_ref):
        return {
            "query_contract_refs": (query_ref,),
            "result_refs": (f"result:{prefix}",),
            "query_execution_record_refs": (f"query-record:{prefix}",),
            "query_execution_record_digests": (f"query-digest:{prefix}",),
            "rows_refs": (f"rows:{prefix}",),
            "rows_metadata_record_refs": (f"rows-record:{prefix}",),
            "rows_metadata_record_digests": (f"rows-digest:{prefix}",),
            "rows_content_hashes": (f"rows-hash:{prefix}",),
            "completeness_report_refs": (f"report:{prefix}",),
            "completeness_record_refs": (f"report-record:{prefix}",),
            "completeness_record_digests": (f"report-digest:{prefix}",),
            "source_snapshot_refs": tuple(channel_query.dataset_snapshot_refs),
        }

    primary = provenance("channel", channel_query.query_contract_id)
    validation = {
        f"validation_{key}": value
        for key, value in provenance(
            "total",
            total_query.query_contract_id,
        ).items()
    }
    payload = {
        "status": "degraded",
        "reasons": (
            "completeness_not_accepted:dimension_contribution_scan:region",
        ),
        **primary,
        **validation,
        "supported_evidence_types": tuple(plan.supported_evidence_types),
        "supported_claim_types": tuple(plan.supported_claim_types),
        "maximum_claim_strength": plan.maximum_claim_strength,
        "maximum_claim_strength_rank": plan.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": plan.claim_strength_taxonomy_version,
        "input_completeness_statuses": ("complete", "complete"),
    }
    binding = RuntimeEvidenceAuthority()._runtime_writer().record_capability_binding(
        plan,
        payload,
    )

    validate_capability_binding_plan_semantics(
        binding,
        registry,
        {
            channel_query.query_contract_id: channel_query,
            total_query.query_contract_id: total_query,
        },
    )


def _shared_dimension_validation_context():
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDINGS)
    catalog, release_resolver = _released_paid_order_catalog()
    outcome = compile_analysis_contract(
        run_id="run-shared-dimension-validation",
        proposal={
            "question_families": ["paid_amount_change_explanation"],
            "target_metrics": ["paid_amount"],
            "requested_dimensions": ["channel", "region"],
            "baselines": ["previous_day"],
            "claim_intents": ["segment_contribution_or_mix_shift"],
            "scope": {"type": "full_sample"},
        },
        accepted_capabilities=("candidate_dimension_screen",),
        catalog=catalog,
        registry=registry,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        release_resolver=release_resolver,
    )
    snapshot = catalog.snapshots()[0]
    authority = RuntimeEvidenceAuthority()
    results = {}
    reports = {}
    for contract in outcome.query_contracts:
        result = ClickHouseQueryExecutor(
            _StaticRowsRuntime(_rows_for_dimension_contract(contract)),
            evidence_authority=authority,
            release_resolver=release_resolver,
        ).execute(
            contract,
            {snapshot.snapshot_ref: snapshot},
            execution_attempt_ref=f"attempt:{contract.query_contract_id}",
        )
        report = validate_query_result(
            contract,
            result,
            snapshot,
            release_resolver=release_resolver,
        )
        _record_completeness(authority, report)
        results[contract.query_contract_id] = result
        reports[contract.query_contract_id] = report
    return {
        "authority": authority,
        "registry": registry,
        "release_resolver": release_resolver,
        "plan": outcome.capability_plans[0],
        "results": results,
        "reports": reports,
    }


def _rows_for_dimension_contract(contract):
    values = {
        "target": (100.0, 10, 8),
        "baseline": (80.0, 8, 7),
    }
    dimension_id = (
        contract.dimension_bindings[0].dimension_id
        if contract.dimension_bindings
        else ""
    )
    rows = []
    for window in contract.resolved_windows:
        amount, orders, users = values[window.role]
        base = {
            "window_id": window.window_id,
            "window_role": window.role,
            "observation_key": window.start_inclusive,
        }
        if not dimension_id:
            rows.append(
                {
                    **base,
                    "paid_amount": amount,
                    "paid_orders": orders,
                    "paid_users": users,
                }
            )
            continue
        order_splits = (orders - orders // 2, orders // 2)
        for index, (member, share) in enumerate((("A", 0.6), ("B", 0.4))):
            rows.append(
                {
                    **base,
                    dimension_id: member,
                    "paid_amount": amount * share,
                    "paid_orders": order_splits[index],
                    "paid_users": users,
                }
            )
    return tuple(rows)


class _StaticRowsRuntime:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def aggregate(self, sql, query_id, **kwargs):
        return ClickHouseQueryResult(
            ok=True,
            rows=self.rows,
            query_hash=canonical_digest(
                {"sql": sql, "parameters": kwargs.get("parameters", {})}
            ),
            query_id=query_id,
            provider_stats={
                "requested_settings": dict(kwargs.get("settings") or {}),
                "summary": {"read_rows": len(self.rows)},
            },
            execution_attempt_ref=kwargs.get("execution_attempt_ref", ""),
        )


def _replace_binding_completeness_record(
    binding,
    *,
    original_record_ref,
    changed,
):
    record_refs = tuple(
        changed.record_ref if ref == original_record_ref else ref
        for ref in binding.completeness_record_refs
    )
    record_digests = tuple(
        changed.report_digest if ref == original_record_ref else digest
        for ref, digest in zip(
            binding.completeness_record_refs,
            binding.completeness_record_digests,
        )
    )
    payload = dict(binding.binding_payload)
    payload["completeness_record_refs"] = record_refs
    payload["completeness_record_digests"] = record_digests
    forged = replace(
        binding,
        completeness_record_refs=record_refs,
        completeness_record_digests=record_digests,
        binding_payload=payload,
    )
    binding_digest = canonical_digest(
        {"plan": forged.plan_payload, "binding": forged.binding_payload}
    )
    return replace(
        forged,
        record_ref=f"capability-binding:{forged.capability_id}:{binding_digest}",
        binding_digest=binding_digest,
    )


def test_route_added_dimension_claim_remains_auxiliary_in_evidence_reduction():
    from bi_agent.runtime import langgraph_workflow as workflow

    resolution = workflow._required_claim_evidence_resolution(
        {
            "intent": {
                "required_claim_types": ["comparative_change"],
                "auxiliary_claim_types": [
                    "segment_contribution_or_mix_shift"
                ],
            },
            "analysis_route": {
                "claim_intent_resolution": {
                    "auxiliary_claim_intents": [
                        "segment_contribution_or_mix_shift"
                    ]
                }
            },
            "evidence": [
                {
                    "claim_type": "segment_contribution_or_mix_shift",
                    "capability_id": "candidate_dimension_screen",
                    "limitations": ["one_dimension_sparse"],
                }
            ],
        }
    )

    assert resolution["candidate_claim_types"] == (
        "segment_contribution_or_mix_shift",
    )
    assert resolution["material_limitations"] == (
        "missing_required_claim_evidence:comparative_change",
    )
    assert resolution["auxiliary_limitations"] == ("one_dimension_sparse",)


def test_dimension_claim_selector_survives_authority_normalization():
    from bi_agent.runtime import langgraph_workflow as workflow

    claim = {
        "text": "渠道A的付费金额较基线增加50。",
        "evidence_refs": ["candidate_dimension_screen:inline"],
        "numbers": {
            "paid_amount_baseline_value": 100,
            "paid_amount_target_value": 150,
            "paid_amount_delta": 50,
        },
        "dimensions": {"channel": "A"},
        "scope": "full_sample",
        "time_window": "2026-06-01",
        "claim_type": "segment_contribution_or_mix_shift",
        "claim_strength": "medium",
    }

    normalized = workflow._normalize_authority_claim_candidates(
        [claim],
        {
            "intent": {
                "scope": "full_sample",
                "time_window": "2026-06-01",
            },
            "request": {"run_mode": "production"},
            "evidence": [
                {
                    "evidence_ref": "candidate_dimension_screen:inline",
                    "numeric_facts": claim["numbers"],
                }
            ],
        },
    )

    assert normalized[0]["dimensions"] == {"channel": "A"}


def test_ready_auxiliary_dimension_evidence_is_published_with_required_claims():
    from bi_agent.runtime import langgraph_workflow as workflow

    state = {
        "request": {"run_mode": "production"},
        "intent": {
            "scope": "full_sample",
            "time_window": "2026-06-01",
            "target_metric": "paid_amount",
            "required_claim_types": ["comparative_change"],
            "auxiliary_claim_types": [
                "segment_contribution_or_mix_shift"
            ],
            "target": {"label": "2026-06-01"},
            "baseline": {"label": "2026-05-31"},
        },
        "analysis_route": {
            "claim_intent_resolution": {
                "required_claim_intents": ["comparative_change"],
                "auxiliary_claim_intents": [
                    "segment_contribution_or_mix_shift"
                ],
                "auto_routed_claim_intents": {
                    "segment_contribution_or_mix_shift": {
                        "capability_id": "candidate_dimension_screen",
                        "evidence_status": "queryable",
                        "publication_status": "evidence_required",
                    }
                },
            }
        },
        "evidence": [
            {
                "evidence_ref": "compare_periods:ready",
                "capability_id": "compare_periods",
                "claim_type": "comparative_change",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:compare",
                "evidence_type": "statistical_association",
                "supported_evidence_types": ["statistical_association"],
                "supported_claim_types": ["comparative_change"],
                "maximum_claim_strength": "directional",
                "maximum_claim_strength_rank": 1,
                "strength": "directional",
                "wording_limit": "quantified",
                "limitations": [],
                "numeric_facts": {
                    "target_value": 308_240_309,
                    "baseline_value": 304_142_630,
                    "absolute_change": 4_097_679,
                    "relative_change": 0.013472886060069909,
                },
                "typed_payload": {
                    "scope": "full_sample",
                    "time_window": "2026-06-01",
                    "target_value": 308_240_309,
                    "baseline_value": 304_142_630,
                    "absolute_change": 4_097_679,
                    "relative_change": 0.013472886060069909,
                },
            },
            {
                "evidence_ref": "candidate_dimension_screen:ready",
                "capability_id": "candidate_dimension_screen",
                "claim_type": "segment_contribution_or_mix_shift",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:dimension",
                "evidence_type": "statistical_association",
                "supported_evidence_types": ["statistical_association"],
                "supported_claim_types": [
                    "segment_contribution_or_mix_shift"
                ],
                "maximum_claim_strength": "candidate_driver",
                "maximum_claim_strength_rank": 2,
                "strength": "medium",
                "wording_limit": "candidate",
                "limitations": ["sparse_dimension_values:region"],
                "numeric_facts": {
                    "paid_amount_target_value": 135_701_843,
                    "paid_amount_baseline_value": 128_826_283,
                    "paid_amount_delta": 6_875_560,
                    "paid_amount_relative_change": 0.05337078614617795,
                    "dimension_count": 5,
                    "eligible_dimension_count": 5,
                },
                "typed_payload": {
                    "scope": "full_sample",
                    "time_window": "2026-06-01",
                    "selected_dimension": "region",
                    "selected_dimension_label": "地区",
                    "selected_value": "拉各斯州",
                    "business_readout": (
                        "地区是当前优先排查维度，重点关注拉各斯州："
                        "目标期付费金额135,701,843.00，基线期128,826,283.00，"
                        "变化+6,875,560.00。该优先级用于定位，跨维度不可相加。"
                    ),
                },
            },
        ],
    }

    claims = workflow._authority_claims_from_evidence(state)

    assert [claim["claim_type"] for claim in claims] == [
        "comparative_change",
        "segment_contribution_or_mix_shift",
    ]
    assert claims[1]["text"] == state["evidence"][1]["typed_payload"][
        "business_readout"
    ]
    assert claims[1]["dimensions"] == {"region": "拉各斯州"}
    assert claims[1]["claim_strength"] == "medium"
    assert claims[1]["numbers"] == {
        "paid_amount_target_value": 135_701_843,
        "paid_amount_baseline_value": 128_826_283,
        "paid_amount_delta": 6_875_560,
        "paid_amount_relative_change": 0.05337078614617795,
    }

    business_context = workflow._business_evidence_context(state)

    assert [slot["statement"] for slot in business_context["claimSlots"]] == [
        claims[0]["text"],
        state["evidence"][1]["typed_payload"]["business_readout"],
    ]
    assert business_context["claimSlots"][1]["strength"] == "中等强度证据"


def test_unready_auxiliary_dimension_evidence_does_not_block_required_claim():
    from bi_agent.runtime import langgraph_workflow as workflow

    state = {
        "request": {"run_mode": "production"},
        "intent": {
            "scope": "full_sample",
            "time_window": "2026-06-01",
            "target_metric": "paid_amount",
            "required_claim_types": ["comparative_change"],
            "auxiliary_claim_types": [
                "segment_contribution_or_mix_shift"
            ],
            "target": {"label": "2026-06-01"},
            "baseline": {"label": "2026-05-31"},
        },
        "analysis_route": {
            "claim_intent_resolution": {
                "required_claim_intents": ["comparative_change"],
                "auxiliary_claim_intents": [
                    "segment_contribution_or_mix_shift"
                ],
                "auto_routed_claim_intents": {
                    "segment_contribution_or_mix_shift": {
                        "capability_id": "candidate_dimension_screen",
                        "evidence_status": "queryable",
                        "publication_status": "evidence_required",
                    }
                },
            }
        },
        "evidence": [
            {
                "evidence_ref": "compare_periods:ready",
                "capability_id": "compare_periods",
                "claim_type": "comparative_change",
                "claim_input_ready": True,
                "binding_manifest_ref": "binding:compare",
                "evidence_type": "statistical_association",
                "supported_evidence_types": ["statistical_association"],
                "supported_claim_types": ["comparative_change"],
                "maximum_claim_strength": "directional",
                "maximum_claim_strength_rank": 1,
                "strength": "directional",
                "wording_limit": "quantified",
                "limitations": [],
                "numeric_facts": {
                    "target_value": 120,
                    "baseline_value": 100,
                    "absolute_change": 20,
                    "relative_change": 0.2,
                },
                "typed_payload": {
                    "scope": "full_sample",
                    "time_window": "2026-06-01",
                    "target_value": 120,
                    "baseline_value": 100,
                    "absolute_change": 20,
                    "relative_change": 0.2,
                },
            },
            {
                "evidence_ref": "candidate_dimension_screen:blocked",
                "capability_id": "candidate_dimension_screen",
                "claim_type": "segment_contribution_or_mix_shift",
                "claim_input_ready": False,
                "binding_manifest_ref": "binding:dimension",
                "evidence_type": "insufficient",
                "supported_evidence_types": ["statistical_association"],
                "supported_claim_types": [
                    "segment_contribution_or_mix_shift"
                ],
                "maximum_claim_strength": "candidate_driver",
                "maximum_claim_strength_rank": 2,
                "strength": "low",
                "wording_limit": "blocked",
                "limitations": ["missing_required_input"],
                "numeric_facts": {},
                "typed_payload": {
                    "scope": "full_sample",
                    "time_window": "2026-06-01",
                },
            },
        ],
    }

    claims = workflow._authority_claims_from_evidence(state)

    assert [claim["claim_type"] for claim in claims] == [
        "comparative_change"
    ]


def test_proportional_dimension_growth_stays_coverage_only():
    from bi_agent.runtime import langgraph_workflow as workflow

    context = _shared_dimension_validation_context()
    bound = bind_capability_inputs(
        context["plan"],
        results=context["results"],
        reports=context["reports"],
        evidence_authority=context["authority"],
        runtime_registry=context["registry"],
        release_resolver=context["release_resolver"],
    )
    raw_evidence = candidate_dimension_screen(
        {
            "channel": (
                {
                    "group": "baseline",
                    "channel": "A",
                    "amount": 48,
                    "paid_orders": 4,
                    "paid_users": 7,
                },
                {
                    "group": "target",
                    "channel": "A",
                    "amount": 60,
                    "paid_orders": 5,
                    "paid_users": 8,
                },
                {
                    "group": "baseline",
                    "channel": "B",
                    "amount": 32,
                    "paid_orders": 4,
                    "paid_users": 7,
                },
                {
                    "group": "target",
                    "channel": "B",
                    "amount": 40,
                    "paid_orders": 5,
                    "paid_users": 8,
                },
            ),
            "region": (
                {
                    "group": "baseline",
                    "region": "A",
                    "amount": 48,
                    "paid_orders": 4,
                    "paid_users": 7,
                },
                {
                    "group": "target",
                    "region": "A",
                    "amount": 60,
                    "paid_orders": 5,
                    "paid_users": 8,
                },
                {
                    "group": "baseline",
                    "region": "B",
                    "amount": 32,
                    "paid_orders": 4,
                    "paid_users": 7,
                },
                {
                    "group": "target",
                    "region": "B",
                    "amount": 40,
                    "paid_orders": 5,
                    "paid_users": 8,
                },
            ),
        },
        overall_by_group={"baseline": 80, "target": 100},
        complete_dimensions=("channel", "region"),
        dimension_labels={"channel": "渠道", "region": "地区"},
        min_sample_size=1,
    )
    state = {
        "run_id": "run-dimension-claim-verifier",
        "request": {
            "run_mode": "production",
            "bound_capability_inputs": {
                "candidate_dimension_screen": bound
            },
            "runtime_registry": context["registry"],
            "evidence_resolver": context["authority"],
            "rows_loader": context["authority"].rows_loader,
            "release_resolver": context["release_resolver"],
        },
        "intent": {
            "scope": "full_sample",
            "time_window": "2026-06-01",
            "target_metric": "paid_amount",
            "pattern_family": "custom_baseline",
            "required_claim_types": [
                "segment_contribution_or_mix_shift"
            ],
            "auxiliary_claim_types": [
                "segment_contribution_or_mix_shift"
            ],
        },
        "evidence": [],
    }
    evidence = workflow._evidence_dict(raw_evidence, state)
    state["evidence"] = [evidence]

    claims = workflow._authority_claims_from_evidence(state)

    assert evidence["typed_payload"]["coverage_ready_dimensions"] == [
        "channel",
        "region",
    ]
    assert evidence["typed_payload"]["eligible_dimensions"] == []
    assert evidence["typed_payload"]["selected_business_readouts"] == []
    assert claims == []
