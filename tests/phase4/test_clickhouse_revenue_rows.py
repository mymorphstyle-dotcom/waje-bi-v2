from dataclasses import replace
import unittest

from bi_agent.runtime.analysis_contracts import QueryResultEnvelope
from bi_agent.runtime.clickhouse_revenue_rows import (
    ClickHouseRevenueRows as _ClickHouseRevenueRows,
)
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult, ClickHouseRuntime
from bi_agent.runtime.query_executor import (
    AggregateRowsStore,
    ClickHouseQueryExecutor as _ClickHouseQueryExecutor,
)
from tests.phase4.test_clickhouse_query_compiler import (
    contract,
    resigned,
    snapshot as raw_snapshot,
)
from tests.phase4.test_query_completeness import (
    _PAID_RELEASE_RESOLVER,
    authorize_paid_snapshot,
)


class ClickHouseQueryExecutor(_ClickHouseQueryExecutor):
    def __init__(self, runtime, **kwargs):
        kwargs.setdefault("release_resolver", _PAID_RELEASE_RESOLVER)
        super().__init__(runtime, **kwargs)


class ClickHouseRevenueRows(_ClickHouseRevenueRows):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("release_resolver", _PAID_RELEASE_RESOLVER)
        super().__init__(*args, **kwargs)


def snapshot():
    return authorize_paid_snapshot(raw_snapshot())


class FakeRuntime:
    def __init__(self, rows=(), ok=True, reason="", rows_by_query_id=None, describe_rows=()):
        self.rows = tuple(rows)
        self.describe_rows = tuple(describe_rows)
        self.rows_by_query_id = {
            str(query_id): tuple(query_rows)
            for query_id, query_rows in (rows_by_query_id or {}).items()
        }
        self.ok = ok
        self.reason = reason
        self.calls = []
        self.binding = type("Binding", (), {"ok": True, "reason": ""})()

    def configured(self):
        return self.binding.ok

    def describe_table(self, table_name):
        return ClickHouseQueryResult(
            ok=self.ok,
            reason=self.reason,
            rows=self.describe_rows,
            query_hash=f"hash-describe-{table_name}",
            query_id=f"describe:{table_name}",
        )

    def aggregate(
        self,
        sql,
        query_id,
        *,
        parameters=None,
        settings=None,
        execution_attempt_ref="",
    ):
        self.calls.append((sql, query_id))
        rows = self.rows_by_query_id.get(query_id, self.rows)
        return ClickHouseQueryResult(
            ok=self.ok,
            reason=self.reason,
            rows=rows,
            query_hash=f"hash-{query_id}",
            query_id=query_id,
            execution_attempt_ref=execution_attempt_ref,
        )


class FakeParameterizedResult:
    column_names = ("window_id", "window_role", "observation_key", "paid_amount")
    result_rows = (("target_day", "target", "2026-06-02", 120.0),)
    summary = {"read_rows": 42, "read_bytes": 2048}
    query_id = "provider-query-id"


class FakeParameterizedClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        return FakeParameterizedResult()


class FakeInternalTypeErrorClient:
    def query(self, sql, **kwargs):
        raise TypeError("client decoding bug")


class FakeResultClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        return self.result


class ClickHouseRevenueRowsTest(unittest.TestCase):
    def test_execution_refs_are_attempt_scoped_while_rows_content_is_reusable(self):
        client = FakeParameterizedClient()
        executor = ClickHouseQueryExecutor(ClickHouseRuntime(client=client))
        selected_snapshot = snapshot()
        selected_contract = contract()

        first = executor.execute(
            selected_contract,
            {selected_snapshot.snapshot_ref: selected_snapshot},
            execution_attempt_ref="attempt:run-a:1",
        )
        second = executor.execute(
            selected_contract,
            {selected_snapshot.snapshot_ref: selected_snapshot},
            execution_attempt_ref="attempt:run-a:2",
        )
        other_run = executor.execute(
            resigned(selected_contract, query_contract_id="query:run-b:1"),
            {selected_snapshot.snapshot_ref: selected_snapshot},
            execution_attempt_ref="attempt:run-a:1",
        )

        self.assertEqual(first.execution_attempt_ref, "attempt:run-a:1")
        self.assertEqual(second.execution_attempt_ref, "attempt:run-a:2")
        self.assertNotEqual(first.result_ref, second.result_ref)
        self.assertNotEqual(
            first.completeness_report_ref,
            second.completeness_report_ref,
        )
        self.assertEqual(first.rows_ref, second.rows_ref)
        self.assertNotEqual(first.result_ref, other_run.result_ref)
        self.assertEqual(first.rows_ref, other_run.rows_ref)
        provider_query_ids = tuple(
            str(kwargs["query_id"]) for _, kwargs in client.calls
        )
        self.assertEqual(len(provider_query_ids), 3)
        self.assertEqual(len(set(provider_query_ids)), 3)
        self.assertEqual(
            tuple(item.query_id for item in (first, second, other_run)),
            provider_query_ids,
        )

    def test_failed_execution_refs_are_unique_per_attempt(self):
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=FakeParameterizedClient())
        )
        selected_snapshot = snapshot()
        invalid = replace(contract(), contract_signature="tampered")

        first = executor.execute(
            invalid,
            {selected_snapshot.snapshot_ref: selected_snapshot},
            execution_attempt_ref="attempt:failure:1",
        )
        second = executor.execute(
            invalid,
            {selected_snapshot.snapshot_ref: selected_snapshot},
            execution_attempt_ref="attempt:failure:2",
        )

        self.assertEqual(first.execution_status, "blocked")
        self.assertEqual(second.execution_status, "blocked")
        self.assertNotEqual(first.result_ref, second.result_ref)
        self.assertNotEqual(
            first.completeness_report_ref,
            second.completeness_report_ref,
        )
        self.assertEqual(first.rows_ref, second.rows_ref)

    def test_default_execution_attempt_refs_are_unique(self):
        client = FakeParameterizedClient()
        executor = ClickHouseQueryExecutor(ClickHouseRuntime(client=client))
        selected_snapshot = snapshot()

        first = executor.execute(
            contract(),
            {selected_snapshot.snapshot_ref: selected_snapshot},
        )
        second = executor.execute(
            contract(),
            {selected_snapshot.snapshot_ref: selected_snapshot},
        )

        self.assertTrue(first.execution_attempt_ref.startswith("attempt:"))
        self.assertTrue(second.execution_attempt_ref.startswith("attempt:"))
        self.assertNotEqual(first.execution_attempt_ref, second.execution_attempt_ref)
        self.assertNotEqual(first.result_ref, second.result_ref)

    def test_high_value_adapter_extracts_reviewed_join_audit_statistics(self):
        result = type(
            "HighValueJoinAuditResult",
            (),
            {
                "column_names": (
                    "window_id",
                    "window_role",
                    "observation_key",
                    "paid_amount",
                    "high_value_threshold",
                    "high_value_amount",
                    "high_value_paid_users",
                    "__join_input_rows",
                    "__join_output_rows",
                    "__join_duplicate_keys",
                    "__join_unmatched_rows",
                ),
                "result_rows": (
                    (
                        "target_day",
                        "target",
                        "2026-06-02",
                        120.0,
                        10.0,
                        80.0,
                        4,
                        10,
                        12,
                        2,
                        0,
                    ),
                ),
                "summary": {"read_rows": 10},
                "query_id": "high-value-audit",
            },
        )()
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=FakeResultClient(result))
        )
        selected_snapshot = snapshot()

        envelope = executor.execute(
            contract(query_intent="high_value_scan"),
            {selected_snapshot.snapshot_ref: selected_snapshot},
        )

        self.assertEqual(envelope.execution_status, "succeeded")
        self.assertEqual(envelope.provider_stats["join_input_rows"], 10)
        self.assertEqual(envelope.provider_stats["join_output_rows"], 12)
        self.assertEqual(envelope.provider_stats["join_duplicate_keys"], 2)
        self.assertEqual(envelope.provider_stats["join_unmatched_rows"], 0)
        self.assertNotIn("join_cardinality", envelope.provider_stats)
        self.assertFalse(
            any(key.startswith("__join_") for key in envelope.rows[0])
        )

    def test_executor_blocks_invalid_direct_nested_runtime_type(self):
        client = FakeParameterizedClient()
        executor = ClickHouseQueryExecutor(ClickHouseRuntime(client=client))
        dataset_snapshot = snapshot()
        invalid = resigned(contract(), filters=("not-a-filter-mapping",))

        envelope = executor.execute(
            invalid,
            {dataset_snapshot.snapshot_ref: dataset_snapshot},
        )

        self.assertEqual(envelope.execution_status, "blocked")
        self.assertIn(
            "invalid_query_contract_runtime_type:filters",
            envelope.failure_reason,
        )
        self.assertEqual(client.calls, [])

    def test_executor_returns_blocked_envelope_for_tampered_contract(self):
        dataset_snapshot = snapshot()
        cases = (
            (
                replace(contract(), contract_signature="tampered"),
                dataset_snapshot,
                "query_contract_signature_mismatch",
            ),
        )
        for query_contract, selected_snapshot, reason in cases:
            with self.subTest(reason=reason):
                executor = ClickHouseQueryExecutor(
                    ClickHouseRuntime(client=FakeParameterizedClient())
                )
                envelope = executor.execute(
                    query_contract,
                    {selected_snapshot.snapshot_ref: selected_snapshot},
                )
                self.assertEqual(envelope.execution_status, "blocked")
                self.assertIn(reason, envelope.failure_reason)
                self.assertTrue(envelope.result_ref)
                self.assertTrue(envelope.rows_ref)
                self.assertTrue(envelope.completeness_report_ref)

    def test_observed_grain_contains_only_expected_keys_present_in_every_row(self):
        result = type(
            "MissingObservationResult",
            (),
            {
                "column_names": ("window_id", "window_role", "paid_amount"),
                "result_rows": (("target_day", "target", 120.0),),
                "summary": {"read_rows": 1},
                "query_id": "missing-observation",
            },
        )()
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=FakeResultClient(result))
        )
        dataset_snapshot = snapshot()

        envelope = executor.execute(
            contract(),
            {dataset_snapshot.snapshot_ref: dataset_snapshot},
        )

        self.assertEqual(envelope.execution_status, "succeeded")
        self.assertEqual(envelope.observed_grain, ("window_id",))

    def test_raw_identifier_result_is_blocked(self):
        result = type(
            "RawIdentifierResult",
            (),
            {
                "column_names": (
                    "window_id",
                    "window_role",
                    "observation_key",
                    "paid_amount",
                    "user_id",
                ),
                "result_rows": (
                    ("target_day", "target", "2026-06-02", 120.0, "raw-user"),
                ),
                "summary": {"read_rows": 1},
                "query_id": "raw-identifier",
            },
        )()
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=FakeResultClient(result))
        )
        dataset_snapshot = snapshot()

        envelope = executor.execute(
            contract(),
            {dataset_snapshot.snapshot_ref: dataset_snapshot},
        )

        self.assertEqual(envelope.execution_status, "blocked")
        self.assertEqual(
            envelope.failure_reason,
            "unreviewed_output_field_rejected:user_id",
        )

    def test_arbitrary_unreviewed_output_field_is_blocked(self):
        result = type(
            "UnreviewedFieldResult",
            (),
            {
                "column_names": (
                    "window_id",
                    "window_role",
                    "observation_key",
                    "paid_amount",
                    "invented_score",
                ),
                "result_rows": (
                    ("target_day", "target", "2026-06-02", 120.0, 99),
                ),
                "summary": {"read_rows": 1},
                "query_id": "unreviewed-field",
            },
        )()
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=FakeResultClient(result))
        )
        dataset_snapshot = snapshot()

        envelope = executor.execute(
            contract(),
            {dataset_snapshot.snapshot_ref: dataset_snapshot},
        )

        self.assertEqual(envelope.execution_status, "blocked")
        self.assertEqual(
            envelope.failure_reason,
            "unreviewed_output_field_rejected:invented_score",
        )
        self.assertTrue(envelope.result_ref)

    def test_rows_store_ref_is_audit_complete_and_reads_are_isolated(self):
        rows_store = AggregateRowsStore()
        query_contract = contract()
        original = ({"window_id": "target_day", "nested": {"value": 1}},)

        rows_ref = rows_store.persist(
            "query-hash",
            query_contract.contract_signature,
            query_contract.dataset_snapshot_refs,
            original,
        )
        first_read = rows_store.get(rows_ref)
        first_read[0]["nested"]["value"] = 99

        self.assertIn("query-hash", rows_ref)
        self.assertIn(query_contract.contract_signature[:16], rows_ref)
        self.assertEqual(rows_store.get(rows_ref)[0]["nested"]["value"], 1)

    def test_result_and_report_share_execution_identity_separate_from_rows_content(self):
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=FakeParameterizedClient())
        )
        dataset_snapshot = snapshot()

        envelope = executor.execute(
            contract(),
            {dataset_snapshot.snapshot_ref: dataset_snapshot},
        )

        self.assertEqual(
            envelope.result_ref.removeprefix("result:"),
            envelope.completeness_report_ref.removeprefix("completeness:"),
        )
        self.assertNotEqual(
            envelope.rows_ref.removeprefix("rows:"),
            envelope.result_ref.removeprefix("result:"),
        )

    def test_typed_execution_collects_success_after_an_earlier_blocked_contract(self):
        client = FakeParameterizedClient()
        dataset_snapshot = snapshot()
        valid = contract()
        blocked = replace(
            valid,
            query_contract_id="query:run:blocked:1",
            contract_signature="tampered",
        )
        succeeding = replace(valid, query_contract_id="query:run:succeeding:2")
        provider = ClickHouseRevenueRows(
            runtime=ClickHouseRuntime(client=client),
            snapshots={dataset_snapshot.snapshot_ref: dataset_snapshot},
        )
        plan = provider.plan(
            {
                "run_id": "run-multi-typed",
                "compiler_runtime_plan": {
                    "query_contracts": (blocked, succeeding),
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods",),
        )

        result = provider.fetch(plan)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.query_envelopes), 2)
        self.assertEqual(
            tuple(item.execution_status for item in result.query_envelopes),
            ("blocked", "succeeded"),
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.rows[0]["paid_amount"], 120.0)

    def test_typed_projection_rejects_malformed_nested_values_without_fallback(self):
        provider = ClickHouseRevenueRows(
            runtime=ClickHouseRuntime(client=FakeParameterizedClient()),
            snapshots={snapshot().snapshot_ref: snapshot()},
        )
        projections = []
        malformed_filter = contract().to_dict()
        malformed_filter["filters"] = ("silently-dropped",)
        projections.append(malformed_filter)
        malformed_binding = contract().to_dict()
        malformed_binding["metric_bindings"] = ({"metric_id": "paid_amount"},)
        projections.append(malformed_binding)
        malformed_parameters = contract().to_dict()
        malformed_parameters["query_parameters"] = ("not", "a", "mapping")
        projections.append(malformed_parameters)

        for index, projection in enumerate(projections):
            with self.subTest(index=index):
                plan = provider.plan(
                    {
                        "run_id": f"run-malformed-{index}",
                        "compiler_runtime_plan": {
                            "query_contracts": (projection,),
                        },
                    },
                    {"time_window": "yesterday"},
                    ("compare_periods",),
                )
                result = provider.fetch(plan)
                self.assertEqual(plan.contract_mode, "typed")
                self.assertIn("invalid_typed_query_contract_projection", plan.reason)
                self.assertFalse(result.ok)
                self.assertEqual(result.contract_mode, "typed")

    def test_task4_serialized_projection_is_rejected_at_explicit_schema_boundary(self):
        provider = ClickHouseRevenueRows(
            runtime=ClickHouseRuntime(client=FakeParameterizedClient()),
            snapshots={snapshot().snapshot_ref: snapshot()},
        )
        legacy_projection = contract().to_dict()
        for field in (
            "query_role_ref",
            "reconciliation_binding",
            "join_expectation",
        ):
            legacy_projection.pop(field)
        legacy_projection["metric_bindings"][0].pop(
            "reconciliation_tolerance"
        )
        legacy_projection["metric_bindings"][0].pop(
            "reconciliation_strategy"
        )

        plan = provider.plan(
            {
                "run_id": "run-legacy-task4-projection",
                "compiler_runtime_plan": {
                    "query_contracts": (legacy_projection,),
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods",),
        )

        self.assertEqual(plan.contract_mode, "typed")
        self.assertIn(
            "legacy_query_contract_projection_unsupported",
            plan.reason,
        )

    def test_typed_projection_rejects_invalid_snapshots_and_unknown_nested_keys(self):
        unexpected_binding_key = contract().to_dict()
        unexpected_binding_key["metric_bindings"][0]["silent_extension"] = True
        projections = (
            {
                "query_contracts": (unexpected_binding_key,),
                "dataset_snapshots": (snapshot(),),
            },
            {
                "query_contracts": (contract(),),
                "dataset_snapshots": (snapshot(), "silently-invalid"),
            },
        )

        for index, runtime_plan in enumerate(projections):
            with self.subTest(index=index):
                client = FakeParameterizedClient()
                provider = ClickHouseRevenueRows(
                    runtime=ClickHouseRuntime(client=client),
                )
                plan = provider.plan(
                    {
                        "run_id": f"run-invalid-projection-{index}",
                        "compiler_runtime_plan": runtime_plan,
                    },
                    {"time_window": "yesterday"},
                    ("compare_periods",),
                )
                result = provider.fetch(plan)

                self.assertEqual(plan.contract_mode, "typed")
                self.assertIn("invalid_typed_query_contract_projection", plan.reason)
                self.assertFalse(result.ok)
                self.assertEqual(client.calls, [])

    def test_schema_fields_reads_clickhouse_describe_rows(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(
                describe_rows=(
                    {"name": "business_date_lagos", "type": "Date"},
                    {"name": "package_name", "type": "String"},
                )
            ),
            table="paid_order_success_clean_20240101_20260704",
        )

        self.assertEqual(
            provider.schema_fields(),
            ("business_date_lagos", "package_name"),
        )

    def test_plans_aggregate_only_rows_for_driver_and_joint_attribution(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {"run_id": "run-1"},
            {
                "question_family": "paid_amount_change_explanation",
                "target_metric": "paid_amount",
                "scope": "full_sample",
                "time_window": "yesterday",
            },
            ("compare_periods", "driver_decomposition", "joint_attribution"),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertIn("sum(paid_amount_ngn) AS amount", plan.sql_text)
        self.assertIn("uniqExact(user_id) AS paid_users", plan.sql_text)
        self.assertIn("count() AS orders", plan.sql_text)
        self.assertIn("channel", plan.dimension_keys)
        self.assertIn("payment_method", plan.dimension_keys)
        self.assertIn("amount", plan.required_fields)

    def test_plan_uses_compiler_runtime_row_shape_dimensions(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-compiler-plan",
                "compiler_runtime_plan": {
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel", "payment_method", "region"),
                            "required_fields": ("period", "group", "amount", "orders"),
                        }
                    ]
                },
            },
            {"time_window": "yesterday"},
            ("segment_contribution",),
        )

        self.assertEqual(plan.dimension_keys, ("channel", "payment_method", "region"))
        self.assertEqual(plan.required_fields, ("period", "group", "amount", "orders"))

    def test_plan_uses_compiler_query_specs_before_graph_fallback(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-compiler-plan",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("dimension_scan",),
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel",),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "paid_users",
                                "orders",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods",),
        )

        self.assertEqual(plan.query_id, "run-compiler-plan:dimension_scan")
        self.assertEqual(plan.dimension_keys, ("channel",))
        self.assertIn("GROUP BY period, group, channel", plan.sql_text)
        self.assertIn("- 12", plan.sql_text)

    def test_data_quality_probe_uses_schema_safe_payment_and_duplicate_metrics(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-quality-risk",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("data_quality_probe",),
                    "row_shapes": [
                        {
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "orders",
                                "paid_users",
                            ),
                            "optional_fields": ("payment_status", "order_id"),
                            "schema_fields": ("payment_status", "order_id"),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("data_quality_profile",),
        )

        self.assertIn("non_success_orders", plan.sql_text)
        self.assertIn("duplicate_orders", plan.sql_text)
        self.assertIn("non_success_orders", plan.required_fields)
        self.assertIn("duplicate_orders", plan.required_fields)

    def test_high_value_scan_uses_schema_safe_aggregate_fields(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-high-value",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("high_value_scan",),
                    "row_shapes": [
                        {
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "paid_users",
                            ),
                            "optional_fields": ("high_value_amount", "high_value_paid_users"),
                            "schema_fields": ("high_value_amount", "high_value_paid_users"),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("high_value_user_contribution",),
        )

        self.assertIn("sum(high_value_amount) AS high_value_amount", plan.sql_text)
        self.assertIn("sum(high_value_paid_users) AS high_value_paid_users", plan.sql_text)
        self.assertIn("high_value_amount", plan.required_fields)
        self.assertIn("high_value_paid_users", plan.required_fields)

    def test_plan_prefers_joint_scan_when_multi_intent_graph_needs_dimensions(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-multi-intent",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "joint_candidate_scan"),
                    "capability_params": {"joint_attribution": {"max_dimension_count": 2}},
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel", "payment_method", "region"),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "orders",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods", "joint_attribution"),
        )

        self.assertEqual(plan.query_id, "run-multi-intent:joint_candidate_scan")
        self.assertEqual(plan.dimension_keys, ("channel", "payment_method"))
        self.assertIn("GROUP BY period, group, channel, payment_method", plan.sql_text)

    def test_plan_blocks_unbound_custom_baseline_windows(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-custom-baseline",
                "compiler_runtime_plan": {
                    "windows": {"target": "Q2", "baseline": "Q1"},
                    "baselines": ("custom_baseline",),
                    "query_intents": ("daily_metric_baselines",),
                },
            },
            {"time_window": "2026-01-01..2026-06-30"},
            ("compare_periods",),
        )

        self.assertEqual(plan.sql_text, "")
        self.assertEqual(plan.reason, "custom_baseline_window_unbound")
        self.assertEqual(plan.query_id, "run-custom-baseline:daily_metric_baselines")

    def test_plan_prefers_executable_dimension_scan_when_reuse_is_blocked(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-reuse",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "dimension_scan_reuse"),
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel",),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "orders",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("segment_contribution",),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertEqual(plan.reason, "")
        self.assertEqual(plan.query_id, "run-reuse:daily_metric_baselines")

    def test_plan_prefers_executable_baseline_when_event_probe_is_unbound(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-event",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "event_context_probe"),
                },
            },
            {"time_window": "yesterday"},
            ("event_evidence",),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertEqual(plan.reason, "")
        self.assertEqual(plan.query_id, "run-event:daily_metric_baselines")

    def test_plan_prefers_executable_baseline_query_when_event_probe_is_blocked(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-event-fallback",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "event_context_probe"),
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods", "driver_decomposition", "event_evidence"),
        )

        self.assertIn("SELECT", plan.sql_text)
        self.assertEqual(plan.reason, "")
        self.assertEqual(plan.query_id, "run-event-fallback:daily_metric_baselines")

    def test_plan_with_explicit_dimension_scan_and_empty_dimensions_stays_blocked(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-empty-dimension-scan",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "query_intents": ("dimension_scan",),
                    "row_shapes": [
                        {
                            "required_fields": ("period", "group", "amount"),
                            "dimension_keys": (),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("segment_contribution",),
        )

        self.assertEqual(plan.sql_text, "")
        self.assertEqual(plan.reason, "missing_dimension_keys")
        self.assertEqual(plan.query_id, "run-empty-dimension-scan:dimension_scan")

    def test_plan_with_unsafe_compiler_dimension_does_not_emit_dimension_sql(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-unsafe-dimension-scan",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "query_intents": ("dimension_scan",),
                    "row_shapes": [
                        {
                            "required_fields": ("period", "group", "amount"),
                            "dimension_keys": ("channel;DROP",),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("segment_contribution",),
        )

        self.assertEqual(plan.sql_text, "")
        self.assertEqual(plan.reason, "unsafe_dimension_keys")
        self.assertNotIn("channel;DROP", plan.sql_text)

    def test_fetch_returns_bounded_aggregate_rows_and_query_ref(self):
        runtime = FakeRuntime(
            rows=({"period": "2026-07-08", "group": "target", "amount": 120.0},)
        )
        provider = ClickHouseRevenueRows(
            runtime=runtime,
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {"run_id": "run-1"},
            {"time_window": "yesterday"},
            ("compare_periods",),
        )
        result = provider.fetch(plan)

        self.assertTrue(result.ok)
        self.assertEqual(result.rows[0]["amount"], 120.0)
        self.assertEqual(result.query_id, plan.query_id)
        self.assertEqual(result.result_refs, ("hash-run-1:clickhouse_revenue_rows",))

    def test_fetch_executes_all_compiler_query_specs_and_groups_rows_by_intent(self):
        runtime = FakeRuntime(
            rows_by_query_id={
                "run-multi:daily_metric_baselines": (
                    {"period": "2026-07-07", "group": "baseline", "amount": 90.0, "orders": 9},
                    {"period": "2026-07-08", "group": "target", "amount": 120.0, "orders": 10},
                ),
                "run-multi:dimension_scan": (
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "channel": "ads",
                        "amount": 80.0,
                        "orders": 7,
                    },
                ),
                "run-multi:data_quality_probe": (
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "orders": 10,
                        "paid_users": 8,
                        "min_period": "2026-07-01",
                        "max_period": "2026-07-08",
                    },
                ),
            }
        )
        provider = ClickHouseRevenueRows(
            runtime=runtime,
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-multi",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": (
                        "daily_metric_baselines",
                        "dimension_scan",
                        "data_quality_probe",
                    ),
                    "row_shapes": [
                        {
                            "dimension_keys": ("channel",),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "orders",
                                "paid_users",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods", "segment_contribution", "data_quality_profile"),
        )

        result = provider.fetch(plan)

        self.assertTrue(result.ok)
        self.assertEqual(
            [query_id for _, query_id in runtime.calls],
            [
                "run-multi:daily_metric_baselines",
                "run-multi:dimension_scan",
                "run-multi:data_quality_probe",
            ],
        )
        self.assertEqual(result.rows_by_intent["daily_metric_baselines"][0]["amount"], 90.0)
        self.assertEqual(result.rows_by_intent["dimension_scan"][0]["channel"], "ads")
        self.assertEqual(result.rows_by_intent["data_quality_probe"][0]["orders"], 10)
        self.assertEqual(
            result.result_refs_by_intent["dimension_scan"],
            ("hash-run-multi:dimension_scan",),
        )

    def test_compare_plan_uses_baseline_rows_as_primary_when_quality_probe_also_runs(self):
        runtime = FakeRuntime(
            rows_by_query_id={
                "run-quality:daily_metric_baselines": (
                    {"period": "2026-07-07", "group": "previous_day", "amount": 90.0, "orders": 9},
                    {"period": "2026-07-08", "group": "target", "amount": 120.0, "orders": 10},
                ),
                "run-quality:data_quality_probe": (
                    {
                        "period": "2026-07-08",
                        "group": "target",
                        "orders": 10,
                        "paid_users": 8,
                        "min_period": "2026-07-01",
                        "max_period": "2026-07-08",
                    },
                ),
            }
        )
        provider = ClickHouseRevenueRows(
            runtime=runtime,
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {
                "run_id": "run-quality",
                "compiler_runtime_plan": {
                    "windows": {"target": "yesterday", "history_days": 12},
                    "baselines": ("previous_day",),
                    "query_intents": ("daily_metric_baselines", "data_quality_probe"),
                    "row_shapes": [
                        {
                            "dimension_keys": (),
                            "required_fields": (
                                "period",
                                "group",
                                "amount",
                                "orders",
                                "paid_users",
                            ),
                        }
                    ],
                },
            },
            {"time_window": "yesterday"},
            ("data_quality_profile", "compare_periods", "driver_decomposition"),
        )

        result = provider.fetch(plan)

        self.assertTrue(result.ok)
        self.assertEqual(plan.query_id, "run-quality:daily_metric_baselines")
        self.assertEqual(result.rows[0]["amount"], 90.0)
        self.assertEqual(result.rows_by_intent["data_quality_probe"][0]["orders"], 10)

    def test_fetch_blocks_when_runtime_query_fails(self):
        provider = ClickHouseRevenueRows(
            runtime=FakeRuntime(ok=False, reason="clickhouse_query_failed"),
            table="paid_order_success_clean_20240101_20260704",
        )
        plan = provider.plan(
            {"run_id": "run-1"},
            {"time_window": "yesterday"},
            ("compare_periods",),
        )
        result = provider.fetch(plan)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "clickhouse_query_failed")

    def test_unsafe_table_identifier_is_blocked_before_runtime_call(self):
        runtime = FakeRuntime()
        provider = ClickHouseRevenueRows(
            runtime=runtime,
            table="paid_order_success_clean_20240101_20260704; DROP TABLE raw",
        )
        plan = provider.plan(
            {"run_id": "run-1"},
            {"time_window": "yesterday"},
            ("compare_periods",),
        )
        result = provider.fetch(plan)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "invalid_identifier")
        self.assertEqual(runtime.calls, [])

    def test_typed_executor_passes_parameters_settings_and_preserves_audit_envelope(self):
        client = FakeParameterizedClient()
        runtime = ClickHouseRuntime(client=client)
        executor = ClickHouseQueryExecutor(runtime)
        query_contract = contract()
        dataset_snapshot = snapshot()

        envelope = executor.execute(
            query_contract,
            {dataset_snapshot.snapshot_ref: dataset_snapshot},
        )

        self.assertIsInstance(envelope, QueryResultEnvelope)
        self.assertEqual(client.calls[0][1]["parameters"]["window_id_0"], "target_day")
        self.assertEqual(client.calls[0][1]["settings"]["result_overflow_mode"], "throw")
        self.assertEqual(envelope.provider_stats["read_rows"], 42)
        self.assertEqual(
            envelope.provider_stats["provider_query_id"],
            "provider-query-id",
        )
        self.assertTrue(envelope.rows_ref.startswith("rows:"))
        self.assertEqual(envelope.row_count, 1)
        self.assertTrue(envelope.completeness_report_ref)
        self.assertNotIn("rows", envelope.to_dict())

    def test_query_hash_covers_parameters_as_well_as_sql(self):
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=FakeParameterizedClient())
        )
        dataset_snapshot = snapshot()
        first = contract(filters=({"field": "channel", "op": "eq", "value": "A"},))
        second = contract(filters=({"field": "channel", "op": "eq", "value": "B"},))

        first_result = executor.execute(
            first,
            {dataset_snapshot.snapshot_ref: dataset_snapshot},
        )
        second_result = executor.execute(
            second,
            {dataset_snapshot.snapshot_ref: dataset_snapshot},
        )

        self.assertNotEqual(first_result.query_hash, second_result.query_hash)
        self.assertNotEqual(first_result.rows_ref, second_result.rows_ref)

    def test_internal_type_error_is_not_downgraded_to_compatibility_call(self):
        runtime = ClickHouseRuntime(client=FakeInternalTypeErrorClient())

        with self.assertRaisesRegex(TypeError, "client decoding bug"):
            runtime.aggregate(
                "SELECT count() FROM paid_success",
                query_id="typed-error",
                parameters={"value": 1},
                settings={"readonly": 2},
            )

    def test_revenue_adapter_uses_typed_contracts_without_legacy_fallback(self):
        client = FakeParameterizedClient()
        dataset_snapshot = snapshot()
        provider = ClickHouseRevenueRows(
            runtime=ClickHouseRuntime(client=client),
            snapshots={dataset_snapshot.snapshot_ref: dataset_snapshot},
        )
        plan = provider.plan(
            {
                "run_id": "run-typed",
                "compiler_runtime_plan": {
                    "query_contracts": (contract().to_dict(),),
                },
            },
            {"time_window": "yesterday"},
            ("compare_periods",),
        )

        result = provider.fetch(plan)

        self.assertEqual(plan.contract_mode, "typed")
        self.assertTrue(result.ok)
        self.assertEqual(
            result.query_envelopes[0].query_contract_ref,
            contract().query_contract_id,
        )
        self.assertTrue(
            all(item["contract_mode"] == "typed" for item in result.query_results)
        )
        self.assertNotIn("now(", client.calls[0][0].casefold())
        self.assertNotIn("limit 5000", client.calls[0][0].casefold())


if __name__ == "__main__":
    unittest.main()
