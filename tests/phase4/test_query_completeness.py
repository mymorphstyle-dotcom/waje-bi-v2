from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import unittest
from unittest.mock import patch

from bi_agent.runtime.analysis_contracts import (
    CompletenessReport,
    DimensionBinding,
    JoinExpectation,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    ReconciliationBinding,
    ResolvedWindow,
    ResultShape,
    query_contract_signature,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetReleaseAuthorityRecord,
    DatasetSnapshot,
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    RuntimeEvidenceAuthority,
    canonical_result_rows_hash,
    canonical_rows_hash,
)
from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.dataset_catalog import DatasetCatalog
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult, ClickHouseRuntime
from bi_agent.runtime.clickhouse_query_compiler import CompiledQuery
from bi_agent.runtime.query_executor import (
    AggregateRowsStore,
    ClickHouseQueryExecutor,
    _aggregate_rows,
)
from bi_agent.runtime.query_audit import query_audit_refs
from bi_agent.runtime.query_audit import query_rows_ref
from bi_agent.runtime.query_completeness import (
    ASSERTIONS,
    CURRENT_DATA_ASSERTIONS,
    _event_recurrence_occurs_in_window,
    validate_query_result as _validate_query_result,
    validate_query_set,
)
from bi_agent.runtime.query_repair import plan_query_repair
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase4.test_clickhouse_query_compiler import (
    contract as reviewed_contract,
    snapshot as reviewed_snapshot,
)


class _TransientClickHouseClient:
    def query(self, sql, **kwargs):
        raise ConnectionError("connection reset")


class _TruncatedClickHouseResult:
    column_names = ("window_id",)
    result_rows = ()
    summary = {"result_overflow_mode": "break"}
    query_id = "provider-truncated"


class _TruncatedClickHouseClient:
    def query(self, sql, **kwargs):
        return _TruncatedClickHouseResult()


class _SuccessfulClickHouseResult:
    column_names = ("window_id", "window_role", "observation_key", "paid_amount")
    result_rows = (
        ("target_day", "target", "2026-06-02", 42.0),
        ("rolling_7_day_baseline", "baseline", "2026-05-26", 40.0),
        ("same_weekday_last_week", "baseline", "2026-05-26", 41.0),
    )
    summary = {}
    query_id = "provider-success"


class _SuccessfulClickHouseClient:
    def query(self, sql, **kwargs):
        return _SuccessfulClickHouseResult()


class _PaidReleaseResolver:
    def __init__(self):
        self.records = {}

    def add(self, record: DatasetReleaseAuthorityRecord) -> None:
        self.records[record.release_ref] = record

    def resolve_dataset_release(self, release_ref: str) -> DatasetReleaseAuthorityRecord:
        return self.records[release_ref]


_PAID_RELEASE_RESOLVER = _PaidReleaseResolver()


def raw_paid_snapshot(watermark="2026-07-04", snapshot_ref="snapshot:paid:1"):
    return DatasetSnapshot(
        snapshot_ref,
        "paid_order_success",
        "analytics.paid_success",
        watermark,
        "schema:paid:1",
        (
            "business_date_lagos",
            "paid_amount_ngn",
            "channel",
            "order_id",
            "user_id",
        ),
        "contract:paid@1",
        ("analyst",),
        "2026-07-05T00:00:00Z",
        "active",
    )


def authorize_paid_snapshot(snapshot):
    logical_id = f"paid-logical:{snapshot.snapshot_ref}"
    revision = f"paid-load:{snapshot.watermark}"
    release_ref = dataset_snapshot_release_ref(
        logical_id,
        revision,
        (snapshot.snapshot_ref,),
    )
    released = replace(
        snapshot,
        logical_snapshot_id=logical_id,
        load_revision=revision,
        snapshot_id=logical_id,
        release_ref=release_ref,
        rows_content_hash="a" * 64,
    )
    record = build_dataset_release_authority_record(
        ({**released.to_dict(), "requires_release": True},)
    )
    _PAID_RELEASE_RESOLVER.add(record)
    return replace(released, authority_record_ref=record.authority_record_ref)


def paid_snapshot(watermark="2026-07-04", snapshot_ref="snapshot:paid:1"):
    return authorize_paid_snapshot(raw_paid_snapshot(watermark, snapshot_ref))


def paid_catalog(snapshot):
    authorized = authorize_paid_snapshot(snapshot) if not snapshot.release_ref else snapshot
    return (
        DatasetCatalog((authorized,), release_resolver=_PAID_RELEASE_RESOLVER),
        _PAID_RELEASE_RESOLVER,
        authorized,
    )


def validate_query_result(contract, result, snapshot, **kwargs):
    kwargs.setdefault("release_resolver", _PAID_RELEASE_RESOLVER)
    return _validate_query_result(contract, result, snapshot, **kwargs)


def paid_metric(*, tolerance=0.01):
    return MetricBinding(
        "paid_amount",
        "metric:paid_amount@1",
        "paid_order_success",
        "sum(paid_amount_ngn)",
        "sum",
        ("paid_amount_ngn",),
        ("window_id",),
        reconciliation_tolerance=tolerance,
        reconciliation_strategy="additive_sum",
    )


def count_metric():
    return MetricBinding(
        "paid_orders",
        "metric:paid_orders@1",
        "paid_order_success",
        "uniqExact(order_id)",
        "distinct_count",
        ("order_id",),
        ("window_id",),
        reconciliation_strategy="exact_additive_count",
    )


def ratio_metric():
    return MetricBinding(
        "paid_frequency",
        "metric:paid_frequency@1",
        "paid_order_success",
        "paid_orders / nullIf(paid_users, 0)",
        "ratio",
        ("order_id", "user_id"),
        ("window_id",),
        numerator_metric="paid_orders",
        denominator_metric="paid_users",
        reconciliation_strategy="ratio_from_components",
    )


def channel_dimension():
    return DimensionBinding(
        "channel",
        "dimension:channel@1",
        "paid_order_success",
        "channel",
        ("day", "window_id"),
    )


def window(window_id):
    definitions = {
        "target_day": (
            "target",
            "2026-06-02",
            "2026-06-03",
            1,
            "2026-06-02",
        ),
        "previous_day": (
            "baseline",
            "2026-06-01",
            "2026-06-02",
            1,
            "2026-06-01",
        ),
        "rolling_7_day_baseline": (
            "baseline",
            "2026-05-26",
            "2026-06-02",
            7,
            "2026-06-01",
        ),
        "same_weekday_last_week": (
            "baseline",
            "2026-05-26",
            "2026-05-27",
            1,
            "2026-05-26",
        ),
    }
    role, start, end, complete_days, watermark = definitions[window_id]
    return ResolvedWindow(
        window_id,
        role,
        window_id,
        start,
        end,
        "Africa/Lagos",
        "daily_total",
        complete_days,
        watermark,
    )


def baseline_contract(
    *,
    query_id="query:baseline:1",
    required_windows=("target_day", "previous_day"),
    dimensions=(),
    metric=None,
    query_parameters=None,
    filters=(),
    join_expectation=None,
):
    selected_metric = metric or paid_metric()
    selected_windows = tuple(window(item) for item in required_windows)
    dimension_ids = tuple(item.dimension_id for item in dimensions)
    required_fields = (
        "window_id",
        "window_role",
        "observation_key",
        selected_metric.metric_id,
        *dimension_ids,
    )
    grain = ("window_id", "observation_key", *dimension_ids)
    return QueryContract(
        query_contract_id=query_id,
        analysis_contract_ref="analysis:run:1",
        query_intent=(
            "dimension_contribution_scan" if dimensions else "daily_metric_baselines"
        ),
        dataset_snapshot_refs=("snapshot:paid:1",),
        metric_bindings=(selected_metric,),
        dimension_bindings=tuple(dimensions),
        window_refs=required_windows,
        resolved_windows=selected_windows,
        filters=tuple(filters),
        result_shape=ResultShape(
            required_fields,
            grain,
            grain,
            required_windows,
        ),
        completeness_assertions=ASSERTIONS,
        permission_scope="analyst",
        workload_class="interactive_aggregate",
        contract_signature=f"signature:{query_id}",
        query_parameters=dict(query_parameters or {}),
        query_role_ref=f"query-role:{query_id}",
        join_expectation=join_expectation,
    )


def rolling_contract():
    return baseline_contract(required_windows=("rolling_7_day_baseline",))


def bind_dimension_reference(dimension_contract, total_contract):
    return replace(
        dimension_contract,
        reconciliation_binding=ReconciliationBinding(
            reference_query_role_ref=total_contract.query_role_ref,
            reference_contract_signature=total_contract.contract_signature,
        ),
    )


def multi_metric_contract(*, query_id, metrics, dimensions=()):
    contract = baseline_contract(
        query_id=query_id,
        metric=metrics[0],
        dimensions=dimensions,
    )
    dimension_ids = tuple(item.dimension_id for item in dimensions)
    fields = (
        "window_id",
        "window_role",
        "observation_key",
        *(metric.metric_id for metric in metrics),
        *dimension_ids,
    )
    grain = ("window_id", "observation_key", *dimension_ids)
    return replace(
        contract,
        metric_bindings=tuple(metrics),
        result_shape=ResultShape(fields, grain, grain, contract.window_refs),
    )


def overlap_contract():
    contract = baseline_contract(
        required_windows=(
            "rolling_7_day_baseline",
            "same_weekday_last_week",
        )
    )
    overlapping_windows = tuple(
        replace(item, required_complete_days=1)
        if item.window_id == "rolling_7_day_baseline"
        else item
        for item in contract.resolved_windows
    )
    return replace(contract, resolved_windows=overlapping_windows)


def successful_result(
    contract,
    *,
    rows,
    provider_stats=None,
    query_contract_ref=None,
    source_snapshot_refs=None,
    observed_grain=None,
    execution_attempt_ref=None,
):
    rows = tuple(rows)
    observed_windows = tuple(
        dict.fromkeys(
            str(row["window_id"])
            for row in rows
            if row.get("window_id") not in (None, "")
        )
    )
    schema = {}
    for row in rows:
        for key, value in row.items():
            schema[str(key)] = (
                "null" if value is None else "number" if isinstance(value, (int, float)) else "string"
            )
    attempt_ref = execution_attempt_ref or (
        f"attempt:test:{contract.query_contract_id}"
    )
    selected_query_contract_ref = query_contract_ref or contract.query_contract_id
    try:
        rows_content_hash = canonical_result_rows_hash(
            rows,
            contract.result_shape.unique_key,
        )
    except EvidenceIntegrityError:
        rows_content_hash = ""
    audit_refs = query_audit_refs(
        f"hash:{contract.query_contract_id}",
        contract.contract_signature,
        (
            tuple(source_snapshot_refs)
            if source_snapshot_refs is not None
            else contract.dataset_snapshot_refs
        ),
        query_contract_ref=selected_query_contract_ref,
        execution_attempt_ref=attempt_ref,
        rows_content_hash=rows_content_hash,
    )
    return QueryResultEnvelope(
        query_contract_ref=selected_query_contract_ref,
        query_id=f"provider:{contract.query_contract_id}",
        query_hash=f"hash:{contract.query_contract_id}",
        result_ref=audit_refs.result_ref,
        execution_status="succeeded",
        rows_ref=audit_refs.rows_ref,
        row_count=len(rows),
        completeness_report_ref=audit_refs.completeness_report_ref,
        rows=rows,
        observed_schema=schema,
        observed_windows=observed_windows,
        observed_grain=(
            contract.result_shape.grain
            if observed_grain is None and rows
            else tuple(observed_grain or ())
        ),
        source_snapshot_refs=(
            tuple(source_snapshot_refs)
            if source_snapshot_refs is not None
            else contract.dataset_snapshot_refs
        ),
        provider_stats=dict(provider_stats or {}),
        execution_attempt_ref=attempt_ref,
    )


def failed_result(contract, *, status, reason):
    attempt_ref = f"attempt:test:{contract.query_contract_id}"
    audit_refs = query_audit_refs(
        "",
        contract.contract_signature,
        contract.dataset_snapshot_refs,
        query_contract_ref=contract.query_contract_id,
        execution_attempt_ref=attempt_ref,
    )
    return QueryResultEnvelope(
        query_contract_ref=contract.query_contract_id,
        query_id=f"provider:{contract.query_contract_id}",
        query_hash="",
        result_ref=audit_refs.result_ref,
        execution_status=status,
        rows_ref=audit_refs.rows_ref,
        row_count=0,
        completeness_report_ref=audit_refs.completeness_report_ref,
        rows=(),
        source_snapshot_refs=contract.dataset_snapshot_refs,
        failure_reason=reason,
        execution_attempt_ref=attempt_ref,
    )


def complete_rows(*, metric_id="paid_amount", target=120.0, baseline=100.0):
    return (
        {
            "window_id": "target_day",
            "window_role": "target",
            "observation_key": "2026-06-02",
            metric_id: target,
        },
        {
            "window_id": "previous_day",
            "window_role": "baseline",
            "observation_key": "2026-06-01",
            metric_id: baseline,
        },
    )


def repair_report(contract, *reasons):
    return CompletenessReport(
        report_ref=f"completeness:{contract.query_contract_id}",
        result_ref=f"result:{contract.query_contract_id}",
        query_contract_ref=contract.query_contract_id,
        completeness_status="partial",
        analysis_readiness="blocked",
        assertion_results=(),
        failure_reasons=tuple(reasons),
        coverage_summary={},
    )


class QueryCompletenessTest(unittest.TestCase):
    def test_cross_year_annual_recurrence_occurs_on_both_sides_only(self):
        row = {
            "recurrence_kind": "annual_month_day_range",
            "recurrence_month_start": 12,
            "recurrence_day_start": 20,
            "recurrence_month_end": 1,
            "recurrence_day_end": 5,
        }
        self.assertTrue(
            _event_recurrence_occurs_in_window(
                row,
                datetime.fromisoformat("2026-12-25").date(),
                datetime.fromisoformat("2026-12-26").date(),
            )
        )

        self.assertTrue(
            _event_recurrence_occurs_in_window(
                row,
                datetime.fromisoformat("2027-01-02").date(),
                datetime.fromisoformat("2027-01-03").date(),
            )
        )
        self.assertFalse(
            _event_recurrence_occurs_in_window(
                row,
                datetime.fromisoformat("2027-02-02").date(),
                datetime.fromisoformat("2027-02-03").date(),
            )
        )

    def test_executor_uses_bounded_context_policy_for_event_rows(self):
        contract = reviewed_contract(
            dataset_id="external_event",
            query_intent="event_context_probe",
            metrics=(),
        )
        snapshot = reviewed_snapshot("external_event")
        row = {
            field: ""
            for field in contract.result_shape.required_fields
        }
        row.update(
            {
                "window_id": contract.window_refs[0],
                "window_role": contract.resolved_windows[0].role,
                "observation_key": f"__no_event__:{contract.window_refs[0]}",
                "event_id": f"__no_event__:{contract.window_refs[0]}",
                "event_count": 0,
                "wording_limit": "context",
                "recurrence_day_start": 0,
                "recurrence_day_end": 0,
                "payload": "{}",
            }
        )

        class ContextRuntime:
            def __init__(self):
                self.bounded_calls = 0

            def aggregate(self, *args, **kwargs):
                raise AssertionError("aggregate path must not execute context rows")

            def bounded_context(self, sql, query_id, **kwargs):
                self.bounded_calls += 1
                return ClickHouseQueryResult(
                    ok=True,
                    rows=(row,),
                    query_hash="hash:event-context",
                    query_id=query_id,
                    provider_stats={"result_overflow_mode": "throw"},
                    execution_attempt_ref=kwargs.get("execution_attempt_ref", ""),
                )

        runtime = ContextRuntime()
        compiled = CompiledQuery(
            sql_text="SELECT event_id FROM events WHERE snapshot_id = 's' LIMIT 5001",
            parameters={},
            settings={"readonly": 2, "max_result_rows": 5001},
            query_contract_ref=contract.query_contract_id,
            max_context_rows=5000,
        )
        with patch(
            "bi_agent.runtime.query_executor.compile_clickhouse_query",
            return_value=compiled,
        ):
            envelope = ClickHouseQueryExecutor(runtime).execute(
                contract,
                {snapshot.snapshot_ref: snapshot},
                execution_attempt_ref="attempt:event-context",
            )

        self.assertEqual(envelope.execution_status, "succeeded")
        self.assertEqual(runtime.bounded_calls, 1)

    def test_executor_blocks_context_probe_row_overflow(self):
        contract = reviewed_contract(
            dataset_id="external_event",
            query_intent="event_context_probe",
            metrics=(),
        )
        snapshot = reviewed_snapshot("external_event")

        class OverflowRuntime:
            def aggregate(self, *args, **kwargs):
                raise AssertionError("aggregate path must not execute context rows")

            def bounded_context(self, sql, query_id, **kwargs):
                return ClickHouseQueryResult(
                    ok=True,
                    rows=tuple(
                        {
                            "window_id": "target_day",
                            "window_role": "target",
                            "observation_key": f"event:{index}",
                            "event_id": f"event:{index}",
                        }
                        for index in range(5001)
                    ),
                    query_hash="hash:event-context-overflow",
                    query_id=query_id,
                    provider_stats={"result_overflow_mode": "throw"},
                    execution_attempt_ref=kwargs.get("execution_attempt_ref", ""),
                )

        compiled = CompiledQuery(
            sql_text="SELECT * FROM context_rows ORDER BY event_id LIMIT 5001",
            parameters={},
            settings={"readonly": 2, "max_result_rows": 5001},
            query_contract_ref=contract.query_contract_id,
            max_context_rows=5000,
        )
        with patch(
            "bi_agent.runtime.query_executor.compile_clickhouse_query",
            return_value=compiled,
        ):
            envelope = ClickHouseQueryExecutor(OverflowRuntime()).execute(
                contract,
                {snapshot.snapshot_ref: snapshot},
                execution_attempt_ref="attempt:event-context-overflow",
            )

        self.assertEqual(envelope.execution_status, "failed")
        self.assertEqual(envelope.failure_reason, "context_row_bound_exceeded:5000")

    def test_event_context_zero_rows_are_explicit_complete_window_sentinels(self):
        windows = (window("target_day"), window("rolling_7_day_baseline"))
        context_fields = (
            "window_id",
            "window_role",
            "observation_key",
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
        )
        unsigned = QueryContract(
            query_contract_id="query:event:zero",
            analysis_contract_ref="analysis:event:zero",
            query_intent="event_context_probe",
            dataset_snapshot_refs=("snapshot:paid:1",),
            metric_bindings=(),
            dimension_bindings=(),
            window_refs=tuple(item.window_id for item in windows),
            resolved_windows=windows,
            filters=(),
            result_shape=ResultShape(
                context_fields,
                ("window_id", "event_id"),
                ("window_id", "event_id"),
                tuple(item.window_id for item in windows),
                "complete_context_rows",
            ),
            completeness_assertions=("required_windows_complete",),
            permission_scope="analyst",
            workload_class="interactive_aggregate",
            contract_signature="",
        )
        contract = replace(
            unsigned, contract_signature=query_contract_signature(unsigned)
        )
        rows = tuple(
            {
                "window_id": item.window_id,
                "window_role": item.role,
                "observation_key": f"__no_event__:{item.window_id}",
                "event_count": 0,
                "source_family": "",
                "event_id": f"__no_event__:{item.window_id}",
                "event_type": "",
                "event_start_date": "",
                "event_end_date": "",
                "affected_scope": "",
                "authority": "",
                "evidence_level": "",
                "wording_limit": "context",
                "recurrence_kind": "",
                "recurrence_month_start": 0,
                "recurrence_day_start": 0,
                "recurrence_month_end": 0,
                "recurrence_day_end": 0,
                "payload": "{}",
            }
            for item in windows
        )
        result = successful_result(contract, rows=rows)
        report = validate_query_result(contract, result, paid_snapshot())
        self.assertEqual(report.completeness_status, "complete")

    def test_event_context_recurrence_is_independently_checked_against_window(self):
        contract = reviewed_contract(
            dataset_id="external_event",
            query_intent="event_context_probe",
            metrics=(),
        )
        row = {
            field: ""
            for field in contract.result_shape.required_fields
        }
        row.update(
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "event:payday:wrong-window",
                "event_count": 1,
                "source_family": "external_event",
                "event_id": "event:payday:wrong-window",
                "event_type": "payday_context",
                "event_start_date": "2024-01-01",
                "event_end_date": "2026-06-08",
                "affected_scope": "Nigeria",
                "authority": "reviewed_workbook_pending_owner_review",
                "evidence_level": "context",
                "wording_limit": "context",
                "recurrence_kind": "monthly_day_range",
                "recurrence_month_start": 0,
                "recurrence_day_start": 25,
                "recurrence_month_end": 0,
                "recurrence_day_end": 30,
                "payload": "{}",
            }
        )
        result = successful_result(contract, rows=(row,))
        report = validate_query_result(
            contract,
            result,
            reviewed_snapshot("external_event"),
        )
        self.assertEqual(report.completeness_status, "invalid")
        self.assertTrue(
            any("context_event_recurrence_outside_window" in reason for reason in report.failure_reasons),
            report.failure_reasons,
        )

    def test_executor_rejects_wrong_type_query_execution_record(self):
        contract = reviewed_contract()
        snapshot = reviewed_snapshot()
        authority = RuntimeEvidenceAuthority()

        class WrongWriter:
            def record_query_execution(self, contract, result, snapshots):
                return object()

            def record_completeness(self, report):
                return object()

            def record_capability_binding(self, plan, binding_payload):
                return object()

        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=_SuccessfulClickHouseClient()),
            evidence_resolver=authority,
            evidence_writer=WrongWriter(),
            rows_loader=authority.rows_loader,
        )

        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "query_execution_writer_record_invalid",
        ):
            executor.execute(
                contract,
                {snapshot.snapshot_ref: snapshot},
                execution_attempt_ref="attempt:wrong-writer",
            )

    def test_completeness_wrong_writer_returns_typed_blocked_report(self):
        class WrongWriter:
            def record_query_execution(self, contract, result, snapshots):
                return object()

            def record_completeness(self, report):
                return object()

            def record_capability_binding(self, plan, binding_payload):
                return object()

        contract = baseline_contract()
        report = validate_query_result(
            contract,
            successful_result(contract, rows=complete_rows()),
            paid_snapshot(),
            evidence_writer=WrongWriter(),
        )

        self.assertEqual(report.completeness_status, "invalid")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn(
            "runtime_evidence_writer_record_invalid",
            report.failure_reasons,
        )
        self.assertFalse(
            next(
                assertion
                for assertion in report.assertion_results
                if assertion["assertion"] == "runtime_evidence_authority_write"
            )["passed"]
        )

    def test_executor_does_not_hide_authority_collision(self):
        contract = reviewed_contract()
        snapshot = reviewed_snapshot()
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=_SuccessfulClickHouseClient())
        )

        with patch(
            "bi_agent.runtime.query_executor._record_query_execution",
            side_effect=EvidenceIntegrityError(
                "authority_ref_collision:query:result"
            ),
        ):
            with self.assertRaisesRegex(
                EvidenceIntegrityError,
                "authority_ref_collision",
            ):
                executor.execute(
                    contract,
                    {snapshot.snapshot_ref: snapshot},
                    execution_attempt_ref="attempt:collision",
                )

    def test_sql_success_with_history_but_missing_target_is_partial(self):
        contract = baseline_contract()
        report = validate_query_result(
            contract,
            successful_result(
                contract,
                rows=(
                    {
                        "window_id": "previous_day",
                        "window_role": "baseline",
                        "observation_key": "2026-06-01",
                        "paid_amount": 100.0,
                    },
                ),
            ),
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "partial")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn("missing_required_window:target_day", report.failure_reasons)

    def test_successful_zero_row_result_is_empty_and_blocked(self):
        contract = baseline_contract()
        report = validate_query_result(
            contract,
            successful_result(contract, rows=()),
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "empty")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn("empty_result", report.failure_reasons)

    def test_rolling_window_requires_seven_complete_days(self):
        contract = rolling_contract()
        rows = tuple(
            {
                "window_id": "rolling_7_day_baseline",
                "window_role": "baseline",
                "observation_key": f"2026-05-{day:02d}",
                "paid_amount": 100.0,
            }
            for day in range(26, 32)
        )
        report = validate_query_result(
            contract,
            successful_result(contract, rows=rows),
            paid_snapshot(),
        )

        self.assertIn(
            "incomplete_window:rolling_7_day_baseline:6/7",
            report.failure_reasons,
        )

    def test_same_observation_can_satisfy_two_window_memberships(self):
        contract = overlap_contract()
        rows = (
            {
                "window_id": "rolling_7_day_baseline",
                "window_role": "baseline",
                "observation_key": "2026-05-26",
                "paid_amount": 100.0,
            },
            {
                "window_id": "same_weekday_last_week",
                "window_role": "baseline",
                "observation_key": "2026-05-26",
                "paid_amount": 100.0,
            },
        )
        report = validate_query_result(
            contract,
            successful_result(contract, rows=rows),
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "complete")

    def test_provider_break_is_truncated_even_when_result_is_empty(self):
        contract = baseline_contract()
        report = validate_query_result(
            contract,
            successful_result(
                contract,
                rows=(),
                provider_stats={"result_overflow_mode": "break"},
            ),
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "truncated")
        self.assertEqual(report.analysis_readiness, "blocked")

    def test_report_links_result_and_reuses_envelope_report_ref(self):
        contract = baseline_contract()
        result = successful_result(contract, rows=complete_rows())

        report = validate_query_result(contract, result, paid_snapshot())

        self.assertEqual(report.result_ref, result.result_ref)
        self.assertEqual(report.report_ref, result.completeness_report_ref)

    def test_stale_snapshot_is_blocked_with_typed_reason(self):
        contract = baseline_contract()
        report = validate_query_result(
            contract,
            successful_result(contract, rows=complete_rows()),
            paid_snapshot("2026-06-01"),
        )

        self.assertEqual(report.completeness_status, "stale")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn("snapshot_stale:2026-06-01:2026-06-02", report.failure_reasons)

    def test_every_source_snapshot_must_be_ready_in_any_input_order(self):
        contract = baseline_contract()
        second_ref = "snapshot:paid:2"
        contract = replace(
            contract,
            dataset_snapshot_refs=(paid_snapshot().snapshot_ref, second_ref),
        )
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        result = successful_result(contract, rows=complete_rows())
        good = paid_snapshot()
        stale = paid_snapshot("2026-06-01", second_ref)

        reports = (
            validate_query_result(contract, result, snapshots)
            for snapshots in ((good, stale), (stale, good))
        )

        for report in reports:
            self.assertEqual(report.completeness_status, "stale")
            self.assertEqual(report.analysis_readiness, "blocked")
            self.assertEqual(
                report.coverage_summary["snapshot_refs"],
                contract.dataset_snapshot_refs,
            )
            self.assertTrue(
                any(reason.startswith(f"snapshot_stale:{second_ref}:") for reason in report.failure_reasons)
            )

    def test_blocked_and_failed_execution_are_invalid(self):
        contract = baseline_contract()
        for status in ("blocked", "failed"):
            with self.subTest(status=status):
                report = validate_query_result(
                    contract,
                    failed_result(contract, status=status, reason=f"{status}_cause"),
                    paid_snapshot(),
                )
                self.assertEqual(report.completeness_status, "invalid")
                self.assertEqual(report.analysis_readiness, "blocked")
                self.assertIn(f"execution_status:{status}", report.failure_reasons)
                self.assertIn(f"{status}_cause", report.failure_reasons)

    def test_unsigned_required_release_snapshot_fails_closed(self):
        contract = baseline_contract()
        report = _validate_query_result(
            contract,
            successful_result(contract, rows=complete_rows()),
            raw_paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "invalid")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn(
            "snapshot_release_unverified:snapshot:paid:1",
            report.failure_reasons,
        )

    def test_query_and_snapshot_reference_mismatches_are_invalid(self):
        contract = baseline_contract()
        cases = (
            successful_result(
                contract,
                rows=complete_rows(),
                query_contract_ref="query:other:1",
            ),
            successful_result(
                contract,
                rows=complete_rows(),
                source_snapshot_refs=("snapshot:other:1",),
            ),
        )
        expected = (
            "query_contract_ref_mismatch:query:other:1:query:baseline:1",
            "source_snapshot_refs_mismatch:snapshot:other:1:snapshot:paid:1",
        )
        for result, reason in zip(cases, expected):
            with self.subTest(reason=reason):
                report = validate_query_result(contract, result, paid_snapshot())
                self.assertEqual(report.completeness_status, "invalid")
                self.assertIn(reason, report.failure_reasons)

    def test_successful_result_requires_query_audit_identity(self):
        contract = baseline_contract()
        base = successful_result(contract, rows=complete_rows())
        cases = (
            (replace(base, query_id=""), "missing_query_id"),
            (replace(base, query_hash=""), "missing_query_hash"),
            (
                replace(base, execution_attempt_ref=""),
                "missing_execution_attempt_ref",
            ),
        )
        for result, reason in cases:
            with self.subTest(reason=reason):
                report = validate_query_result(contract, result, paid_snapshot())
                self.assertEqual(report.completeness_status, "invalid")
                self.assertIn(reason, report.failure_reasons)

    def test_nonempty_but_tampered_audit_refs_are_invalid(self):
        contract = baseline_contract()
        base = successful_result(contract, rows=complete_rows())
        cases = (
            (replace(base, result_ref="result:tampered"), "result_ref_mismatch"),
            (replace(base, rows_ref="rows:tampered"), "rows_ref_mismatch"),
            (
                replace(base, completeness_report_ref="completeness:tampered"),
                "completeness_report_ref_mismatch",
            ),
            (
                replace(base, execution_attempt_ref="attempt:tampered"),
                "result_ref_mismatch",
            ),
        )
        for result, reason in cases:
            with self.subTest(reason=reason):
                report = validate_query_result(contract, result, paid_snapshot())
                self.assertEqual(report.completeness_status, "invalid")
                self.assertIn(reason, report.failure_reasons)

    def test_snapshot_permission_scope_mismatch_is_invalid(self):
        contract = baseline_contract()
        snapshot = replace(paid_snapshot(), permission_scopes=("business_reader",))

        report = validate_query_result(
            contract,
            successful_result(contract, rows=complete_rows()),
            snapshot,
        )

        self.assertEqual(report.completeness_status, "invalid")
        self.assertIn(
            "snapshot_permission_scope_mismatch:analyst:business_reader",
            report.failure_reasons,
        )

    def test_physical_revision_without_verified_release_is_invalid(self):
        contract = baseline_contract()
        snapshot = replace(
            paid_snapshot(),
            logical_snapshot_id="dashboard-logical",
            load_revision="dashboard-load:sha256:unverified",
            release_ref="dataset-release:sha256:missing",
            authority_record_ref="",
            rows_content_hash="a" * 64,
        )

        report = validate_query_result(
            contract,
            successful_result(contract, rows=complete_rows()),
            snapshot,
        )

        self.assertEqual(report.completeness_status, "invalid")
        self.assertIn(
            "snapshot_release_unverified:snapshot:paid:1",
            report.failure_reasons,
        )

    def test_unreviewed_output_fields_are_invalid_and_blocked(self):
        contract = baseline_contract()
        rows = tuple(
            {**row, "user_id": "raw-user", "invented_score": 99}
            for row in complete_rows()
        )
        report = validate_query_result(
            contract,
            successful_result(contract, rows=rows),
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "invalid")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn("unreviewed_output_field:user_id", report.failure_reasons)
        self.assertIn(
            "unreviewed_output_field:invented_score",
            report.failure_reasons,
        )

    def test_window_membership_validates_role_and_half_open_date_bounds(self):
        contract = baseline_contract()
        rows = (
            {
                "window_id": "target_day",
                "window_role": "baseline",
                "observation_key": "2026-06-03",
                "paid_amount": 120.0,
            },
            complete_rows()[1],
        )

        report = validate_query_result(
            contract,
            successful_result(contract, rows=rows),
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "partial")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn(
            "window_role_mismatch:target_day:baseline:target",
            report.failure_reasons,
        )
        self.assertIn(
            "observation_outside_window:target_day:2026-06-03:"
            "2026-06-02:2026-06-03",
            report.failure_reasons,
        )
        self.assertIn("incomplete_window:target_day:0/1", report.failure_reasons)

    def test_validator_compares_actual_observed_grain_without_backfill(self):
        contract = baseline_contract()
        report = validate_query_result(
            contract,
            successful_result(
                contract,
                rows=complete_rows(),
                observed_grain=("window_id",),
            ),
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "partial")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn(
            "observed_grain_mismatch:window_id,observation_key:window_id",
            report.failure_reasons,
        )

    def test_required_metric_null_is_partial_and_blocked(self):
        contract = baseline_contract()
        rows = tuple(
            {**row, "paid_amount": None}
            if row["window_id"] == "target_day"
            else row
            for row in complete_rows()
        )

        report = validate_query_result(
            contract,
            successful_result(contract, rows=rows),
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "partial")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn("null_required_metric:paid_amount", report.failure_reasons)

    def test_assertion_order_is_stable_and_auditable(self):
        contract = baseline_contract()
        report = validate_query_result(
            contract,
            successful_result(contract, rows=complete_rows()),
            paid_snapshot(),
        )

        self.assertEqual(
            tuple(item["assertion"] for item in report.assertion_results),
            ASSERTIONS,
        )

    def test_dimension_totals_use_metric_contract_tolerance(self):
        total_contract = baseline_contract(query_id="query:total:1")
        dimension_contract = baseline_contract(
            query_id="query:dimension:1",
            dimensions=(channel_dimension(),),
            metric=paid_metric(tolerance=0.01),
        )
        dimension_contract = bind_dimension_reference(
            dimension_contract, total_contract
        )
        total_result = successful_result(
            total_contract,
            rows=complete_rows(target=100.0, baseline=80.0),
        )
        dimension_result = successful_result(
            dimension_contract,
            rows=(
                {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 60.0},
                {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "B", "paid_amount": 39.995},
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_amount": 50.0},
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "B", "paid_amount": 30.0},
            ),
        )
        reports = (
            validate_query_result(total_contract, total_result, paid_snapshot()),
            validate_query_result(dimension_contract, dimension_result, paid_snapshot()),
        )
        self.assertEqual(reports[1].completeness_status, "partial")
        self.assertEqual(reports[1].analysis_readiness, "blocked")
        self.assertTrue(
            any(
                reason.startswith("dimension_total_reconciliation_pending:")
                for reason in reports[1].failure_reasons
            )
        )
        self.assertFalse(
            any(
                reason.startswith("dimension_total_mismatch:")
                for reason in reports[1].failure_reasons
            )
        )

        reconciled = validate_query_set(
            (total_contract, dimension_contract),
            (total_result, dimension_result),
            reports,
        )

        dimension_report = reconciled[1]
        assertion = next(
            item
            for item in dimension_report.assertion_results
            if item["assertion"] == "dimension_total_reconciliation"
        )
        self.assertTrue(assertion["passed"])
        self.assertEqual(assertion["details"]["tolerance"], 0.01)
        self.assertEqual(dimension_report.completeness_status, "complete")

    def test_overall_channel_reconciliation_executes_match_mismatch_and_partial(self):
        overall = baseline_contract(query_id="query:overall:closure")
        channel = bind_dimension_reference(
            baseline_contract(
                query_id="query:channel:closure",
                dimensions=(channel_dimension(),),
            ),
            overall,
        )
        channel = replace(
            channel,
            query_intent="channel_context_probe",
            completeness_assertions=CURRENT_DATA_ASSERTIONS,
        )
        overall_rows = complete_rows(target=100.0, baseline=80.0)
        scenarios = (
            (
                "match",
                (
                    {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 60.0},
                    {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "B", "paid_amount": 40.0},
                    {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_amount": 50.0},
                    {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "B", "paid_amount": 30.0},
                ),
                "complete",
                "ready",
            ),
            (
                "mismatch",
                (
                    {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 99.0},
                    {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_amount": 80.0},
                ),
                "partial",
                "blocked",
            ),
        )
        for label, channel_rows, status, readiness in scenarios:
            with self.subTest(label=label):
                reports = validate_query_set(
                    (overall, channel),
                    (
                        successful_result(overall, rows=overall_rows),
                        successful_result(channel, rows=channel_rows),
                    ),
                    (
                        validate_query_result(overall, successful_result(overall, rows=overall_rows), paid_snapshot()),
                        validate_query_result(channel, successful_result(channel, rows=channel_rows), paid_snapshot()),
                    ),
                )
                assertion = next(
                    item
                    for item in reports[1].assertion_results
                    if item["assertion"] == "overall_channel_reconciliation"
                )
                self.assertEqual(reports[1].completeness_status, status)
                self.assertEqual(reports[1].analysis_readiness, readiness)
                self.assertEqual(assertion["passed"], label == "match")

        missing_reference = validate_query_set(
            (channel,),
            (successful_result(channel, rows=scenarios[0][1]),),
            (
                validate_query_result(
                    channel,
                    successful_result(channel, rows=scenarios[0][1]),
                    paid_snapshot(),
                ),
            ),
        )[0]
        self.assertEqual(missing_reference.completeness_status, "partial")
        self.assertEqual(missing_reference.analysis_readiness, "blocked")
        self.assertTrue(
            any(
                reason.startswith("dimension_total_reference_missing:")
                for reason in missing_reference.failure_reasons
            )
        )

        incomplete_overall_rows = (overall_rows[1],)
        reports = validate_query_set(
            (overall, channel),
            (
                successful_result(overall, rows=incomplete_overall_rows),
                successful_result(channel, rows=scenarios[0][1]),
            ),
            (
                validate_query_result(overall, successful_result(overall, rows=incomplete_overall_rows), paid_snapshot()),
                validate_query_result(channel, successful_result(channel, rows=scenarios[0][1]), paid_snapshot()),
            ),
        )
        self.assertEqual(reports[1].completeness_status, "partial")
        self.assertEqual(reports[1].analysis_readiness, "blocked")
        self.assertTrue(
            any(
                reason.startswith("dimension_total_reference_incomplete:")
                for reason in reports[1].failure_reasons
            )
        )

    def test_count_reconciliation_is_exact_by_default(self):
        total_contract = baseline_contract(
            query_id="query:count-total:1",
            metric=count_metric(),
        )
        dimension_contract = baseline_contract(
            query_id="query:count-dimension:1",
            dimensions=(channel_dimension(),),
            metric=count_metric(),
        )
        dimension_contract = bind_dimension_reference(
            dimension_contract, total_contract
        )
        total_result = successful_result(
            total_contract,
            rows=complete_rows(metric_id="paid_orders", target=10, baseline=8),
        )
        dimension_result = successful_result(
            dimension_contract,
            rows=(
                {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_orders": 11},
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_orders": 8},
            ),
        )
        reports = (
            validate_query_result(total_contract, total_result, paid_snapshot()),
            validate_query_result(dimension_contract, dimension_result, paid_snapshot()),
        )
        self.assertEqual(reports[1].completeness_status, "partial")
        self.assertEqual(reports[1].analysis_readiness, "blocked")
        self.assertTrue(
            any(
                reason.startswith("dimension_total_reconciliation_pending:")
                for reason in reports[1].failure_reasons
            )
        )

        reconciled = validate_query_set(
            (total_contract, dimension_contract),
            (total_result, dimension_result),
            reports,
        )

        self.assertEqual(reconciled[1].completeness_status, "partial")
        self.assertIn(
            "dimension_total_mismatch:paid_orders:target_day:11:10:0.0",
            reconciled[1].failure_reasons,
        )
        mismatch_provenance = reconciled[1].coverage_summary[
            "reconciliation_validation"
        ]
        self.assertEqual(mismatch_provenance["status"], "failed")
        self.assertEqual(
            mismatch_provenance["validation_query_contract_ref"],
            total_contract.query_contract_id,
        )
        self.assertEqual(
            mismatch_provenance["validation_result_ref"],
            total_result.result_ref,
        )
        self.assertEqual(
            mismatch_provenance["validation_report_ref"],
            reports[0].report_ref,
        )

    def test_exact_additive_count_rejects_fractional_total_and_dimension_values(self):
        total_contract = baseline_contract(
            query_id="query:fractional-count-total:1",
            metric=count_metric(),
        )
        dimension_contract = bind_dimension_reference(
            baseline_contract(
                query_id="query:fractional-count-dimension:1",
                dimensions=(channel_dimension(),),
                metric=count_metric(),
            ),
            total_contract,
        )
        total_result = successful_result(
            total_contract,
            rows=complete_rows(
                metric_id="paid_orders",
                target=10.5,
                baseline=8.5,
            ),
        )
        dimension_result = successful_result(
            dimension_contract,
            rows=(
                {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_orders": 10.5},
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_orders": 8.5},
            ),
        )

        reports = (
            validate_query_result(total_contract, total_result, paid_snapshot()),
            validate_query_result(
                dimension_contract,
                dimension_result,
                paid_snapshot(),
            ),
        )

        for report in reports:
            self.assertEqual(report.completeness_status, "invalid")
            self.assertEqual(report.analysis_readiness, "blocked")
            self.assertIn(
                "invalid_type:paid_orders:exact_additive_count",
                report.failure_reasons,
            )

    def test_exact_additive_count_accepts_integral_decimal_and_canonicalizes_to_int(self):
        contract = baseline_contract(
            query_id="query:decimal-count:1",
            metric=count_metric(),
        )
        decimal_rows = complete_rows(
            metric_id="paid_orders",
            target=Decimal("10.000"),
            baseline=Decimal("8"),
        )

        normalized, _, failure_reason = _aggregate_rows(decimal_rows, contract)
        report = validate_query_result(
            contract,
            successful_result(contract, rows=decimal_rows),
            paid_snapshot(),
        )

        self.assertEqual(failure_reason, "")
        self.assertIs(type(normalized[0]["paid_orders"]), int)
        self.assertEqual(normalized[0]["paid_orders"], 10)
        self.assertEqual(report.completeness_status, "complete")

    def test_exact_additive_count_rejects_fractional_and_nonfinite_decimal(self):
        contract = baseline_contract(
            query_id="query:invalid-decimal-count:1",
            metric=count_metric(),
        )

        for value in (Decimal("10.5"), Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=str(value)):
                report = validate_query_result(
                    contract,
                    successful_result(
                        contract,
                        rows=complete_rows(
                            metric_id="paid_orders",
                            target=value,
                            baseline=Decimal("8"),
                        ),
                    ),
                    paid_snapshot(),
                )
                self.assertEqual(report.completeness_status, "invalid")
                self.assertTrue(
                    any(
                        reason.startswith("invalid_type:paid_orders")
                        for reason in report.failure_reasons
                    )
                )

    def test_rows_ref_includes_canonical_content_identity(self):
        store = AggregateRowsStore()
        first = store.persist(
            "query-hash",
            "contract-signature",
            ("snapshot:1",),
            ({"window_id": "target_day", "active_users": 10},),
        )
        repeated = store.persist(
            "query-hash",
            "contract-signature",
            ("snapshot:1",),
            ({"window_id": "target_day", "active_users": 10},),
        )
        changed = store.persist(
            "query-hash",
            "contract-signature",
            ("snapshot:1",),
            ({"window_id": "target_day", "active_users": 11},),
        )

        self.assertEqual(repeated, first)
        self.assertNotEqual(changed, first)

    def test_succeeded_legacy_rows_ref_is_rejected_while_failed_ref_stays_compatible(self):
        contract = baseline_contract(query_id="query:legacy-rows-ref:1")
        succeeded = successful_result(contract, rows=complete_rows())
        legacy = replace(
            succeeded,
            rows_ref=query_rows_ref(
                succeeded.query_hash,
                contract.contract_signature,
                contract.dataset_snapshot_refs,
            ),
        )
        failed = failed_result(
            contract,
            status="failed",
            reason="transient_clickhouse:connection_reset",
        )

        legacy_report = validate_query_result(contract, legacy, paid_snapshot())
        failed_report = validate_query_result(contract, failed, paid_snapshot())

        self.assertIn("rows_ref_mismatch", legacy_report.failure_reasons)
        self.assertNotIn("rows_ref_mismatch", failed_report.failure_reasons)

    def test_ratio_reconciliation_uses_components_in_multi_metric_result(self):
        metrics = (count_metric(), replace(count_metric(), metric_id="paid_users"), ratio_metric())
        total_contract = multi_metric_contract(
            query_id="query:ratio-total:1",
            metrics=metrics,
        )
        dimension_contract = bind_dimension_reference(
            multi_metric_contract(
                query_id="query:ratio-dimension:1",
                metrics=metrics,
                dimensions=(channel_dimension(),),
            ),
            total_contract,
        )
        total_rows = (
            {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "paid_orders": 5, "paid_users": 20, "paid_frequency": 0.5},
            {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "paid_orders": 4, "paid_users": 20, "paid_frequency": 0.2},
        )
        dimension_rows = (
            {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_orders": 2, "paid_users": 10, "paid_frequency": 0.2},
            {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "B", "paid_orders": 3, "paid_users": 10, "paid_frequency": 0.3},
            {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_orders": 2, "paid_users": 10, "paid_frequency": 0.2},
            {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "B", "paid_orders": 2, "paid_users": 10, "paid_frequency": 0.2},
        )
        total_result = successful_result(total_contract, rows=total_rows)
        dimension_result = successful_result(
            dimension_contract,
            rows=dimension_rows,
        )
        reports = (
            validate_query_result(total_contract, total_result, paid_snapshot()),
            validate_query_result(
                dimension_contract,
                dimension_result,
                paid_snapshot(),
            ),
        )

        reconciled = validate_query_set(
            (total_contract, dimension_contract),
            (total_result, dimension_result),
            reports,
        )

        self.assertEqual(reconciled[1].completeness_status, "partial")
        self.assertIn(
            "ratio_component_mismatch:paid_frequency:target_day:0.25:0.5",
            reconciled[1].failure_reasons,
        )

    def test_standalone_validator_rejects_nan_reconciliation_tolerance(self):
        contract = baseline_contract()
        invalid_metric = replace(
            contract.metric_bindings[0],
            reconciliation_tolerance=float("nan"),
        )
        invalid_contract = replace(
            contract,
            metric_bindings=(invalid_metric,),
        )
        result = successful_result(invalid_contract, rows=complete_rows())

        report = validate_query_result(
            invalid_contract,
            result,
            paid_snapshot(),
        )

        self.assertEqual(report.completeness_status, "invalid")
        self.assertIn(
            "invalid_reconciliation_tolerance:paid_amount",
            report.failure_reasons,
        )

    def test_ratio_components_respect_their_reviewed_tolerances(self):
        average = MetricBinding(
            "avg_order_amount",
            "metric:avg_order_amount@1",
            "paid_order_success",
            "paid_amount / nullIf(paid_orders, 0)",
            "ratio",
            ("paid_amount_ngn", "order_id"),
            ("window_id",),
            numerator_metric="paid_amount",
            denominator_metric="paid_orders",
            reconciliation_tolerance=0.001,
            reconciliation_strategy="ratio_from_components",
        )
        metrics = (paid_metric(tolerance=0.01), count_metric(), average)
        total_contract = multi_metric_contract(
            query_id="query:tolerant-ratio-total:1",
            metrics=metrics,
        )
        dimension_contract = bind_dimension_reference(
            multi_metric_contract(
                query_id="query:tolerant-ratio-dimension:1",
                metrics=metrics,
                dimensions=(channel_dimension(),),
            ),
            total_contract,
        )
        total_rows = (
            {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "paid_amount": 100.0, "paid_orders": 10, "avg_order_amount": 10.0},
            {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "paid_amount": 80.0, "paid_orders": 8, "avg_order_amount": 10.0},
        )
        dimension_rows = (
            {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 99.995, "paid_orders": 10, "avg_order_amount": 9.9995},
            {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_amount": 80.0, "paid_orders": 8, "avg_order_amount": 10.0},
        )
        total_result = successful_result(total_contract, rows=total_rows)
        dimension_result = successful_result(
            dimension_contract,
            rows=dimension_rows,
        )
        reports = (
            validate_query_result(total_contract, total_result, paid_snapshot()),
            validate_query_result(
                dimension_contract,
                dimension_result,
                paid_snapshot(),
            ),
        )

        reconciled = validate_query_set(
            (total_contract, dimension_contract),
            (total_result, dimension_result),
            reports,
        )

        self.assertEqual(reconciled[1].completeness_status, "complete")
        self.assertEqual(reconciled[1].analysis_readiness, "ready")

    def test_query_set_rejects_incomplete_total_as_reconciliation_reference(self):
        total_contract = baseline_contract(query_id="query:incomplete-total:1")
        dimension_contract = baseline_contract(
            query_id="query:dimension-with-incomplete-total:1",
            dimensions=(channel_dimension(),),
        )
        dimension_contract = bind_dimension_reference(
            dimension_contract, total_contract
        )
        total_result = successful_result(
            total_contract,
            rows=complete_rows(target=100.0, baseline=80.0),
        )
        dimension_result = successful_result(
            dimension_contract,
            rows=(
                {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 100.0},
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_amount": 80.0},
            ),
        )
        total_report = replace(
            validate_query_result(total_contract, total_result, paid_snapshot()),
            completeness_status="partial",
            analysis_readiness="blocked",
            failure_reasons=("missing_field:audited_total",),
        )
        dimension_report = validate_query_result(
            dimension_contract,
            dimension_result,
            paid_snapshot(),
        )

        reconciled = validate_query_set(
            (total_contract, dimension_contract),
            (total_result, dimension_result),
            (total_report, dimension_report),
        )

        self.assertEqual(reconciled[1].completeness_status, "partial")
        self.assertEqual(reconciled[1].analysis_readiness, "blocked")
        self.assertIn(
            "dimension_total_reference_incomplete:"
            "query:incomplete-total:1:partial:blocked",
            reconciled[1].failure_reasons,
        )

    def test_explicit_dimension_reference_rejects_filter_scope_mismatch(self):
        total_contract = baseline_contract(
            query_id="query:filtered-total:1",
            filters=({"field": "channel", "op": "eq", "value": "A"},),
        )
        dimension_contract = bind_dimension_reference(
            baseline_contract(
                query_id="query:unfiltered-dimension:1",
                dimensions=(channel_dimension(),),
            ),
            total_contract,
        )
        total_result = successful_result(
            total_contract,
            rows=complete_rows(target=100.0, baseline=80.0),
        )
        dimension_result = successful_result(
            dimension_contract,
            rows=(
                {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 100.0},
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_amount": 80.0},
            ),
        )
        reports = (
            validate_query_result(total_contract, total_result, paid_snapshot()),
            validate_query_result(
                dimension_contract,
                dimension_result,
                paid_snapshot(),
            ),
        )

        reconciled = validate_query_set(
            (total_contract, dimension_contract),
            (total_result, dimension_result),
            reports,
        )

        self.assertIn(
            "dimension_total_scope_mismatch:filters",
            reconciled[1].failure_reasons,
        )
        self.assertEqual(reconciled[1].analysis_readiness, "blocked")

    def test_segment_only_compiler_query_set_can_be_ready(self):
        snapshot = replace(
            reviewed_snapshot(),
            loaded_at="2026-06-03T00:00:00Z",
        )
        catalog, release_resolver, snapshot = paid_catalog(snapshot)
        outcome = compile_analysis_contract(
            run_id="run-segment-ready",
            proposal={
                "target_metrics": ["paid_amount"],
                "requested_dimensions": ["channel"],
                "baselines": ["previous_day"],
                "claim_intents": ["segment_contribution_or_mix_shift"],
            },
            accepted_capabilities=("segment_contribution",),
            catalog=catalog,
            registry=RuntimeContractRegistry.from_path(
                "contracts/runtime/clickhouse-analysis-bindings.yaml"
            ),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
            release_resolver=release_resolver,
        )
        dimension_contract = next(
            query for query in outcome.query_contracts if query.dimension_bindings
        )
        companion_contract = next(
            query
            for query in outcome.query_contracts
            if query.query_role_ref
            == dimension_contract.reconciliation_binding.reference_query_role_ref
        )
        companion_result = successful_result(
            companion_contract,
            rows=complete_rows(target=100.0, baseline=80.0),
        )
        dimension_result = successful_result(
            dimension_contract,
            rows=(
                {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 100.0},
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_amount": 80.0},
            ),
        )
        reports = (
            validate_query_result(
                companion_contract,
                companion_result,
                snapshot,
            ),
            validate_query_result(
                dimension_contract,
                dimension_result,
                snapshot,
            ),
        )
        standalone = reports[1]
        self.assertEqual(standalone.completeness_status, "partial")
        self.assertEqual(standalone.analysis_readiness, "blocked")
        self.assertEqual(
            tuple(
                item["assertion"]
                for item in standalone.assertion_results[: len(ASSERTIONS)]
            ),
            ASSERTIONS,
        )
        self.assertEqual(
            standalone.assertion_results[-1]["assertion"],
            "dimension_total_reconciliation",
        )
        self.assertIn(
            "dimension_total_reconciliation_pending:"
            f"{dimension_contract.reconciliation_binding.reference_query_role_ref}",
            standalone.failure_reasons,
        )

        reconciled = validate_query_set(
            (companion_contract, dimension_contract),
            (companion_result, dimension_result),
            reports,
        )

        self.assertEqual(reconciled[1].completeness_status, "complete")
        self.assertEqual(reconciled[1].analysis_readiness, "ready")
        self.assertFalse(
            any(
                reason.startswith("dimension_total_reconciliation_pending:")
                for reason in reconciled[1].failure_reasons
            )
        )
        reconciliation_assertions = tuple(
            item
            for item in reconciled[1].assertion_results
            if item["assertion"] == "dimension_total_reconciliation"
        )
        self.assertEqual(len(reconciliation_assertions), 1)
        self.assertTrue(reconciliation_assertions[0]["passed"])
        provenance = reconciled[1].coverage_summary[
            "reconciliation_validation"
        ]
        self.assertEqual(
            provenance["validation_query_contract_ref"],
            companion_contract.query_contract_id,
        )
        self.assertEqual(
            provenance["validation_result_ref"],
            companion_result.result_ref,
        )
        self.assertEqual(
            provenance["validation_report_ref"],
            reports[0].report_ref,
        )
        self.assertEqual(
            provenance["validation_snapshot_refs"],
            companion_result.source_snapshot_refs,
        )

    def test_query_set_rejects_report_ref_not_bound_to_result(self):
        contract = baseline_contract()
        result = successful_result(contract, rows=complete_rows())
        report = replace(
            validate_query_result(contract, result, paid_snapshot()),
            report_ref="completeness:other-result",
        )

        with self.assertRaisesRegex(
            ValueError,
            "query_set_report_ref_mismatch:0",
        ):
            validate_query_set((contract,), (result,), (report,))

    def test_query_set_blocks_unpaired_dimension_and_invalid_join_cardinality(self):
        total_contract = baseline_contract(query_id="query:paired-total:1")
        dimension_contract = baseline_contract(
            query_id="query:paired-dimension:1",
            dimensions=(channel_dimension(),),
            join_expectation=JoinExpectation(
                cardinality="many_to_one",
                audit_fields=(
                    "__join_input_rows",
                    "__join_output_rows",
                    "__join_duplicate_keys",
                    "__join_unmatched_rows",
                ),
                max_duplicate_keys=0,
                max_unmatched_rows=0,
            ),
        )
        dimension_contract = bind_dimension_reference(
            dimension_contract, total_contract
        )
        total_result = successful_result(
            total_contract,
            rows=complete_rows(target=100, baseline=80),
        )
        dimension_result = successful_result(
            dimension_contract,
            rows=(
                {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 100},
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "B", "paid_amount": 80},
            ),
            provider_stats={
                "join_input_rows": 2,
                "join_output_rows": 3,
                "join_duplicate_keys": 1,
                "join_unmatched_rows": 0,
            },
        )
        reports = (
            validate_query_result(total_contract, total_result, paid_snapshot()),
            validate_query_result(dimension_contract, dimension_result, paid_snapshot()),
        )

        reconciled = validate_query_set(
            (total_contract, dimension_contract),
            (total_result, dimension_result),
            reports,
        )

        dimension_report = reconciled[1]
        self.assertEqual(dimension_report.completeness_status, "partial")
        self.assertEqual(dimension_report.analysis_readiness, "blocked")
        self.assertIn("unpaired_dimension:channel:A:missing_baseline", dimension_report.failure_reasons)
        self.assertIn(
            "join_row_expansion:2:3",
            dimension_report.failure_reasons,
        )
        self.assertIn(
            "join_duplicate_keys_exceeded:1:0",
            dimension_report.failure_reasons,
        )

    def test_dimension_presence_policy_controls_cross_window_pairing(self):
        rows = (
            {"window_id": "target_day", "window_role": "target", "observation_key": "2026-06-02", "channel": "A", "paid_amount": 100},
            {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "A", "paid_amount": 501},
            {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "channel": "B", "paid_amount": 80},
        )
        outcomes = {}
        for policy in ("paired_required", "sparse_allowed"):
            contract = baseline_contract(
                query_id=f"query:dimension-presence:{policy}",
                dimensions=(channel_dimension(),),
            )
            contract = replace(
                contract,
                result_shape=replace(
                    contract.result_shape,
                    dimension_presence_policy=policy,
                ),
            )
            result = successful_result(contract, rows=rows)
            report = validate_query_result(contract, result, paid_snapshot())
            outcomes[policy] = validate_query_set(
                (contract,),
                (result,),
                (report,),
            )[0]

        self.assertEqual(
            (
                outcomes["paired_required"].completeness_status,
                outcomes["paired_required"].analysis_readiness,
            ),
            ("partial", "blocked"),
        )
        self.assertIn(
            "unpaired_dimension:channel:B:missing_target",
            outcomes["paired_required"].failure_reasons,
        )
        self.assertEqual(
            (
                outcomes["sparse_allowed"].completeness_status,
                outcomes["sparse_allowed"].analysis_readiness,
                outcomes["sparse_allowed"].failure_reasons,
            ),
            ("complete", "ready", ()),
        )

    def test_join_expectation_requires_complete_audit_statistics(self):
        contract = baseline_contract(
            join_expectation=JoinExpectation(
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
        )
        result = successful_result(
            contract,
            rows=complete_rows(),
            provider_stats={},
        )
        report = validate_query_result(contract, result, paid_snapshot())

        reconciled = validate_query_set((contract,), (result,), (report,))

        self.assertEqual(reconciled[0].completeness_status, "partial")
        self.assertEqual(reconciled[0].analysis_readiness, "blocked")
        for field in (
            "join_input_rows",
            "join_output_rows",
            "join_duplicate_keys",
            "join_unmatched_rows",
        ):
            self.assertIn(
                f"join_audit_missing:{field}",
                reconciled[0].failure_reasons,
            )

    def test_standalone_validator_rejects_unreviewed_join_audit_shape(self):
        contract = baseline_contract(
            join_expectation=JoinExpectation(
                cardinality="many_to_one",
                audit_fields=("join_rows",),
                max_duplicate_keys=0,
                max_unmatched_rows=0,
            )
        )
        result = successful_result(contract, rows=complete_rows())

        report = validate_query_result(contract, result, paid_snapshot())

        self.assertEqual(report.completeness_status, "invalid")
        self.assertIn(
            "invalid_join_expectation_audit_fields",
            report.failure_reasons,
        )

    def test_repair_retries_same_signature_only_for_transient_transport(self):
        contract = baseline_contract()
        transient = repair_report(contract, "transient_clickhouse:connection_reset")

        decision = plan_query_repair(
            contract,
            transient,
            attempted_signatures=(contract.contract_signature,),
        )

        self.assertEqual(decision.action, "retry_same")
        self.assertEqual(decision.report_ref, transient.report_ref)
        self.assertEqual(decision.failure_reasons, transient.failure_reasons)

    def test_runtime_failure_preserves_transient_type_through_repair(self):
        contract = reviewed_contract()
        snapshot = authorize_paid_snapshot(reviewed_snapshot())
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=_TransientClickHouseClient()),
            release_resolver=_PAID_RELEASE_RESOLVER,
        )
        envelope = executor.execute(contract, {snapshot.snapshot_ref: snapshot})

        report = validate_query_result(
            contract,
            envelope,
            snapshot,
            evidence_authority=executor.evidence_authority,
        )
        decision = plan_query_repair(
            contract,
            report,
            attempted_signatures=(contract.contract_signature,),
        )

        self.assertEqual(envelope.execution_status, "failed")
        self.assertTrue(envelope.failure_reason.startswith("transient_clickhouse:"))
        expected_refs = query_audit_refs(
            envelope.query_hash,
            contract.contract_signature,
            contract.dataset_snapshot_refs,
            query_contract_ref=contract.query_contract_id,
            execution_attempt_ref=envelope.execution_attempt_ref,
        )
        self.assertEqual(envelope.result_ref, expected_refs.result_ref)
        self.assertEqual(envelope.rows_ref, expected_refs.rows_ref)
        self.assertEqual(
            envelope.completeness_report_ref,
            expected_refs.completeness_report_ref,
        )
        self.assertIn(envelope.failure_reason, report.failure_reasons)
        self.assertFalse(
            any(
                reason.startswith(("missing_required_window:", "incomplete_window:"))
                for reason in report.failure_reasons
            )
        )
        self.assertEqual(decision.action, "retry_same")
        authority_record = executor.evidence_authority.resolve_query_execution(
            envelope.result_ref
        )
        self.assertEqual(authority_record.contract_signature, contract.contract_signature)
        self.assertEqual(authority_record.rows_content_hash, canonical_rows_hash((), ()))
        completeness_record = executor.evidence_authority.resolve_completeness(
            executor.evidence_authority.resolve_latest_completeness(
                report.report_ref
            ).record_ref
        )
        self.assertEqual(completeness_record.result_ref, envelope.result_ref)
        self.assertEqual(
            completeness_record.report_payload["failure_reasons"],
            report.failure_reasons,
        )

    def test_transient_mixed_with_hard_failure_does_not_retry_same(self):
        contract = baseline_contract()
        report = repair_report(
            contract,
            "transient_clickhouse:connection_error",
            "permission_blocked:channel",
        )

        decision = plan_query_repair(
            contract,
            report,
            attempted_signatures=(contract.contract_signature,),
        )

        self.assertEqual(decision.action, "degrade")
        self.assertNotEqual(decision.reason, "transient_clickhouse")

    def test_runtime_confirmed_provider_break_is_truncated(self):
        contract = reviewed_contract()
        snapshot = authorize_paid_snapshot(reviewed_snapshot())
        envelope = ClickHouseQueryExecutor(
            ClickHouseRuntime(client=_TruncatedClickHouseClient()),
            release_resolver=_PAID_RELEASE_RESOLVER,
        ).execute(contract, {snapshot.snapshot_ref: snapshot})

        report = validate_query_result(contract, envelope, snapshot)
        decision = plan_query_repair(contract, report, attempted_signatures=())

        self.assertEqual(envelope.execution_status, "failed")
        self.assertEqual(report.completeness_status, "truncated")
        self.assertNotEqual(decision.action, "retry_same")

    def test_repair_prevents_repeating_non_transient_signature(self):
        contract = baseline_contract()
        report = repair_report(contract, "missing_field:paid_amount")

        repeated = plan_query_repair(
            contract,
            report,
            attempted_signatures=(contract.contract_signature,),
        )
        fresh = plan_query_repair(contract, report, attempted_signatures=())

        self.assertEqual(repeated.action, "degrade")
        self.assertEqual(repeated.reason, "repeated_query_contract_signature")
        self.assertEqual(repeated.failure_reasons, report.failure_reasons)
        self.assertEqual(fresh.action, "recompile")
        self.assertEqual(fresh.report_ref, report.report_ref)

    def test_contract_source_permission_and_sample_gaps_do_not_retry_same_query(self):
        contract = baseline_contract()
        for reason in (
            "contract_gap:metric:paid_amount",
            "source_unbound:market_dashboard",
            "permission_blocked:channel",
            "insufficient_sample:channel:A",
        ):
            with self.subTest(reason=reason):
                report = repair_report(contract, reason)
                decision = plan_query_repair(contract, report, attempted_signatures=())
                self.assertEqual(decision.action, "degrade")
                self.assertEqual(decision.failure_reasons, (reason,))

    def test_window_coverage_repair_clarifies_with_exact_failure_context(self):
        contract = baseline_contract()
        report = repair_report(contract, "missing_required_window:target_day")

        decision = plan_query_repair(contract, report, attempted_signatures=())

        self.assertEqual(decision.action, "clarify")
        self.assertTrue(decision.requires_clarification)
        self.assertEqual(decision.report_ref, report.report_ref)
        self.assertEqual(
            decision.failure_reasons,
            ("missing_required_window:target_day",),
        )


if __name__ == "__main__":
    unittest.main()
