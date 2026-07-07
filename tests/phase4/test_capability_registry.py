import unittest

from bi_agent.runtime.capability_models import (
    BudgetState,
    CapabilityCard,
    CapabilityEvidenceEnvelope,
    CapabilityRequest,
)
from bi_agent.runtime.capability_registry import (
    get_capability_card,
    llm_capability_cards,
    public_capability_ids,
)


class CapabilityModelTest(unittest.TestCase):
    def test_capability_card_llm_summary_hides_physical_sql(self):
        card = CapabilityCard(
            capability_id="compare_periods",
            business_name="周期对比",
            description="Compare one metric between target and baseline periods.",
            input_schema={"metric": "metric_id"},
            output_schema={"evidence_ref": "string"},
            supported_question_families=("custom_baseline_comparison",),
            supported_grains=("day",),
            allowed_claim_types=("comparative_change",),
            default_evidence_type="statistical_association",
            cost_tier="low",
            runtime_tier="short",
            preconditions=("metric_contract_active",),
            failure_modes=("coverage_gap",),
        )

        summary = card.to_llm_summary()

        self.assertEqual(summary["capability_id"], "compare_periods")
        self.assertNotIn("sql", repr(summary).lower())
        self.assertNotIn("table", repr(summary).lower())

    def test_request_and_envelope_keep_business_labels(self):
        budget = BudgetState(
            mode="research", used_capability_calls=0, soft_limit=50, hard_limit=100
        )
        request = CapabilityRequest(
            run_id="run-1",
            accepted_graph_id="graph-1",
            graph_version=1,
            capability_id="compare_periods",
            question_family="custom_baseline_comparison",
            target_claim="comparative_change",
            claim_type="comparative_change",
            metric="paid_amount_ngn",
            scope="all_successful_paid_orders",
            time_window="2026-01-01..2026-06-30",
            baseline={"label": "Q1", "start": "2026-01-01", "end": "2026-04-01"},
            target={"label": "Q2", "start": "2026-04-01", "end": "2026-07-01"},
            grain="day",
            filters={},
            dimensions=(),
            contract_versions={"metric": "paid_amount.v1"},
            role="analyst",
            budget_state=budget,
            llm_business_reason="Compare Q2 against Q1.",
            params={},
        )
        envelope = CapabilityEvidenceEnvelope(
            evidence_ref="compare_periods:run-1:0",
            capability_id=request.capability_id,
            question_family=request.question_family,
            target_claim=request.target_claim,
            claim_type=request.claim_type,
            metric=request.metric,
            scope=request.scope,
            grain=request.grain,
            baseline_label="Q1",
            target_label="Q2",
            time_window=request.time_window,
            numeric_facts={"percent_delta": 0.15},
            typed_payload={"comparison_type": "period_average"},
            result_refs=("sqlhash-1",),
            sql_hashes=("sqlhash-1",),
            evidence_type="statistical_association",
            strength="medium",
            wording_limit="supported",
            limitations=(),
            disabled_degraded_blocked_path_refs=(),
            verifier_handoff={
                "requires_baseline_label": "Q1",
                "requires_target_label": "Q2",
            },
            admin_audit_ref="audit-1",
        )

        self.assertEqual(envelope.baseline_label, "Q1")
        self.assertEqual(envelope.target_label, "Q2")
        self.assertEqual(envelope.numeric_facts["percent_delta"], 0.15)


class CapabilityRegistryTest(unittest.TestCase):
    def test_registry_contains_general_catalog(self):
        expected = {
            "metric_coverage_profile",
            "metric_timeseries",
            "data_quality_profile",
            "compare_periods",
            "compare_period_phases",
            "rolling_window_compare",
            "weekday_calendar_compare",
            "event_window_compare",
            "formula_decompose",
            "component_contribution",
            "segment_breakdown",
            "segment_shift_compare",
            "candidate_dimension_screen",
            "joint_attribution",
            "outlier_scan",
            "change_point_scan",
            "evidence_reduce",
            "answer_verify",
        }

        self.assertEqual(set(public_capability_ids()), expected)

    def test_llm_cards_do_not_expose_physical_details(self):
        cards = llm_capability_cards()
        text = repr(cards).lower()

        self.assertIn("compare_periods", text)
        self.assertNotIn("paid_order_success_clean", text)
        self.assertNotIn("select ", text)
        self.assertNotIn("clickhouse", text)

    def test_unknown_capability_raises_key_error(self):
        with self.assertRaises(KeyError):
            get_capability_card("raw_sql")


if __name__ == "__main__":
    unittest.main()
