import tempfile
import unittest

from bi_agent.runtime.compiler import SUPPORTED_CAPABILITIES, compile_graph
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.models import RecipeEntry
from bi_agent.runtime.recipe_registry import load_recipe_registry


class RecipeRegistryAndCompilerTest(unittest.TestCase):
    def test_registry_has_eight_recipe_entries(self):
        registry = load_recipe_registry()
        self.assertEqual(
            set(registry),
            {
                "pattern_explanation",
                "paid_amount_change_explanation",
                "business_object_impact_review",
                "revenue_health_review",
                "segment_or_factor_attribution",
                "anomaly_or_black_swan_review",
                "custom_baseline_comparison",
                "data_quality_or_evidence_review",
            },
        )
        self.assertTrue(all(registry[key].subgraph_nodes for key in registry))
        for key, entry in registry.items():
            self.assertIsInstance(entry, RecipeEntry)
            self.assertEqual(entry.recipe_id, key)
            self.assertEqual(entry.question_family, key)
            self.assertTrue(set(entry.subgraph_nodes).issubset(SUPPORTED_CAPABILITIES))
            self.assertEqual(entry.default_degraded, key != "pattern_explanation")

    def test_pattern_compile_adds_required_paths_and_records_mutations(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="intra_period",
            requested_nodes=["pattern_scan"],
            registry=registry,
        )

        accepted_capabilities = {node.capability for node in compiled.accepted_nodes}
        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {
                "data_quality_check",
                "pattern_scan",
                "formula_decompose",
                "event_evidence",
                "outlier_scan",
                "answer_verify",
            }.issubset(accepted_capabilities)
        )
        self.assertIn("segment_bridge", accepted_capabilities)
        self.assertTrue(compiled.mutations.proposed_graph)
        self.assertTrue(compiled.mutations.accepted_graph)
        self.assertTrue(
            any(item.action == "auto_added" for item in compiled.mutations.records)
        )

    def test_explicit_empty_registry_rejects_known_question_family(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            requested_nodes=["pattern_scan"],
            registry={},
        )

        self.assertEqual(compiled.status, "rejected")
        self.assertFalse(compiled.accepted_nodes)
        self.assertIn("pattern_explanation", compiled.mutations.rejected_or_degraded)
        self.assertTrue(
            any(
                item.action == "rejected"
                and item.capability == "pattern_explanation"
                and item.reason == "unknown_question_family"
                for item in compiled.mutations.records
            )
        )

    def test_pattern_compile_rejects_unknown_without_degrading_required_paths(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="intra_period",
            requested_nodes=["pattern_scan", "unsupported_magic"],
            registry=registry,
        )

        accepted_by_capability = {
            node.capability: node.status for node in compiled.accepted_nodes
        }
        self.assertEqual(compiled.status, "degraded")
        self.assertIn("unsupported_magic", compiled.mutations.rejected_or_degraded)
        self.assertEqual(
            accepted_by_capability,
            {
                "pattern_scan": "accepted",
                "data_quality_check": "accepted",
                "formula_decompose": "accepted",
                "event_evidence": "accepted",
                "segment_bridge": "accepted",
                "outlier_scan": "accepted",
                "answer_verify": "accepted",
            },
        )
        self.assertTrue(
            any(
                item.action == "rejected"
                and item.capability == "unsupported_magic"
                and item.reason == "unknown_capability"
                for item in compiled.mutations.records
            )
        )

    def test_compiler_uses_registry_capabilities(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="intra_period",
            requested_nodes=("compare_period_phases", "raw_sql"),
        )

        self.assertEqual(compiled.status, "degraded")
        self.assertIn("compare_period_phases", compiled.mutations.accepted_graph)
        self.assertIn("pattern_scan", compiled.mutations.accepted_graph)
        self.assertIn("raw_sql", compiled.mutations.rejected_or_degraded)

    def test_compiler_rejects_known_capability_for_unsupported_question_family(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="weekly",
            requested_nodes=("metric_coverage_profile", "weekday_calendar_compare"),
        )

        self.assertEqual(compiled.status, "degraded")
        self.assertIn("weekday_calendar_compare", compiled.mutations.accepted_graph)
        self.assertNotIn("metric_coverage_profile", compiled.mutations.accepted_graph)
        self.assertIn("metric_coverage_profile", compiled.mutations.rejected_or_degraded)
        self.assertTrue(
            any(
                item.action == "rejected"
                and item.capability == "metric_coverage_profile"
                and item.reason == "unsupported_question_family"
                for item in compiled.mutations.records
            )
        )

    def test_non_pattern_recipe_compiles_as_degraded_dry_run_skeleton(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            requested_nodes=[],
            registry=registry,
        )

        self.assertEqual(compiled.status, "degraded")
        self.assertTrue(compiled.accepted_nodes)
        self.assertTrue(all(node.status == "degraded" for node in compiled.accepted_nodes))
        self.assertTrue(
            any(
                item.action == "degraded"
                and item.reason == "non_pattern_dry_run_skeleton"
                for item in compiled.mutations.records
            )
        )

    def test_custom_baseline_comparison_accepts_harness_route(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=(
                "data_quality_profile",
                "compare_periods",
                "evidence_reduce",
                "answer_verify",
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertEqual(
            compiled.mutations.accepted_graph,
            (
                "data_quality_profile",
                "compare_periods",
                "evidence_reduce",
                "answer_verify",
            ),
        )
        self.assertFalse(compiled.mutations.rejected_or_degraded)

    def test_non_pattern_recipe_accepts_llm_selected_supported_nodes(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            requested_nodes=["driver_decomposition", "answer_verify"],
            registry=registry,
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertEqual(
            compiled.mutations.accepted_graph,
            ("driver_decomposition", "answer_verify"),
        )
        self.assertFalse(compiled.mutations.rejected_or_degraded)

    def test_paid_amount_change_recipe_accepts_phase6_required_capabilities(self):
        compiled = compile_graph(
            question_family="paid_amount_change_explanation",
            target_metric="paid_amount",
            requested_nodes=(
                "data_quality_profile",
                "driver_decomposition",
                "answer_verify",
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertIn("driver_decomposition", compiled.mutations.accepted_graph)
        self.assertNotIn(
            "paid_amount_change_explanation",
            compiled.mutations.rejected_or_degraded,
        )

    def test_business_object_recipe_stays_degraded_without_event_or_comparison_path(self):
        compiled = compile_graph(
            question_family="business_object_impact_review",
            target_metric="paid_amount",
            requested_nodes=("answer_verify",),
        )

        self.assertEqual(compiled.status, "degraded")
        self.assertIn(
            "business_object_impact_review",
            compiled.mutations.rejected_or_degraded,
        )

    def test_composite_family_allows_secondary_family_capability(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            question_families=(
                "custom_baseline_comparison",
                "segment_or_factor_attribution",
            ),
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=(
                "data_quality_profile",
                "segment_contribution",
                "answer_verify",
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertIn("segment_contribution", compiled.mutations.accepted_graph)
        self.assertNotIn("segment_contribution", compiled.mutations.rejected_or_degraded)

    def test_custom_baseline_accepts_explicit_driver_decomposition(self):
        registry = load_recipe_registry()
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            requested_nodes=["driver_decomposition", "answer_verify"],
            registry=registry,
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertEqual(
            compiled.mutations.accepted_graph,
            ("driver_decomposition", "answer_verify"),
        )
        self.assertFalse(compiled.mutations.rejected_or_degraded)

    def test_revenue_pattern_attribution_compiles_complete_capability_bundle(self):
        compiled = compile_graph(
            question_family="pattern_explanation",
            target_metric="paid_amount",
            pattern_family="weekly",
            requested_nodes=("segment_contribution",),
            question_text=(
                "最近付费金额是否存在固定规律，比如周末更高、月初更高、晚上更高？"
                "这个规律主要由哪个渠道、地区、用户类型或玩法带动？"
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {
                "pattern_scan",
                "segment_contribution",
                "joint_attribution",
                "answer_verify",
            }.issubset(set(compiled.mutations.accepted_graph))
        )
        row_shape = compiled.runtime_plan["row_shapes"][0]
        self.assertEqual(row_shape["source"], "clickhouse")
        self.assertIn("channel", row_shape["dimension_keys"])
        self.assertIn("payment_method", row_shape["dimension_keys"])
        self.assertIn("region", row_shape["dimension_keys"])
        self.assertIn("device_brand", row_shape["dimension_keys"])
        self.assertIn("gameplay_contract_missing", row_shape["contract_gaps"])
        self.assertTrue(
            any(
                record.action == "auto_added"
                and record.capability == "joint_attribution"
                and record.reason == "revenue_diagnostics:pattern_attribution"
                for record in compiled.mutations.records
            )
        )

    def test_revenue_health_compiles_risk_and_concentration_bundle(self):
        compiled = compile_graph(
            question_family="revenue_health_review",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("data_quality_profile", "driver_decomposition"),
            question_text=(
                "当前收入健康吗？是靠正常用户增长带动，还是靠少数大额用户、"
                "短期活动或异常渠道拉动？收入结构里最大的风险点是什么？"
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {
                "data_quality_profile",
                "driver_decomposition",
                "user_mix_contribution",
                "high_value_user_contribution",
                "outlier_scan",
                "event_evidence",
                "answer_verify",
            }.issubset(set(compiled.mutations.accepted_graph))
        )
        row_shape = compiled.runtime_plan["row_shapes"][0]
        self.assertIn("user_mix_bucket", row_shape["optional_fields"])
        self.assertIn("high_value_amount", row_shape["optional_fields"])
        self.assertIn("high_value_user_contract_missing", row_shape["contract_gaps"])

    def test_multi_baseline_question_preserves_rolling_window_and_adds_compare(self):
        compiled = compile_graph(
            question_family="custom_baseline_comparison",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("rolling_window_compare", "driver_decomposition"),
            question_families=("custom_baseline_comparison", "paid_amount_change_explanation"),
            question_text="相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？",
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertIn("rolling_window_compare", compiled.mutations.accepted_graph)
        self.assertIn("compare_periods", compiled.mutations.accepted_graph)
        self.assertIn("driver_decomposition", compiled.mutations.accepted_graph)
        self.assertIn("answer_verify", compiled.mutations.accepted_graph)
        self.assertIn(
            "rolling_7_day_baseline",
            compiled.runtime_plan["baseline_windows"],
        )

    def test_evidence_quality_question_compiles_audit_and_attribution_bundle(self):
        compiled = compile_graph(
            question_family="data_quality_or_evidence_review",
            target_metric="paid_amount",
            requested_nodes=("data_quality_profile", "answer_verify"),
            question_text=(
                "这个结论的数据证据够不够？是否存在数据延迟、渠道归因异常、"
                "支付状态缺失、重复订单或异常用户影响判断？"
            ),
        )

        self.assertEqual(compiled.status, "accepted")
        self.assertTrue(
            {
                "data_quality_profile",
                "segment_contribution",
                "joint_attribution",
                "outlier_scan",
                "answer_verify",
            }.issubset(set(compiled.mutations.accepted_graph))
        )
        row_shape = compiled.runtime_plan["row_shapes"][0]
        self.assertIn("payment_status_contract_missing", row_shape["contract_gaps"])
        self.assertIn("duplicate_order_contract_missing", row_shape["contract_gaps"])

    def test_compiler_keeps_llm_requested_nodes_when_adding_supporting_nodes(self):
        compiled = compile_graph(
            question_family="segment_or_factor_attribution",
            target_metric="paid_amount",
            pattern_family="custom_baseline",
            requested_nodes=("joint_attribution", "answer_verify"),
            question_text="Q2 比 Q1 是用户数还是客单价驱动？",
        )

        self.assertIn("joint_attribution", compiled.mutations.accepted_graph)
        self.assertIn("driver_decomposition", compiled.mutations.accepted_graph)
        self.assertIn("answer_verify", compiled.mutations.accepted_graph)

    def test_contract_loader_rejects_non_mapping_yaml(self):
        for contents in ("- unsupported\n", "false\n", "0\n", "[]\n"):
            with self.subTest(contents=contents):
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = f"{tmpdir}/bad.yaml"
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(contents)

                    with self.assertRaisesRegex(ValueError, path):
                        load_contract(path)
