import unittest

from bi_agent.runtime.compiler import compile_graph
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
