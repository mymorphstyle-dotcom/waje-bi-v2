import unittest

from bi_agent.runtime.compiler import SUPPORTED_CAPABILITIES, compile_graph
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
