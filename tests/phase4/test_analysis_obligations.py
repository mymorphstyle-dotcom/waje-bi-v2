from copy import deepcopy
import unittest

from bi_agent.runtime.analysis_obligations import (
    ObligationRequest,
    resolve_analysis_obligations,
)
from bi_agent.runtime.capability_registry import (
    get_capability_card,
    public_capability_ids,
)
from bi_agent.runtime.contracts import load_contract
from bi_agent.runtime.recipe_registry import load_recipe_registry
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


class AnalysisObligationsTest(unittest.TestCase):
    def test_obligation_registry_covers_every_recipe_and_uses_public_capabilities(self):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        recipes = load_recipe_registry()
        self.assertEqual(set(registry.question_family_ids), set(recipes))
        referenced = {
            capability
            for family in registry.question_family_ids
            for field in ("required_capabilities", "independent_capabilities")
            for capability in registry.question_family_obligation(family)[field]
        }
        self.assertTrue(referenced.issubset(set(public_capability_ids())))
        for family in registry.question_family_ids:
            contract = registry.question_family_obligation(family)
            for capability in (
                *contract["required_capabilities"],
                *contract["independent_capabilities"],
                *(item for rule in contract["conditional_rules"] for item in rule["add"]),
            ):
                self.assertIn(family, get_capability_card(capability).supported_question_families)

    def test_obligation_registry_covers_every_public_capability(self):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        referenced = set()
        for family in registry.question_family_ids:
            contract = registry.question_family_obligation(family)
            referenced.update(contract["required_capabilities"])
            referenced.update(contract["independent_capabilities"])
            for rule in contract["conditional_rules"]:
                referenced.update(rule["add"])
        payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
        for contract in payload["diagnostic_obligations"].values():
            referenced.update(contract["required_capabilities"])
        self.assertEqual(referenced, set(public_capability_ids()))

    def test_resolver_adds_contract_required_and_conditional_capabilities(self):
        result = resolve_analysis_obligations(
            ObligationRequest(
                question_families=("segment_or_factor_attribution",),
                diagnostic_tags=("factor_topk",),
                target_metrics=("paid_amount",),
                requested_dimensions=("channel",),
                baselines=("previous_day",),
                context_sources=(),
                claim_intents=("segment_contribution_or_mix_shift",),
            ),
            RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH),
        )
        self.assertEqual(
            result.required_capabilities,
            ("data_quality_profile", "segment_contribution", "joint_attribution", "answer_verify"),
        )
        self.assertIn("market_channel_context", result.conditional_capabilities)
        self.assertEqual(
            tuple(item["capability"] for item in result.mutations),
            (*result.required_capabilities, *result.conditional_capabilities),
        )

    def test_from_intent_deduplicates_families_and_defaults_target_metric(self):
        request = ObligationRequest.from_intent(
            question_family="pattern_explanation",
            question_families=("pattern_explanation", "custom_baseline_comparison"),
            target_metric="paid_amount",
            bound_context={"analysis_requirements": {"baselines": ["previous_day"]}},
        )
        self.assertEqual(
            request.question_families,
            ("pattern_explanation", "custom_baseline_comparison"),
        )
        self.assertEqual(request.target_metrics, ("paid_amount",))
        self.assertEqual(request.baselines, ("previous_day",))

    def test_obligation_contract_rejects_eval_specific_keys(self):
        payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
        payload["question_family_obligations"]["paid_amount_change_explanation"][
            "case_id"
        ] = "fixed-eight"
        with self.assertRaisesRegex(ValueError, "runtime_obligation_eval_specific_key"):
            RuntimeContractRegistry(payload)

    def test_obligation_contract_rejects_unknown_references_and_conditions(self):
        payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
        unknown_capability = deepcopy(payload)
        unknown_capability["diagnostic_obligations"]["factor_topk"][
            "required_capabilities"
        ].append("unreviewed_capability")
        with self.assertRaisesRegex(ValueError, "runtime_obligation_unknown_capability"):
            RuntimeContractRegistry(unknown_capability)

        unknown_condition = deepcopy(payload)
        unknown_condition["diagnostic_obligations"]["factor_topk"][
            "condition"
        ] = "question_contains_fixed_sentence"
        with self.assertRaisesRegex(ValueError, "runtime_obligation_unknown_condition"):
            RuntimeContractRegistry(unknown_condition)

    def test_resolver_rejects_unknown_target_metric_reference(self):
        request = ObligationRequest(
            question_families=("pattern_explanation",),
            diagnostic_tags=(),
            target_metrics=("unreviewed_metric",),
            requested_dimensions=(),
            baselines=(),
            context_sources=(),
            claim_intents=(),
        )
        with self.assertRaisesRegex(ValueError, "unknown_obligation_target_metric"):
            resolve_analysis_obligations(
                request,
                RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH),
            )

    def test_registry_rejects_duplicate_empty_and_contradictory_capability_classes(self):
        payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
        mutations = {
            "duplicate": lambda item: item["required_capabilities"].append(
                item["required_capabilities"][0]
            ),
            "empty_required": lambda item: item.update(required_capabilities=[]),
            "required_independent_overlap": lambda item: item[
                "independent_capabilities"
            ].append(item["required_capabilities"][0]),
            "conditional_required_overlap": lambda item: item[
                "conditional_rules"
            ][0]["add"].append(item["required_capabilities"][0]),
            "conditional_independent_overlap": lambda item: item[
                "conditional_rules"
            ][0]["add"].append(item["independent_capabilities"][0]),
            "conditional_cross_rule_duplicate": lambda item: item[
                "conditional_rules"
            ][1]["add"].append(item["conditional_rules"][0]["add"][0]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = deepcopy(payload)
                mutate(changed["question_family_obligations"]["pattern_explanation"])
                with self.assertRaisesRegex(
                    ValueError,
                    "runtime_obligation_(capabilities_duplicate|capabilities_empty|classification_conflict)",
                ):
                    RuntimeContractRegistry(changed)

    def test_registry_rejects_invalid_publishability_and_degradation_contracts(self):
        payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
        mutations = {
            "empty_evidence": lambda item: item.update(minimum_publishable_evidence=[]),
            "duplicate_evidence": lambda item: item["minimum_publishable_evidence"].append(
                item["minimum_publishable_evidence"][0]
            ),
            "blank_evidence": lambda item: item.update(minimum_publishable_evidence=[""]),
            "blank_owner": lambda item: item.update(missing_contract_owner=""),
            "missing_degradation_key": lambda item: item.update(
                degradation_policy={"missing_required_input": "explicit_gap"}
            ),
            "extra_degradation_key": lambda item: item["degradation_policy"].update(
                unexpected="fallback"
            ),
            "blank_degradation_value": lambda item: item["degradation_policy"].update(
                missing_required_input=""
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = deepcopy(payload)
                mutate(changed["question_family_obligations"]["pattern_explanation"])
                with self.assertRaisesRegex(
                    ValueError,
                    "runtime_obligation_(evidence_invalid|owner_invalid|degradation_policy_invalid)",
                ):
                    RuntimeContractRegistry(changed)

    def test_order_capabilities_rejects_unknown_reference(self):
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        with self.assertRaisesRegex(ValueError, "runtime_obligation_unknown_capability"):
            registry.order_capabilities(("answer_verify", "unreviewed_capability"))

    def test_registry_rejects_incomplete_public_capability_coverage(self):
        payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
        payload["question_family_obligations"]["pattern_explanation"][
            "required_capabilities"
        ].remove("evidence_reduce")
        with self.assertRaisesRegex(ValueError, "runtime_obligation_capability_coverage"):
            RuntimeContractRegistry(payload)

    def test_registry_rejects_capability_unsupported_by_question_family(self):
        payload = load_contract(CANONICAL_RUNTIME_BINDINGS_PATH)
        payload["question_family_obligations"]["pattern_explanation"][
            "required_capabilities"
        ].append("market_health_compare")
        with self.assertRaisesRegex(ValueError, "runtime_obligation_unsupported_family"):
            RuntimeContractRegistry(payload)


if __name__ == "__main__":
    unittest.main()
