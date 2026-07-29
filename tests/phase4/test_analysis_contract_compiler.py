from dataclasses import replace
from datetime import datetime
import unittest

from bi_agent.runtime.analysis_contract_compiler import (
    _canonical_query_windows,
    _context_window_specs,
    compile_analysis_contract as _compile_analysis_contract,
    expand_dynamic_dimension_queries,
)
from bi_agent.runtime.analysis_contracts import (
    ResolvedWindow,
    query_contract_signature,
)
from bi_agent.runtime.contract_gaps import (
    is_canonical_direct_analysis_source_ambiguity,
)
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.dataset_catalog import (
    canonical_dataset_release_members,
    canonical_dataset_requires_release,
    DatasetCatalog,
    DatasetSnapshot,
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from bi_agent.runtime.query_ir import compile_capability_query_route
from tests.support.temporal_authority import (
    resolved_test_daily_pair_authority,
    resolved_test_temporal_authority,
)


def _single_day_pair_temporal_authority(
    *,
    target: str = "2026-06-02",
    baseline_id: str = "previous_day",
):
    return resolved_test_daily_pair_authority(
        target=target,
        baseline_id=baseline_id,
    )


def _target_only_temporal_authority():
    return resolved_test_temporal_authority(
        time_spec={"kind": "date", "target": "2026-06-02"},
        comparison_spec={"kind": "none"},
        require_physical_baseline=False,
    )


def _aggregate_pair_temporal_authority():
    return resolved_test_temporal_authority(
        time_spec={
            "kind": "date_range",
            "start": "2026-06-01",
            "end": "2026-06-30",
        },
        comparison_spec={
            "kind": "fixed_window",
            "baseline_class": "prior_period",
            "baseline_start": "2026-05-01",
            "baseline_end": "2026-05-31",
            "aggregation": "sum_of_complete_days",
        },
        require_physical_baseline=True,
    )


def _calendar_partition_temporal_authority():
    return resolved_test_temporal_authority(
        time_spec={
            "kind": "date_range",
            "start": "2024-01-01",
            "end": "2026-06-30",
        },
        comparison_spec={
            "kind": "calendar_partition",
            "baseline_class": "same_month_phase",
            "period_grain": "month",
            "partition_field": "month_phase",
            "target_members": ["start"],
            "baseline_members": ["mid", "end"],
            "aggregation": "mean_of_complete_days",
            "member_definitions": [
                {"member": "start", "day_start": 1, "day_end": 10},
                {"member": "mid", "day_start": 11, "day_end": 20},
                {"member": "end", "day_start": 21, "day_end": 31},
            ],
        },
        require_physical_baseline=False,
    )


def _required_roles(*capability_ids):
    return {
        capability_id: {
            "analysis_role": "required",
            "sources": ("closed_contract_test",),
        }
        for capability_id in capability_ids
    }


def snapshot(dataset_id, table, watermark):
    return DatasetSnapshot(
        f"snapshot:{dataset_id}:1",
        dataset_id,
        table,
        watermark,
        f"schema:{dataset_id}",
        (
            "business_date_lagos",
            "business_date",
            "event_start_date",
            "paid_amount_ngn",
            "user_id",
            "order_id",
            "channel",
            "payment_method",
            "final_outcome",
            "terminal_orders",
            "successful_paid_amount_ngn",
            "active_users",
            "region",
            "device_brand",
            "gameplay",
            "is_new_user",
            "is_first_payment",
            "订单id",
            "支付状态",
            "支付发起时间",
        ),
        f"contract:{dataset_id}@1",
        "2026-06-03T00:00:00+00:00",
        "active",
    )


class _ReleaseResolver:
    def __init__(self, record):
        self.record = record

    def resolve_dataset_release(self, release_ref):
        if release_ref != self.record.release_ref:
            raise KeyError(release_ref)
        return self.record


def released_catalog(*snapshots):
    catalog, _, _ = canonical_release_catalog(*snapshots)
    return catalog


def canonical_release_catalog(*snapshots):
    selected = list(snapshots)
    if not selected:
        raise ValueError("canonical_release_fixture_empty")
    members = canonical_dataset_release_members(selected[0].dataset_id)
    logical_id = selected[0].logical_snapshot_id or f"{selected[0].dataset_id}-logical"
    revision = selected[0].load_revision or f"{selected[0].dataset_id}-load:sha256:test"
    existing_ids = {item.dataset_id for item in selected}
    registry = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    )
    for member in members:
        if member in existing_ids:
            continue
        selected.append(
            replace(
                selected[0],
                snapshot_ref=f"snapshot:synthetic:{member}",
                dataset_id=member,
                physical_table=f"{member}_daily__synthetic",
                schema_fields=tuple(
                    registry.dataset(member).get("schema_fields") or ()
                ),
                evidence_state="context_only",
                reconciliation_status="mismatch",
            )
        )
    if {item.dataset_id for item in selected} != set(members):
        raise ValueError("canonical_release_fixture_dataset_set")
    release_ref = dataset_snapshot_release_ref(
        logical_id,
        revision,
        (item.snapshot_ref for item in selected),
    )
    released = tuple(
        replace(
            item,
            logical_snapshot_id=logical_id,
            load_revision=revision,
            snapshot_id=item.snapshot_id or logical_id,
            release_ref=release_ref,
            rows_content_hash=item.rows_content_hash
            or ("a" * 64 if item.dataset_id == "market_dashboard" else "b" * 64),
        )
        for item in selected
    )
    payloads = tuple(
        {
            **item.to_dict(),
            "requires_release": True,
        }
        for item in released
    )
    record = build_dataset_release_authority_record(payloads)
    authorized = tuple(
        replace(item, authority_record_ref=record.authority_record_ref)
        for item in released
    )
    requested_refs = {item.snapshot_ref for item in snapshots}
    resolver = _ReleaseResolver(record)
    catalog = DatasetCatalog(
        tuple(item for item in authorized if item.snapshot_ref in requested_refs),
        release_resolver=resolver,
    )
    return (
        catalog,
        resolver,
        tuple(item for item in authorized if item.snapshot_ref in requested_refs),
    )


def compile_analysis_contract(**kwargs):
    """Compile against canonical release-signed dataset fixtures."""
    catalog = kwargs["catalog"]
    snapshots = catalog.snapshots()
    unsigned_release_datasets = tuple(
        item
        for item in snapshots
        if canonical_dataset_requires_release(item.dataset_id)
        and not item.release_ref
    )
    if not unsigned_release_datasets:
        return _compile_analysis_contract(**kwargs)
    released = []
    release_records = {}
    for unsigned_snapshot in unsigned_release_datasets:
        _, dataset_resolver, signed = canonical_release_catalog(unsigned_snapshot)
        released.extend(signed)
        release_records[dataset_resolver.record.release_ref] = dataset_resolver.record

    class TestReleaseResolver:
        def resolve_dataset_release(self, release_ref):
            return release_records[release_ref]

    release_resolver = TestReleaseResolver()
    signed_dataset_ids = {item.dataset_id for item in unsigned_release_datasets}
    other_snapshots = tuple(
        item for item in snapshots if item.dataset_id not in signed_dataset_ids
    )
    signed_catalog = DatasetCatalog(
        (*tuple(released), *other_snapshots),
        release_resolver=release_resolver,
    )
    return _compile_analysis_contract(
        **{
            **kwargs,
            "catalog": signed_catalog,
            "release_resolver": release_resolver,
        }
    )


def _market_dashboard_snapshots():
    common = {
        "watermark": "2026-06-02",
        "schema_fingerprint": "schema1234567890abcdef",
        "contract_ref": "contract:market-dashboard@1",
        "loaded_at": "2026-06-03T00:00:00+00:00",
        "status": "active",
        "logical_snapshot_id": "dashboard-logical",
        "load_revision": "dashboard-load:sha256:capability-local",
    }
    dashboard = DatasetSnapshot(
        snapshot_ref="snapshot:market:capability-local",
        dataset_id="market_dashboard",
        physical_table="market_dashboard_daily__schema1234567890",
        schema_fields=(
            "snapshot_id",
            "load_revision",
            "business_date",
            "game",
            "active_users",
            "new_users",
            "registrations",
            "aggregate_marketing_cost",
            "profit",
            "paid_amount",
        ),
        evidence_state="claim_ready",
        reconciliation_status="matched",
        rows_content_hash="a" * 64,
        **common,
    )
    channel = DatasetSnapshot(
        snapshot_ref="snapshot:channel:capability-local",
        dataset_id="market_dashboard_channel",
        physical_table="market_dashboard_channel_daily__schema1234567890",
        schema_fields=(
            "snapshot_id",
            "load_revision",
            "business_date",
            "game",
            "channel",
            "active_users",
            "paid_amount",
        ),
        evidence_state="context_only",
        reconciliation_status="mismatch",
        reconciliation_ref="reconciliation:capability-local:mismatch",
        rows_content_hash="b" * 64,
        **common,
    )
    return dashboard, channel


class AnalysisContractCompilerTest(unittest.TestCase):
    def _assert_temporal_unsupported(self, capability_id, *, proposal=None):
        with self.assertRaisesRegex(
            ValueError,
            (
                "analysis_query_input_route_unavailable:"
                f"{capability_id}:query_input_binding_missing"
            ),
        ):
            compile_analysis_contract(
                run_id=f"run-temporal-unsupported-{capability_id}",
                proposal={
                    "scope": {"type": "full_sample"},
                    "grain": "window_id",
                    "capability_roles": _required_roles(capability_id),
                    **(proposal or {"target_metrics": ["paid_amount"]}),
                },
                accepted_capabilities=(capability_id,),
                catalog=DatasetCatalog(
                    (snapshot("paid_order_success", "paid", "2026-07-04"),)
                ),
                registry=RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                ),
                temporal_authority=_single_day_pair_temporal_authority(),
                as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
            )

    def _assert_dynamic_pair_adapter(self, capability_id, *, proposal=None):
        outcome = compile_analysis_contract(
            run_id=f"run-dynamic-pair-{capability_id}",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(capability_id),
                **(proposal or {"target_metrics": ["paid_amount"]}),
            },
            accepted_capabilities=(capability_id,),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )
        self.assertEqual(
            tuple(item.capability_id for item in outcome.capability_plans),
            (capability_id,),
        )
        self.assertTrue(outcome.query_contracts)
        return outcome

    def test_target_only_authority_selects_only_target_query_window(self):
        outcome = compile_analysis_contract(
            run_id="run-target-only-authority",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("metric_timeseries"),
                "target_metrics": ["active_users"],
            },
            accepted_capabilities=("metric_timeseries",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        self.assertEqual(
            tuple(
                window.window_id
                for window in outcome.analysis_contract.resolved_windows
            ),
            ("target_day",),
        )
        self.assertEqual(outcome.query_contracts[0].window_refs, ("target_day",))

    def test_single_day_pair_authority_selects_target_and_baseline_windows(self):
        outcome = compile_analysis_contract(
            run_id="run-single-day-pair-authority",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        self.assertEqual(
            outcome.query_contracts[0].window_refs,
            ("target_day", "previous_day"),
        )

    def test_aggregate_pair_authority_keeps_complete_window_boundaries(self):
        outcome = compile_analysis_contract(
            run_id="run-aggregate-pair-authority",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("formula_decompose"),
                "target_metrics": ["paid_amount"],
            },
            accepted_capabilities=("formula_decompose",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_aggregate_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        windows = outcome.analysis_contract.resolved_windows
        self.assertEqual(
            tuple(
                (window.role, window.start_inclusive, window.end_exclusive)
                for window in windows
            ),
            (
                ("target", "2026-06-01", "2026-07-01"),
                ("baseline", "2026-05-01", "2026-06-01"),
            ),
        )
        self.assertTrue(
            all(
                query.window_refs == tuple(window.window_id for window in windows)
                for query in outcome.query_contracts
            )
        )

    def test_calendar_partition_authority_queries_only_evaluation_window(self):
        outcome = compile_analysis_contract(
            run_id="run-calendar-partition-authority",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_period_phases"),
                "target_metrics": ["paid_amount"],
            },
            accepted_capabilities=("compare_period_phases",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_calendar_partition_temporal_authority(),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        self.assertEqual(
            tuple(
                window.window_id
                for window in outcome.analysis_contract.resolved_windows
            ),
            ("target_day",),
        )
        self.assertEqual(outcome.query_contracts[0].window_refs, ("target_day",))
        self.assertEqual(
            outcome.query_contracts[0].query_intent,
            "time_bucket_scan",
        )
        self.assertEqual(
            outcome.query_contracts[0].query_parameters[
                "month_phase_member_definitions"
            ],
            (
                {"member": "start", "day_start": 1, "day_end": 10},
                {"member": "mid", "day_start": 11, "day_end": 20},
                {"member": "end", "day_start": 21, "day_end": 31},
            ),
        )

    def test_calendar_partition_derives_pair_frame_for_formula_analysis(self):
        outcome = compile_analysis_contract(
            run_id="run-calendar-partition-formula",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("formula_decompose"),
                "target_metrics": ["paid_amount"],
            },
            accepted_capabilities=("formula_decompose",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_calendar_partition_temporal_authority(),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        self.assertEqual(
            tuple(
                window.window_id
                for window in outcome.analysis_contract.resolved_windows
            ),
            ("target_day",),
        )
        contract = outcome.query_contracts[0]
        self.assertEqual(contract.query_intent, "component_driver_scan")
        self.assertEqual(
            contract.query_parameters["calendar_partition_role_frame"],
            {
                "schema_version": "calendar-partition-role-frame.v2",
                "baseline_class": "same_month_phase",
                "period_grain": "month",
                "partition_field": "month_phase",
                "target_members": ("start",),
                "baseline_members": ("mid", "end"),
                "aggregation": "mean_of_complete_days",
                "member_definitions": (
                    {"member": "start", "day_start": 1, "day_end": 10},
                    {"member": "mid", "day_start": 11, "day_end": 20},
                    {"member": "end", "day_start": 21, "day_end": 31},
                ),
            },
        )
        self.assertEqual(
            contract.result_shape.unique_key,
            ("window_id", "observation_key", "window_role"),
        )

    def test_calendar_partition_daily_frame_uses_evaluation_range_context(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        capability_id = "cross_source_association"
        authority = resolved_test_temporal_authority(
            time_spec={
                "kind": "date_range",
                "start": "2024-01-01",
                "end": "2026-05-31",
            },
            comparison_spec={
                "kind": "calendar_partition",
                "baseline_class": "same_month_phase",
                "period_grain": "month",
                "partition_field": "month_phase",
                "target_members": ["start"],
                "baseline_members": ["mid", "end"],
                "aggregation": "mean_of_complete_days",
                "member_definitions": [
                    {"member": "start", "day_start": 1, "day_end": 10},
                    {"member": "mid", "day_start": 11, "day_end": 20},
                    {"member": "end", "day_start": 21, "day_end": 31},
                ],
            },
            require_physical_baseline=False,
        )
        route = compile_capability_query_route(
            capability_id=capability_id,
            capability_contract=registry.capability_inputs(capability_id),
            temporal_authority=authority,
        )

        specs = _context_window_specs(
            {
                "context_window_specs": (
                    {
                        "capability_id": capability_id,
                        "relation": "evaluation_range",
                        "unit": "day",
                        "count": 882,
                    },
                ),
            },
            accepted_capabilities=(capability_id,),
            registry=registry,
            capability_query_routes={capability_id: route},
        )

        self.assertEqual(
            specs,
            (
                {
                    "capability_id": capability_id,
                    "relation": "evaluation_range",
                    "unit": "day",
                    "count": 882,
                },
            ),
        )

    def test_compiler_rejects_grain_with_surrounding_whitespace_before_binding(self):
        with self.assertRaisesRegex(
            ValueError,
            "analysis_contract_grain_invalid",
        ):
            _compile_analysis_contract(
                run_id="run-whitespace-grain",
                proposal={
                    "question_families": ["paid_amount_change_explanation"],
                    "target_metrics": ["paid_amount"],
                    "scope": {"type": "full_sample"},
                    "grain": " window_id ",
                    "capability_roles": {
                        "compare_periods": {
                            "analysis_role": "required",
                            "sources": ("closed_contract_test",),
                        }
                    },
                },
                accepted_capabilities=("compare_periods",),
                catalog=DatasetCatalog(()),
                registry=RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                ),
                temporal_authority=_single_day_pair_temporal_authority(),
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            )

    def test_queryless_reducer_is_not_an_analysis_temporal_consumer(self):
        self._assert_temporal_unsupported("evidence_reduce")

    def test_source_gap_keeps_empty_claim_scope_when_capability_claims_are_disjoint(
        self,
    ):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        cases = (
            (
                "event_evidence",
                {"requested_context_sources": ["external_event"]},
                "external_event",
                "comparative_change",
            ),
        )
        for capability_id, proposal, dataset_id, unrelated_claim in cases:
            with self.subTest(capability_id=capability_id):
                outcome = _compile_analysis_contract(
                    run_id=f"run-disjoint-claim-{capability_id}",
                    proposal={
                        **proposal,
                        "claim_intents": [unrelated_claim],
                        "scope": {"type": "full_sample"},
                        "grain": "window_id",
                        "capability_roles": {
                            capability_id: {
                                "analysis_role": "required",
                                "sources": ("closed_contract_test",),
                            }
                        },
                    },
                    accepted_capabilities=(capability_id,),
                    catalog=DatasetCatalog(()),
                    registry=registry,
                    temporal_authority=_single_day_pair_temporal_authority(),
                    as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
                )

                gap = next(
                    item
                    for item in outcome.analysis_contract.contract_gaps
                    if item.dataset_id == dataset_id
                )
                self.assertEqual(gap.affected_capabilities, (capability_id,))
                self.assertEqual(gap.affected_claim_types, ())
                self.assertNotIn(unrelated_claim, gap.affected_claim_types)

    def test_unsigned_required_release_is_source_unbound(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = _compile_analysis_contract(
            run_id="run-unsigned-required-release",
            proposal={
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": {
                    "compare_periods": {
                        "analysis_role": "required",
                        "sources": ("closed_contract_test",),
                    }
                },
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertFalse(outcome.query_contracts)
        self.assertIn(
            "dataset:paid_order_success:source_unbound",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )

    def test_source_isolation_preserves_unaffected_current_data_queries(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )

        def current_snapshot(dataset_id):
            contract = registry.dataset(dataset_id)
            return DatasetSnapshot(
                snapshot_ref=f"snapshot:{dataset_id}:source-isolation",
                dataset_id=dataset_id,
                physical_table=f"{dataset_id}_daily__source_isolation",
                watermark="2026-06-02",
                schema_fingerprint=f"schema:{dataset_id}:source-isolation",
                schema_fields=tuple(
                    contract.get("schema_fields")
                    or snapshot(dataset_id, dataset_id, "2026-06-02").schema_fields
                ),
                contract_ref=str(
                    contract.get("contract_ref") or f"contract:{dataset_id}@1"
                ),
                loaded_at="2026-06-03T00:00:00+00:00",
                status="active",
                evidence_state=(
                    "claim_ready"
                    if dataset_id in {"market_dashboard", "paid_order_success"}
                    else "context_only"
                ),
                logical_snapshot_id=f"logical:{dataset_id}",
                load_revision=f"load:{dataset_id}:sha256:source-isolation",
                rows_content_hash=("abcdef"[sum(map(ord, dataset_id)) % 6] * 64),
                reconciliation_status=(
                    "matched" if dataset_id == "market_dashboard" else "not_applicable"
                ),
            )

        def authorized_catalog(*members):
            records = {}
            authorized = []
            requested = {member.dataset_id: member for member in members}
            families = []
            seen_families = set()
            for member in members:
                family = canonical_dataset_release_members(member.dataset_id)
                if family in seen_families:
                    continue
                seen_families.add(family)
                families.append(family)
            for family in families:
                seed = requested[next(item for item in family if item in requested)]
                release_members = []
                for dataset_id in family:
                    release_members.append(
                        requested.get(dataset_id)
                        or replace(
                            current_snapshot(dataset_id),
                            logical_snapshot_id=seed.logical_snapshot_id,
                            load_revision=seed.load_revision,
                            reconciliation_status="mismatch",
                        )
                    )
                release_ref = dataset_snapshot_release_ref(
                    seed.logical_snapshot_id,
                    seed.load_revision,
                    (member.snapshot_ref for member in release_members),
                )
                released = tuple(
                    replace(member, release_ref=release_ref)
                    for member in release_members
                )
                record = build_dataset_release_authority_record(
                    tuple(
                        {**member.to_dict(), "requires_release": True}
                        for member in released
                    )
                )
                records[record.release_ref] = record
                authorized.extend(
                    replace(member, authority_record_ref=record.authority_record_ref)
                    for member in released
                )

            class Resolver:
                def resolve_dataset_release(self, release_ref):
                    return records[release_ref]

            resolver = Resolver()
            return DatasetCatalog(authorized, release_resolver=resolver), resolver

        proposal = {
            "scope": {"type": "full_sample"},
            "grain": "window_id",
            "capability_roles": _required_roles(
                "formula_decompose",
                "market_health_compare",
                "event_evidence",
            ),
            "question_families": ["paid_amount_change_explanation"],
            "target_metrics": ["active_users"],
            "requested_components": ["paid_amount"],
            "requested_dimensions": [],
            "requested_context_sources": ["external_event"],
            "metric_dataset_overrides": {
                "active_users": "market_dashboard",
                "paid_amount": "paid_order_success",
                "paid_users": "paid_order_success",
                "paid_orders": "paid_order_success",
                "first_paid_users": "paid_order_success",
                "paid_frequency": "paid_order_success",
                "avg_order_amount": "paid_order_success",
            },
            "claim_intents": [
                "comparative_change",
                "formula_component_contribution",
                "candidate_mechanism",
            ],
        }
        capabilities = (
            "formula_decompose",
            "market_health_compare",
            "event_evidence",
        )
        available = {
            "paid_order_success": current_snapshot("paid_order_success"),
            "market_dashboard": current_snapshot("market_dashboard"),
            "external_event": current_snapshot("external_event"),
        }
        cases = (
            (
                "paid_order_success",
                "formula_decompose",
                ("formula_component_contribution",),
                {
                    "daily_metric_baselines",
                    "event_context_probe",
                },
                {"component_driver_scan"},
            ),
            (
                "external_event",
                "event_evidence",
                ("candidate_mechanism",),
                {
                    "daily_metric_baselines",
                    "component_driver_scan",
                },
                {"event_context_probe"},
            ),
        )
        for (
            missing_dataset,
            affected_capability,
            affected_claim,
            present,
            absent,
        ) in cases:
            with self.subTest(missing_dataset=missing_dataset):
                catalog, resolver = authorized_catalog(
                    *(item for key, item in available.items() if key != missing_dataset)
                )
                outcome = compile_analysis_contract(
                    run_id=f"run-source-isolation-{missing_dataset}",
                    proposal=proposal,
                    accepted_capabilities=capabilities,
                    catalog=catalog,
                    registry=registry,
                    temporal_authority=_single_day_pair_temporal_authority(),
                    as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
                    release_resolver=resolver,
                )
                intents = {item.query_intent for item in outcome.query_contracts}
                self.assertTrue(
                    present <= intents,
                    (
                        intents,
                        [
                            item.gap_id
                            for item in outcome.analysis_contract.contract_gaps
                        ],
                    ),
                )
                self.assertTrue(absent.isdisjoint(intents))
                gap = next(
                    item
                    for item in outcome.analysis_contract.contract_gaps
                    if item.dataset_id == missing_dataset
                )
                self.assertEqual(gap.affected_capabilities, (affected_capability,))
                self.assertEqual(gap.affected_claim_types, affected_claim)

    def test_canonical_paid_release_fixture_threads_one_authority_resolver(self):
        paid = snapshot("paid_order_success", "paid", "2026-07-04")

        catalog, resolver, released = canonical_release_catalog(paid)

        self.assertIs(catalog._release_resolver, resolver)
        self.assertEqual(len(released), 1)
        self.assertTrue(released[0].release_ref)
        self.assertTrue(released[0].authority_record_ref)
        selected = catalog.resolve(
            "paid_order_success",
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )
        self.assertEqual(selected.snapshot_ref, paid.snapshot_ref)

    def test_obligation_capability_order_is_stable_across_input_order(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        expected = registry.order_capabilities(registry.public_capability_ids)
        self.assertEqual(
            expected,
            registry.order_capabilities(reversed(registry.public_capability_ids)),
        )
        self.assertEqual(set(expected), set(registry.public_capability_ids))
        internal_capabilities = set(registry.capability_ids) - set(
            registry.public_capability_ids
        )
        self.assertTrue(internal_capabilities)
        with self.assertRaisesRegex(
            ValueError,
            "runtime_obligation_unknown_capability:order",
        ):
            registry.order_capabilities((min(internal_capabilities),))

    def test_capability_metric_gaps_merge_ownership_by_stable_semantic_identity(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        capability_orders = (
            (
                "market_health_compare",
                "market_channel_context",
                "source_reconciliation",
            ),
            (
                "source_reconciliation",
                "market_health_compare",
                "market_channel_context",
            ),
        )
        merged_payloads = []
        for capabilities in capability_orders:
            outcome = compile_analysis_contract(
                run_id="run-gap-identity-" + capabilities[0],
                proposal={
                    "scope": {"type": "full_sample"},
                    "grain": "window_id",
                    "capability_roles": _required_roles(*capabilities),
                    "target_metrics": ("paid_users", "paid_orders"),
                    "claim_intents": ("comparative_change",),
                },
                accepted_capabilities=capabilities,
                catalog=DatasetCatalog(()),
                registry=registry,
                temporal_authority=_single_day_pair_temporal_authority(),
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            )
            unsupported = tuple(
                gap
                for gap in outcome.analysis_contract.contract_gaps
                if gap.gap_type == "capability_metric_unsupported"
            )
            paid_users = tuple(
                gap for gap in unsupported if "metric:paid_users:" in gap.gap_id
            )
            paid_orders = tuple(
                gap for gap in unsupported if "metric:paid_orders:" in gap.gap_id
            )
            self.assertEqual(len(paid_users), 1)
            self.assertEqual(len(paid_orders), 1)
            self.assertEqual(
                paid_users[0].affected_capabilities,
                tuple(
                    sorted(
                        capability
                        for capability in capabilities
                        if capability != "market_health_compare"
                    )
                ),
            )
            self.assertTrue(
                any(
                    gap.gap_type != "capability_metric_unsupported"
                    and "metric:paid_users:" in gap.gap_id
                    for gap in outcome.analysis_contract.contract_gaps
                )
            )
            merged_payloads.append(paid_users[0].to_dict())

        self.assertEqual(merged_payloads[0], merged_payloads[1])

    def test_requested_metrics_are_scoped_to_each_capability(self):
        dashboard, channel = _market_dashboard_snapshots()
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )

        cases = (
            (
                ("active_users",),
                {
                    "market_health_compare": (
                        "active_users",
                        "aggregate_marketing_cost",
                        "new_users",
                        "profit",
                        "registrations",
                    ),
                    "source_reconciliation": (),
                },
            ),
            (
                ("paid_amount",),
                {
                    "market_health_compare": (
                        "active_users",
                        "aggregate_marketing_cost",
                        "new_users",
                        "profit",
                        "registrations",
                    ),
                    "source_reconciliation": ("paid_amount", "paid_amount"),
                },
            ),
            (
                ("paid_amount", "active_users"),
                {
                    "market_health_compare": (
                        "active_users",
                        "aggregate_marketing_cost",
                        "new_users",
                        "profit",
                        "registrations",
                    ),
                    "source_reconciliation": ("paid_amount", "paid_amount"),
                },
            ),
        )
        for target_metrics, expected in cases:
            with self.subTest(target_metrics=target_metrics):
                outcome = compile_analysis_contract(
                    run_id="run-capability-local-metrics-" + "-".join(target_metrics),
                    proposal={
                        "scope": {"type": "full_sample"},
                        "grain": "window_id",
                        "capability_roles": _required_roles(
                            "market_health_compare",
                            "source_reconciliation",
                        ),
                        "target_metrics": target_metrics,
                        "claim_intents": (
                            "comparative_change",
                            "source_reconciliation",
                        ),
                    },
                    accepted_capabilities=(
                        "market_health_compare",
                        "source_reconciliation",
                    ),
                    catalog=released_catalog(dashboard, channel),
                    registry=registry,
                    temporal_authority=_single_day_pair_temporal_authority(),
                    as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
                )
                queries = {
                    item.query_contract_id: item for item in outcome.query_contracts
                }
                plans = {item.capability_id: item for item in outcome.capability_plans}
                actual = {
                    capability_id: tuple(
                        metric.metric_id
                        for slot in plans[capability_id].required_input_slots
                        for query_ref in slot.query_contract_refs
                        for metric in queries[query_ref].metric_bindings
                    )
                    for capability_id in expected
                }
                self.assertEqual(actual, expected)
                unsupported = tuple(
                    gap
                    for gap in outcome.analysis_contract.contract_gaps
                    if gap.gap_type == "capability_metric_unsupported"
                )
                if "active_users" in target_metrics:
                    self.assertTrue(
                        any(
                            gap.affected_capabilities == ("source_reconciliation",)
                            and "active_users" in gap.gap_id
                            for gap in unsupported
                        ),
                        unsupported,
                    )
                self.assertFalse(
                    any(
                        gap.affected_capabilities == ("market_health_compare",)
                        for gap in unsupported
                    ),
                    unsupported,
                )

    def test_market_health_capability_selects_unique_dashboard_sources_without_override(
        self,
    ):
        dashboard = DatasetSnapshot(
            "snapshot:market:verified",
            "market_dashboard",
            "market_dashboard_daily__schema1234567890",
            "2026-06-02",
            "schema1234567890abcdef",
            (
                "snapshot_id",
                "load_revision",
                "business_date",
                "game",
                "active_users",
                "new_users",
                "registrations",
                "aggregate_marketing_cost",
                "profit",
                "paid_amount",
            ),
            "contract:market-dashboard@1",
            "2026-06-03T00:00:00Z",
            "active",
            logical_snapshot_id="dashboard-logical",
            load_revision="dashboard-load:sha256:verified",
        )
        object.__setattr__(dashboard, "release_ref", "dataset-release:sha256:verified")
        object.__setattr__(dashboard, "rows_content_hash", "a" * 64)

        outcome = compile_analysis_contract(
            run_id="run-market-health",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("market_health_compare"),
                "target_metrics": [
                    "active_users",
                    "new_users",
                    "aggregate_marketing_cost",
                    "profit",
                ],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("market_health_compare",),
            catalog=released_catalog(dashboard),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(
            outcome.analysis_contract.dataset_requirements, ("market_dashboard",)
        )
        self.assertEqual(
            {
                binding.metric_id
                for binding in outcome.analysis_contract.metric_bindings
            },
            {
                "active_users",
                "new_users",
                "registrations",
                "aggregate_marketing_cost",
                "profit",
            },
        )
        self.assertEqual(len(outcome.query_contracts), 1)

    def test_market_health_uses_context_metrics_when_target_is_owned_elsewhere(
        self,
    ):
        dashboard = DatasetSnapshot(
            "snapshot:market:verified",
            "market_dashboard",
            "market_dashboard_daily__schema1234567890",
            "2026-06-02",
            "schema1234567890abcdef",
            (
                "snapshot_id",
                "load_revision",
                "business_date",
                "game",
                "active_users",
                "new_users",
                "registrations",
                "aggregate_marketing_cost",
                "profit",
            ),
            "contract:market-dashboard@1",
            "2026-06-03T00:00:00Z",
            "active",
            logical_snapshot_id="dashboard-logical",
            load_revision="dashboard-load:sha256:verified",
        )
        object.__setattr__(dashboard, "release_ref", "dataset-release:sha256:verified")
        object.__setattr__(dashboard, "rows_content_hash", "a" * 64)

        outcome = compile_analysis_contract(
            run_id="run-market-health-unreviewed-metric",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("market_health_compare"),
                "target_metrics": ["paid_users"],
            },
            accepted_capabilities=("market_health_compare",),
            catalog=released_catalog(dashboard),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(len(outcome.query_contracts), 1)
        self.assertEqual(
            tuple(
                binding.metric_id
                for binding in outcome.query_contracts[0].metric_bindings
            ),
            (
                "active_users",
                "aggregate_marketing_cost",
                "new_users",
                "profit",
                "registrations",
            ),
        )
        self.assertNotIn(
            "metric:paid_users:capability_metric_family_unsupported",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )

    def test_channel_context_capability_adds_same_source_total_reconciliation(self):
        channel = DatasetSnapshot(
            "snapshot:channel:context",
            "market_dashboard_channel",
            "market_dashboard_channel_daily__schema1234567890",
            "2026-06-02",
            "schema1234567890abcdef",
            (
                "snapshot_id",
                "load_revision",
                "business_date",
                "game",
                "channel",
                "paid_amount",
            ),
            "contract:market-dashboard@1",
            "2026-06-03T00:00:00Z",
            "active",
            evidence_state="context_only",
            reconciliation_status="mismatch",
            reconciliation_ref="reconciliation:mismatch",
            logical_snapshot_id="dashboard-logical",
            load_revision="dashboard-load:sha256:verified",
        )
        object.__setattr__(channel, "release_ref", "dataset-release:sha256:verified")
        object.__setattr__(channel, "rows_content_hash", "b" * 64)

        outcome = compile_analysis_contract(
            run_id="run-channel-context",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("market_channel_context"),
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
                "claim_intents": ["contract_coverage_and_trust_boundary"],
            },
            accepted_capabilities=("market_channel_context",),
            catalog=released_catalog(channel),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(len(outcome.query_contracts), 2)
        detail = next(
            item for item in outcome.query_contracts if item.dimension_bindings
        )
        total = next(
            item for item in outcome.query_contracts if not item.dimension_bindings
        )
        self.assertEqual(detail.query_intent, "channel_context_probe")
        self.assertEqual(detail.dimension_bindings[0].dimension_id, "channel")
        self.assertEqual(
            detail.result_shape.dimension_presence_policy, "sparse_allowed"
        )
        self.assertIn("overall_channel_reconciliation", detail.completeness_assertions)
        self.assertIsNotNone(detail.reconciliation_binding)
        self.assertEqual(total.query_intent, "channel_context_total_probe")
        self.assertEqual(total.dataset_snapshot_refs, detail.dataset_snapshot_refs)

    def test_malformed_source_override_is_a_hard_contract_error(self):
        with self.assertRaisesRegex(ValueError, "metric_dataset_overrides.*mapping"):
            compile_analysis_contract(
                run_id="run-malformed-source-override",
                proposal={
                    "scope": {"type": "full_sample"},
                    "grain": "window_id",
                    "capability_roles": _required_roles("compare_periods"),
                    "target_metrics": ["paid_amount"],
                    "metric_dataset_overrides": "market_dashboard",
                },
                accepted_capabilities=("compare_periods",),
                catalog=DatasetCatalog(
                    (snapshot("paid_order_success", "paid", "2026-07-04"),)
                ),
                registry=RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                ),
                temporal_authority=_single_day_pair_temporal_authority(),
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            )

    def test_unreviewed_context_snapshot_blocks_quality_and_strong_paths(self):
        channel = DatasetSnapshot(
            "snapshot:channel:context",
            "market_dashboard_channel",
            "market_dashboard_channel_daily__schema1234567890",
            "2026-06-02",
            "schema1234567890abcdef",
            (
                "snapshot_id",
                "load_revision",
                "business_date",
                "game",
                "channel",
                "paid_amount",
            ),
            "contract:market-dashboard@1",
            "2026-06-03T00:00:00Z",
            "active",
            evidence_state="context_only",
            reconciliation_status="mismatch",
            reconciliation_ref="reconciliation:mismatch",
            logical_snapshot_id="dashboard-logical",
            load_revision="dashboard-load:sha256:verified",
        )
        object.__setattr__(channel, "release_ref", "dataset-release:sha256:verified")
        object.__setattr__(channel, "rows_content_hash", "b" * 64)

        outcome = compile_analysis_contract(
            run_id="run-mixed-evidence-purpose",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "data_quality_profile",
                    "candidate_dimension_screen",
                ),
                "target_metrics": ["paid_amount"],
                "dataset_requirements": ["market_dashboard_channel"],
                "requested_dimensions": ["channel"],
                "claim_intents": [
                    "contract_coverage_and_trust_boundary",
                    "segment_contribution_or_mix_shift",
                ],
            },
            accepted_capabilities=(
                "data_quality_profile",
                "candidate_dimension_screen",
            ),
            catalog=released_catalog(channel),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertFalse(outcome.query_contracts)
        metric_gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id
            == (
                "metric:paid_amount:requested_source_unreviewed:"
                "market_dashboard_channel"
            )
        )
        self.assertEqual(
            set(metric_gap.affected_capabilities),
            {"data_quality_profile", "candidate_dimension_screen"},
        )
        self.assertEqual(
            set(metric_gap.affected_claim_types),
            {
                "contract_coverage_and_trust_boundary",
                "segment_contribution_or_mix_shift",
            },
        )

    def test_source_reconciliation_capability_plans_both_reviewed_sources(self):
        fields = (
            "snapshot_id",
            "load_revision",
            "business_date",
            "game",
            "paid_amount",
        )
        overall = DatasetSnapshot(
            "snapshot:overall:verified",
            "market_dashboard",
            "market_dashboard_daily__schema1234567890",
            "2026-06-02",
            "schema1234567890abcdef",
            fields,
            "contract:dashboard@1",
            "2026-06-03T00:00:00Z",
            "active",
            reconciliation_status="mismatch",
            reconciliation_ref="reconciliation:mismatch",
            logical_snapshot_id="dashboard-logical",
            load_revision="dashboard-load:sha256:verified",
        )
        channel = DatasetSnapshot(
            "snapshot:channel:verified",
            "market_dashboard_channel",
            "market_dashboard_channel_daily__schema1234567890",
            "2026-06-02",
            "schema1234567890abcdef",
            (*fields[:4], "channel", *fields[4:]),
            "contract:dashboard@1",
            "2026-06-03T00:00:00Z",
            "active",
            evidence_state="context_only",
            reconciliation_status="mismatch",
            reconciliation_ref="reconciliation:mismatch",
            logical_snapshot_id="dashboard-logical",
            load_revision="dashboard-load:sha256:verified",
        )
        for item in (overall, channel):
            object.__setattr__(item, "release_ref", "dataset-release:sha256:verified")
            object.__setattr__(item, "rows_content_hash", "c" * 64)

        outcome = compile_analysis_contract(
            run_id="run-source-reconciliation",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("source_reconciliation"),
                "target_metrics": ["paid_amount"],
                "dataset_requirements": [
                    "market_dashboard",
                    "market_dashboard_channel",
                ],
                "claim_intents": ["source_reconciliation"],
            },
            accepted_capabilities=("source_reconciliation",),
            catalog=released_catalog(overall, channel),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(
            {
                binding.dataset_id
                for binding in outcome.analysis_contract.metric_bindings
            },
            {"market_dashboard", "market_dashboard_channel"},
        )
        self.assertEqual(
            {query.query_intent for query in outcome.query_contracts},
            {"source_reconciliation_probe"},
        )
        self.assertEqual(len(outcome.query_contracts), 2)

    def test_explicit_metric_override_cannot_bypass_capability_dataset_contract(self):
        dashboard_snapshot = DatasetSnapshot(
            "snapshot:market-dashboard:20260602:revision-a",
            "market_dashboard",
            "market_dashboard_daily",
            "2026-06-02",
            "schema:market-dashboard:1",
            ("business_date", "load_revision", "paid_amount"),
            "contract:market-dashboard@1",
            "2026-06-03T00:00:00Z",
            "active",
            logical_snapshot_id="market-dashboard:20260602",
            load_revision="sha256:revision-a",
        )

        outcome = compile_analysis_contract(
            run_id="run-dashboard-source-adapter",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
                "metric_dataset_overrides": {"paid_amount": "market_dashboard"},
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=released_catalog(dashboard_snapshot),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(outcome.analysis_contract.dataset_requirements, ())
        self.assertFalse(outcome.analysis_contract.metric_bindings)
        self.assertFalse(outcome.query_contracts)
        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id
            == "metric:paid_amount:requested_source_unreviewed:market_dashboard"
        )
        self.assertEqual(
            gap.affected_capabilities,
            ("compare_periods",),
        )
        self.assertEqual(gap.affected_claim_types, ("comparative_change",))

    def test_explicit_reviewed_metric_override_remains_bound(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        paid = snapshot("paid_order_success", "paid", "2026-07-04")
        catalog, resolver, released = canonical_release_catalog(paid)
        outcome = compile_analysis_contract(
            run_id="run-reviewed-source-override",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
                "metric_dataset_overrides": {"paid_amount": "paid_order_success"},
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=catalog,
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            release_resolver=resolver,
        )

        self.assertEqual(
            outcome.analysis_contract.dataset_requirements,
            ("paid_order_success",),
        )
        self.assertEqual(
            outcome.query_contracts[0].dataset_snapshot_refs,
            (released[0].snapshot_ref,),
        )
        self.assertFalse(
            any(
                "requested_source_unreviewed" in gap.gap_id
                for gap in outcome.analysis_contract.contract_gaps
            )
        )

    def test_explicit_dimension_override_cannot_bypass_capability_dataset_contract(
        self,
    ):
        channel_snapshot = DatasetSnapshot(
            "snapshot:market-dashboard-channel:20260602:revision-a",
            "market_dashboard_channel",
            "market_dashboard_channel_daily",
            "2026-06-02",
            "schema:market-dashboard-channel:1",
            ("business_date", "load_revision", "paid_amount", "channel"),
            "contract:market-dashboard-channel@1",
            "2026-06-03T00:00:00Z",
            "active",
            reconciliation_status="matched",
            reconciliation_ref="reconciliation:market-dashboard:revision-a",
            logical_snapshot_id="market-dashboard-channel:20260602",
            load_revision="sha256:revision-a",
        )

        outcome = compile_analysis_contract(
            run_id="run-dashboard-dimension-adapter",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("candidate_dimension_screen"),
                "target_metrics": ["paid_amount"],
                "metric_dataset_overrides": {"paid_amount": "market_dashboard_channel"},
                "requested_dimensions": ["channel"],
                "dimension_dataset_overrides": {"channel": "market_dashboard_channel"},
                "claim_intents": ["segment_contribution_or_mix_shift"],
            },
            accepted_capabilities=("candidate_dimension_screen",),
            catalog=released_catalog(channel_snapshot),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(
            outcome.analysis_contract.dataset_requirements,
            ("paid_order_success",),
        )
        self.assertEqual(
            tuple(
                binding.metric_id
                for binding in outcome.analysis_contract.metric_bindings
            ),
            ("paid_orders", "paid_users"),
        )
        self.assertFalse(outcome.analysis_contract.dimension_bindings)
        self.assertFalse(outcome.query_contracts)
        gap_ids = {gap.gap_id for gap in outcome.analysis_contract.contract_gaps}
        self.assertIn(
            "metric:paid_amount:requested_source_unreviewed:market_dashboard_channel",
            gap_ids,
        )
        self.assertIn(
            "dimension:channel:requested_source_unreviewed:market_dashboard_channel",
            gap_ids,
        )

    def test_claim_strength_taxonomy_is_ranked_and_part_of_capability_signature(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        registry = RuntimeContractRegistry(payload)
        original = registry.capability_contract_signature("compare_periods")

        self.assertEqual(registry.claim_strength_rank("observed"), 1)
        self.assertEqual(registry.maximum_claim_strength_rank("directional"), 1)
        with self.assertRaisesRegex(KeyError, "unknown_claim_strength"):
            registry.claim_strength_rank("invented_strength")

        changed = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        changed["claim_strength_taxonomy"]["version"] = "2"
        self.assertNotEqual(
            RuntimeContractRegistry(changed).capability_contract_signature(
                "compare_periods"
            ),
            original,
        )

    def test_runtime_metric_business_labels_cover_every_bound_metric(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        registry = RuntimeContractRegistry(payload)

        self.assertEqual(
            registry.metric_business_labels("payment_success_rate"),
            ("支付成功率",),
        )
        self.assertEqual(
            set(payload["metric_business_labels"]["labels"]),
            set(registry.metric_ids),
        )

        del payload["metric_business_labels"]["labels"]["payment_success_rate"]
        with self.assertRaisesRegex(
            ValueError,
            "runtime_metric_business_labels_incomplete",
        ):
            RuntimeContractRegistry(payload)

    def test_claim_strength_taxonomy_rejects_reversed_duplicate_and_unknown_ranks(self):
        cases = {
            "reversed": lambda value: value["claim_strength_ranks"].update(
                {"observed": 3, "medium": 2}
            ),
            "duplicate": lambda value: value["claim_strength_ranks"].update(
                {"observed": 2, "medium": 2}
            ),
            "zero_layer": lambda value: value["claim_strength_ranks"].update(
                {"context_only": 1}
            ),
            "unknown": lambda value: value["maximum_strength_ranks"].update(
                {"invented_category": 1}
            ),
            "ceiling": lambda value: value["maximum_strength_ranks"].update(
                {"verifier_only": 1}
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                payload = load_contract(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                )
                mutate(payload["claim_strength_taxonomy"])
                with self.assertRaisesRegex(
                    ValueError,
                    "runtime_claim_strength_taxonomy",
                ):
                    RuntimeContractRegistry(payload)

    def test_capability_policy_drift_changes_canonical_contract_ref(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        changed = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        changed["capability_inputs"]["compare_periods"]["degradation_policy"] = {
            "missing_required_input": "block_claim",
            "incomplete_input": "context_only",
        }
        catalogs = DatasetCatalog(
            (snapshot("paid_order_success", "paid", "2026-07-04"),)
        )

        original = compile_analysis_contract(
            run_id="run-capability-signature-original",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=catalogs,
            registry=RuntimeContractRegistry(payload),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )
        drifted = compile_analysis_contract(
            run_id="run-capability-signature-drifted",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=catalogs,
            registry=RuntimeContractRegistry(changed),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertNotEqual(
            original.capability_plans[0].capability_contract_ref,
            drifted.capability_plans[0].capability_contract_ref,
        )
        self.assertNotEqual(
            original.capability_plans[0].capability_contract_signature,
            drifted.capability_plans[0].capability_contract_signature,
        )

    def test_segment_queries_are_independent_and_companions_are_validation_only(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-segment-companion",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("candidate_dimension_screen"),
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel", "region"],
                "claim_intents": ["segment_contribution_or_mix_shift"],
            },
            accepted_capabilities=("candidate_dimension_screen",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        dimension_queries = tuple(
            query for query in outcome.query_contracts if query.dimension_bindings
        )
        self.assertEqual(len(dimension_queries), 2)
        self.assertEqual(
            {
                tuple(binding.dimension_id for binding in query.dimension_bindings)
                for query in dimension_queries
            },
            {("channel",), ("region",)},
        )
        companion_refs = set()
        for dimension_query in dimension_queries:
            binding = dimension_query.reconciliation_binding
            self.assertIsNotNone(binding)
            companion = next(
                query
                for query in outcome.query_contracts
                if query.query_role_ref == binding.reference_query_role_ref
            )
            self.assertFalse(companion.dimension_bindings)
            self.assertEqual(
                binding.reference_contract_signature,
                companion.contract_signature,
            )
            self.assertEqual(
                dimension_query.contract_signature,
                query_contract_signature(dimension_query),
            )
            companion_refs.add(companion.query_contract_id)
        input_slots = outcome.capability_plans[0].required_input_slots
        dimension_slots = tuple(
            slot for slot in input_slots if slot.validation_query_contract_refs
        )
        self.assertEqual(
            outcome.capability_plans[0].analysis_contract_ref,
            outcome.analysis_contract.analysis_contract_id,
        )
        self.assertEqual(
            outcome.capability_plans[0].supported_claim_types,
            ("segment_contribution_or_mix_shift",),
        )
        self.assertEqual(len(input_slots), 2)
        self.assertEqual(len(dimension_slots), 2)
        self.assertEqual(
            {slot.query_contract_refs[0] for slot in dimension_slots},
            {query.query_contract_id for query in dimension_queries},
        )
        self.assertEqual(
            {
                ref
                for slot in dimension_slots
                for ref in slot.validation_query_contract_refs
            },
            companion_refs,
        )
        queries_by_ref = {query.query_contract_id: query for query in dimension_queries}
        for slot in dimension_slots:
            self.assertEqual(len(slot.query_contract_refs), 1)
            primary = queries_by_ref[slot.query_contract_refs[0]]
            self.assertEqual(
                slot.required_fields,
                primary.result_shape.required_fields,
            )
            self.assertEqual(len(slot.validation_query_contract_refs), 1)

    def test_joint_attribution_materializes_only_after_candidate_selection(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        paid = snapshot("paid_order_success", "paid", "2026-07-04")
        temporal_authority = _single_day_pair_temporal_authority()
        proposal = {
            "scope": {"type": "full_sample"},
            "grain": "window_id",
            "capability_roles": _required_roles(
                "candidate_dimension_screen",
                "joint_attribution",
            ),
            "target_metrics": ["paid_amount"],
            "requested_dimensions": ["channel", "region"],
        }
        outcome = compile_analysis_contract(
            run_id="run-dynamic-pair-joint-attribution",
            proposal=proposal,
            accepted_capabilities=(
                "candidate_dimension_screen",
                "joint_attribution",
            ),
            catalog=DatasetCatalog((paid,)),
            registry=registry,
            temporal_authority=temporal_authority,
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )
        self.assertFalse(
            tuple(
                item
                for item in outcome.query_contracts
                if item.query_intent == "joint_candidate_scan"
            )
        )
        joint_plan = next(
            item
            for item in outcome.capability_plans
            if item.capability_id == "joint_attribution"
        )
        self.assertEqual(
            joint_plan.required_input_slots[0].query_contract_refs,
            (),
        )

        expanded = expand_dynamic_dimension_queries(
            outcome,
            run_id="run-dynamic-pair-joint-attribution",
            capability_id="joint_attribution",
            selected_combinations=(("channel", "region"),),
            proposal=proposal,
            snapshots=(paid,),
            registry=registry,
            temporal_authority=temporal_authority,
        )
        joint_queries = tuple(
            item
            for item in expanded.query_contracts
            if item.query_intent == "joint_candidate_scan"
            and item.dimension_bindings
        )
        self.assertEqual(len(joint_queries), 1)
        self.assertEqual(
            tuple(
                item.dimension_id
                for item in joint_queries[0].dimension_bindings
            ),
            ("channel", "region"),
        )
        self.assertEqual(
            tuple(item.metric_id for item in joint_queries[0].metric_bindings),
            ("paid_amount", "paid_orders"),
        )
        self.assertEqual(
            joint_queries[0].result_shape.dimension_presence_policy,
            "sparse_allowed",
        )

    def test_high_value_contribution_uses_the_dynamic_window_pair_adapter(self):
        self._assert_dynamic_pair_adapter("high_value_user_contribution")

    def test_capability_queries_only_use_their_reviewed_datasets(self):
        from bi_agent.runtime.analysis_contract_compiler import (
            _bind_metrics,
            _build_query_contracts,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        dashboard, _ = _market_dashboard_snapshots()
        paid = snapshot("paid_order_success", "paid", "2026-07-04")
        temporal_authority = _single_day_pair_temporal_authority()
        base = compile_analysis_contract(
            run_id="run-capability-reviewed-datasets-base",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("candidate_dimension_screen"),
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
                "claim_intents": ["candidate_driver"],
            },
            accepted_capabilities=("candidate_dimension_screen",),
            catalog=DatasetCatalog((paid,)),
            registry=registry,
            temporal_authority=temporal_authority,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )
        bindings, gaps = _bind_metrics(
            ("paid_amount",),
            registry,
            (paid, dashboard),
            {"paid_amount": ("candidate_dimension_screen",)},
            {"paid_amount": ("paid_order_success", "market_dashboard")},
        )
        self.assertFalse(gaps)
        queries, _ = _build_query_contracts(
            "run-capability-reviewed-datasets",
            "analysis:run-capability-reviewed-datasets:1",
            ("candidate_dimension_screen",),
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("candidate_dimension_screen"),
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
            },
            snapshots=(paid, dashboard),
            windows=base.analysis_contract.resolved_windows,
            metric_bindings=bindings,
            dimension_bindings=base.analysis_contract.dimension_bindings,
            registry=registry,
            temporal_authority=temporal_authority,
        )

        dimension_queries = tuple(
            query
            for query in queries
            if query.query_intent == "dimension_contribution_scan"
        )
        self.assertTrue(dimension_queries)
        self.assertEqual(
            {query.dataset_snapshot_refs[0] for query in dimension_queries},
            {paid.snapshot_ref},
        )

    def test_independent_capability_selects_its_exact_reviewed_dataset_from_requested_sources(
        self,
    ):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        dashboard, channel = _market_dashboard_snapshots()
        catalog, resolver, released = canonical_release_catalog(dashboard, channel)
        released_by_dataset = {item.dataset_id: item for item in released}

        outcome = compile_analysis_contract(
            run_id="run-independent-exact-source",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("market_health_compare"),
                "question_families": ["revenue_health_review"],
                "target_metrics": ["active_users"],
                "dataset_requirements": [
                    "market_dashboard",
                    "market_dashboard_channel",
                ],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("market_health_compare",),
            catalog=catalog,
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            release_resolver=resolver,
        )

        queries = tuple(
            query
            for query in outcome.query_contracts
            if query.query_intent == "daily_metric_baselines"
        )
        self.assertEqual(len(queries), 1)
        self.assertEqual(
            queries[0].dataset_snapshot_refs,
            (released_by_dataset["market_dashboard"].snapshot_ref,),
        )
        plan = outcome.capability_plans[0]
        self.assertEqual(
            plan.required_input_slots[0].query_contract_refs,
            (queries[0].query_contract_id,),
        )
        self.assertNotIn(
            "metric:active_users:source_ambiguous:market_dashboard,market_dashboard_channel",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )

    def test_source_selection_gap_claims_are_partitioned_by_capability_owner(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        event_snapshot = replace(
            snapshot(
                "external_event",
                "business_events__fixed_analysis",
                "2026-06-02",
            ),
            schema_fields=tuple(
                registry.dataset("external_event").get("schema_fields") or ()
            ),
            logical_snapshot_id="external-event:fixed-analysis",
            load_revision="sha256:external-event-fixed-analysis",
        )
        catalog, resolver, _ = canonical_release_catalog(event_snapshot)
        outcome = compile_analysis_contract(
            run_id="run-source-gap-claim-partition",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "market_health_compare",
                    "event_evidence",
                ),
                "target_metrics": ["active_users"],
                "dataset_requirements": [
                    "market_dashboard",
                    "market_dashboard_channel",
                ],
                "requested_context_sources": ["external_event"],
                "claim_intents": [
                    "comparative_change",
                    "candidate_mechanism",
                ],
            },
            accepted_capabilities=("market_health_compare", "event_evidence"),
            catalog=catalog,
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            release_resolver=resolver,
        )

        market_gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id
            == "dataset:market_dashboard:source_unbound"
        )
        self.assertEqual(
            market_gap.affected_capabilities,
            ("market_health_compare",),
        )
        self.assertEqual(
            market_gap.affected_claim_types,
            ("comparative_change",),
        )
        self.assertNotIn("candidate_mechanism", market_gap.affected_claim_types)
        event_plan = next(
            plan
            for plan in outcome.capability_plans
            if plan.capability_id == "event_evidence"
        )
        self.assertTrue(
            any(slot.query_contract_refs for slot in event_plan.required_input_slots)
        )

    def test_data_quality_capabilities_review_only_contract_allowed_datasets(self):
        from bi_agent.runtime.analysis_contract_compiler import (
            _capability_reviews_dataset,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        for capability_id in ("data_quality_check", "data_quality_profile"):
            with self.subTest(capability_id=capability_id):
                self.assertTrue(
                    _capability_reviews_dataset(
                        capability_id, "paid_order_success", registry
                    )
                )
                self.assertTrue(
                    _capability_reviews_dataset(
                        capability_id, "payment_final_outcome", registry
                    )
                )
                self.assertFalse(
                    _capability_reviews_dataset(
                        capability_id, "market_dashboard", registry
                    )
                )

    def test_data_quality_unreviewed_requested_source_produces_exact_gap(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-quality-unreviewed-source",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("data_quality_profile"),
                "target_metrics": ["paid_amount"],
                "dataset_requirements": ["market_dashboard"],
                "claim_intents": ["contract_coverage_and_trust_boundary"],
            },
            accepted_capabilities=("data_quality_profile",),
            catalog=DatasetCatalog(()),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id
            == "metric:paid_amount:requested_source_unreviewed:market_dashboard"
        )
        self.assertEqual(gap.affected_capabilities, ("data_quality_profile",))
        self.assertEqual(
            gap.affected_claim_types,
            ("contract_coverage_and_trust_boundary",),
        )
        self.assertFalse(outcome.query_contracts)

    def test_data_quality_allowed_paid_source_remains_bound(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        paid = snapshot("paid_order_success", "paid", "2026-07-04")
        catalog, resolver, released = canonical_release_catalog(paid)
        outcome = compile_analysis_contract(
            run_id="run-quality-reviewed-source",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("data_quality_profile"),
                "target_metrics": ["paid_amount"],
                "dataset_requirements": ["paid_order_success"],
                "claim_intents": ["contract_coverage_and_trust_boundary"],
            },
            accepted_capabilities=("data_quality_profile",),
            catalog=catalog,
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            release_resolver=resolver,
        )

        self.assertTrue(outcome.query_contracts)
        self.assertEqual(
            outcome.query_contracts[0].dataset_snapshot_refs,
            (released[0].snapshot_ref,),
        )
        self.assertIn(
            "country_scope_violation_count",
            outcome.query_contracts[0].result_shape.required_fields,
        )
        self.assertFalse(
            any(
                "requested_source_unreviewed" in gap.gap_id
                for gap in outcome.analysis_contract.contract_gaps
            )
        )

    def test_cross_source_quality_owner_does_not_widen_strong_capability_binding(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        dashboard, channel = _market_dashboard_snapshots()
        catalog, resolver, released = canonical_release_catalog(dashboard, channel)
        released_by_dataset = {item.dataset_id: item for item in released}

        outcome = compile_analysis_contract(
            run_id="run-purpose-scoped-source-selection",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "market_health_compare",
                    "data_quality_profile",
                ),
                "target_metrics": ["active_users", "paid_amount"],
                "dataset_requirements": [
                    "market_dashboard",
                    "market_dashboard_channel",
                ],
                "claim_intents": [
                    "comparative_change",
                    "contract_coverage_and_trust_boundary",
                ],
            },
            accepted_capabilities=(
                "market_health_compare",
                "data_quality_profile",
            ),
            catalog=catalog,
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            release_resolver=resolver,
        )

        queries_by_ref = {
            query.query_contract_id: query for query in outcome.query_contracts
        }
        plans = {plan.capability_id: plan for plan in outcome.capability_plans}
        market_ref = (
            plans["market_health_compare"].required_input_slots[0].query_contract_refs
        )
        self.assertEqual(len(market_ref), 1)
        self.assertEqual(
            queries_by_ref[market_ref[0]].dataset_snapshot_refs,
            (released_by_dataset["market_dashboard"].snapshot_ref,),
        )
        self.assertEqual(
            tuple(
                item.metric_id
                for item in queries_by_ref[market_ref[0]].metric_bindings
            ),
            (
                "active_users",
                "aggregate_marketing_cost",
                "new_users",
                "profit",
                "registrations",
            ),
        )
        quality_refs = tuple(
            ref
            for slot in plans["data_quality_profile"].required_input_slots
            for ref in slot.query_contract_refs
        )
        self.assertEqual(quality_refs, ())
        gaps = outcome.analysis_contract.contract_gaps
        quality_gap = next(
            gap
            for gap in gaps
            if gap.gap_id
            == (
                "metric:paid_amount:requested_source_unreviewed:"
                "market_dashboard,market_dashboard_channel"
            )
        )
        self.assertEqual(
            quality_gap.affected_capabilities,
            ("data_quality_profile",),
        )
        self.assertEqual(
            quality_gap.affected_claim_types,
            ("contract_coverage_and_trust_boundary",),
        )

    def test_explicit_unreviewed_source_is_audited_while_strong_query_stays_bound(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        dashboard, channel = _market_dashboard_snapshots()
        catalog, resolver, _ = canonical_release_catalog(dashboard, channel)

        outcome = compile_analysis_contract(
            run_id="run-explicit-unreviewed-source",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("market_health_compare"),
                "target_metrics": ["active_users"],
                "dataset_requirements": [
                    "market_dashboard",
                    "market_dashboard_channel",
                ],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("market_health_compare",),
            catalog=catalog,
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            release_resolver=resolver,
        )

        self.assertTrue(outcome.query_contracts)
        self.assertEqual(
            outcome.query_contracts[0].dataset_snapshot_refs,
            (next(item for item in catalog.snapshots() if item.dataset_id == "market_dashboard").snapshot_ref,),
        )

    def test_disjoint_strong_capabilities_bind_their_own_reviewed_sources(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        dashboard, channel = _market_dashboard_snapshots()
        catalog, resolver, released = canonical_release_catalog(dashboard, channel)
        released_by_dataset = {item.dataset_id: item for item in released}

        outcome = compile_analysis_contract(
            run_id="run-disjoint-reviewed-sources",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "market_health_compare",
                    "market_channel_context",
                ),
                "target_metrics": ["active_users", "paid_amount"],
                "dataset_requirements": [
                    "market_dashboard",
                    "market_dashboard_channel",
                ],
                "requested_dimensions": ["channel"],
                "claim_intents": [
                    "comparative_change",
                    "contract_coverage_and_trust_boundary",
                ],
            },
            accepted_capabilities=(
                "market_health_compare",
                "market_channel_context",
            ),
            catalog=catalog,
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            release_resolver=resolver,
        )

        queries_by_ref = {
            query.query_contract_id: query for query in outcome.query_contracts
        }
        plans = {plan.capability_id: plan for plan in outcome.capability_plans}
        expected = {
            "market_health_compare": (
                "daily_metric_baselines",
                released_by_dataset["market_dashboard"].snapshot_ref,
            ),
            "market_channel_context": (
                "channel_context_probe",
                released_by_dataset["market_dashboard_channel"].snapshot_ref,
            ),
        }
        for capability_id, (query_intent, snapshot_ref) in expected.items():
            refs = tuple(
                ref
                for slot in plans[capability_id].required_input_slots
                for ref in slot.query_contract_refs
            )
            self.assertEqual(len(refs), 1)
            query = queries_by_ref[refs[0]]
            self.assertEqual(query.query_intent, query_intent)
            self.assertEqual(query.dataset_snapshot_refs, (snapshot_ref,))
        channel_query = next(
            query
            for query in outcome.query_contracts
            if query.query_intent == "channel_context_probe"
        )
        self.assertEqual(
            channel_query.result_shape.dimension_presence_policy,
            "sparse_allowed",
        )
        self.assertIn(
            "overall_channel_reconciliation",
            channel_query.completeness_assertions,
        )
        self.assertIsNotNone(channel_query.reconciliation_binding)
        channel_total = next(
            query
            for query in outcome.query_contracts
            if query.query_role_ref
            == channel_query.reconciliation_binding.reference_query_role_ref
        )
        self.assertEqual(channel_total.query_intent, "channel_context_total_probe")
        self.assertEqual(channel_total.dimension_bindings, ())
        self.assertFalse(
            any(
                "source_ambiguous" in gap.gap_id
                for gap in outcome.analysis_contract.contract_gaps
            )
        )

    def test_explicit_claim_outside_capability_ceiling_is_rejected(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-claim-ceiling",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["causal_effect"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(
            outcome.analysis_contract.claim_intents, ("unbound_claim_intent",)
        )
        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id == "claim_intent:causal_effect:unsupported"
        )
        self.assertEqual(gap.gap_type, "contract_partial")
        self.assertEqual(gap.affected_claim_types, ("causal_effect",))
        self.assertEqual(gap.owner, "contract_owner")
        self.assertEqual(
            gap.repair_options,
            ("add_supporting_capability", "report_unavailable_claim"),
        )
        self.assertFalse(gap.requires_clarification)
        self.assertEqual(
            dict(gap.diagnostic_context),
            {
                "claim_origin": "user_required",
                "publication_status": "unavailable",
            },
        )

    def test_missing_dataset_date_field_blocks_query_with_typed_gap(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        missing_date = DatasetSnapshot(
            "snapshot:paid:no-date",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:no-date",
            ("paid_amount_ngn",),
            "contract:paid@1",
            "2026-06-03T00:00:00Z",
            "active",
        )
        outcome = compile_analysis_contract(
            run_id="run-no-date",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog((missing_date,)),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertIn(
            "dataset:paid_order_success:schema_missing:business_date_lagos",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )
        self.assertFalse(outcome.query_contracts)

    def test_invalid_dataset_execution_contract_is_typed_and_blocks_query(self):
        cases = {
            "missing": lambda contract: (
                contract.pop("required_fields", None),
                contract.pop("date_field", None),
            ),
            "dual_date_source": lambda contract: contract.update(
                date_expression="toDate(business_date_lagos)"
            ),
            "empty": lambda contract: contract.update(
                required_fields=[],
                date_field="",
            ),
        }
        for case_name, mutate in cases.items():
            with self.subTest(case_name=case_name):
                payload = load_contract(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                )
                mutate(payload["datasets"]["paid_order_success"])
                outcome = self._compile_compare_with_registry(
                    RuntimeContractRegistry(payload)
                )

                gap = next(
                    gap
                    for gap in outcome.analysis_contract.contract_gaps
                    if gap.dataset_id == "paid_order_success"
                    and gap.owner == "contract_owner"
                )
                self.assertEqual(gap.gap_type, "contract_partial")
                self.assertEqual(
                    gap.repair_options,
                    ("complete_dataset_contract",),
                )
                self.assertEqual(
                    gap.affected_capabilities,
                    ("compare_periods",),
                )
                self.assertFalse(outcome.query_contracts)

    def test_valid_dataset_date_field_and_expression_compile(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        field_outcome = self._compile_compare_with_registry(registry)

        expression_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        dataset_contract = expression_payload["datasets"]["paid_order_success"]
        dataset_contract.pop("date_field")
        dataset_contract["date_expression"] = "toDate(business_date_lagos)"
        expression_outcome = self._compile_compare_with_registry(
            RuntimeContractRegistry(expression_payload)
        )

        self.assertEqual(len(field_outcome.query_contracts), 1)
        self.assertEqual(len(expression_outcome.query_contracts), 1)
        for outcome in (field_outcome, expression_outcome):
            self.assertFalse(
                [
                    gap
                    for gap in outcome.analysis_contract.contract_gaps
                    if gap.dataset_id == "paid_order_success"
                    and gap.owner == "contract_owner"
                ]
            )

    def test_missing_metric_field_blocks_binding_and_query(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        missing_metric = DatasetSnapshot(
            "snapshot:paid:no-amount",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:no-amount",
            ("business_date_lagos",),
            "contract:paid@1",
            "2026-06-03T00:00:00Z",
            "active",
        )
        outcome = compile_analysis_contract(
            run_id="run-no-metric-field",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog((missing_metric,)),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertIn(
            "metric:paid_amount:schema_missing:paid_amount_ngn",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )
        self.assertFalse(outcome.analysis_contract.metric_bindings)
        self.assertFalse(outcome.query_contracts)

    def test_missing_dimension_field_blocks_binding_and_segment_query(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        missing_dimension = DatasetSnapshot(
            "snapshot:paid:no-channel",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:no-channel",
            ("business_date_lagos", "paid_amount_ngn"),
            "contract:paid@1",
            "2026-06-03T00:00:00Z",
            "active",
        )
        outcome = compile_analysis_contract(
            run_id="run-no-dimension-field",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("candidate_dimension_screen"),
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
                "claim_intents": ["segment_contribution_or_mix_shift"],
            },
            accepted_capabilities=("candidate_dimension_screen",),
            catalog=DatasetCatalog((missing_dimension,)),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertIn(
            "dimension:channel:schema_missing:channel",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )
        self.assertFalse(outcome.analysis_contract.dimension_bindings)
        self.assertFalse(outcome.query_contracts)

    def test_semantic_query_signature_is_run_independent_and_input_complete(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )

        def compile_one(run_id, *, filters=(), catalog=None, selected_registry=None):
            return compile_analysis_contract(
                run_id=run_id,
                proposal={
                    "scope": {"type": "full_sample"},
                    "grain": "window_id",
                    "capability_roles": _required_roles("compare_periods"),
                    "target_metrics": ["paid_amount"],
                    "claim_intents": ["comparative_change"],
                    "filters": filters,
                },
                accepted_capabilities=("compare_periods",),
                catalog=catalog
                or DatasetCatalog(
                    (snapshot("paid_order_success", "paid", "2026-07-04"),)
                ),
                registry=selected_registry or registry,
                temporal_authority=_single_day_pair_temporal_authority(),
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            )

        first = compile_one("run-signature-a").query_contracts[0]
        second = compile_one("run-signature-b").query_contracts[0]
        filtered = compile_one(
            "run-signature-filter",
            filters=({"field": "channel", "op": "eq", "value": "A"},),
        ).query_contracts[0]
        other_snapshot = DatasetSnapshot(
            "snapshot:paid:other",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:other",
            snapshot("paid_order_success", "paid", "2026-07-04").schema_fields,
            "contract:paid@1",
            "2026-06-03T00:00:00Z",
            "active",
        )
        snapshot_changed = compile_one(
            "run-signature-snapshot",
            catalog=DatasetCatalog((other_snapshot,)),
        ).query_contracts[0]
        changed_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload["metrics"]["paid_amount"]["expression"] = (
            "sum(paid_amount_ngn) * 1"
        )
        binding_changed = compile_one(
            "run-signature-binding",
            selected_registry=RuntimeContractRegistry(changed_payload),
        ).query_contracts[0]

        self.assertNotEqual(first.query_contract_id, second.query_contract_id)
        self.assertEqual(first.contract_signature, second.contract_signature)
        self.assertEqual(first.workload_class, "interactive_aggregate")
        self.assertNotEqual(first.contract_signature, filtered.contract_signature)
        self.assertNotEqual(
            first.contract_signature, snapshot_changed.contract_signature
        )
        self.assertNotEqual(
            first.contract_signature, binding_changed.contract_signature
        )

    def test_metric_reconciliation_tolerance_is_bound_and_signed(self):
        base_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload["metrics"]["paid_amount"]["reconciliation_tolerance"] = 0.25

        base = self._compile_compare_with_registry(
            RuntimeContractRegistry(base_payload)
        ).query_contracts[0]
        changed = self._compile_compare_with_registry(
            RuntimeContractRegistry(changed_payload)
        ).query_contracts[0]

        self.assertEqual(base.metric_bindings[0].reconciliation_tolerance, 0.01)
        self.assertEqual(changed.metric_bindings[0].reconciliation_tolerance, 0.25)
        self.assertNotEqual(base.contract_signature, changed.contract_signature)

    def test_metric_display_policy_is_bound_and_signed(self):
        base_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        changed_payload["metrics"]["paid_amount"].update(
            {
                "value_semantics": "scalar_ratio",
                "display_format": "percent",
            }
        )

        base = self._compile_compare_with_registry(
            RuntimeContractRegistry(base_payload)
        ).query_contracts[0]
        changed = self._compile_compare_with_registry(
            RuntimeContractRegistry(changed_payload)
        ).query_contracts[0]

        self.assertEqual(base.metric_bindings[0].value_semantics, "raw_scalar")
        self.assertEqual(base.metric_bindings[0].display_format, "number")
        self.assertEqual(changed.metric_bindings[0].value_semantics, "scalar_ratio")
        self.assertEqual(changed.metric_bindings[0].display_format, "percent")
        self.assertNotEqual(base.contract_signature, changed.contract_signature)

    def test_invalid_or_missing_metric_display_policy_blocks_binding(self):
        for case_id, mutate in (
            (
                "invalid_pair",
                lambda metric: metric.update(
                    {"value_semantics": "raw_scalar", "display_format": "percent"}
                ),
            ),
            ("missing", lambda metric: metric.pop("display_format")),
        ):
            payload = load_contract(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            )
            mutate(payload["metrics"]["paid_amount"])

            outcome = self._compile_compare_with_registry(
                RuntimeContractRegistry(payload)
            )

            with self.subTest(case_id=case_id):
                self.assertFalse(outcome.query_contracts)
                self.assertTrue(
                    any(
                        gap.gap_id.startswith("metric:paid_amount:")
                        for gap in outcome.analysis_contract.contract_gaps
                    )
                )

    def test_invalid_metric_reconciliation_strategy_blocks_binding(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        payload["metrics"]["paid_amount"]["reconciliation_strategy"] = (
            "average_the_totals"
        )

        outcome = self._compile_compare_with_registry(RuntimeContractRegistry(payload))

        self.assertFalse(outcome.query_contracts)
        self.assertIn(
            "metric:paid_amount:invalid:reconciliation_strategy",
            {gap.gap_id for gap in outcome.analysis_contract.contract_gaps},
        )

    def test_workload_class_participates_in_dedupe(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        common = {
            "task_input_binding": {
                "payload_kind": "metric_timeseries",
                "query_families": {"primary": "workload_scan"},
            },
            "temporal_compatibility": {
                "modes": ["single_day_window_pair"],
                "window_roles": ["target", "baseline"],
                "consumption_semantics": ["daily_series"],
                "calendar_partition_fields": [],
            },
            "query_families": ["workload_scan"],
            "required_metrics": ["paid_amount"],
            "allowed_datasets": ["paid_order_success"],
            "minimum_readiness": {"accepted_completeness": ["complete"]},
            "degradation_policy": {"missing_required_input": "block_claim"},
            "supported_evidence_types": ["statistical_association"],
            "supported_claim_types": ["comparative_change"],
            "maximum_claim_strength": "directional",
        }
        payload["capability_inputs"]["interactive_scan"] = dict(common)
        payload["capability_inputs"]["batch_scan"] = {
            **common,
            "workload_class": "batch_aggregate",
        }
        payload["query_shapes"]["workload_scan"] = {
            "required_fields": ["window_id", "window_role", "observation_key"],
            "unique_key": ["window_id", "observation_key"],
            "grain": ["window_id", "observation_key"],
            "dimension_presence_policy": "paired_required",
            "max_result_rows": 10000,
        }
        outcome = compile_analysis_contract(
            run_id="run-workload-dedupe",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "interactive_scan",
                    "batch_scan",
                ),
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("interactive_scan", "batch_scan"),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry(payload),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(len(outcome.query_contracts), 2)
        self.assertEqual(
            {query.workload_class for query in outcome.query_contracts},
            {"interactive_aggregate", "batch_aggregate"},
        )

    def test_compiler_requires_typed_temporal_authority(self):
        with self.assertRaisesRegex(TypeError, "temporal_authority"):
            compile_analysis_contract(
                run_id="run-missing-temporal-authority",
                proposal={
                    "target_metrics": ["paid_amount"],
                    "scope": {"type": "full_sample"},
                    "grain": "window_id",
                    "capability_roles": _required_roles("compare_periods"),
                },
                accepted_capabilities=("compare_periods",),
                catalog=DatasetCatalog(()),
                registry=RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                ),
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            )

        with self.assertRaisesRegex(
            ValueError,
            "analysis_temporal_authority_invalid",
        ):
            _compile_analysis_contract(
                run_id="run-invalid-temporal-authority",
                proposal={
                    "target_metrics": ["paid_amount"],
                    "scope": {"type": "full_sample"},
                    "grain": "window_id",
                    "capability_roles": {
                        "compare_periods": {
                            "analysis_role": "required",
                            "sources": ("closed_contract_test",),
                        }
                    },
                },
                accepted_capabilities=("compare_periods",),
                catalog=DatasetCatalog(()),
                registry=RuntimeContractRegistry.from_path(
                    "contracts/runtime/clickhouse-analysis-bindings.yaml"
                ),
                temporal_authority={"mode": "window_pair"},
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            )

    def test_direct_compiler_rejects_legacy_temporal_proposal_fields(self):
        legacy_fields = {
            "target_semantic": "someday",
            "baselines": ["quarter_to_date"],
            "fixed_window_bounds": {
                "start": "2026-01-01",
                "end": "2026-03-31",
            },
        }
        for field, value in legacy_fields.items():
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    ValueError,
                    f"analysis_legacy_temporal_fields_forbidden:{field}",
                ),
            ):
                _compile_analysis_contract(
                    run_id=f"run-legacy-temporal-field-{field}",
                    proposal={
                        "scope": {"type": "full_sample"},
                        "grain": "window_id",
                        "capability_roles": _required_roles("compare_periods"),
                        "target_metrics": ["paid_amount"],
                        field: value,
                    },
                    accepted_capabilities=("compare_periods",),
                    catalog=DatasetCatalog(()),
                    registry=RuntimeContractRegistry.from_path(
                        "contracts/runtime/clickhouse-analysis-bindings.yaml"
                    ),
                    temporal_authority=_single_day_pair_temporal_authority(),
                    as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
                )

    def test_canonical_query_windows_rejects_unknown_role(self):
        unknown = ResolvedWindow(
            window_id="future_window",
            role="future_role",
            label="future",
            start_inclusive="2026-06-01",
            end_exclusive="2026-06-02",
            timezone="Africa/Lagos",
            aggregation="daily_total",
            required_complete_days=1,
            source_watermark_requirement="2026-06-01",
        )

        with self.assertRaisesRegex(
            ValueError,
            "analysis_window_role_invalid:future_window=future_role",
        ):
            _canonical_query_windows((unknown,))

    def test_canonical_query_windows_stably_orders_dynamic_reference_ids(self):
        def window(window_id, role):
            return ResolvedWindow(
                window_id=window_id,
                role=role,
                label=window_id,
                start_inclusive="2026-06-01",
                end_exclusive="2026-06-02",
                timezone="Africa/Lagos",
                aggregation="daily_total",
                required_complete_days=1,
                source_watermark_requirement="2026-06-01",
            )

        ordered = _canonical_query_windows(
            (
                window("context__z", "reference"),
                window("target_day", "target"),
                window("context__a", "reference"),
            )
        )

        self.assertEqual(
            tuple(item.window_id for item in ordered),
            ("target_day", "context__a", "context__z"),
        )

    def test_event_source_gap_has_only_event_capability_owner(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-event-owner",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "compare_periods",
                    "internal_operation_event_evidence",
                ),
                "target_metrics": ["paid_amount"],
                "requested_context_sources": ["internal_operation_event"],
                "claim_intents": ["candidate_mechanism"],
            },
            accepted_capabilities=(
                "compare_periods",
                "internal_operation_event_evidence",
            ),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id == "dataset:internal_operation_event:source_unbound"
        )
        self.assertEqual(
            gap.affected_capabilities,
            ("internal_operation_event_evidence",),
        )

    def test_event_context_query_is_not_owned_outside_reviewed_allowlist(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-incompatible-context-source",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("event_evidence"),
                "requested_context_sources": ["gameplay"],
                "claim_intents": ["candidate_mechanism"],
            },
            accepted_capabilities=("event_evidence",),
            catalog=DatasetCatalog((snapshot("gameplay", "gameplay", "2026-07-04"),)),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertFalse(outcome.query_contracts)
        self.assertFalse(
            any(
                gap.dataset_id == "gameplay"
                and "event_evidence" in gap.affected_capabilities
                for gap in outcome.analysis_contract.contract_gaps
            )
        )

    def test_context_capability_dataset_review_is_cross_source_closed(self):
        from bi_agent.runtime.analysis_contract_compiler import (
            _capability_reviews_dataset,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        self.assertTrue(
            _capability_reviews_dataset("event_evidence", "external_event", registry)
        )
        self.assertFalse(
            _capability_reviews_dataset("event_evidence", "gameplay", registry)
        )
        self.assertTrue(
            _capability_reviews_dataset(
                "cross_source_association", "gameplay", registry
            )
        )
        self.assertTrue(
            _capability_reviews_dataset(
                "cross_source_panel_association",
                "gameplay_channel",
                registry,
            )
        )
        self.assertFalse(
            _capability_reviews_dataset(
                "gameplay_activity_context", "external_event", registry
            )
        )

    def test_target_only_source_gap_uses_analysis_contract_owner(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-target-owner",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": {},
                "target_metrics": ["paid_amount"],
            },
            accepted_capabilities=(),
            catalog=DatasetCatalog(()),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id.startswith("metric:paid_amount:source_ambiguous:")
        )
        self.assertEqual(gap.affected_capabilities, ("analysis_contract",))
        self.assertTrue(gap.requires_clarification)

    def test_metric_source_ambiguity_links_requested_claim_intents(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-target-claim-link",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": {},
                "target_metrics": ["paid_amount"],
                "claim_intents": [
                    "comparative_change",
                    "candidate_mechanism",
                ],
            },
            accepted_capabilities=(),
            catalog=DatasetCatalog(()),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_id.startswith("metric:paid_amount:source_ambiguous:")
        )
        self.assertEqual(
            gap.affected_claim_types,
            ("comparative_change", "candidate_mechanism"),
        )
        self.assertTrue(
            any(
                "paid-amount.metric.yaml" in ref
                for ref in outcome.analysis_contract.target_metric_refs
            )
        )
        self.assertEqual(
            gap.diagnostic_context,
            {
                "item_kind": "metric",
                "item_id": "paid_amount",
                "claim_intents": [
                    "comparative_change",
                    "candidate_mechanism",
                ],
            },
        )

    def test_direct_analysis_source_ambiguity_rejects_forged_source_suffix(self):
        from bi_agent.runtime.analysis_contract_compiler import (
            _source_ambiguity_gap,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        for item_kind, item_id, source_ids in (
            ("metric", "paid_users", tuple(registry.metric_sources("paid_users"))),
            ("dimension", "channel", tuple(registry.dimension_sources("channel"))),
        ):
            with self.subTest(item_kind=item_kind, item_id=item_id):
                gap = _source_ambiguity_gap(
                    item_kind,
                    item_id,
                    source_ids,
                    ("analysis_contract",),
                    ("comparative_change",),
                    registered_source_ids=source_ids,
                )
                forged = replace(
                    gap,
                    gap_id=(f"{item_kind}:{item_id}:source_ambiguous:forged_source"),
                )

                self.assertFalse(
                    is_canonical_direct_analysis_source_ambiguity(
                        forged,
                        forged.affected_capabilities,
                        registry=registry,
                    )
                )

    def test_direct_analysis_source_ambiguity_accepts_reviewed_source_subset(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-reviewed-source-ambiguity-subset",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": {},
                "target_metrics": ["paid_amount"],
                "dataset_requirements": [
                    "market_dashboard",
                    "market_dashboard_channel",
                ],
            },
            accepted_capabilities=(),
            catalog=DatasetCatalog(()),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        gap = next(
            item
            for item in outcome.analysis_contract.contract_gaps
            if item.gap_id.startswith("metric:paid_amount:source_ambiguous:")
        )

        self.assertEqual(
            gap.gap_id,
            (
                "metric:paid_amount:source_ambiguous:"
                "market_dashboard,market_dashboard_channel"
            ),
        )
        self.assertEqual(
            gap.affected_capabilities,
            ("analysis_contract",),
        )
        self.assertTrue(
            is_canonical_direct_analysis_source_ambiguity(
                gap,
                gap.affected_capabilities,
                registry=registry,
            )
        )
        expected_refs = tuple(
            dict.fromkeys(
                registry.metric_sources("paid_amount")[source_id]["contract_ref"]
                for source_id in ("market_dashboard", "market_dashboard_channel")
            )
        )
        self.assertEqual(
            outcome.analysis_contract.target_metric_refs,
            expected_refs,
        )

    def test_direct_analysis_source_ambiguity_rejects_noncanonical_suffixes(self):
        from bi_agent.runtime.analysis_contract_compiler import (
            _source_ambiguity_gap,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        source_ids = tuple(registry.metric_sources("paid_amount"))
        gap = _source_ambiguity_gap(
            "metric",
            "paid_amount",
            source_ids,
            ("analysis_contract",),
            ("comparative_change",),
            registered_source_ids=source_ids,
        )
        suffixes = (
            "paid_order_success,forged_source",
            "market_dashboard,paid_order_success",
            "paid_order_success,paid_order_success",
        )

        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                candidate = replace(
                    gap,
                    gap_id=f"metric:paid_amount:source_ambiguous:{suffix}",
                )
                self.assertFalse(
                    is_canonical_direct_analysis_source_ambiguity(
                        candidate,
                        candidate.affected_capabilities,
                        registry=registry,
                    )
                )

    def test_direct_analysis_source_ambiguity_requires_exact_diagnostic_shape(self):
        from bi_agent.runtime.analysis_contract_compiler import (
            _source_ambiguity_gap,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        gap = _source_ambiguity_gap(
            "metric",
            "paid_users",
            tuple(registry.metric_sources("paid_users")),
            ("analysis_contract",),
            ("comparative_change", "recurring_pattern_existence"),
            registered_source_ids=tuple(registry.metric_sources("paid_users")),
        )
        diagnostics = (
            {
                **gap.diagnostic_context,
                "claim_intents": [
                    "recurring_pattern_existence",
                    "comparative_change",
                ],
            },
            {
                **gap.diagnostic_context,
                "claim_intents": [
                    "comparative_change",
                    "recurring_pattern_existence",
                    "recurring_pattern_existence",
                ],
            },
            {**gap.diagnostic_context, "unreviewed": True},
        )

        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                self.assertFalse(
                    is_canonical_direct_analysis_source_ambiguity(
                        replace(gap, diagnostic_context=diagnostic),
                        gap.affected_capabilities,
                        registry=registry,
                    )
                )

    def test_direct_analysis_source_ambiguity_rejects_claim_intent_drift(self):
        from bi_agent.runtime.analysis_contract_compiler import (
            _source_ambiguity_gap,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        gap = _source_ambiguity_gap(
            "metric",
            "paid_users",
            tuple(registry.metric_sources("paid_users")),
            ("analysis_contract",),
            ("comparative_change",),
            registered_source_ids=tuple(registry.metric_sources("paid_users")),
        )
        drifted = replace(
            gap,
            diagnostic_context={
                **gap.diagnostic_context,
                "claim_intents": ["candidate_mechanism"],
            },
        )

        self.assertFalse(
            is_canonical_direct_analysis_source_ambiguity(
                drifted,
                drifted.affected_capabilities,
                registry=registry,
            )
        )

    def test_direct_analysis_source_ambiguity_requires_distinct_claim_intents(self):
        from bi_agent.runtime.analysis_contract_compiler import (
            _source_ambiguity_gap,
        )

        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        source_ids = tuple(registry.metric_sources("paid_users"))
        canonical = _source_ambiguity_gap(
            "metric",
            "paid_users",
            source_ids,
            ("analysis_contract",),
            ("comparative_change",),
            registered_source_ids=source_ids,
        )

        for claim_intents in (
            (),
            ("comparative_change", "comparative_change"),
        ):
            with self.subTest(claim_intents=claim_intents):
                drifted = replace(
                    canonical,
                    affected_claim_types=claim_intents,
                    diagnostic_context={
                        **canonical.diagnostic_context,
                        "claim_intents": list(claim_intents),
                    },
                )
                self.assertFalse(
                    is_canonical_direct_analysis_source_ambiguity(
                        drifted,
                        drifted.affected_capabilities,
                        registry=registry,
                    )
                )

    def test_source_ambiguity_links_inferred_claim_intents_from_bound_peer_metric(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-inferred-target-claim-link",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": {},
                "target_metrics": ["active_users", "paid_amount"],
                "requested_dimensions": ["channel"],
                "metric_dataset_overrides": {
                    "active_users": "market_dashboard",
                },
            },
            accepted_capabilities=(),
            catalog=DatasetCatalog(
                (snapshot("market_dashboard", "market", "2026-06-03"),)
            ),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(
            outcome.analysis_contract.claim_intents,
            ("comparative_change", "source_reconciliation"),
        )
        for gap_prefix in (
            "metric:paid_amount:source_ambiguous:",
            "dimension:channel:source_ambiguous:",
        ):
            with self.subTest(gap_prefix=gap_prefix):
                gap = next(
                    gap
                    for gap in outcome.analysis_contract.contract_gaps
                    if gap.gap_id.startswith(gap_prefix)
                )
                self.assertEqual(
                    gap.affected_claim_types,
                    outcome.analysis_contract.claim_intents,
                )
                self.assertEqual(
                    gap.diagnostic_context["claim_intents"],
                    list(outcome.analysis_contract.claim_intents),
                )

    def test_compiled_target_metric_refs_follow_requested_bound_target_order(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        as_of = datetime.fromisoformat("2026-06-03T12:00:00+01:00")
        for target_metrics in (
            ("paid_amount", "paid_users"),
            ("paid_users", "paid_amount"),
        ):
            with self.subTest(target_metrics=target_metrics):
                contract = _compile_analysis_contract(
                    run_id="run-ordered-target-refs",
                    proposal={
                        "question_families": ["revenue_health_review"],
                        "target_metrics": list(target_metrics),
                        "scope": {"type": "full_sample"},
                        "grain": "window_id",
                        "capability_roles": {
                            "compare_periods": {
                                "analysis_role": "required",
                                "sources": ("closed_contract_test",),
                            }
                        },
                        "metric_dataset_overrides": {
                            "paid_amount": "paid_order_success",
                            "paid_users": "paid_order_success",
                        },
                    },
                    accepted_capabilities=("compare_periods",),
                    catalog=DatasetCatalog(()),
                    registry=registry,
                    temporal_authority=_single_day_pair_temporal_authority(),
                    as_of=as_of,
                ).analysis_contract
                ids_by_ref = {
                    binding.contract_ref: binding.metric_id
                    for binding in contract.metric_bindings
                }
                self.assertEqual(
                    tuple(ids_by_ref[ref] for ref in contract.target_metric_refs),
                    target_metrics,
                )

    def test_future_snapshot_is_typed_unavailable_as_of_not_source_unbound(self):
        future = DatasetSnapshot(
            "snapshot:paid:future",
            "paid_order_success",
            "paid",
            "2026-07-04",
            "schema:future",
            snapshot("paid_order_success", "paid", "2026-07-04").schema_fields,
            "contract:paid@1",
            "2026-06-04T00:00:00Z",
            "active",
        )
        outcome = self._compile_compare_with_catalog(DatasetCatalog((future,)))

        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_type == "dataset_snapshot_unavailable_as_of"
        )
        self.assertEqual(
            gap.gap_id,
            "dataset:paid_order_success:dataset_snapshot_unavailable_as_of",
        )
        self.assertEqual(
            gap.diagnostic_context["as_of"],
            "2026-06-03T11:00:00+00:00",
        )
        self.assertEqual(
            gap.diagnostic_context["earliest_loaded_at"],
            "2026-06-04T00:00:00+00:00",
        )
        self.assertEqual(
            gap.diagnostic_context["earliest_snapshot_ref"],
            "snapshot:paid:future",
        )
        self.assertTrue(gap.requires_clarification)

    def test_future_context_snapshot_does_not_remove_independent_ready_query(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        market, channel = _market_dashboard_snapshots()
        future_event = replace(
            snapshot("external_event", "business_events__future", "2026-06-08"),
            snapshot_ref="snapshot:event:future",
            schema_fields=tuple(registry.dataset("external_event")["schema_fields"]),
            loaded_at="2026-06-09T00:00:00+00:00",
            evidence_state="context_only",
            logical_snapshot_id="event-logical",
            load_revision="event-load:sha256:future",
            rows_content_hash="c" * 64,
        )

        def authorized_release(*members):
            release_ref = dataset_snapshot_release_ref(
                members[0].logical_snapshot_id,
                members[0].load_revision,
                (member.snapshot_ref for member in members),
            )
            released = tuple(
                replace(member, release_ref=release_ref) for member in members
            )
            record = build_dataset_release_authority_record(
                tuple(
                    {**member.to_dict(), "requires_release": True}
                    for member in released
                )
            )
            return (
                tuple(
                    replace(member, authority_record_ref=record.authority_record_ref)
                    for member in released
                ),
                record,
            )

        market_release, market_record = authorized_release(market, channel)
        event_release, event_record = authorized_release(future_event)

        class Resolver:
            def resolve_dataset_release(self, release_ref):
                records = {
                    market_record.release_ref: market_record,
                    event_record.release_ref: event_record,
                }
                return records[release_ref]

        resolver = Resolver()

        outcome = compile_analysis_contract(
            run_id="run-ready-with-future-context",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "market_health_compare",
                    "event_evidence",
                ),
                "question_families": ["anomaly_or_black_swan_review"],
                "target_metrics": ["active_users"],
                "requested_context_sources": ["external_event"],
                "claim_intents": ["comparative_change", "candidate_mechanism"],
            },
            accepted_capabilities=("market_health_compare", "event_evidence"),
            catalog=DatasetCatalog(
                (*market_release, *event_release), release_resolver=resolver
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            release_resolver=resolver,
        )

        self.assertEqual(
            {query.query_intent for query in outcome.query_contracts},
            {"daily_metric_baselines"},
        )
        plans = {plan.capability_id: plan for plan in outcome.capability_plans}
        self.assertTrue(
            plans["market_health_compare"].required_input_slots[0].query_contract_refs
        )
        self.assertFalse(
            plans["event_evidence"].required_input_slots[0].query_contract_refs
        )
        gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_type == "dataset_snapshot_unavailable_as_of"
        )
        self.assertEqual(gap.affected_capabilities, ("event_evidence",))
        self.assertTrue(gap.requires_clarification)

    def _compile_compare_with_catalog(self, catalog):
        return compile_analysis_contract(
            run_id="run-source-availability-classification",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=catalog,
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

    def _compile_compare_with_registry(self, registry):
        return compile_analysis_contract(
            run_id="run-dataset-contract",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

    def test_daily_query_shape_preserves_observation_grain(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-shape",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        shape = outcome.query_contracts[0].result_shape
        self.assertEqual(
            shape.required_fields,
            ("window_id", "window_role", "observation_key", "paid_amount"),
        )
        self.assertEqual(shape.unique_key, ("window_id", "observation_key"))
        self.assertEqual(shape.grain, ("window_id", "observation_key"))

    def test_registry_covers_canonical_revenue_capabilities(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        self.assertTrue(
            {
                "market_health_compare",
                "market_channel_context",
                "source_reconciliation",
            }.issubset(registry.public_capability_ids)
        )

        for capability_id in registry.public_capability_ids:
            with self.subTest(capability_id=capability_id):
                self.assertTrue(registry.capability_inputs(capability_id))

    def test_rolling_window_compare_consumes_target_and_owned_history(self):
        outcome = compile_analysis_contract(
            run_id="run-rolling-window-authority",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("rolling_window_compare"),
                "target_metrics": ["paid_amount"],
                "context_window_specs": [
                    {
                        "capability_id": "rolling_window_compare",
                        "relation": "trailing_complete_periods",
                        "unit": "day",
                        "count": 10,
                    }
                ],
            },
            accepted_capabilities=("rolling_window_compare",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        self.assertEqual(
            tuple(window.role for window in outcome.analysis_contract.resolved_windows),
            ("target", "baseline", "reference"),
        )
        self.assertEqual(
            outcome.query_contracts[0].window_refs,
            (
                "target_day",
                "context__rolling_window_compare__trailing_complete_periods__10_day",
            ),
        )

    def test_change_point_scan_materializes_its_minimum_daily_context(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-change-point-context",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("change_point_scan"),
                "question_families": ["anomaly_or_black_swan_review"],
                "target_metrics": ["paid_amount"],
                "context_window_specs": [
                    {
                        "capability_id": "change_point_scan",
                        "relation": "trailing_complete_periods",
                        "unit": "day",
                        "count": 8,
                    }
                ],
                "claim_intents": ["external_shock_candidate_or_anomaly"],
            },
            accepted_capabilities=("change_point_scan",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(target="2026-06-01"),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        context_window = next(
            window
            for window in outcome.analysis_contract.resolved_windows
            if window.role == "reference"
        )
        self.assertEqual(
            (
                context_window.start_inclusive,
                context_window.end_exclusive,
                context_window.required_complete_days,
                context_window.capability_refs,
            ),
            ("2026-05-24", "2026-06-01", 8, ("change_point_scan",)),
        )
        query = next(
            query
            for query in outcome.query_contracts
            if query.query_intent == "daily_metric_baselines"
        )
        self.assertEqual(
            query.window_refs,
            (context_window.window_id,),
        )
        self.assertGreaterEqual(
            context_window.required_complete_days,
            registry.capability_inputs("change_point_scan")["task_input_binding"][
                "parameters"
            ]["min_total_samples"],
        )

    def test_outlier_scan_uses_owned_history_for_single_day_and_range_targets(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        authorities = (
            _single_day_pair_temporal_authority(target="2026-06-01"),
            _aggregate_pair_temporal_authority(),
        )

        for index, temporal_authority in enumerate(authorities):
            with self.subTest(mode=temporal_authority.mode):
                outcome = compile_analysis_contract(
                    run_id=f"run-outlier-context-{index}",
                    proposal={
                        "scope": {"type": "full_sample"},
                        "grain": "window_id",
                        "capability_roles": _required_roles("outlier_scan"),
                        "question_families": ["anomaly_or_black_swan_review"],
                        "target_metrics": ["paid_amount"],
                        "context_window_specs": [
                            {
                                "capability_id": "outlier_scan",
                                "relation": "trailing_complete_periods",
                                "unit": "day",
                                "count": 28,
                            }
                        ],
                        "claim_intents": ["external_shock_candidate_or_anomaly"],
                    },
                    accepted_capabilities=("outlier_scan",),
                    catalog=DatasetCatalog(
                        (snapshot("paid_order_success", "paid", "2026-07-04"),)
                    ),
                    registry=registry,
                    temporal_authority=temporal_authority,
                    as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
                )

                context_window = next(
                    window
                    for window in outcome.analysis_contract.resolved_windows
                    if window.role == "reference"
                )
                query = next(
                    query
                    for query in outcome.query_contracts
                    if query.query_intent == "daily_metric_baselines"
                )

                self.assertEqual(context_window.required_complete_days, 28)
                self.assertEqual(context_window.capability_refs, ("outlier_scan",))
                self.assertEqual(
                    tuple(window.role for window in query.resolved_windows),
                    ("target", "reference"),
                )
                self.assertNotIn("baseline", query.window_refs)

    def test_auxiliary_context_window_is_scoped_to_its_own_capability_queries(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        capabilities = (
            "compare_periods",
            "formula_decompose",
            "change_point_scan",
        )
        outcome = compile_analysis_contract(
            run_id="run-auxiliary-window-isolation",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "context_window_specs": [
                    {
                        "capability_id": "change_point_scan",
                        "relation": "trailing_complete_periods",
                        "unit": "day",
                        "count": 8,
                    }
                ],
                "claim_intents": [
                    "comparative_change",
                    "formula_component_contribution",
                    "external_shock_candidate_or_anomaly",
                ],
                "capability_roles": {
                    "compare_periods": {
                        "analysis_role": "required",
                        "sources": [
                            "question_family_required:paid_amount_change_explanation"
                        ],
                    },
                    "formula_decompose": {
                        "analysis_role": "required",
                        "sources": [
                            "question_family_required:paid_amount_change_explanation"
                        ],
                    },
                    "change_point_scan": {
                        "analysis_role": "auxiliary",
                        "sources": ["analysis_axis:anomaly_validation:auxiliary"],
                    },
                },
            },
            accepted_capabilities=capabilities,
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(target="2026-06-01"),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        queries_by_ref = {
            query.query_contract_id: query for query in outcome.query_contracts
        }
        plans = {plan.capability_id: plan for plan in outcome.capability_plans}

        def owned_window_sets(capability_id):
            return {
                queries_by_ref[query_ref].window_refs
                for slot in plans[capability_id].required_input_slots
                for query_ref in slot.query_contract_refs
            }

        primary_windows = {("target_day", "previous_day")}
        self.assertEqual(owned_window_sets("compare_periods"), primary_windows)
        self.assertEqual(
            owned_window_sets("formula_decompose"),
            primary_windows,
        )
        context_windows = next(iter(owned_window_sets("change_point_scan")))
        self.assertEqual(len(context_windows), 1)
        self.assertIn("__8_day", context_windows[0])

    def test_rolling_window_compare_declares_required_history(self):
        contract = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ).capability_inputs("rolling_window_compare")

        self.assertEqual(
            contract["context_window_policy"]["execution_default"],
            {"unit": "day", "count": 10},
        )

    def test_rolling_window_compare_declares_target_context_daily_series(self):
        compatibility = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        ).capability_inputs("rolling_window_compare")["temporal_compatibility"]

        self.assertEqual(compatibility["window_roles"], ["target", "reference"])
        self.assertEqual(
            compatibility["consumption_semantics"],
            ["daily_series", "capability_context"],
        )

    def test_calendar_partition_suppresses_context_window_materialization(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-quarter-context",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_period_phases"),
                "question_families": ["pattern_explanation"],
                "target_metrics": ["paid_amount"],
                "context_window_specs": [
                    {
                        "capability_id": "compare_period_phases",
                        "relation": "trailing_complete_periods",
                        "unit": "quarter",
                        "count": 1,
                    }
                ],
                "claim_intents": ["recurring_pattern_existence"],
            },
            accepted_capabilities=("compare_period_phases",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_calendar_partition_temporal_authority(),
            as_of=datetime.fromisoformat("2026-07-17T01:56:11+00:00"),
        )

        self.assertEqual(
            tuple(
                (window.window_id, window.role)
                for window in outcome.analysis_contract.resolved_windows
            ),
            (("target_day", "target"),),
        )

    def test_required_context_source_absence_is_a_typed_capability_gap(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-context-input",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("event_evidence"),
                "question_families": ["business_object_impact_review"],
                "target_metrics": [],
                "claim_intents": ["candidate_mechanism"],
            },
            accepted_capabilities=("event_evidence",),
            catalog=DatasetCatalog(()),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        gap_ids = {gap.gap_id for gap in outcome.analysis_contract.contract_gaps}
        self.assertIn(
            "capability:event_evidence:required_context_source:unbound", gap_ids
        )
        self.assertIn(
            "capability:event_evidence:required_query:event_context_probe:unbound",
            gap_ids,
        )
        self.assertFalse(
            outcome.capability_plans[0].required_input_slots[0].query_contract_refs
        )

    def test_required_dimension_absence_does_not_compile_unsegmented_query(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-dimension-input",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("candidate_dimension_screen"),
                "question_families": ["segment_or_factor_attribution"],
                "target_metrics": ["paid_amount"],
                "requested_dimensions": [],
                "claim_intents": ["segment_contribution_or_mix_shift"],
            },
            accepted_capabilities=("candidate_dimension_screen",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        gap_ids = {gap.gap_id for gap in outcome.analysis_contract.contract_gaps}
        self.assertIn(
            "capability:candidate_dimension_screen:required_dimension:unbound",
            gap_ids,
        )
        self.assertNotIn(
            "dimension_contribution_scan",
            {query.query_intent for query in outcome.query_contracts},
        )

    def test_auxiliary_user_mix_uses_the_dynamic_window_pair_adapter(self):
        outcome = self._assert_dynamic_pair_adapter(
            "user_mix_contribution",
            proposal={
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
            },
        )
        self.assertEqual(
            tuple(item.query_intent for item in outcome.query_contracts),
            ("user_mix_joint_scan", "user_mix_joint_scan"),
        )
        joint_contract = next(
            item for item in outcome.query_contracts if item.dimension_bindings
        )
        self.assertEqual(
            tuple(
                item.dimension_id
                for item in joint_contract.dimension_bindings
            ),
            ("channel", "user_mix_bucket"),
        )
        self.assertEqual(
            tuple(
                item.metric_id
                for item in joint_contract.metric_bindings
            ),
            ("paid_amount", "paid_users"),
        )

    def test_required_user_mix_uses_the_dynamic_window_pair_adapter(self):
        self._assert_dynamic_pair_adapter(
            "user_mix_contribution",
            proposal={
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
            },
        )

    def test_shared_query_family_plans_bind_only_owned_metric_contracts(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        common = {
            "task_input_binding": {
                "payload_kind": "metric_timeseries",
                "query_families": {"primary": "shared_metric_scan"},
            },
            "temporal_compatibility": {
                "modes": ["single_day_window_pair"],
                "window_roles": ["target", "baseline"],
                "consumption_semantics": ["daily_series"],
                "calendar_partition_fields": [],
            },
            "query_families": ["shared_metric_scan"],
            "allowed_datasets": ["paid_order_success"],
            "minimum_readiness": {"accepted_completeness": ["complete"]},
            "degradation_policy": {"missing_required_input": "block_claim"},
            "supported_evidence_types": ["statistical_association"],
            "supported_claim_types": ["comparative_change"],
            "maximum_claim_strength": "directional",
        }
        payload["capability_inputs"]["shared_amount"] = {
            **common,
            "required_metrics": ["paid_amount"],
        }
        payload["capability_inputs"]["shared_users"] = {
            **common,
            "required_metrics": ["paid_users"],
        }
        payload["query_shapes"]["shared_metric_scan"] = {
            "required_fields": ["window_id", "window_role", "observation_key"],
            "unique_key": ["window_id", "observation_key"],
            "grain": ["window_id", "observation_key"],
            "dimension_presence_policy": "paired_required",
            "max_result_rows": 10000,
        }
        outcome = compile_analysis_contract(
            run_id="run-owned-query",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "shared_amount",
                    "shared_users",
                ),
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("shared_amount", "shared_users"),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry(payload),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        query_by_ref = {
            query.query_contract_id: query for query in outcome.query_contracts
        }
        refs_by_capability = {
            plan.capability_id: plan.required_input_slots[0].query_contract_refs
            for plan in outcome.capability_plans
        }
        self.assertEqual(len(refs_by_capability["shared_amount"]), 1)
        self.assertEqual(len(refs_by_capability["shared_users"]), 1)
        self.assertEqual(
            tuple(
                binding.metric_id
                for binding in query_by_ref[
                    refs_by_capability["shared_amount"][0]
                ].metric_bindings
            ),
            ("paid_amount",),
        )
        self.assertEqual(
            tuple(
                binding.metric_id
                for binding in query_by_ref[
                    refs_by_capability["shared_users"][0]
                ].metric_bindings
            ),
            ("paid_users",),
        )

    def test_deduplicates_logical_query_when_metric_set_order_differs(self):
        payload = load_contract("contracts/runtime/clickhouse-analysis-bindings.yaml")
        common = {
            "task_input_binding": {
                "payload_kind": "metric_timeseries",
                "query_families": {"primary": "ordered_metric_scan"},
            },
            "temporal_compatibility": {
                "modes": ["single_day_window_pair"],
                "window_roles": ["target", "baseline"],
                "consumption_semantics": ["daily_series"],
                "calendar_partition_fields": [],
            },
            "query_families": ["ordered_metric_scan"],
            "allowed_datasets": ["paid_order_success"],
            "minimum_readiness": {"accepted_completeness": ["complete"]},
            "degradation_policy": {"missing_required_input": "block_claim"},
            "supported_evidence_types": ["statistical_association"],
            "supported_claim_types": ["comparative_change"],
            "maximum_claim_strength": "directional",
        }
        payload["capability_inputs"]["order_a"] = {
            **common,
            "required_metrics": ["paid_amount", "paid_users"],
        }
        payload["capability_inputs"]["order_b"] = {
            **common,
            "required_metrics": ["paid_users", "paid_amount"],
        }
        payload["query_shapes"]["ordered_metric_scan"] = {
            "required_fields": ["window_id", "window_role", "observation_key"],
            "unique_key": ["window_id", "observation_key"],
            "grain": ["window_id", "observation_key"],
            "dimension_presence_policy": "paired_required",
            "max_result_rows": 10000,
        }
        outcome = compile_analysis_contract(
            run_id="run-dedupe",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("order_a", "order_b"),
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "claim_intents": ["comparative_change"],
            },
            accepted_capabilities=("order_a", "order_b"),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid", "2026-07-04"),)
            ),
            registry=RuntimeContractRegistry(payload),
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(len(outcome.query_contracts), 1)

    def test_compiles_explicit_llm_proposal_without_question_keywords(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        catalog = DatasetCatalog(
            (
                snapshot("paid_order_success", "paid_success", "2026-07-04"),
                snapshot(
                    "payment_final_outcome",
                    "payment_final_outcome_daily__schema",
                    "2026-07-04",
                ),
            )
        )
        outcome = compile_analysis_contract(
            run_id="run-1",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles(
                    "compare_periods",
                    "formula_decompose",
                ),
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "requested_components": [
                    "paid_users",
                    "first_paid_users",
                    "paid_frequency",
                    "avg_order_amount",
                    "payment_success_rate",
                ],
                "requested_dimensions": [],
                "claim_intents": [
                    "comparative_change",
                    "formula_component_contribution",
                ],
            },
            accepted_capabilities=("compare_periods", "formula_decompose"),
            catalog=catalog,
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        self.assertEqual(
            outcome.analysis_contract.resolved_windows[0].label, "2026-06-02"
        )
        intents = {contract.query_intent for contract in outcome.query_contracts}
        self.assertIn("daily_metric_baselines", intents)
        self.assertIn("component_driver_scan", intents)
        self.assertFalse(outcome.analysis_contract.contract_gaps)

    def test_scope_requires_a_catalog_id_without_business_alias_inference(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        catalog = DatasetCatalog(
            (snapshot("paid_order_success", "paid_success", "2026-07-04"),)
        )

        def compile_with(scope):
            return compile_analysis_contract(
                run_id=f"run-scope-{scope}",
                proposal={
                    "question_families": ["custom_baseline_comparison"],
                    "target_metrics": ["paid_amount"],
                    "claim_intents": ["comparative_change"],
                    "scope": {"type": scope},
                    "grain": "window_id",
                    "capability_roles": _required_roles("compare_periods"),
                },
                accepted_capabilities=("compare_periods",),
                catalog=catalog,
                registry=registry,
                temporal_authority=_single_day_pair_temporal_authority(),
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            )

        canonical = compile_with("full_sample")
        self.assertEqual(canonical.analysis_contract.scope["type"], "full_sample")
        with self.assertRaisesRegex(ValueError, "analysis_scope_invalid:catalog_ref"):
            compile_with("全平台")
        with self.assertRaisesRegex(ValueError, "analysis_scope_invalid:catalog_ref"):
            compile_with(None)

    def test_distinguishes_source_absent_from_contract_absent(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-2",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("event_evidence"),
                "question_families": ["business_object_impact_review"],
                "target_metrics": ["paid_amount"],
                "requested_dimensions": [],
                "requested_context_sources": ["internal_operation_event"],
                "claim_intents": ["candidate_mechanism"],
            },
            accepted_capabilities=("event_evidence",),
            catalog=DatasetCatalog(()),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )
        self.assertIn(
            "source_unbound",
            {gap.gap_type for gap in outcome.analysis_contract.contract_gaps},
        )

    def test_omitted_claim_intents_with_stale_snapshot_returns_typed_window_gap(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-stale",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": _required_roles("compare_periods"),
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "requested_dimensions": [],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog(
                (snapshot("paid_order_success", "paid_success", "2026-07-04"),)
            ),
            registry=registry,
            temporal_authority=_single_day_pair_temporal_authority(target="2026-07-09"),
            as_of=datetime.fromisoformat("2026-07-10T12:00:00+01:00"),
        )

        window_gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_type == "window_data_unavailable"
        )
        self.assertEqual(
            outcome.analysis_contract.claim_intents, ("comparative_change",)
        )
        self.assertEqual(window_gap.affected_claim_types, ("comparative_change",))

    def test_unbound_claim_intent_returns_contract_partial_gap(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        outcome = compile_analysis_contract(
            run_id="run-unbound-claim",
            proposal={
                "scope": {"type": "full_sample"},
                "grain": "window_id",
                "capability_roles": {},
                "question_families": ["evidence_quality_review"],
                "target_metrics": [],
                "requested_dimensions": [],
            },
            accepted_capabilities=(),
            catalog=DatasetCatalog(()),
            registry=registry,
            temporal_authority=_target_only_temporal_authority(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        )

        claim_gap = next(
            gap
            for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_type == "contract_partial"
        )
        self.assertEqual(
            outcome.analysis_contract.claim_intents, ("unbound_claim_intent",)
        )
        self.assertEqual(claim_gap.affected_claim_types, ("unbound_claim_intent",))


if __name__ == "__main__":
    unittest.main()
