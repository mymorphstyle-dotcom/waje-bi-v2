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
from tests.support.temporal_authority import resolved_test_daily_pair_authority


RUNTIME_BINDINGS = "contracts/runtime/clickhouse-analysis-bindings.yaml"


def _temporal_authority():
    return resolved_test_daily_pair_authority(
        target="2026-06-02",
        baseline_id="previous_day",
    )


def _candidate_dimension_proposal(dimensions):
    return {
        "question_families": ["paid_amount_change_explanation"],
        "target_metrics": ["paid_amount"],
        "requested_dimensions": list(dimensions),
        "claim_intents": ["segment_contribution_or_mix_shift"],
        "scope": {"type": "full_sample"},
        "grain": "window_id",
        "capability_roles": {
            "candidate_dimension_screen": {
                "analysis_role": "required",
                "sources": ("closed_contract_test",),
            }
        },
    }


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
        registry.query_shape("dimension_contribution_scan")["dimension_presence_policy"]
        == "sparse_allowed"
    )
    assert (
        "candidate_dimension_screen"
        in registry.analysis_axis("dimension_localization")["capability_refs"]
    )
    joint = registry.capability_inputs("joint_attribution")
    assert joint["task_dependencies"] == ["candidate_dimension_screen"]
    assert joint["dynamic_dimension_combination_policy"]["source_dependency"] == (
        "candidate_dimension_screen"
    )


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


def test_candidate_dimension_screen_keeps_companion_validation_only():
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
        proposal=_candidate_dimension_proposal(dimensions),
        accepted_capabilities=("candidate_dimension_screen",),
        catalog=catalog,
        registry=registry,
        temporal_authority=_temporal_authority(),
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
    required_slots = outcome.capability_plans[0].required_input_slots
    assert len(outcome.query_contracts) == len(dimensions) + 1
    assert len(required_slots) == len(dimensions)
    assert {slot.query_contract_refs[0] for slot in required_slots} == {
        query.query_contract_id for query in dimension_queries
    }
    companion_query = next(
        query for query in outcome.query_contracts if not query.dimension_bindings
    )
    assert all(
        slot.validation_query_contract_refs == (companion_query.query_contract_id,)
        for slot in required_slots
    )
    assert {
        ref for slot in required_slots for ref in slot.query_contract_refs
    }.isdisjoint(
        ref for slot in required_slots for ref in slot.validation_query_contract_refs
    )


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
    assert all(
        report.completeness_status == "complete" for report in chain.primary_reports
    )
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
        proposal=_candidate_dimension_proposal(("channel", "region")),
        accepted_capabilities=("candidate_dimension_screen",),
        catalog=catalog,
        registry=registry,
        temporal_authority=_temporal_authority(),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        release_resolver=release_resolver,
    )
    plan = outcome.capability_plans[0]
    channel_query = next(
        query
        for query in outcome.query_contracts
        if tuple(binding.dimension_id for binding in query.dimension_bindings)
        == ("channel",)
    )
    reconciliation = channel_query.reconciliation_binding
    assert reconciliation is not None
    total_query = next(
        query
        for query in outcome.query_contracts
        if query.query_role_ref == reconciliation.reference_query_role_ref
        and query.contract_signature == reconciliation.reference_contract_signature
    )
    region_query = next(
        query
        for query in outcome.query_contracts
        if tuple(binding.dimension_id for binding in query.dimension_bindings)
        == ("region",)
    )
    unavailable_slots = tuple(
        slot
        for slot in plan.required_input_slots
        if slot.query_contract_refs == (region_query.query_contract_id,)
    )
    assert len(unavailable_slots) == 1

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
        "capability_id": plan.capability_id,
        "capability_contract_ref": plan.capability_contract_ref,
        "capability_contract_version": plan.capability_contract_version,
        "capability_contract_signature": plan.capability_contract_signature,
        "analysis_contract_ref": plan.analysis_contract_ref,
        "status": "degraded",
        "rows_by_slot": {},
        "reasons": tuple(
            f"slot_input_missing:{slot.slot_id}" for slot in unavailable_slots
        ),
        "issues": tuple(
            {
                "code": "slot_input_missing",
                "failure_class": "availability",
                "input_state": "missing",
                "slot_id": slot.slot_id,
                "slot_role": "required",
                "diagnostic": f"slot_input_missing:{slot.slot_id}",
            }
            for slot in unavailable_slots
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
    binding = (
        RuntimeEvidenceAuthority()
        ._runtime_writer()
        .record_capability_binding(
            plan,
            payload,
        )
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
        proposal=_candidate_dimension_proposal(("channel", "region")),
        accepted_capabilities=("candidate_dimension_screen",),
        catalog=catalog,
        registry=registry,
        temporal_authority=_temporal_authority(),
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
            "observation_key": window.window_id,
            "source_complete_days": 1,
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
        user_splits = (users - users // 2, users // 2)
        for index, (member, share) in enumerate((("A", 0.6), ("B", 0.4))):
            rows.append(
                {
                    **base,
                    dimension_id: member,
                    "paid_amount": amount * share,
                    "paid_orders": order_splits[index],
                    "paid_users": user_splits[index],
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
