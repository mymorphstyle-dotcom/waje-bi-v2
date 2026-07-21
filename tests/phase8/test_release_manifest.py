from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bi_agent.runtime.release_manifest import (
    ACTIVE_REF_PATTERN,
    FORBIDDEN_LEGACY_PATHS,
    FORBIDDEN_LEGACY_SYMBOLS,
    PRODUCTION_INVENTORY_ROOTS,
    REQUIRED_ROLLBACK_COMPONENTS,
    ROLLBACK_REF_PATTERN,
    active_ref_for_paths,
    load_release_manifest,
    rollback_plan,
    validate_release_manifest,
)


ROLLBACK_REF = "git:1ec06ae93e816f2037b9eefb12c3801fa62b0bba"


class ReleaseManifestTest(unittest.TestCase):
    def test_manifest_covers_all_release_rollback_components(self):
        manifest = load_release_manifest()
        problems = validate_release_manifest(manifest)

        self.assertEqual(problems, [])
        components = {item["component"] for item in manifest["components"]}
        self.assertEqual(components, REQUIRED_ROLLBACK_COMPONENTS)

    def test_rollback_plan_names_refs_owner_paths_and_checks(self):
        plan = rollback_plan("conversation_and_plan_authority")

        self.assertEqual(plan["component"], "conversation_and_plan_authority")
        self.assertTrue(plan["paths"])
        self.assertTrue(ACTIVE_REF_PATTERN.fullmatch(plan["active_ref"]))
        self.assertEqual(plan["rollback_ref"], ROLLBACK_REF)
        self.assertTrue(plan["owner"])
        self.assertIn("full_acceptance_eval", plan["required_checks"])

    def test_manifest_covers_the_complete_single_authority_runtime(self):
        plan = rollback_plan("conversation_and_plan_authority")
        paths = set(plan["paths"])

        self.assertTrue(
            {
                "bi_agent/runtime/single_authority.py",
                "bi_agent/runtime/authority_context_resolver.py",
                "bi_agent/runtime/context_manifest.py",
                "bi_agent/runtime/authoritative_plan_result.py",
                "bi_agent/runtime/plan_authority.py",
                "bi_agent/runtime/plan_compiler.py",
                "bi_agent/runtime/runtime_contract_registry.py",
                "bi_agent/runtime/durable_call_journal.py",
                "bi_agent/runtime/llm_prompts.py",
                "bi_agent/runtime/langgraph_workflow.py",
                "bi_agent/runtime/claim_coverage.py",
                "bi_agent/conversation",
            }
            <= paths
        )
        self.assertEqual(plan["active_ref"], active_ref_for_paths(plan["paths"]))
        self.assertEqual(plan["rollback_ref"], ROLLBACK_REF)
        self.assertEqual(plan["owner"], "agent_runtime_owner")
        self.assertTrue(
            {
                "single_authority_phase01_tests",
                "single_authority_phase02_tests",
                "single_authority_postgres_tests",
                "gateway_phase02_planned_tests",
            }
            <= set(plan["required_checks"])
        )
        self.assertEqual(
            {
                "contracts",
                "ledger",
                "conversation_and_plan_authority",
                "capability_execution_authority",
                "claim_and_narrative_authority",
                "publication_and_delivery",
                "gateway",
                "eval_governance",
            },
            REQUIRED_ROLLBACK_COMPONENTS,
        )
        self.assertIn(
            "single-authority.final", load_release_manifest()["manifest_version"]
        )
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(
            [path for path in plan["paths"] if not (root / path).exists()],
            [],
        )

    def test_manifest_maps_all_new_authorities_to_rollback_components(self):
        manifest = load_release_manifest()
        paths_by_component = {
            item["component"]: set(item["paths"]) for item in manifest["components"]
        }

        self.assertTrue(
            {
                "bi_agent/runtime/context_manifest.py",
                "bi_agent/runtime/authoritative_plan_result.py",
                "bi_agent/runtime/durable_call_journal.py",
            }
            <= paths_by_component["conversation_and_plan_authority"]
        )
        self.assertTrue(
            {
                "bi_agent/runtime/analysis_runtime.py",
                "bi_agent/runtime/authoritative_execution_result.py",
                "bi_agent/runtime/formula_graph.py",
                "bi_agent/runtime/evidence_taxonomy.py",
            }
            <= paths_by_component["capability_execution_authority"]
        )
        self.assertIn(
            "bi_agent/runtime/post_seal_failure_persistence.py",
            paths_by_component["publication_and_delivery"],
        )
        self.assertIn(
            "bi_agent/runtime/narrative_material_projection.py",
            paths_by_component["claim_and_narrative_authority"],
        )
        self.assertIn(
            "bi_agent/runtime/narrative_material_persistence.py",
            paths_by_component["publication_and_delivery"],
        )
        self.assertIn(
            "tools/runtime/cutover_single_authority_schema.py",
            paths_by_component["publication_and_delivery"],
        )
        self.assertIn(
            "tools/runtime/backup_waje_runtime.py",
            paths_by_component["publication_and_delivery"],
        )
        self.assertIn(
            "bi_agent/runtime/temporal_comparison.py",
            paths_by_component["conversation_and_plan_authority"],
        )
        self.assertIn(
            "bi_agent/runtime/insight_quality_rubric.py",
            paths_by_component["eval_governance"],
        )

    def test_manifest_covers_business_ssot_and_complete_production_inventory(self):
        manifest = load_release_manifest()
        paths = {
            path for component in manifest["components"] for path in component["paths"]
        }

        self.assertIn("contracts/ssot", paths)
        self.assertIn("tools/contracts/generate-ssot-node-map.rb", paths)
        self.assertIn("tools/contracts/validate-contracts.rb", paths)
        self.assertIn("tools/data", paths)
        self.assertIn("app", paths)
        self.assertIn("bi_agent/runtime/exploration_budget_policy.py", paths)
        self.assertIn("contracts/authorities", paths)
        self.assertTrue(PRODUCTION_INVENTORY_ROOTS)
        self.assertFalse(
            [
                problem
                for problem in validate_release_manifest(manifest)
                if problem.startswith("uncovered_production_path:")
            ]
        )

    def test_manifest_rejects_reintroduced_legacy_authority(self):
        self.assertIn(
            "bi_agent/runtime/answer_package.py",
            FORBIDDEN_LEGACY_PATHS,
        )
        self.assertIn(
            "bi_agent/conversation/clarification_options.py",
            FORBIDDEN_LEGACY_PATHS,
        )
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            forbidden = root / "bi_agent/runtime/answer_package.py"
            forbidden.parent.mkdir(parents=True)
            forbidden.write_text("legacy = True\n", encoding="utf-8")

            problems = validate_release_manifest(
                load_release_manifest(),
                root=root,
            )

        self.assertIn(
            "forbidden_legacy_path:bi_agent/runtime/answer_package.py",
            problems,
        )

    def test_manifest_rejects_reintroduced_legacy_workflow_symbols(self):
        expected = {
            "run_pattern_workflow",
            "build_pattern_graph",
            "choice_actions",
            "phase4-draft",
            "recommended_assumption",
            "recommended_choice_id",
        }
        self.assertEqual(FORBIDDEN_LEGACY_SYMBOLS, expected)
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "bi_agent/runtime/langgraph_workflow.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "run_pattern_workflow = build_pattern_graph = 'phase4-draft'\n"
                "choice_actions = recommended_assumption = "
                "recommended_choice_id = None\n",
                encoding="utf-8",
            )

            problems = validate_release_manifest(
                load_release_manifest(),
                root=root,
            )

        for symbol in expected:
            self.assertIn(
                "forbidden_legacy_symbol:"
                f"{symbol}:bi_agent/runtime/langgraph_workflow.py",
                problems,
            )

    def test_component_refs_match_current_content_and_explicit_rollback_commit(self):
        for item in load_release_manifest()["components"]:
            with self.subTest(component=item["component"]):
                self.assertEqual(
                    item["active_ref"],
                    active_ref_for_paths(item["paths"]),
                )
                self.assertTrue(ACTIVE_REF_PATTERN.fullmatch(item["active_ref"]))
                self.assertTrue(ROLLBACK_REF_PATTERN.fullmatch(item["rollback_ref"]))
                self.assertEqual(item["rollback_ref"], ROLLBACK_REF)

    def test_content_ref_is_deterministic_and_excludes_generated_files(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            (source / "authoritative.py").write_text("value = 1\n", encoding="utf-8")
            (source / ".coverage").write_text("generated-a", encoding="utf-8")
            cache = source / "__pycache__"
            cache.mkdir()
            (cache / "authoritative.pyc").write_bytes(b"generated-a")

            first = active_ref_for_paths(["source"], root=root)
            (source / ".coverage").write_text("generated-b", encoding="utf-8")
            (cache / "authoritative.pyc").write_bytes(b"generated-b")
            second = active_ref_for_paths(["source"], root=root)
            self.assertEqual(second, first)

            (source / "authoritative.py").write_text("value = 2\n", encoding="utf-8")
            self.assertNotEqual(active_ref_for_paths(["source"], root=root), first)

    def test_manifest_rejects_missing_paths_and_stale_content_refs(self):
        missing_path_manifest = deepcopy(load_release_manifest())
        missing_path_plan = missing_path_manifest["components"][0]
        missing_path_plan["paths"] = ["missing-authority"]
        missing_problems = validate_release_manifest(missing_path_manifest)
        self.assertIn(
            f"{missing_path_plan['component']}:path_missing:missing-authority",
            missing_problems,
        )

        stale_ref_manifest = deepcopy(load_release_manifest())
        stale_ref_plan = stale_ref_manifest["components"][0]
        stale_ref_plan["active_ref"] = f"sha256:{'0' * 64}"
        stale_problems = validate_release_manifest(stale_ref_manifest)
        self.assertEqual(
            len(
                [
                    problem
                    for problem in stale_problems
                    if problem.startswith(
                        f"{stale_ref_plan['component']}:active_ref_mismatch:"
                    )
                ]
            ),
            1,
        )

    def test_manifest_rejects_symbolic_or_malformed_refs(self):
        manifest = deepcopy(load_release_manifest())
        plan = manifest["components"][0]
        plan["active_ref"] = "HEAD"
        plan["rollback_ref"] = "last_release"

        problems = validate_release_manifest(manifest)
        self.assertIn(f"{plan['component']}:active_ref_invalid", problems)
        self.assertIn(f"{plan['component']}:rollback_ref_invalid", problems)

    def test_manifest_rejects_unknown_components(self):
        manifest = deepcopy(load_release_manifest())
        plan = next(
            item
            for item in manifest["components"]
            if item["component"] == "conversation_and_plan_authority"
        )
        plan["component"] = "legacy_planner"

        self.assertEqual(
            validate_release_manifest(manifest),
            [
                "missing_components:conversation_and_plan_authority",
                "unknown_components:legacy_planner",
            ],
        )

    def test_manifest_rejects_duplicate_components(self):
        manifest = deepcopy(load_release_manifest())
        manifest["components"].append(deepcopy(manifest["components"][0]))

        self.assertEqual(
            validate_release_manifest(manifest),
            ["duplicate_component:contracts"],
        )


if __name__ == "__main__":
    unittest.main()
