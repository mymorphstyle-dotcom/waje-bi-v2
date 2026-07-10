from dataclasses import replace
import unittest
from unittest.mock import patch

from bi_agent.capabilities.driver_decomposition import driver_decomposition
from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.outlier_contribution import outlier_contribution
from bi_agent.capabilities.segment_contribution import segment_contribution
from bi_agent.runtime.analysis_contracts import (
    CapabilityExecutionPlan,
    CapabilityInputSlot,
    CompletenessReport,
    QueryResultEnvelope,
)
from bi_agent.runtime.capability_harness import execute_capability
from bi_agent.runtime.answer_package import verify_answer_package
from bi_agent.runtime.capability_execution import bind_capability_inputs
from bi_agent.runtime.capability_models import BudgetState, CapabilityRequest


def run_capability(capability_id, params):
    value = dict(params)
    rows = value.pop("rows")
    if capability_id == "outlier_contribution":
        return outlier_contribution(rows, **value)
    if capability_id == "high_value_user_contribution":
        from bi_agent.capabilities.high_value_user_contribution import (
            high_value_user_contribution,
        )

        return high_value_user_contribution(rows, **value)
    if capability_id == "user_mix_contribution":
        from bi_agent.capabilities.user_mix_contribution import user_mix_contribution

        return user_mix_contribution(rows, **value)
    raise KeyError(capability_id)


class CapabilityHarnessTest(unittest.TestCase):
    def test_data_quality_check_carries_payment_and_duplicate_risks(self):
        result = data_quality_check(
            [
                {
                    "period": "2026-07-08",
                    "group": "target",
                    "amount": 120,
                    "non_success_orders": 2,
                    "duplicate_orders": 1,
                }
            ],
            result_refs=("hash-quality",),
        )

        self.assertEqual(result.typed_payload["quality_risks"]["non_success_orders"], 2)
        self.assertEqual(result.typed_payload["quality_risks"]["duplicate_orders"], 1)
        self.assertIn("payment_status_risk", result.limitations)
        self.assertIn("duplicate_order_risk", result.limitations)

    def test_pattern_scan_returns_normalized_envelope(self):
        pattern_rows = (
            {"month": "2026-01", "phase": "start", "amount": 130},
            {"month": "2026-01", "phase": "middle", "amount": 100},
            {"month": "2026-01", "phase": "end", "amount": 90},
            {"month": "2026-02", "phase": "start", "amount": 140},
            {"month": "2026-02", "phase": "middle", "amount": 100},
            {"month": "2026-02", "phase": "end", "amount": 90},
        )
        query_result = QueryResultEnvelope(
            query_contract_ref="query:pattern:1",
            query_id="provider:pattern:1",
            query_hash="hash:pattern:1",
            result_ref="result:pattern:1",
            execution_status="succeeded",
            rows_ref="rows:pattern:1",
            row_count=len(pattern_rows),
            completeness_report_ref="complete:pattern:1",
            rows=pattern_rows,
            observed_windows=("target_day",),
            source_snapshot_refs=("snapshot:paid:1",),
        )
        completeness = CompletenessReport(
            report_ref="complete:pattern:1",
            query_contract_ref="query:pattern:1",
            result_ref="result:pattern:1",
            completeness_status="complete",
            analysis_readiness="ready",
            assertion_results=(
                {"assertion": "execution_succeeded", "passed": True},
            ),
            failure_reasons=(),
            coverage_summary={
                "row_count": len(pattern_rows),
                "required_windows": ("target_day",),
                "observed_windows": ("target_day",),
                "snapshot_ref": "snapshot:paid:1",
            },
        )
        fixture_env = {
            "WAJE_ALLOW_LEGACY_FIXTURES": "1",
            "WAJE_RUNTIME_ENV": "test",
        }
        with patch.dict("os.environ", fixture_env):
            bound = bind_capability_inputs(
                CapabilityExecutionPlan(
                capability_id="compare_period_phases",
                capability_contract_ref="capability:compare_period_phases@1",
                required_input_slots=(
                    CapabilityInputSlot(
                        slot_id="time_bucket_scan",
                        query_contract_refs=("query:pattern:1",),
                        required=True,
                        accepted_completeness=("complete",),
                        required_fields=("month", "phase", "amount"),
                        required_window_ids=("target_day",),
                    ),
                ),
                optional_input_slots=(),
                merge_strategy="by_query_family",
                minimum_readiness={
                    "required_slots": "all",
                    "accepted_completeness": ("complete",),
                },
                degradation_policy={"missing_required_input": "block_claim"},
                supported_evidence_types=("statistical_association",),
                maximum_claim_strength="recurring_pattern",
                analysis_contract_ref="analysis:run-1:1",
                supported_claim_types=("recurring_pattern_existence",),
                capability_contract_version="1",
                capability_contract_signature="sha256:compare-period-phases",
            ),
                results={"query:pattern:1": query_result},
                reports={"query:pattern:1": completeness},
                run_mode="fixture",
            )
        request = CapabilityRequest(
            run_id="run-1",
            accepted_graph_id="graph-1",
            graph_version=1,
            capability_id="compare_period_phases",
            question_family="pattern_explanation",
            target_claim="recurring_pattern_existence",
            claim_type="recurring_pattern_existence",
            metric="paid_amount_ngn",
            scope="all_successful_paid_orders",
            time_window="2026-01..2026-03",
            baseline={"label": "middle_or_end"},
            target={"label": "start"},
            grain="month",
            filters={},
            dimensions=(),
            contract_versions={"metric": "paid_amount.v1"},
            role="analyst",
            budget_state=BudgetState(
                mode="research",
                used_capability_calls=0,
                soft_limit=50,
                hard_limit=100,
            ),
            llm_business_reason="Check whether month start is higher than sibling phases.",
            params={
                "rows": list(pattern_rows),
                "result_refs": ("sqlhash-1",),
                "pattern_family": "intra_period",
                "target_phase": "start",
                "min_periods": 2,
            },
            bound_input=bound,
            run_mode="fixture",
            fixture_input_mode="legacy_unbound_fixture",
        )

        with patch.dict("os.environ", fixture_env):
            envelope = execute_capability(request)

        self.assertEqual(envelope.capability_id, "compare_period_phases")
        self.assertEqual(envelope.target_label, "start")
        self.assertEqual(envelope.baseline_label, "middle_or_end")
        self.assertEqual(
            envelope.result_refs,
            ("result:pattern:1",),
        )
        self.assertEqual(envelope.analysis_contract_ref, "analysis:run-1:1")
        self.assertEqual(
            envelope.capability_contract_ref,
            "capability:compare_period_phases@1",
        )
        self.assertEqual(
            envelope.query_contract_refs,
            ("query:pattern:1",),
        )
        self.assertEqual(
            envelope.completeness_report_refs,
            ("complete:pattern:1",),
        )
        self.assertEqual(envelope.source_snapshot_refs, ("snapshot:paid:1",))
        self.assertEqual(envelope.binding_manifest_ref, "")
        self.assertEqual(
            envelope.supported_claim_types,
            ("recurring_pattern_existence",),
        )
        self.assertEqual(envelope.sql_hashes, ())
        self.assertEqual(envelope.evidence_type, "statistical_association")
        verifier = verify_answer_package(
            draft_claims=(
                {
                    "text": "月初模式稳定。",
                    "claim_strength": "observed",
                    "claim_type": "recurring_pattern_existence",
                    "evidence_refs": (envelope.evidence_ref,),
                },
            ),
            evidence=(envelope.to_dict(),),
            visible_limitations=envelope.limitations,
        )
        self.assertEqual(verifier["status"], "failed")
        self.assertIn("median_uplift", envelope.numeric_facts)

    def test_compare_periods_public_capability_uses_custom_baseline_scan(self):
        request = CapabilityRequest(
            run_id="run-custom-baseline",
            accepted_graph_id="graph-1",
            graph_version=1,
            capability_id="compare_periods",
            question_family="custom_baseline_comparison",
            target_claim="comparative_change",
            claim_type="comparative_change",
            metric="paid_amount_ngn",
            scope="all_successful_paid_orders",
            time_window="2026-01-01..2026-06-30",
            baseline={"label": "Q1"},
            target={"label": "Q2"},
            grain="custom_baseline",
            filters={},
            dimensions=(),
            contract_versions={"metric": "paid_amount.v1"},
            role="analyst",
            budget_state=BudgetState(
                mode="research",
                used_capability_calls=0,
                soft_limit=50,
                hard_limit=100,
            ),
            llm_business_reason="Compare Q2 daily paid amount against Q1.",
            params={
                "rows": [
                    {"period": "h1_2026", "group": "baseline", "amount": 100},
                    {"period": "h1_2026", "group": "target", "amount": 120},
                ],
                "result_refs": ("sqlhash-2",),
                "pattern_family": "custom_baseline",
                "period_key": "period",
                "group_key": "group",
                "target_group": "target",
                "baseline_group": "baseline",
                "min_periods": 1,
            },
        )

        envelope = execute_capability(request)

        self.assertEqual(envelope.capability_id, "compare_periods")
        self.assertEqual(envelope.evidence_type, "insufficient")
        self.assertEqual(envelope.wording_limit, "blocked")
        self.assertEqual(envelope.numeric_facts, {})
        self.assertIn("missing_bound_capability_input", envelope.limitations)
        self.assertEqual(envelope.result_refs, ())
        self.assertEqual(envelope.query_contract_refs, ())
        self.assertEqual(envelope.completeness_report_refs, ())
        self.assertEqual(envelope.source_snapshot_refs, ())
        self.assertEqual(envelope.supported_claim_types, ())

        fixture_request = replace(
            request,
            run_mode="fixture",
            fixture_input_mode="legacy_unbound_fixture",
        )
        with patch.dict("os.environ", {}, clear=True):
            unset_environment = execute_capability(fixture_request)
        self.assertEqual(unset_environment.wording_limit, "blocked")
        self.assertEqual(unset_environment.numeric_facts, {})

        with patch.dict(
            "os.environ",
            {
                "WAJE_ALLOW_LEGACY_FIXTURES": "1",
                "WAJE_RUNTIME_ENV": "test",
            },
        ):
            fixture_envelope = execute_capability(fixture_request)
        self.assertEqual(fixture_envelope.result_refs, ("sqlhash-2",))
        self.assertEqual(fixture_envelope.supported_claim_types, ())
        self.assertEqual(fixture_envelope.numeric_facts["comparable_periods"], 1)
        self.assertEqual(fixture_envelope.evidence_type, "statistical_association")

        with patch.dict(
            "os.environ",
            {
                "WAJE_ALLOW_LEGACY_FIXTURES": "1",
                "WAJE_RUNTIME_ENV": "production",
            },
        ):
            production = execute_capability(
                replace(
                    request,
                    run_mode="fixture",
                    fixture_input_mode="legacy_unbound_fixture",
                )
            )
        self.assertEqual(production.wording_limit, "blocked")
        self.assertEqual(production.numeric_facts, {})

    def test_driver_decomposition_identifies_volume_or_unit_driver(self):
        result = driver_decomposition(
            [
                {"period": "h1", "group": "baseline", "amount": 100, "paid_users": 10},
                {"period": "h1", "group": "target", "amount": 150, "paid_users": 12},
            ]
        )

        self.assertEqual(result.wording_limit, "quantified")
        self.assertEqual(result.typed_payload["primary_driver"], "unit_value")
        self.assertGreater(
            result.typed_payload["decompositions"][0]["unit_value_share"],
            result.typed_payload["decompositions"][0]["volume_share"],
        )

    def test_driver_decomposition_reports_revenue_component_changes(self):
        result = driver_decomposition(
            [
                {
                    "period": "2026-07-07",
                    "group": "baseline",
                    "amount": 100,
                    "paid_users": 10,
                    "orders": 20,
                    "first_paid_users": 4,
                    "payment_success_rate": 0.80,
                },
                {
                    "period": "2026-07-07",
                    "group": "target",
                    "amount": 150,
                    "paid_users": 12,
                    "orders": 30,
                    "first_paid_users": 6,
                    "payment_success_rate": 0.90,
                },
            ]
        )

        components = {
            item["component_id"]: item
            for item in result.typed_payload["decompositions"][0]["component_changes"]
        }
        self.assertAlmostEqual(components["paid_users"]["delta_ratio"], 0.2)
        self.assertAlmostEqual(components["paid_frequency"]["baseline_value"], 2.0)
        self.assertAlmostEqual(components["paid_frequency"]["target_value"], 2.5)
        self.assertAlmostEqual(components["avg_order_amount"]["baseline_value"], 5.0)
        self.assertAlmostEqual(components["avg_order_amount"]["target_value"], 5.0)
        self.assertAlmostEqual(components["first_paid_users"]["delta_ratio"], 0.5)
        self.assertAlmostEqual(components["payment_success_rate"]["delta"], 0.10)

    def test_driver_decomposition_skips_null_period_without_crashing(self):
        result = driver_decomposition(
            [
                {"period": None, "group": "baseline", "amount": 100, "paid_users": 10},
                {"period": "h1", "group": "baseline", "amount": 100, "paid_users": 10},
                {"period": "h1", "group": "target", "amount": 150, "paid_users": 12},
            ]
        )

        self.assertEqual(result.wording_limit, "quantified")
        self.assertEqual(result.typed_payload["decompositions"][0]["period"], "h1")

    def test_segment_contribution_ranks_dragging_segments(self):
        result = segment_contribution(
            [
                {"period": "A", "group": "baseline", "amount": 100},
                {"period": "A", "group": "target", "amount": 80},
                {"period": "B", "group": "baseline", "amount": 100},
                {"period": "B", "group": "target", "amount": 130},
            ]
        )

        self.assertEqual(result.wording_limit, "contextual")
        self.assertEqual(result.typed_payload["top_drags"][0]["segment"], "A")

    def test_outlier_contribution_reports_top_period_share(self):
        result = outlier_contribution(
            [
                {"period": "1", "group": "baseline", "amount": 100},
                {"period": "1", "group": "target", "amount": 200},
                {"period": "2", "group": "baseline", "amount": 100},
                {"period": "2", "group": "target", "amount": 105},
            ]
        )

        self.assertEqual(result.wording_limit, "contextual")
        self.assertGreater(result.typed_payload["top_positive_share"], 0.9)

    def test_outlier_contribution_reports_direction_after_removal(self):
        result = run_capability(
            "outlier_contribution",
            {
                "rows": [
                {"period": "1", "group": "baseline", "amount": 100},
                {"period": "1", "group": "target", "amount": 180},
                {"period": "2", "group": "baseline", "amount": 100},
                {"period": "2", "group": "target", "amount": 120},
                {"period": "3", "group": "baseline", "amount": 100},
                {"period": "3", "group": "target", "amount": 90},
                ],
                "period_grain": "day",
                "removal_policy": "top_positive_contribution_periods",
                "max_removed_periods": 1,
            },
        )

        self.assertIn("direction_preserved_after_top_positive", result.typed_payload)
        self.assertIn("remaining_delta_after_top_positive", result.typed_payload)

    def test_outlier_contribution_blocks_unsupported_removal_policy(self):
        result = run_capability(
            "outlier_contribution",
            {
                "rows": [
                    {"period": "1", "group": "baseline", "amount": 100},
                    {"period": "1", "group": "target", "amount": 180},
                ],
                "removal_policy": "custom_rule",
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("unsupported_removal_policy", result.limitations)
        self.assertEqual(result.typed_payload["removal_policy"], "custom_rule")

    def test_outlier_contribution_can_skip_direction_after_removal_claims(self):
        result = run_capability(
            "outlier_contribution",
            {
                "rows": [
                    {"period": "1", "group": "baseline", "amount": 100},
                    {"period": "1", "group": "target", "amount": 180},
                    {"period": "2", "group": "baseline", "amount": 100},
                    {"period": "2", "group": "target", "amount": 120},
                ],
                "removal_policy": "top_positive_contribution_periods",
                "direction_after_removal": False,
            },
        )

        self.assertNotIn(
            "direction_preserved_after_top_positive",
            result.typed_payload,
        )
        self.assertNotIn("direction_after_removal", result.typed_payload)
        self.assertEqual(
            result.typed_payload["direction_after_removal_evaluated"],
            False,
        )

    def test_user_mix_contribution_is_aggregate_only(self):
        result = run_capability(
            "user_mix_contribution",
            {
                "rows": [
                {
                    "period": "Q1",
                    "group": "baseline",
                    "channel": "Organic",
                    "user_mix_bucket": "new",
                    "amount": 100,
                    "paid_users": 10,
                },
                {
                    "period": "Q1",
                    "group": "target",
                    "channel": "Organic",
                    "user_mix_bucket": "new",
                    "amount": 120,
                    "paid_users": 12,
                },
                ],
                "segment_key": "channel",
                "user_grain_policy": "new_vs_returning",
            },
        )

        self.assertEqual(result.typed_payload["privacy_policy"], "aggregate_only")
        self.assertNotIn("raw_user_ids", result.typed_payload)

    def test_high_value_user_contribution_applies_threshold_indicators(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "bucket": "p95_plus",
                        "amount": 100,
                        "paid_users": 10,
                        "value_percentile": 0.97,
                    },
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "bucket": "regular",
                        "amount": 60,
                        "paid_users": 30,
                        "value_percentile": 0.40,
                    },
                    {
                        "period": "Q1",
                        "group": "target",
                        "bucket": "p95_plus",
                        "amount": 180,
                        "paid_users": 12,
                        "value_percentile": 0.98,
                    },
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.typed_payload["privacy_policy"], "aggregate_only")
        self.assertNotIn("raw_user_ids", result.typed_payload)
        self.assertEqual(result.wording_limit, "contextual")
        self.assertEqual(result.typed_payload["high_value_amount"], 280.0)
        self.assertEqual(result.typed_payload["high_value_paid_users"], 22.0)
        self.assertGreater(result.typed_payload["high_value_amount_share"], 0.8)

    def test_high_value_user_contribution_degrades_without_indicator_fields(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {"period": "Q1", "group": "baseline", "amount": 100, "paid_users": 10},
                    {"period": "Q1", "group": "target", "amount": 180, "paid_users": 12},
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("missing_high_value_indicator", result.limitations)
        self.assertIn("不能验证", result.typed_payload["business_readout"])

    def test_high_value_user_contribution_rejects_threshold_mismatch_bucket(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "bucket": "top_10_percent",
                        "amount": 100,
                        "paid_users": 10,
                    },
                    {
                        "period": "Q1",
                        "group": "target",
                        "bucket": "top_20",
                        "amount": 180,
                        "paid_users": 12,
                    },
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("missing_high_value_indicator", result.limitations)
        self.assertEqual(result.typed_payload["high_value_amount"], 0.0)

    def test_high_value_user_contribution_degrades_partial_explicit_aggregate_fields(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "amount": 100,
                        "paid_users": 10,
                        "high_value_amount": 80,
                    },
                    {
                        "period": "Q1",
                        "group": "target",
                        "amount": 180,
                        "paid_users": 12,
                        "high_value_paid_users": 9,
                    },
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("partial_high_value_aggregate_fields", result.limitations)
        self.assertNotIn("high_value_amount_share", result.typed_payload)
        self.assertIn("字段不完整", result.typed_payload["business_readout"])

    def test_high_value_user_contribution_rejects_generic_bucket_for_top_percentile(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "bucket": "vip",
                        "amount": 100,
                        "paid_users": 10,
                    },
                    {
                        "period": "Q1",
                        "group": "target",
                        "bucket": "whale",
                        "amount": 180,
                        "paid_users": 12,
                    },
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("missing_high_value_indicator", result.limitations)
        self.assertEqual(result.typed_payload["high_value_amount"], 0.0)

    def test_high_value_user_contribution_requires_positive_threshold_evidence(self):
        result = run_capability(
            "high_value_user_contribution",
            {
                "rows": [
                    {
                        "period": "Q1",
                        "group": "baseline",
                        "bucket": "regular",
                        "amount": 100,
                        "paid_users": 10,
                    },
                    {
                        "period": "Q1",
                        "group": "target",
                        "bucket": "other",
                        "amount": 180,
                        "paid_users": 12,
                    },
                ],
                "threshold_policy": {"type": "top_percentile", "value": 0.95},
            },
        )

        self.assertEqual(result.wording_limit, "insufficient")
        self.assertIn("missing_high_value_indicator", result.limitations)
        self.assertEqual(result.typed_payload["high_value_amount"], 0.0)


if __name__ == "__main__":
    unittest.main()
