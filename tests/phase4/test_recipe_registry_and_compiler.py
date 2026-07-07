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

    def test_contract_loader_rejects_non_mapping_yaml(self):
        for contents in ("- unsupported\n", "false\n", "0\n", "[]\n"):
            with self.subTest(contents=contents):
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = f"{tmpdir}/bad.yaml"
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(contents)

                    with self.assertRaisesRegex(ValueError, path):
                        load_contract(path)
