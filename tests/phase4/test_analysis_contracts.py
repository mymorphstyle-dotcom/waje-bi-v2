from datetime import date, datetime
import unittest

from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    MetricBinding,
    QueryContract,
    QueryResultEnvelope,
    ResolvedWindow,
    ResultShape,
    query_contract_semantic_body,
    query_contract_signature,
    stable_contract_signature,
)
from bi_agent.runtime.window_resolver import resolve_revenue_windows


class AnalysisContractsTest(unittest.TestCase):
    def test_resolves_fixed_yesterday_and_three_baselines(self):
        result = resolve_revenue_windows(
            target_semantic="yesterday",
            baselines=("previous_day", "rolling_7_day_baseline", "same_weekday_last_week"),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            timezone_name="Africa/Lagos",
            dataset_watermarks={"paid_order_success": date(2026, 7, 4)},
            affected_capabilities=("compare_periods",),
            affected_claim_types=("comparative_change",),
        )

        windows = {window.window_id: window for window in result.windows}
        self.assertEqual(windows["target_day"].start_inclusive, "2026-06-02")
        self.assertEqual(windows["previous_day"].start_inclusive, "2026-06-01")
        self.assertEqual(windows["rolling_7_day_baseline"].start_inclusive, "2026-05-26")
        self.assertEqual(windows["rolling_7_day_baseline"].end_exclusive, "2026-06-02")
        self.assertEqual(windows["same_weekday_last_week"].start_inclusive, "2026-05-26")
        self.assertTrue(all(window.membership_policy == "allow_overlap" for window in result.windows))
        self.assertEqual(result.gaps, ())

    def test_fixed_eval_adds_pattern_and_anomaly_history_windows(self):
        result = resolve_revenue_windows(
            target_semantic="yesterday",
            baselines=(),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            timezone_name="Africa/Lagos",
            dataset_watermarks={"paid_order_success": date(2026, 7, 4)},
            affected_capabilities=("pattern_scan", "outlier_scan"),
            affected_claim_types=(
                "recurring_pattern_existence",
                "external_shock_candidate_or_anomaly",
            ),
            fixed_window_bounds={
                "target_day": ("2026-06-02", "2026-06-02"),
                "previous_day": ("2026-06-01", "2026-06-01"),
                "rolling_7_day_baseline": ("2026-05-26", "2026-06-01"),
                "same_weekday_last_week": ("2026-05-26", "2026-05-26"),
                "pattern_history": ("2026-01-01", "2026-06-02"),
                "anomaly_history": ("2026-05-03", "2026-06-01"),
            },
        )

        windows = {window.window_id: window for window in result.windows}
        self.assertEqual(windows["previous_day"].start_inclusive, "2026-06-01")
        self.assertEqual(
            windows["rolling_7_day_baseline"].aggregation,
            "mean_of_complete_days",
        )
        self.assertEqual(
            windows["same_weekday_last_week"].start_inclusive,
            "2026-05-26",
        )
        self.assertEqual(windows["pattern_history"].start_inclusive, "2026-01-01")
        self.assertEqual(windows["pattern_history"].end_exclusive, "2026-06-03")
        self.assertEqual(windows["anomaly_history"].start_inclusive, "2026-05-03")
        self.assertEqual(windows["anomaly_history"].end_exclusive, "2026-06-02")
        self.assertEqual(windows["pattern_history"].required_complete_days, 153)
        self.assertEqual(windows["anomaly_history"].required_complete_days, 30)

    def test_reports_requested_target_missing_without_shifting_it(self):
        result = resolve_revenue_windows(
            target_semantic="yesterday",
            baselines=("previous_day",),
            as_of=datetime.fromisoformat("2026-07-10T12:00:00+01:00"),
            timezone_name="Africa/Lagos",
            dataset_watermarks={"paid_order_success": date(2026, 7, 4)},
            affected_capabilities=("compare_periods",),
            affected_claim_types=("comparative_change",),
        )

        self.assertEqual(result.windows[0].start_inclusive, "2026-07-09")
        self.assertEqual(result.gaps[0].gap_type, "window_data_unavailable")
        self.assertEqual(result.gaps[0].dataset_id, "paid_order_success")
        self.assertEqual(result.gaps[0].owner, "data_owner")
        self.assertEqual(result.gaps[0].affected_capabilities, ("compare_periods",))
        self.assertEqual(result.gaps[0].affected_claim_types, ("comparative_change",))

    def test_gap_identity_includes_requested_target_window(self):
        shared = {
            "baselines": ("previous_day",),
            "as_of": datetime.fromisoformat("2026-07-10T12:00:00+01:00"),
            "timezone_name": "Africa/Lagos",
            "dataset_watermarks": {"paid_order_success": date(2026, 7, 4)},
            "affected_capabilities": ("compare_periods",),
            "affected_claim_types": ("comparative_change",),
        }

        first = resolve_revenue_windows(target_semantic="2026-07-09", **shared)
        second = resolve_revenue_windows(target_semantic="2026-07-10", **shared)

        self.assertNotEqual(first.gaps[0].gap_id, second.gaps[0].gap_id)
        self.assertIn("target_day:2026-07-09", first.gaps[0].gap_id)
        self.assertIn("target_day:2026-07-10", second.gaps[0].gap_id)

    def test_rejects_unattributed_window_gap(self):
        common = {
            "target_semantic": "2026-07-09",
            "baselines": ("previous_day",),
            "as_of": datetime.fromisoformat("2026-07-10T12:00:00+01:00"),
            "timezone_name": "Africa/Lagos",
            "dataset_watermarks": {"paid_order_success": date(2026, 7, 4)},
        }

        with self.assertRaisesRegex(ValueError, "window_gap_requires_affected_capabilities"):
            resolve_revenue_windows(
                **common,
                affected_capabilities=(),
                affected_claim_types=("comparative_change",),
            )
        with self.assertRaisesRegex(ValueError, "window_gap_requires_affected_claim_types"):
            resolve_revenue_windows(
                **common,
                affected_capabilities=("compare_periods",),
                affected_claim_types=(),
            )

    def test_rejects_duplicate_baselines(self):
        with self.assertRaisesRegex(ValueError, "duplicate_baseline:previous_day"):
            resolve_revenue_windows(
                target_semantic="yesterday",
                baselines=("previous_day", "previous_day"),
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
                timezone_name="Africa/Lagos",
                dataset_watermarks={"paid_order_success": date(2026, 7, 4)},
                affected_capabilities=("compare_periods",),
                affected_claim_types=("comparative_change",),
            )

    def test_result_envelope_serializes_external_schema_and_keeps_internal_rows(self):
        rows = ({"window_id": "target_day", "paid_amount": 120},)
        envelope = QueryResultEnvelope(
            query_contract_ref="query:run-1:daily:1",
            query_id="provider-query-1",
            query_hash="query-hash-1",
            result_ref="result:run-1:daily:1",
            execution_status="succeeded",
            rows_ref="artifact:aggregate-rows:1",
            row_count=1,
            completeness_report_ref="completeness:run-1:daily:1",
            rows=rows,
        )

        payload = envelope.to_dict()

        self.assertEqual(envelope.rows, rows)
        self.assertEqual(payload["rows_ref"], "artifact:aggregate-rows:1")
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["completeness_report_ref"], "completeness:run-1:daily:1")
        self.assertNotIn("rows", payload)

    def test_contract_relationships_are_explicit_and_serialize_nested_values(self):
        window = ResolvedWindow(
            "target_day",
            "target",
            "2026-06-02",
            "2026-06-02",
            "2026-06-03",
            "Africa/Lagos",
            "daily_total",
            1,
            "2026-06-02",
        )
        metric = MetricBinding(
            "paid_amount",
            "metric:paid_amount@1",
            "paid_order_success",
            "sum(paid_amount_ngn)",
            "sum",
            ("paid_amount_ngn",),
            ("window_id",),
        )
        slot = CapabilityInputSlot(
            "daily_totals",
            ("query:run-1:daily:1",),
            True,
            ("complete",),
            ("window_id", "paid_amount"),
            ("target_day",),
        )
        plan = CapabilityExecutionPlan(
            capability_id="compare_periods",
            capability_contract_ref="capability:compare_periods@1",
            required_input_slots=(slot,),
            optional_input_slots=(),
            merge_strategy="contract_defined",
            minimum_readiness={"required_slots": "ready"},
            degradation_policy={"missing_optional": "degraded"},
            supported_evidence_types=("accounting_contribution",),
            maximum_claim_strength="medium",
        )
        analysis = AnalysisContract(
            analysis_contract_id="analysis:run-1:1",
            contract_version="1",
            question_families=("paid_amount_change_explanation",),
            target_metric_refs=("metric:paid_amount@1",),
            claim_intents=("comparative_change",),
            scope={"type": "full_sample"},
            business_timezone="Africa/Lagos",
            as_of="2026-06-03T12:00:00+01:00",
            resolved_windows=(window,),
            metric_bindings=(metric,),
            dimension_bindings=(),
            dataset_requirements=("paid_order_success",),
            capability_requirements=("capability:compare_periods@1",),
            permission_scope="analyst",
        )
        query = QueryContract(
            query_contract_id="query:run-1:daily:1",
            analysis_contract_ref=analysis.analysis_contract_id,
            query_intent="daily_metric_baselines",
            dataset_snapshot_refs=("snapshot:paid-order:1",),
            metric_bindings=(metric,),
            dimension_bindings=(),
            window_refs=("target_day",),
            resolved_windows=(window,),
            filters=(),
            result_shape=ResultShape(
                ("window_id", "paid_amount"),
                ("window_id",),
                ("window_id",),
                ("target_day",),
            ),
            completeness_assertions=("required_windows",),
            permission_scope="analyst",
            workload_class="interactive_aggregate",
            contract_signature="signature",
        )

        analysis_payload = analysis.to_dict()
        query_payload = query.to_dict()

        self.assertEqual(
            analysis_payload["capability_requirements"],
            ("capability:compare_periods@1",),
        )
        self.assertNotIn("capability_plans", analysis_payload)
        self.assertEqual(query_payload["analysis_contract_ref"], analysis.analysis_contract_id)
        self.assertEqual(query_payload["window_refs"], ("target_day",))
        self.assertEqual(query_payload["resolved_windows"][0]["window_id"], "target_day")
        self.assertEqual(plan.minimum_readiness["required_slots"], "ready")
        self.assertEqual(plan.degradation_policy["missing_optional"], "degraded")

    def test_contract_signature_is_order_stable(self):
        left = stable_contract_signature({"b": [2, 1], "a": {"x": 1}})
        right = stable_contract_signature({"a": {"x": 1}, "b": [2, 1]})
        self.assertEqual(left, right)

    def test_query_contract_signature_covers_parameters_and_excludes_identity(self):
        base = {
            "query_contract_id": "query:run-a:1",
            "analysis_contract_ref": "analysis:run-a:1",
            "query_intent": "high_value_scan",
            "dataset_snapshot_refs": ("snapshot:paid:1",),
            "metric_bindings": (),
            "dimension_bindings": (),
            "window_refs": (),
            "resolved_windows": (),
            "filters": (),
            "result_shape": ResultShape((), (), (), ()),
            "completeness_assertions": (),
            "permission_scope": "analyst",
            "workload_class": "interactive_aggregate",
            "contract_signature": "ignored",
            "query_parameters": {
                "threshold_quantile": 0.95,
                "threshold_reference": "within_window_user_paid_amount",
                "aggregation_grain": ("window_id", "observation_key", "user_id"),
            },
        }
        first = QueryContract(**base)
        second = QueryContract(
            **{
                **base,
                "query_contract_id": "query:run-b:9",
                "analysis_contract_ref": "analysis:run-b:1",
                "contract_signature": "different-ignored-value",
            }
        )

        self.assertEqual(query_contract_signature(first), query_contract_signature(second))
        self.assertNotIn("query_contract_id", query_contract_semantic_body(first))
        self.assertNotIn("contract_signature", query_contract_semantic_body(first))
        changed = QueryContract(
            **{
                **base,
                "query_parameters": {
                    **base["query_parameters"],
                    "threshold_quantile": 0.9,
                },
            }
        )
        self.assertNotEqual(query_contract_signature(first), query_contract_signature(changed))


if __name__ == "__main__":
    unittest.main()
