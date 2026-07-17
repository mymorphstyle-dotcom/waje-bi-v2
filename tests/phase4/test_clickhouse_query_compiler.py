from dataclasses import replace
from datetime import date
import unittest
from unittest.mock import patch

from bi_agent.runtime.analysis_contracts import (
    DimensionBinding,
    JoinExpectation,
    MetricBinding,
    QueryContract,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.clickhouse_query_compiler import (
    compile_clickhouse_query as _compile_clickhouse_query,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetSnapshot,
    build_dataset_release_authority_record,
    canonical_dataset_release_members,
    canonical_dataset_requires_release,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


class _ReleaseResolver:
    def __init__(self, record):
        self.record = record

    def resolve_dataset_release(self, release_ref):
        if release_ref != self.record.release_ref:
            raise KeyError(release_ref)
        return self.record


_RELEASE_RESOLVERS = {}


def compile_clickhouse_query(contract, snapshots, **kwargs):
    resolver = kwargs.pop("release_resolver", None)
    snapshots = dict(snapshots)
    first = next(iter(snapshots.values()), None)
    if isinstance(first, DatasetSnapshot):
        try:
            date.fromisoformat(first.watermark)
        except (TypeError, ValueError):
            return _compile_clickhouse_query(contract, snapshots, **kwargs)
        if not first.schema_fingerprint or ";" in first.physical_table:
            return _compile_clickhouse_query(contract, snapshots, **kwargs)
    if (
        resolver is None
        and isinstance(first, DatasetSnapshot)
        and canonical_dataset_requires_release(first.dataset_id)
        and first.dataset_id not in {"market_dashboard", "market_dashboard_channel"}
    ):
        table_names = {
            "paid_order_success": "analytics.paid_success",
            "gameplay": "gameplay_daily__a1a1a1a1a1a1a1a1",
            "gameplay_channel": "gameplay_channel_daily__b2b2b2b2b2b2b2b2",
            "external_event": "business_events__c3c3c3c3c3c3c3c3",
            "internal_operation_event": "business_events__d4d4d4d4d4d4d4d4",
        }
        schema_fingerprints = {
            "paid_order_success": "e5" * 32,
            "gameplay": "a1" * 32,
            "gameplay_channel": "b2" * 32,
            "external_event": "c3" * 32,
            "internal_operation_event": "d4" * 32,
        }
        canonical_registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        members = []
        for member_dataset in canonical_dataset_release_members(first.dataset_id):
            member = replace(
                first,
                snapshot_ref=(
                    first.snapshot_ref
                    if member_dataset == first.dataset_id
                    else f"snapshot:{member_dataset}:compile-peer"
                ),
                dataset_id=member_dataset,
                physical_table=table_names[member_dataset],
                schema_fingerprint=schema_fingerprints[member_dataset],
                schema_fields=tuple(
                    canonical_registry.dataset(member_dataset).get("schema_fields")
                    or first.schema_fields
                ),
                logical_snapshot_id=f"{first.dataset_id}-logical",
                load_revision=f"{first.dataset_id}-load:sha256:reviewed",
                rows_content_hash=("a" if member_dataset == first.dataset_id else "b") * 64,
                evidence_state=(
                    "claim_ready"
                    if member_dataset == "paid_order_success"
                    else "context_only"
                ),
                reconciliation_status="not_applicable",
            )
            members.append(member)
        release_ref = dataset_snapshot_release_ref(
            members[0].logical_snapshot_id,
            members[0].load_revision,
            tuple(item.snapshot_ref for item in members),
        )
        members = [replace(item, release_ref=release_ref) for item in members]
        record = build_dataset_release_authority_record(
            tuple({**item.to_dict(), "requires_release": True} for item in members)
        )
        selected = next(item for item in members if item.dataset_id == first.dataset_id)
        selected = replace(selected, authority_record_ref=record.authority_record_ref)
        snapshots = {selected.snapshot_ref: selected}
        resolver = _ReleaseResolver(record)
    if (
        resolver is None
        and isinstance(first, DatasetSnapshot)
        and first.dataset_id in {"market_dashboard", "market_dashboard_channel"}
        and first.logical_snapshot_id
        and first.load_revision
        and first.release_ref
    ):
        peer_dataset_id = (
            "market_dashboard_channel"
            if first.dataset_id == "market_dashboard"
            else "market_dashboard"
        )
        peer = replace(
            first,
            snapshot_ref=f"snapshot:{peer_dataset_id}:compile-peer",
            dataset_id=peer_dataset_id,
            physical_table=(
                "market_dashboard_channel_daily__compilepeer"
                if peer_dataset_id == "market_dashboard_channel"
                else "market_dashboard_daily__compilepeer"
            ),
            schema_fingerprint="compile-peer-schema",
            evidence_state=(
                "context_only"
                if peer_dataset_id == "market_dashboard_channel"
                else "claim_ready"
            ),
            reconciliation_status=(
                "mismatch"
                if peer_dataset_id == "market_dashboard_channel"
                else "matched"
            ),
            rows_content_hash="b" * 64,
        )
        release_ref = dataset_snapshot_release_ref(
            first.logical_snapshot_id,
            first.load_revision,
            (first.snapshot_ref, peer.snapshot_ref),
        )
        first = replace(first, release_ref=release_ref)
        peer = replace(peer, release_ref=release_ref, authority_record_ref="")
        record = build_dataset_release_authority_record(
            tuple({**item.to_dict(), "requires_release": True} for item in (first, peer))
        )
        first = replace(first, authority_record_ref=record.authority_record_ref)
        snapshots[first.snapshot_ref] = first
        resolver = _ReleaseResolver(record)
    return _compile_clickhouse_query(
        contract,
        snapshots,
        release_resolver=resolver,
        **kwargs,
    )


def windows():
    return (
        ResolvedWindow(
            "target_day",
            "target",
            "2026-06-02",
            "2026-06-02",
            "2026-06-03",
            "Africa/Lagos",
            "daily_total",
            1,
            "2026-06-02",
        ),
        ResolvedWindow(
            "rolling_7_day_baseline",
            "baseline",
            "2026-05-26..2026-06-01",
            "2026-05-26",
            "2026-06-02",
            "Africa/Lagos",
            "mean_of_complete_days",
            7,
            "2026-06-01",
        ),
        ResolvedWindow(
            "same_weekday_last_week",
            "baseline",
            "2026-05-26",
            "2026-05-26",
            "2026-05-27",
            "Africa/Lagos",
            "daily_total",
            1,
            "2026-05-26",
        ),
    )


def metric(dataset_id="paid_order_success", expression=None):
    required_fields = (
        ("订单id", "支付状态", "支付发起时间")
        if dataset_id == "payment_attempt"
        else ("paid_amount_ngn",)
    )
    reviewed_expression = expression or (
        "uniqExactIf(`订单id`, `支付状态` = 'pay_success') / "
        "nullIf(uniqExact(`订单id`), 0)"
        if dataset_id == "payment_attempt"
        else "sum(paid_amount_ngn)"
    )
    return MetricBinding(
        "payment_success_rate" if dataset_id == "payment_attempt" else "paid_amount",
        (
            "contracts/backlog/missing-contracts.yaml#payment_status_and_dedup_contract"
            if dataset_id == "payment_attempt"
            else "contracts/metrics/paid-amount.metric.yaml@0.1"
        ),
        dataset_id,
        reviewed_expression,
        "ratio" if dataset_id == "payment_attempt" else "sum",
        required_fields,
        ("window_id",),
        claim_types=(
            ()
            if dataset_id == "payment_attempt"
            else (
                "comparative_change",
                "formula_component_contribution",
                "segment_contribution_or_mix_shift",
                "cross_source_statistical_association",
            )
        ),
        reconciliation_tolerance=(
            0.0 if dataset_id == "payment_attempt" else 0.01
        ),
        reconciliation_strategy=(
            "unsupported_non_additive"
            if dataset_id == "payment_attempt"
            else "additive_sum"
        ),
        value_semantics=(
            "scalar_ratio" if dataset_id == "payment_attempt" else "raw_scalar"
        ),
        display_format=(
            "percent" if dataset_id == "payment_attempt" else "number"
        ),
    )


def dimension(dimension_id="channel"):
    return DimensionBinding(
        dimension_id,
        (
            "contracts/dimensions/dimensions.yaml#acquisition_channel"
            if dimension_id == "channel"
            else f"contracts/dimensions/dimensions.yaml#{dimension_id}"
        ),
        "paid_order_success",
        dimension_id,
        ("day", "window_id"),
        null_bucket=("Blank" if dimension_id == "channel" else "Unknown"),
    )


def dashboard_metric(metric_id="paid_amount", dataset_id="market_dashboard"):
    field = metric_id
    exact_count = metric_id in {"active_users", "new_users", "registrations"}
    return MetricBinding(
        metric_id=metric_id,
        contract_ref=(
            f"contracts/sources/market-dashboard.source.yaml@0.1#field_contracts.{field}"
        ),
        dataset_id=dataset_id,
        expression=f"sum({field})",
        aggregation="sum",
        required_fields=(field,),
        grain=("window_id",),
        claim_types=("comparative_change", "source_reconciliation"),
        reconciliation_tolerance=0.0 if exact_count else 0.01,
        reconciliation_strategy=(
            "exact_additive_count" if exact_count else "additive_sum"
        ),
        value_semantics="raw_scalar",
        display_format="number",
    )


def dashboard_channel_dimension():
    return DimensionBinding(
        "channel",
        "contracts/sources/market-dashboard.source.yaml@0.1#runtime_binding.channel",
        "market_dashboard_channel",
        "channel",
        ("day", "window_id"),
    )


def snapshot(dataset_id="paid_order_success", *, fields=(), table="analytics.paid_success"):
    if table == "analytics.paid_success" and dataset_id == "market_dashboard":
        table = "market_dashboard_daily"
    if table == "analytics.paid_success" and dataset_id == "market_dashboard_channel":
        table = "market_dashboard_channel_daily"
    default_fields = {
        "paid_order_success": ("business_date_lagos", "paid_amount_ngn", "user_id", "channel"),
        "payment_attempt": ("支付发起时间", "订单id", "支付状态"),
        "market_dashboard": ("snapshot_id", "load_revision", "business_date", "paid_amount"),
        "gameplay": ("business_date", "paid_amount_ngn", "gameplay"),
        "external_event": ("event_start_date",),
        "internal_operation_event": ("event_start_date",),
    }
    selected = DatasetSnapshot(
        f"snapshot:{dataset_id}:1",
        dataset_id,
        table,
        "2026-07-04",
        "schema",
        tuple(fields or default_fields[dataset_id]),
        f"contract:{dataset_id}@1",
        "2026-07-05T00:00:00Z",
        "active",
    )
    if dataset_id in {"market_dashboard", "market_dashboard_channel"}:
        peer_dataset_id = (
            "market_dashboard_channel"
            if dataset_id == "market_dashboard"
            else "market_dashboard"
        )
        peer = replace(
            selected,
            snapshot_ref=f"snapshot:{peer_dataset_id}:peer",
            dataset_id=peer_dataset_id,
            physical_table=(
                "market_dashboard_channel_daily__peer"
                if peer_dataset_id == "market_dashboard_channel"
                else "market_dashboard_daily__peer"
            ),
            schema_fingerprint="peer-schema",
            evidence_state=(
                "context_only"
                if peer_dataset_id == "market_dashboard_channel"
                else "claim_ready"
            ),
            reconciliation_status=(
                "mismatch"
                if peer_dataset_id == "market_dashboard_channel"
                else "matched"
            ),
            rows_content_hash="b" * 64,
        )
        release_ref = dataset_snapshot_release_ref(
            "dashboard-logical",
            "dashboard-load:sha256:reviewed",
            (selected.snapshot_ref, peer.snapshot_ref),
        )
        selected = replace(
            selected,
            logical_snapshot_id="dashboard-logical",
            load_revision="dashboard-load:sha256:reviewed",
            release_ref=release_ref,
            rows_content_hash="a" * 64,
        )
        peer = replace(
            peer,
            logical_snapshot_id=selected.logical_snapshot_id,
            load_revision=selected.load_revision,
            release_ref=release_ref,
        )
        record = build_dataset_release_authority_record(
            tuple({**item.to_dict(), "requires_release": True} for item in (selected, peer))
        )
        selected = replace(selected, authority_record_ref=record.authority_record_ref)
        _RELEASE_RESOLVERS[release_ref] = _ReleaseResolver(record)
    return selected


def contract(
    *,
    dataset_id="paid_order_success",
    query_intent="daily_metric_baselines",
    metrics=None,
    dimensions=(),
    filters=(),
    query_parameters=None,
):
    selected_metrics = (
        tuple(metrics)
        if metrics is not None
        else (metric(dataset_id),)
    )
    resolved = windows()
    required_fields = ["window_id", "window_role", "observation_key"]
    required_fields.extend(
        {
            "time_bucket_scan": ("calendar_week", "weekday", "month_phase"),
            "data_quality_probe": ("source_row_count",),
            "event_context_probe": (
                "event_count",
                "source_family",
                "event_id",
                "event_type",
                "event_start_date",
                "event_end_date",
                "affected_scope",
                "authority",
                "evidence_level",
                "wording_limit",
                "recurrence_kind",
                "recurrence_month_start",
                "recurrence_day_start",
                "recurrence_month_end",
                "recurrence_day_end",
                "payload",
            ),
            "high_value_scan": (
                "high_value_threshold",
                "high_value_amount",
                "high_value_paid_users",
            ),
        }.get(query_intent, ())
    )
    required_fields.extend(item.metric_id for item in selected_metrics)
    required_fields.extend(item.dimension_id for item in dimensions)
    grain = [
        "window_id",
        "event_id" if query_intent == "event_context_probe" else "observation_key",
        *(item.dimension_id for item in dimensions),
    ]
    reviewed_parameters = (
        {
            "threshold_quantile": 0.95,
            "threshold_reference": "within_window_user_paid_amount",
            "aggregation_grain": (
                "window_id",
                "observation_key",
                "user_id",
            ),
        }
        if query_intent == "high_value_scan"
        else {}
    )
    reviewed_shape = RuntimeContractRegistry.from_path(
        "contracts/runtime/clickhouse-analysis-bindings.yaml"
    ).query_shape(query_intent)
    unsigned = QueryContract(
        query_contract_id=f"query:run:{dataset_id}:{query_intent}:1",
        analysis_contract_ref="analysis:run:1",
        query_intent=query_intent,
        dataset_snapshot_refs=(f"snapshot:{dataset_id}:1",),
        metric_bindings=selected_metrics,
        dimension_bindings=tuple(dimensions),
        window_refs=tuple(item.window_id for item in resolved),
        resolved_windows=resolved,
        filters=tuple(filters),
        result_shape=ResultShape(
            tuple(required_fields),
            tuple(grain),
            tuple(grain),
            tuple(item.window_id for item in resolved),
            "complete_context_rows" if query_intent == "event_context_probe" else "complete_aggregate",
            str(reviewed_shape["dimension_presence_policy"]),
        ),
        completeness_assertions=("required_windows", "unique_key"),
        workload_class="interactive_aggregate",
        contract_signature="",
        query_parameters=(
            dict(query_parameters)
            if query_parameters is not None
            else reviewed_parameters
        ),
        join_expectation=(
            JoinExpectation(
                cardinality="many_to_one",
                audit_fields=(
                    "__join_input_rows",
                    "__join_output_rows",
                    "__join_duplicate_keys",
                    "__join_unmatched_rows",
                ),
                max_duplicate_keys=0,
                max_unmatched_rows=0,
            )
            if query_intent == "high_value_scan"
            else None
        ),
    )
    return replace(unsigned, contract_signature=query_contract_signature(unsigned))


def resigned(base, **changes):
    unsigned = replace(base, contract_signature="", **changes)
    return replace(
        unsigned,
        contract_signature=query_contract_signature(unsigned),
    )


class ClickHouseQueryCompilerTest(unittest.TestCase):
    def test_dimension_null_bucket_is_normalized_before_aggregation(self):
        query = contract(
            query_intent="dimension_contribution_scan",
            dimensions=(dimension(),),
        )

        compiled = compile_clickhouse_query(
            query,
            {"snapshot:paid_order_success:1": snapshot()},
        )

        self.assertIn(
            "ifNull(nullIf(trim(toString(`channel`)), ''), "
            "%(dimension_null_bucket_0)s) AS `channel`",
            compiled.sql_text,
        )
        self.assertEqual(
            compiled.parameters["dimension_null_bucket_0"],
            "Blank",
        )

    def test_dashboard_bindings_require_verified_release_and_physical_revision(self):
        selected = snapshot(
            "market_dashboard",
            fields=("business_date", "paid_amount"),
            table="market_dashboard_daily",
        )
        selected = replace(
            selected,
            logical_snapshot_id="",
            load_revision="",
            release_ref="",
            authority_record_ref="",
            rows_content_hash="",
        )
        query = contract(
            dataset_id="market_dashboard",
            metrics=(dashboard_metric(),),
        )

        with self.assertRaisesRegex(
            ValueError,
            "dataset_(physical_revision|release)_required",
        ):
            compile_clickhouse_query(query, {selected.snapshot_ref: selected})

    def test_dashboard_release_authority_fields_are_typed_snapshot_contract(self):
        self.assertTrue(
            {"release_ref", "authority_record_ref", "rows_content_hash"}.issubset(
                DatasetSnapshot.__dataclass_fields__
            )
        )

    def test_dashboard_source_adapters_compile_business_metrics_and_revision_filter(self):
        fields = (
            "snapshot_id",
            "load_revision",
            "business_date",
            "game",
            "paid_amount",
            "active_users",
            "new_users",
            "aggregate_marketing_cost",
            "profit",
        )
        selected = snapshot(
            "market_dashboard",
            fields=fields,
            table="market_dashboard_daily",
        )
        selected = replace(
            selected,
            logical_snapshot_id="dashboard-logical",
            load_revision="load:sha256:reviewed",
            evidence_state="claim_ready",
            reconciliation_status="mismatch",
            reconciliation_ref="reconciliation:dashboard",
        )
        for metric_id in (
            "paid_amount",
            "active_users",
            "new_users",
            "aggregate_marketing_cost",
            "profit",
        ):
            with self.subTest(metric_id=metric_id):
                compiled = compile_clickhouse_query(
                    contract(
                        dataset_id="market_dashboard",
                        metrics=(dashboard_metric(metric_id),),
                    ),
                    {selected.snapshot_ref: selected},
                )
                self.assertIn(f"sum({metric_id})", compiled.sql_text)
                self.assertIn("`snapshot_id` = %(physical_snapshot_id)s", compiled.sql_text)
                self.assertIn("`load_revision` = %(load_revision)s", compiled.sql_text)
                self.assertEqual(compiled.parameters["physical_snapshot_id"], "dashboard-logical")
                self.assertEqual(compiled.parameters["load_revision"], "load:sha256:reviewed")

    def test_dashboard_channel_adapter_requires_matched_claim_ready_release(self):
        selected = snapshot(
            "market_dashboard_channel",
            fields=(
                "snapshot_id",
                "load_revision",
                "business_date",
                "game",
                "channel",
                "paid_amount",
            ),
            table="market_dashboard_channel_daily",
        )
        selected = replace(
            selected,
            logical_snapshot_id="dashboard-logical",
            load_revision="load:sha256:reviewed",
            evidence_state="claim_ready",
            reconciliation_status="matched",
            reconciliation_ref="reconciliation:matched",
        )
        query = contract(
            dataset_id="market_dashboard_channel",
            query_intent="dimension_contribution_scan",
            metrics=(dashboard_metric("paid_amount", "market_dashboard_channel"),),
            dimensions=(dashboard_channel_dimension(),),
        )

        compiled = compile_clickhouse_query(query, {selected.snapshot_ref: selected})
        self.assertIn(
            "ifNull(nullIf(trim(toString(`channel`)), ''), "
            "%(dimension_null_bucket_0)s) AS `channel`",
            compiled.sql_text,
        )

        mismatched = replace(
            selected,
            evidence_state="context_only",
            reconciliation_status="mismatch",
        )
        with self.assertRaisesRegex(ValueError, "dataset_evidence_state_not_claim_ready"):
            compile_clickhouse_query(query, {mismatched.snapshot_ref: mismatched})

        context_probe = contract(
            dataset_id="market_dashboard_channel",
            query_intent="data_quality_probe",
            metrics=(),
        )
        compiled_context = compile_clickhouse_query(
            context_probe,
            {mismatched.snapshot_ref: mismatched},
        )
        self.assertIn("count() AS `source_row_count`", compiled_context.sql_text)
    def test_event_context_keeps_reviewed_count_with_metric_projection(self):
        compiled = compile_clickhouse_query(
            contract(
                dataset_id="external_event",
                query_intent="event_context_probe",
                metrics=(),
            ),
            {"snapshot:external_event:1": snapshot("external_event")},
        )

        self.assertIn("toUInt64(1) AS `event_count`", compiled.sql_text)
        self.assertIn("`event_start_date` < tupleElement(analysis_window, 4)", compiled.sql_text)
        self.assertIn("`event_end_date` >= tupleElement(analysis_window, 3)", compiled.sql_text)
        for field in (
            "source_family",
            "event_id",
            "event_type",
            "event_start_date",
            "event_end_date",
            "affected_scope",
            "authority",
            "evidence_level",
            "wording_limit",
            "recurrence_kind",
            "recurrence_month_start",
            "recurrence_day_start",
            "recurrence_month_end",
            "recurrence_day_end",
            "payload",
        ):
            self.assertIn(f"`{field}`", compiled.sql_text)
        self.assertIn("context_rows AS (", compiled.sql_text)
        self.assertIn("UNION ALL", compiled.sql_text)
        self.assertIn("NOT IN (SELECT `window_id` FROM matched_events)", compiled.sql_text)
        self.assertIn("SELECT * FROM context_rows", compiled.sql_text)
        self.assertIn("ORDER BY `window_id`, `event_id`", compiled.sql_text)
        self.assertTrue(compiled.sql_text.endswith("LIMIT 5001"))
        self.assertEqual(compiled.max_context_rows, 5000)
        self.assertEqual(compiled.settings["max_result_rows"], 5001)
        self.assertIn("`recurrence_kind` = 'monthly_day_range'", compiled.sql_text)
        self.assertIn("`recurrence_kind` = 'annual_month_day_range'", compiled.sql_text)
        self.assertIn("arrayExists(recurrence_day_offset", compiled.sql_text)
        self.assertIn("dateDiff('day'", compiled.sql_text)

    def test_derived_metric_alias_collision_prefers_physical_source_columns(self):
        registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        bindings = []
        for metric_id in (
            "player_bet_amount",
            "player_bet_count",
            "player_avg_bet_amount",
        ):
            metric_contract = registry.metric(metric_id, dataset_id="gameplay")
            bindings.append(
                MetricBinding(
                    metric_id=metric_id,
                    contract_ref=metric_contract["contract_ref"],
                    dataset_id="gameplay",
                    expression=metric_contract["expression"],
                    aggregation=metric_contract["aggregation"],
                    required_fields=tuple(metric_contract["required_fields"]),
                    grain=tuple(metric_contract["grain"]),
                    numerator_metric=str(
                        metric_contract.get("numerator_metric") or ""
                    ),
                    denominator_metric=str(
                        metric_contract.get("denominator_metric") or ""
                    ),
                    claim_types=tuple(metric_contract["claim_types"]),
                    reconciliation_tolerance=metric_contract[
                        "reconciliation_tolerance"
                    ],
                    reconciliation_strategy=metric_contract[
                        "reconciliation_strategy"
                    ],
                    value_semantics=metric_contract["value_semantics"],
                    display_format=metric_contract["display_format"],
                )
            )

        compiled = compile_clickhouse_query(
            contract(
                dataset_id="gameplay",
                query_intent="association_candidate_timeseries",
                metrics=tuple(bindings),
            ),
            {"snapshot:gameplay:1": snapshot("gameplay")},
        )

        self.assertIn(
            "sum(player_bet_amount) / nullIf(sum(player_bet_count), 0)",
            compiled.sql_text,
        )
        self.assertEqual(compiled.settings["prefer_column_name_to_alias"], 1)

    def test_rejects_high_value_dimensions_outside_reviewed_threshold_grain(self):
        base = contract(
            query_intent="high_value_scan",
            dimensions=(dimension(),),
        )

        with self.assertRaisesRegex(
            ValueError,
            "high_value_dimension_bindings_unsupported",
        ):
            compile_clickhouse_query(
                base,
                {"snapshot:paid_order_success:1": snapshot()},
            )

    def test_rejects_resigned_window_reference_and_boundary_inconsistency(self):
        base = contract()
        window_ids = base.window_refs
        cases = (
            (
                resigned(
                    base,
                    window_refs=window_ids[:-1],
                    result_shape=replace(
                        base.result_shape,
                        required_window_ids=window_ids[:-1],
                    ),
                ),
                "query_contract_window_refs_mismatch",
            ),
            (
                resigned(
                    base,
                    window_refs=tuple(reversed(window_ids)),
                    result_shape=replace(
                        base.result_shape,
                        required_window_ids=tuple(reversed(window_ids)),
                    ),
                ),
                "query_contract_window_refs_mismatch",
            ),
            (
                resigned(
                    base,
                    result_shape=replace(
                        base.result_shape,
                        required_window_ids=window_ids[:-1],
                    ),
                ),
                "query_contract_result_window_refs_mismatch",
            ),
            (
                resigned(
                    base,
                    resolved_windows=(
                        replace(
                            base.resolved_windows[0],
                            end_exclusive=base.resolved_windows[0].start_inclusive,
                        ),
                        *base.resolved_windows[1:],
                    ),
                ),
                "invalid_resolved_window_boundary:target_day",
            ),
            (
                resigned(
                    base,
                    window_refs=("", *window_ids[1:]),
                    resolved_windows=(
                        replace(base.resolved_windows[0], window_id=""),
                        *base.resolved_windows[1:],
                    ),
                    result_shape=replace(
                        base.result_shape,
                        required_window_ids=("", *window_ids[1:]),
                    ),
                ),
                "invalid_resolved_window_field:window_id",
            ),
        )

        for query_contract, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                (TypeError, ValueError),
                reason,
            ):
                compile_clickhouse_query(
                    query_contract,
                    {"snapshot:paid_order_success:1": snapshot()},
                )

    def test_rejects_invalid_direct_query_contract_nested_runtime_types(self):
        base = contract()
        cases = (
            (resigned(base, metric_bindings=({},)), "metric_bindings"),
            (resigned(base, dimension_bindings=("channel",)), "dimension_bindings"),
            (resigned(base, resolved_windows=({},)), "resolved_windows"),
            (resigned(base, result_shape={}), "result_shape"),
            (resigned(base, query_parameters=()), "query_parameters"),
            (resigned(base, window_refs=list(base.window_refs)), "window_refs"),
        )
        selected_snapshot = snapshot()

        for query_contract, field in cases:
            with self.subTest(field=field), self.assertRaisesRegex(
                TypeError,
                f"invalid_query_contract_runtime_type:{field}",
            ):
                compile_clickhouse_query(
                    query_contract,
                    {selected_snapshot.snapshot_ref: selected_snapshot},
                )

        with self.assertRaisesRegex(
            TypeError,
            "invalid_snapshot_runtime_type",
        ):
            compile_clickhouse_query(
                base,
                {selected_snapshot.snapshot_ref: selected_snapshot.to_dict()},
            )

    def test_rejects_invalid_direct_metric_reconciliation_tolerance(self):
        base = contract()
        for invalid in (-0.01, float("nan"), True, "0.01"):
            invalid_binding = replace(
                base.metric_bindings[0],
                reconciliation_tolerance=invalid,
            )
            invalid_contract = resigned(
                base,
                metric_bindings=(invalid_binding,),
            )
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                (TypeError, ValueError),
                "invalid_query_contract_runtime_type:"
                "metric_bindings.reconciliation_tolerance",
            ):
                compile_clickhouse_query(
                    invalid_contract,
                    {"snapshot:paid_order_success:1": snapshot()},
                )

    def test_rejects_invalid_snapshot_metadata_before_compilation(self):
        base = contract()
        selected_snapshot = snapshot()
        cases = (
            (replace(selected_snapshot, watermark="not-a-date"), "watermark"),
            (replace(selected_snapshot, loaded_at="not-a-time"), "loaded_at"),
            (replace(selected_snapshot, schema_fingerprint=""), "schema_fingerprint"),
            (replace(selected_snapshot, contract_ref=""), "contract_ref"),
        )

        for invalid_snapshot, field in cases:
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError,
                f"invalid_snapshot_metadata:{field}",
            ):
                compile_clickhouse_query(
                    base,
                    {invalid_snapshot.snapshot_ref: invalid_snapshot},
                )

    def test_rejects_unreviewed_date_adapter_functions_fields_and_structure(self):
        expressions = (
            "evilDate(business_date_lagos)",
            "toDate(other_field)",
            "toDate(business_date_lagos) UNION SELECT business_date_lagos",
        )
        for expression in expressions:
            payload = load_contract(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            )
            dataset = payload["datasets"]["paid_order_success"]
            dataset.pop("date_field", None)
            dataset["date_expression"] = expression
            dataset["required_fields"] = ["business_date_lagos"]
            registry = RuntimeContractRegistry(payload)
            with self.subTest(expression=expression), patch(
                "bi_agent.runtime.clickhouse_query_compiler._runtime_registry",
                return_value=registry,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "dataset_date_binding_invalid:paid_order_success",
                ):
                    compile_clickhouse_query(
                        contract(),
                        {"snapshot:paid_order_success:1": snapshot()},
                    )

    def test_rejects_unsafe_expression_even_if_compromised_registry_matches(self):
        payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        expression = "sum(paid_amount_ngn), groupArray(user_id)"
        payload["metrics"]["paid_amount"]["expression"] = expression
        payload["metrics"]["paid_amount"]["required_fields"] = [
            "paid_amount_ngn",
            "user_id",
        ]
        registry = RuntimeContractRegistry(payload)
        base = contract()
        unsafe_binding = replace(
            base.metric_bindings[0],
            expression=expression,
            required_fields=("paid_amount_ngn", "user_id"),
        )
        unsigned = replace(
            base,
            metric_bindings=(unsafe_binding,),
            contract_signature="",
        )
        resigned = replace(
            unsigned,
            contract_signature=query_contract_signature(unsigned),
        )

        with patch(
            "bi_agent.runtime.clickhouse_query_compiler._runtime_registry",
            return_value=registry,
        ), self.assertRaisesRegex(ValueError, "unsafe_metric_expression"):
            compile_clickhouse_query(
                resigned,
                {"snapshot:paid_order_success:1": snapshot()},
            )

    def test_rejects_resigned_display_policy_drift_from_current_registry(self):
        base = contract()
        drifted_binding = replace(
            base.metric_bindings[0],
            value_semantics="scalar_ratio",
            display_format="percent",
        )
        unsigned = replace(
            base,
            metric_bindings=(drifted_binding,),
            contract_signature="",
        )
        resigned = replace(
            unsigned,
            contract_signature=query_contract_signature(unsigned),
        )

        with self.assertRaisesRegex(ValueError, "reviewed_metric_binding_mismatch"):
            compile_clickhouse_query(
                resigned,
                {"snapshot:paid_order_success:1": snapshot()},
            )

    def test_structural_words_inside_reviewed_literals_are_not_sql_structure(self):
        payload = load_contract(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        expression = "countIf(channel = 'DROP')"
        payload["metrics"]["paid_amount"]["expression"] = expression
        payload["metrics"]["paid_amount"]["aggregation"] = "count_if"
        payload["metrics"]["paid_amount"]["required_fields"] = ["channel"]
        registry = RuntimeContractRegistry(payload)
        base = contract()
        reviewed_binding = replace(
            base.metric_bindings[0],
            expression=expression,
            aggregation="count_if",
            required_fields=("channel",),
        )
        unsigned = replace(
            base,
            metric_bindings=(reviewed_binding,),
            contract_signature="",
        )
        resigned = replace(
            unsigned,
            contract_signature=query_contract_signature(unsigned),
        )

        with patch(
            "bi_agent.runtime.clickhouse_query_compiler._runtime_registry",
            return_value=registry,
        ):
            compiled = compile_clickhouse_query(
                resigned,
                {"snapshot:paid_order_success:1": snapshot()},
            )

        self.assertIn(expression, compiled.sql_text)

    def test_rejects_semantic_tampering_before_compilation(self):
        base = contract()
        cases = (
            replace(
                base,
                filters=({"field": "channel", "op": "eq", "value": "tampered"},),
            ),
            replace(base, query_parameters={"unexpected": True}),
            replace(
                base,
                metric_bindings=(
                    replace(
                        base.metric_bindings[0],
                        expression="sum(paid_amount_ngn) * 2",
                    ),
                ),
            ),
        )
        for tampered in cases:
            with self.subTest(tampered=tampered):
                with self.assertRaisesRegex(
                    ValueError,
                    "query_contract_signature_mismatch",
                ):
                    compile_clickhouse_query(
                        tampered,
                        {"snapshot:paid_order_success:1": snapshot()},
                    )

    def test_rejects_resigned_unreviewed_metric_expression_and_raw_alias(self):
        base = contract()
        cases = (
            (
                replace(
                    base.metric_bindings[0],
                    expression="sum(paid_amount_ngn), groupArray(user_id)",
                ),
                "unsafe_metric_expression",
            ),
            (
                replace(
                    base.metric_bindings[0],
                    expression="sum(paid_amount_ngn) * 2",
                ),
                "reviewed_metric_binding_mismatch",
            ),
            (
                replace(base.metric_bindings[0], metric_id="user_id"),
                "reviewed_metric_binding_mismatch",
            ),
        )
        for binding, expected_reason in cases:
            unsigned = replace(
                base,
                metric_bindings=(binding,),
                result_shape=replace(
                    base.result_shape,
                    required_fields=(
                        "window_id",
                        "window_role",
                        "observation_key",
                        binding.metric_id,
                    ),
                ),
                contract_signature="",
            )
            resigned = replace(
                unsigned,
                contract_signature=query_contract_signature(unsigned),
            )
            with self.subTest(binding=binding):
                with self.assertRaisesRegex(ValueError, expected_reason):
                    compile_clickhouse_query(
                        resigned,
                        {"snapshot:paid_order_success:1": snapshot()},
                    )

    def test_rejects_resigned_query_parameters_outside_reviewed_shape(self):
        base = contract(
            query_intent="high_value_scan",
            query_parameters={
                "threshold_quantile": 0.9,
                "threshold_reference": "within_window_user_paid_amount",
                "aggregation_grain": (
                    "window_id",
                    "observation_key",
                    "user_id",
                ),
            },
        )

        with self.assertRaisesRegex(
            ValueError,
            "reviewed_query_parameters_mismatch:high_value_scan",
        ):
            compile_clickhouse_query(
                base,
                {"snapshot:paid_order_success:1": snapshot()},
            )

    def test_rejects_reviewed_high_value_semantics_the_compiler_cannot_execute(self):
        changes = (
            (
                "threshold_reference",
                "whole_window_user_paid_amount",
                "high_value_threshold_reference_unsupported",
            ),
            (
                "aggregation_grain",
                ("window_id", "user_id"),
                "high_value_aggregation_grain_unsupported",
            ),
        )
        for field, value, reason in changes:
            payload = load_contract(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            )
            payload["query_shapes"]["high_value_scan"]["query_parameters"][
                field
            ] = value
            registry = RuntimeContractRegistry(payload)
            base = contract(query_intent="high_value_scan")
            query_parameters = dict(base.query_parameters)
            query_parameters[field] = value
            changed = replace(base, query_parameters=query_parameters)
            changed = replace(
                changed,
                contract_signature=query_contract_signature(changed),
            )

            with self.subTest(field=field), patch(
                "bi_agent.runtime.clickhouse_query_compiler._runtime_registry",
                return_value=registry,
            ):
                with self.assertRaisesRegex(ValueError, reason):
                    compile_clickhouse_query(
                        changed,
                        {"snapshot:paid_order_success:1": snapshot()},
                    )

    def test_compiles_overlapping_windows_as_independent_memberships(self):
        compiled = compile_clickhouse_query(
            contract(),
            {"snapshot:paid_order_success:1": snapshot()},
        )

        self.assertNotIn("now(", compiled.sql_text.casefold())
        self.assertRegex(compiled.sql_text.casefold(), r"\barray\s+join\b")
        self.assertEqual(compiled.parameters["start_1"], compiled.parameters["start_2"])
        self.assertNotEqual(compiled.parameters["window_id_1"], compiled.parameters["window_id_2"])
        self.assertIn("%(window_id_1)s", compiled.sql_text)
        self.assertIn("%(window_id_2)s", compiled.sql_text)
        self.assertNotIn("limit 5000", compiled.sql_text.casefold())
        self.assertEqual(compiled.settings["result_overflow_mode"], "throw")

    def test_uses_validated_date_semantics_for_every_dataset_family(self):
        cases = {
            "paid_order_success": "`business_date_lagos`",
            "payment_attempt": "fromUnixTimestamp64Milli(toInt64OrZero(`支付发起时间`))",
            "market_dashboard": "`business_date`",
            "gameplay": "`business_date`",
            "external_event": "`event_start_date`",
            "internal_operation_event": "`event_start_date`",
        }
        for dataset_id, expected_date_sql in cases.items():
            with self.subTest(dataset_id=dataset_id):
                has_reviewed_metric = dataset_id in {
                    "paid_order_success",
                    "payment_attempt",
                }
                selected_metrics = (
                    (metric(dataset_id),) if has_reviewed_metric else ()
                )
                compiled = compile_clickhouse_query(
                    contract(
                        dataset_id=dataset_id,
                        query_intent=(
                            "event_context_probe"
                            if dataset_id.endswith("event")
                            else "payment_success_scan"
                            if dataset_id == "payment_attempt"
                            else "data_quality_probe"
                            if dataset_id in {"market_dashboard", "gameplay"}
                            else "daily_metric_baselines"
                        ),
                        metrics=selected_metrics,
                    ),
                    {f"snapshot:{dataset_id}:1": snapshot(dataset_id)},
                )
                self.assertIn(expected_date_sql, compiled.sql_text)
                self.assertNotIn("now(", compiled.sql_text.casefold())
                expected_table = (
                    "FROM `market_dashboard_daily`"
                    if dataset_id == "market_dashboard"
                    else "FROM `gameplay_daily__a1a1a1a1a1a1a1a1`"
                    if dataset_id == "gameplay"
                    else "FROM `business_events__c3c3c3c3c3c3c3c3`"
                    if dataset_id == "external_event"
                    else "FROM `business_events__d4d4d4d4d4d4d4d4`"
                    if dataset_id == "internal_operation_event"
                    else "FROM `analytics`.`paid_success`"
                )
                self.assertIn(expected_table, compiled.sql_text)

    def test_compiles_contract_filters_with_parameters(self):
        compiled = compile_clickhouse_query(
            contract(
                dimensions=(dimension(),),
                filters=(
                    {"field": "channel", "op": "eq", "value": "ads' OR 1=1"},
                    {"field": "paid_amount_ngn", "op": "gte", "value": 10},
                ),
            ),
            {"snapshot:paid_order_success:1": snapshot()},
        )

        self.assertNotIn("ads' OR 1=1", compiled.sql_text)
        self.assertIn("`channel` = %(filter_0)s", compiled.sql_text)
        self.assertIn("`paid_amount_ngn` >= %(filter_1)s", compiled.sql_text)
        self.assertEqual(compiled.parameters["filter_0"], "ads' OR 1=1")
        self.assertEqual(compiled.parameters["filter_1"], 10)

    def test_rejects_unsupported_filter_operator_explicitly(self):
        with self.assertRaisesRegex(ValueError, "unsupported_filter_operator:contains"):
            compile_clickhouse_query(
                contract(filters=({"field": "channel", "op": "contains", "value": "ads"},)),
                {"snapshot:paid_order_success:1": snapshot()},
            )

    def test_rejects_unreviewed_metric_expression_shape(self):
        with self.assertRaisesRegex(ValueError, "unsafe_metric_expression"):
            compile_clickhouse_query(
                contract(metrics=(metric(expression="sum(paid_amount_ngn); DROP TABLE x"),)),
                {"snapshot:paid_order_success:1": snapshot()},
            )

    def test_high_value_scan_aggregates_threshold_without_identifier_output(self):
        compiled = compile_clickhouse_query(
            contract(query_intent="high_value_scan"),
            {"snapshot:paid_order_success:1": snapshot()},
        )

        self.assertIn("quantileExact", compiled.sql_text)
        self.assertIn("%(threshold_quantile)s", compiled.sql_text)
        self.assertEqual(compiled.parameters["threshold_quantile"], 0.95)
        self.assertNotIn("quantileExact(0.95)", compiled.sql_text)
        self.assertIn("`threshold_cutoff`", compiled.sql_text)
        self.assertIn("`is_high_value`", compiled.sql_text)
        self.assertIn("high_value_threshold", compiled.sql_text)
        final_select = compiled.sql_text.rsplit("SELECT", 1)[-1].split("FROM", 1)[0]
        self.assertNotIn("user_id", final_select)
        self.assertNotIn("order_id", final_select)
        self.assertIn("LEFT JOIN thresholds", compiled.sql_text)
        self.assertIn("pre_join_audit AS (", compiled.sql_text)
        self.assertIn("right_key_audit AS (", compiled.sql_text)
        self.assertIn("joined_rows AS (", compiled.sql_text)
        self.assertIn("count() AS `join_input_rows`", compiled.sql_text)
        self.assertIn("count() AS `__join_output_rows`", compiled.sql_text)
        self.assertIn("right_key_multiplicity", compiled.sql_text)
        self.assertNotIn("toUInt64(0) AS `__join_duplicate_keys`", compiled.sql_text)
        self.assertIn("AS `__join_input_rows`", compiled.sql_text)
        self.assertIn("AS `__join_output_rows`", compiled.sql_text)
        self.assertIn("AS `__join_duplicate_keys`", compiled.sql_text)
        self.assertIn("AS `__join_unmatched_rows`", compiled.sql_text)
        self.assertEqual(compiled.settings["join_use_nulls"], 1)

    def test_rejects_unvalidated_physical_table(self):
        with self.assertRaisesRegex(ValueError, "invalid_physical_table"):
            compile_clickhouse_query(
                contract(),
                {
                    "snapshot:paid_order_success:1": snapshot(
                        table="paid_success; DROP TABLE raw"
                    )
                },
            )


if __name__ == "__main__":
    unittest.main()
