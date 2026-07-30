from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


VNEXT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNEXT_ROOT / "tools"))

from build_gate3_eval_corpus import (  # noqa: E402
    _expected_artifacts,
    _render,
)
from compile_gate3_eval_views import compile_views  # noqa: E402
from validate_gate3_eval_catalog import (  # noqa: E402
    _policy_is_monotonic,
    canonical_sha256,
    counterfactual_materialization_core,
    episode_core,
    validate_catalog,
    validate_counterfactual_materialization,
)
from validate_gate3_eval_result import validate_result  # noqa: E402
from verify_gate3_e0 import (  # noqa: E402
    TRUST_ARTIFACT_PATHS,
    _authority_roots,
    _has_valid_predecessor,
    _manifest_authorized,
    _source_binding_status,
    _valid_reviews,
    compute_readiness,
)


EVAL_ROOT = VNEXT_ROOT / "evals" / "gate3"
CATALOG_PATH = EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
LEDGER_PATH = EVAL_ROOT / "coverage-ledger.json"
CANDIDATE_ROOT = EVAL_ROOT / "candidates"
CORPUS_PATH = EVAL_ROOT / "registries" / "corpus-registry.json"
SOURCE_REGISTRY_PATH = EVAL_ROOT / "registries" / "source-registry.json"
REVIEW_REGISTRY_PATH = EVAL_ROOT / "registries" / "review-registry.json"
TRUST_SCHEMA_PATH = EVAL_ROOT / "gate3-e0-trust.schema.json"
READINESS_PATH = EVAL_ROOT / "gate3-e0-readiness.json"
REVIEW_PACKAGES_PATH = EVAL_ROOT / "promotion" / "review-packages.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_temp_catalog(catalog: dict) -> Path:
    path = Path(tempfile.mkdtemp()) / "catalog.json"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    return path


def _passing_result() -> tuple[dict, dict]:
    artifact_hashes = {
        "product_behavior": "a" * 64,
        "authority_conformance": "b" * 64,
        "implementation": "c" * 64,
    }
    world_profile = {"profile_id": "WORLD-PROFILE-TEST"}
    authority_profile = {
        "profile_id": "AUTHORITY-PROFILE-TEST",
        "required_invariant_ids": ["authority_check"],
    }
    grader_registry = {
        "profiles": [
            {
                "profile_id": "GRADER-PRODUCT-TEST",
                "required_predicate_ids": ["product_check"],
            }
        ],
        "implementation_checks": [{"check_id": "implementation_check"}],
    }
    run_cell = {
        "run_cell_id": "CELL-001",
        "episode_id": "G3-TEST-001",
        "episode_core_sha256": "d" * 64,
        "world_profile_ref": world_profile["profile_id"],
        "world_profile_sha256": canonical_sha256(world_profile),
        "authority_profile_ref": authority_profile["profile_id"],
        "authority_profile_sha256": canonical_sha256(authority_profile),
        "product_grader_profile_ref": "GRADER-PRODUCT-TEST",
    }
    run_manifest = {
        "status": "frozen",
        "run_cells": [run_cell],
    }
    authority = {
        "run_manifest": run_manifest,
        "grader_registry": grader_registry,
        "authority_profiles": {"profiles": [authority_profile]},
        "world_profiles": {"profiles": [world_profile]},
        "artifact_index": {
            run_cell["run_cell_id"]: {
                layer: [digest]
                for layer, digest in artifact_hashes.items()
            }
        },
    }
    result = {
        "result_version": "gate3.eval-result.v1",
        "run_cell_id": run_cell["run_cell_id"],
        "run_manifest_sha256": canonical_sha256(run_manifest),
        "episode_id": run_cell["episode_id"],
        "episode_core_sha256": run_cell["episode_core_sha256"],
        "world_profile_ref": run_cell["world_profile_ref"],
        "world_profile_sha256": run_cell["world_profile_sha256"],
        "authority_profile_ref": run_cell["authority_profile_ref"],
        "authority_profile_sha256": run_cell[
            "authority_profile_sha256"
        ],
        "product_grader_profile_ref": run_cell[
            "product_grader_profile_ref"
        ],
        "layer_results": {
            "product_behavior": {
                "verdict": "pass",
                "check_results": [
                    {"check_id": "product_check", "verdict": "pass"}
                ],
                "artifact_sha256s": [
                    artifact_hashes["product_behavior"]
                ],
            },
            "authority_conformance": {
                "verdict": "pass",
                "check_results": [
                    {"check_id": "authority_check", "verdict": "pass"}
                ],
                "artifact_sha256s": [
                    artifact_hashes["authority_conformance"]
                ],
            },
            "implementation": {
                "verdict": "pass",
                "check_results": [
                    {
                        "check_id": "implementation_check",
                        "verdict": "pass",
                    }
                ],
                "artifact_sha256s": [
                    artifact_hashes["implementation"]
                ],
            },
        },
        "critical_vetoes": [],
        "artifact_completeness": True,
        "leakage_detected": False,
        "derived_final_verdict": "pass",
    }
    return result, authority


class Gate3EvaluationAuthoringTests(unittest.TestCase):
    def test_every_authoring_catalog_satisfies_v2_contract(self) -> None:
        paths = sorted(CANDIDATE_ROOT.glob("*.json")) + [CATALOG_PATH]
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            with self.subTest(path=path.name):
                findings, report = validate_catalog(
                    path, require_policy_ready=False
                )
                self.assertEqual([], findings)
                self.assertGreater(report["episode_count"], 0)

    def test_catalog_is_exact_full_content_union(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        expected = [
            episode
            for path in sorted(CANDIDATE_ROOT.glob("*.json"))
            for episode in _load_json(path)["episodes"]
        ]
        self.assertEqual(expected, catalog["episodes"])
        ids = [episode["episode_id"] for episode in expected]
        self.assertEqual(len(ids), len(set(ids)))

    def test_generated_corpus_and_review_packages_are_fresh(self) -> None:
        for path, expected in _expected_artifacts().items():
            with self.subTest(path=path.name):
                self.assertTrue(path.exists())
                self.assertEqual(_render(expected), path.read_text(encoding="utf-8"))
        self.assertEqual(
            45, len(_load_json(REVIEW_PACKAGES_PATH)["packages"])
        )
        self.assertFalse(
            any(
                CANDIDATE_ROOT == path.parent
                for path in _expected_artifacts()
            )
        )

    def test_coverage_ledger_is_authoring_only(self) -> None:
        findings, report = validate_catalog(
            CATALOG_PATH, require_policy_ready=False
        )
        self.assertEqual([], findings)
        self.assertEqual(report, _load_json(LEDGER_PATH))
        self.assertEqual(45, report["episode_count"])
        self.assertFalse(report["promotion_ready"])
        self.assertEqual("verify_gate3_e0.py", report["promotion_authority"])

    def test_episode_cannot_self_assign_review_partition_or_grader(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        episode["dataset_partition"] = "development"
        episode["review_status"] = "fully_reviewed"
        episode["grading"] = {"deterministic_checks": ["pass"]}
        path = _write_temp_catalog(mutated)
        findings, _ = validate_catalog(path, require_policy_ready=False)
        self.assertTrue(
            any("Additional properties are not allowed" in finding for finding in findings)
        )

    def test_supported_case_cannot_escape_to_boundary(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["support_expectation"]["contract_supported"]
        )
        episode["support_expectation"][
            "required_disposition"
        ] = "typed_boundary"
        path = _write_temp_catalog(mutated)
        findings, _ = validate_catalog(path, require_policy_ready=False)
        self.assertTrue(
            any(
                "contract_supported baseline must require executable_design"
                in finding
                for finding in findings
            )
        )

    def test_boundary_requires_exact_codes_and_world_facts(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["support_expectation"]["boundary_authorizations"]
        )
        episode["support_expectation"]["boundary_authorizations"][0][
            "boundary_code"
        ] = "forged_boundary"
        episode["support_expectation"]["boundary_authorizations"][0][
            "allowed_when_refs"
        ] = ["DC-NOT-IN-WORLD"]
        path = _write_temp_catalog(mutated)
        findings, _ = validate_catalog(path, require_policy_ready=False)
        self.assertTrue(
            any("boundary authorization mismatch" in finding for finding in findings)
        )
        self.assertTrue(
            any("cites unknown fact" in finding for finding in findings)
        )

    def test_boundary_ceiling_cannot_exceed_outcome_ceiling(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["support_expectation"]["boundary_authorizations"]
        )
        episode["acceptable_outcome"]["claim_ceiling"] = "descriptive"
        episode["support_expectation"]["boundary_authorizations"][0][
            "maximum_claim_ceiling"
        ] = "causal"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("ceiling exceeds outcome ceiling" in item for item in findings)
        )

    def test_boundary_ceiling_is_checked_per_claim_target(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["support_expectation"]["boundary_authorizations"]
        )
        episode["acceptable_outcome"]["claim_ceiling"] = "causal"
        episode["acceptable_outcome"]["estimands"] = [
            {
                "estimand_id": "strong_effect",
                "target_description": "Identifiable intervention effect",
            },
            {
                "estimand_id": "weak_pattern",
                "target_description": "Descriptive pattern",
            },
        ]
        episode["acceptable_outcome"]["claim_targets"] = [
            {
                "claim_target_id": "strong_claim",
                "estimand_id": "strong_effect",
                "target_description": "Causal claim",
                "claim_ceiling": "causal",
            },
            {
                "claim_target_id": "weak_claim",
                "estimand_id": "weak_pattern",
                "target_description": "Descriptive claim",
                "claim_ceiling": "descriptive",
            },
        ]
        if (
            "multi_estimand"
            not in episode["coverage_tags"]["measurement_challenges"]
        ):
            episode["coverage_tags"]["measurement_challenges"].append(
                "multi_estimand"
            )
        for authorization in episode["support_expectation"][
            "boundary_authorizations"
        ]:
            authorization["claim_target_ids"] = ["weak_claim"]
            authorization["maximum_claim_ceiling"] = "causal"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("ceiling exceeds claim target weak_claim" in item for item in findings)
        )

    def test_estimand_claim_targets_require_exact_bidirectional_cover(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        episode["acceptable_outcome"]["estimands"] = [
            {
                "estimand_id": "logo_retention",
                "target_description": "Logo retention",
            },
            {
                "estimand_id": "dollar_retention",
                "target_description": "Dollar retention",
            },
        ]
        episode["acceptable_outcome"]["claim_targets"] = [
            {
                "claim_target_id": "logo_claim",
                "estimand_id": "logo_retention",
                "target_description": "Logo retention claim",
                "claim_ceiling": "descriptive",
            }
        ]
        if (
            "multi_estimand"
            not in episode["coverage_tags"]["measurement_challenges"]
        ):
            episode["coverage_tags"]["measurement_challenges"].append(
                "multi_estimand"
            )
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("exactly cover estimands" in item for item in findings)
        )

    def test_reviewed_truth_requires_world_support(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        truth = mutated["episodes"][0]["business_world"]["truth_facts"][0]
        truth["identifiability"] = "identifiable_from_world"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("lacks valid identification support" in item for item in findings)
        )

    def test_evaluator_only_fact_cannot_make_truth_identifiable(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        condition = episode["business_world"]["data_conditions"][0]
        condition["discoverability"] = "evaluator_only"
        truth = episode["business_world"]["truth_facts"][0]
        truth["identifiability"] = "identifiable_from_world"
        truth["support_refs"] = [condition["condition_id"]]
        truth["identification_basis"] = "Evaluator-only condition"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("lacks valid identification support" in item for item in findings)
        )

    def test_unprojected_condition_cannot_identify_truth(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        condition = episode["business_world"]["data_conditions"][0]
        condition[
            "discoverability"
        ] = "discoverable_by_semantic_inspection"
        truth = episode["business_world"]["truth_facts"][0]
        truth["identifiability"] = "identifiable_from_world"
        truth["support_refs"] = [condition["condition_id"]]
        truth["identification_basis"] = "Unprojected condition"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("lacks valid identification support" in item for item in findings)
        )

    def test_missing_contract_cannot_identify_truth(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        contract = episode["business_world"]["available_contracts"][0]
        contract["state"] = "missing"
        contract[
            "discoverability"
        ] = "discoverable_by_semantic_inspection"
        truth = episode["business_world"]["truth_facts"][0]
        truth["identifiability"] = "identifiable_from_world"
        truth["support_refs"] = [contract["contract_ref"]]
        truth["identification_basis"] = "Missing contract"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("lacks valid identification support" in item for item in findings)
        )

    def test_world_refs_must_be_globally_unique(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        episode["business_world"]["available_contracts"][0][
            "contract_ref"
        ] = episode["business_world"]["data_conditions"][0][
            "condition_id"
        ]
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("refs must be globally unique" in item for item in findings)
        )

    def test_per_claim_ceiling_cannot_exceed_episode_ceiling(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        episode["acceptable_outcome"]["claim_ceiling"] = "descriptive"
        episode["acceptable_outcome"]["estimands"] = [
            {
                "estimand_id": "revenue_change",
                "target_description": "Revenue change",
            }
        ]
        episode["acceptable_outcome"]["claim_targets"] = [
            {
                "claim_target_id": "causal_driver",
                "estimand_id": "revenue_change",
                "target_description": "Causal driver",
                "claim_ceiling": "causal",
            }
        ]
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("per-claim ceiling exceeds" in item for item in findings)
        )

    def test_executable_counterfactual_is_replayed_before_acceptance(self) -> None:
        episode = copy.deepcopy(_load_json(CATALOG_PATH)["episodes"][0])
        sibling = episode["counterfactual_siblings"][0]
        operation = sibling["mutation_operation"]
        before = episode["user_episode"]["messages"][0]["text"]
        after = before + "（同义改写）"
        operation.update(
            {
                "path": "/user_episode/messages/0/text",
                "semantic_intervention_id": "wording_paraphrase",
                "before": before,
                "after": after,
                "execution_status": "executable_verified",
                "materialized_sibling_sha256": "0" * 64,
            }
        )
        materialized = copy.deepcopy(episode)
        materialized["user_episode"]["messages"][0]["text"] = after
        operation["materialized_sibling_sha256"] = canonical_sha256(
            counterfactual_materialization_core(materialized)
        )
        self.assertEqual(
            [], validate_counterfactual_materialization(episode, sibling)
        )
        operation["before"] = None
        operation["materialized_sibling_sha256"] = "f" * 64
        self.assertTrue(
            validate_counterfactual_materialization(episode, sibling)
        )

    def test_materialized_counterfactual_must_remain_schema_valid(
        self,
    ) -> None:
        episode = copy.deepcopy(_load_json(CATALOG_PATH)["episodes"][0])
        sibling = episode["counterfactual_siblings"][0]
        sibling["mutation_dimension"] = "claim_strength_request"
        operation = sibling["mutation_operation"]
        before = episode["acceptable_outcome"]["claim_ceiling"]
        operation.update(
            {
                "path": "/acceptable_outcome/claim_ceiling",
                "semantic_intervention_id": "invalid_claim_ceiling",
                "before": before,
                "after": "not_a_valid_ceiling",
                "execution_status": "executable_verified",
                "materialized_sibling_sha256": "0" * 64,
            }
        )
        materialized = copy.deepcopy(episode)
        materialized["acceptable_outcome"][
            "claim_ceiling"
        ] = "not_a_valid_ceiling"
        operation["materialized_sibling_sha256"] = canonical_sha256(
            counterfactual_materialization_core(materialized)
        )
        self.assertTrue(
            any(
                "materialized Episode is invalid" in finding
                for finding in validate_counterfactual_materialization(
                    episode, sibling
                )
            )
        )

    def test_counterfactual_dimension_must_match_authority_path(
        self,
    ) -> None:
        episode = copy.deepcopy(_load_json(CATALOG_PATH)["episodes"][0])
        sibling = episode["counterfactual_siblings"][0]
        operation = sibling["mutation_operation"]
        operation.update(
            {
                "path": "/business_world/truth_facts/0/statement",
                "semantic_intervention_id": "misclassified_mutation",
                "before": episode["business_world"]["truth_facts"][0][
                    "statement"
                ],
                "after": "changed hidden truth",
                "execution_status": "executable_verified",
                "materialized_sibling_sha256": "0" * 64,
            }
        )
        self.assertTrue(
            any(
                "does not authorize path" in finding
                for finding in validate_counterfactual_materialization(
                    episode, sibling
                )
            )
        )
        sibling["mutation_dimension"] = "scope"
        operation.update(
            {
                "path": "/acceptable_outcome/claim_ceiling",
                "before": episode["acceptable_outcome"]["claim_ceiling"],
                "after": "causal",
            }
        )
        self.assertTrue(
            any(
                "does not authorize path" in finding
                for finding in validate_counterfactual_materialization(
                    episode, sibling
                )
            )
        )


class Gate3ProjectionIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        self.episode = copy.deepcopy(catalog["episodes"][0])
        self.entry = copy.deepcopy(corpus["entries"][0])

    def test_initial_agent_view_excludes_oracle_and_future_messages(self) -> None:
        bundle = compile_views(self.episode, self.entry, visible_turn=1)
        agent = bundle["agent_world_view"]
        rendered = json.dumps(agent, ensure_ascii=False)
        self.assertNotIn("truth_facts", rendered)
        self.assertNotIn("forbidden_outcomes", rendered)
        self.assertNotIn("decision_stakes", rendered)
        self.assertEqual(1, len(agent["injected_messages"]))
        if len(self.episode["user_episode"]["messages"]) > 1:
            self.assertNotIn(
                self.episode["user_episode"]["messages"][1]["text"], rendered
            )

    def test_random_oracle_canaries_do_not_leak(self) -> None:
        future_canary = "CANARY-FUTURE-99501"
        truth_canary = "CANARY-TRUTH-99502"
        title_canary = "CANARY-TITLE-99503"
        self.episode["title"] = title_canary
        self.episode["business_world"]["truth_facts"][0][
            "statement"
        ] = truth_canary
        if len(self.episode["user_episode"]["messages"]) == 1:
            self.episode["user_episode"]["messages"].append(
                {
                    "turn": 2,
                    "speaker": "user",
                    "text": future_canary,
                    "communication_function": "correction",
                    "trigger": {
                        "kind": "while_investigation_pending",
                        "fallback": "inject_when_observable_or_mark_unreached",
                    },
                }
            )
        else:
            self.episode["user_episode"]["messages"][1]["text"] = future_canary
        bundle = compile_views(self.episode, self.entry, visible_turn=1)
        rendered = json.dumps(bundle["agent_world_view"], ensure_ascii=False)
        self.assertNotIn(future_canary, rendered)
        self.assertNotIn(truth_canary, rendered)
        self.assertNotIn(title_canary, rendered)

    def test_evaluator_only_condition_is_not_discoverable(self) -> None:
        condition = self.episode["business_world"]["data_conditions"][0]
        condition["discoverability"] = "evaluator_only"
        bundle = compile_views(self.episode, self.entry)
        rendered = json.dumps(
            bundle["agent_world_view"]["inspection_surfaces"],
            ensure_ascii=False,
        )
        self.assertNotIn(condition["condition_id"], rendered)


class Gate3TrustAndVerdictTests(unittest.TestCase):
    def _source_binding_fixture(
        self, source_pool: str
    ) -> tuple[dict, dict, dict, dict]:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        source_registry = _load_json(SOURCE_REGISTRY_PATH)
        episode = next(
            item
            for item in catalog["episodes"]
            if item["source_pool"] == source_pool
        )
        entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == episode["episode_id"]
        )
        source = next(
            item
            for item in source_registry["records"]
            if item["source_record_id"] == entry["source_record_ref"]
        )
        policy = _load_json(EVAL_ROOT / "gate3-eval-policy.json")
        return episode, entry, source, policy

    def test_all_trust_artifacts_match_strict_schema(self) -> None:
        schema = _load_json(TRUST_SCHEMA_PATH)
        validator = Draft202012Validator(schema)
        for path in TRUST_ARTIFACT_PATHS:
            with self.subTest(path=path.name):
                self.assertEqual(
                    [], list(validator.iter_errors(_load_json(path)))
                )

    def test_same_reviewer_cannot_approve_both_roles(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = catalog["episodes"][0]
        entry = corpus["entries"][0]
        review_packages = _load_json(REVIEW_PACKAGES_PATH)
        package = next(
            item
            for item in review_packages["packages"]
            if item["episode_id"] == episode["episode_id"]
        )
        principal = {
            "principal_id": "REVIEWER-FORGED",
            "roles": ["business_owner", "measurement_reviewer"],
            "status": "active",
            "authority_root_ref": "ROOT-TEST-REVIEWER",
        }
        records = [
            {
                "review_record_id": "REVIEW-FORGED-BUSINESS",
                "episode_id": episode["episode_id"],
                "episode_core_sha256": entry["episode_core_sha256"],
                "principal_id": principal["principal_id"],
                "reviewer_role": role,
                "decision": "approved",
                "reviewed_scopes": (
                    package["business_review_scopes"]
                    if role == "business_owner"
                    else package["measurement_review_scopes"]
                ),
                "review_package_sha256": canonical_sha256(package),
            }
            for role in ("business_owner", "measurement_reviewer")
        ]
        approved, _, findings = _valid_reviews(
            {episode["episode_id"]: episode},
            {episode["episode_id"]: entry},
            {
                "principals": [principal],
                "records": records,
            },
            review_packages,
            {
                "ROOT-TEST-REVIEWER": {
                    "principal_id": principal["principal_id"],
                    "authorized_roles": principal["roles"],
                }
            },
            {canonical_sha256(record) for record in records},
        )
        self.assertTrue(findings)
        self.assertEqual(set(), approved[episode["episode_id"]])

    def test_stale_review_hash_is_rejected(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = catalog["episodes"][0]
        entry = corpus["entries"][0]
        review_packages = _load_json(REVIEW_PACKAGES_PATH)
        package = next(
            item
            for item in review_packages["packages"]
            if item["episode_id"] == episode["episode_id"]
        )
        principal = {
            "principal_id": "REVIEWER-MEASUREMENT",
            "roles": ["measurement_reviewer"],
            "status": "active",
            "authority_root_ref": "ROOT-TEST-MEASUREMENT",
        }
        approved, _, findings = _valid_reviews(
            {episode["episode_id"]: episode},
            {episode["episode_id"]: entry},
            {
                "principals": [principal],
                "records": [
                    {
                        "review_record_id": "REVIEW-STALE",
                        "episode_id": episode["episode_id"],
                        "episode_core_sha256": "f" * 64,
                        "principal_id": principal["principal_id"],
                        "reviewer_role": "measurement_reviewer",
                        "decision": "approved",
                        "reviewed_scopes": package[
                            "measurement_review_scopes"
                        ],
                        "review_package_sha256": canonical_sha256(package),
                    }
                ],
            },
            review_packages,
            {
                "ROOT-TEST-MEASUREMENT": {
                    "principal_id": principal["principal_id"],
                    "authorized_roles": principal["roles"],
                }
            },
        )
        self.assertTrue(any("stale Episode" in finding for finding in findings))
        self.assertEqual({}, approved)

    def test_self_registered_reviewer_has_no_authority(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        review_packages = _load_json(REVIEW_PACKAGES_PATH)
        episode = catalog["episodes"][0]
        entry = corpus["entries"][0]
        package = next(
            item
            for item in review_packages["packages"]
            if item["episode_id"] == episode["episode_id"]
        )
        principal = {
            "principal_id": "REVIEWER-SELF-REGISTERED",
            "roles": ["business_owner"],
            "status": "active",
            "authority_root_ref": "ROOT-SELF-ASSERTED",
        }
        _, valid_ids, findings = _valid_reviews(
            {episode["episode_id"]: episode},
            {episode["episode_id"]: entry},
            {
                "principals": [principal],
                "records": [
                    {
                        "review_record_id": "REVIEW-SELF-ASSERTED",
                        "episode_id": episode["episode_id"],
                        "episode_core_sha256": entry[
                            "episode_core_sha256"
                        ],
                        "principal_id": principal["principal_id"],
                        "reviewer_role": "business_owner",
                        "decision": "approved",
                        "reviewed_scopes": package[
                            "business_review_scopes"
                        ],
                        "review_package_sha256": canonical_sha256(package),
                    }
                ],
            },
            review_packages,
            {},
        )
        self.assertEqual(set(), valid_ids)
        self.assertTrue(
            any("trusted authority root" in item for item in findings)
        )

    def test_local_review_record_needs_protected_external_attestation(
        self,
    ) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        review_packages = _load_json(REVIEW_PACKAGES_PATH)
        episode = catalog["episodes"][0]
        entry = corpus["entries"][0]
        package = next(
            item
            for item in review_packages["packages"]
            if item["episode_id"] == episode["episode_id"]
        )
        principal = {
            "principal_id": "REVIEWER-LOCAL",
            "roles": ["business_owner"],
            "status": "active",
            "authority_root_ref": "ROOT-LOCAL",
        }
        record = {
            "review_record_id": "REVIEW-LOCAL",
            "episode_id": episode["episode_id"],
            "episode_core_sha256": entry["episode_core_sha256"],
            "principal_id": principal["principal_id"],
            "reviewer_role": "business_owner",
            "decision": "approved",
            "reviewed_scopes": package["business_review_scopes"],
            "review_package_sha256": canonical_sha256(package),
        }
        _, valid_ids, findings = _valid_reviews(
            {episode["episode_id"]: episode},
            {episode["episode_id"]: entry},
            {"principals": [principal], "records": [record]},
            review_packages,
            {
                "ROOT-LOCAL": {
                    "principal_id": principal["principal_id"],
                    "authorized_roles": principal["roles"],
                }
            },
            set(),
        )
        self.assertEqual(set(), valid_ids)
        self.assertTrue(
            any("protected external attestation" in item for item in findings)
        )

    def test_stale_source_artifact_hash_is_rejected(self) -> None:
        episode, entry, source, policy = self._source_binding_fixture(
            "generated_business_world"
        )
        with tempfile.TemporaryDirectory() as temp_root:
            workspace_root = Path(temp_root)
            artifact_path = workspace_root / "source.md"
            artifact_path.write_text("trusted source\n", encoding="utf-8")
            attacked_source = copy.deepcopy(source)
            attacked_source["source_artifacts"] = [
                {
                    "ref": "source.md",
                    "content_sha256": hashlib.sha256(
                        artifact_path.read_bytes()
                    ).hexdigest(),
                }
            ]
            artifact_path.write_text("tampered source\n", encoding="utf-8")
            _, _, _, findings = _source_binding_status(
                {episode["episode_id"]: episode},
                {entry["episode_id"]: entry},
                {"records": [attacked_source]},
                policy,
                workspace_root=workspace_root,
            )
        self.assertTrue(
            any("source artifact hash is stale" in finding for finding in findings)
        )

    def test_source_pool_relabel_is_rejected(self) -> None:
        episode, entry, source, policy = self._source_binding_fixture(
            "generated_business_world"
        )
        attacked_source = copy.deepcopy(source)
        attacked_source["source_pool"] = "historical_failure"
        _, _, _, findings = _source_binding_status(
            {episode["episode_id"]: episode},
            {entry["episode_id"]: entry},
            {"records": [attacked_source]},
            policy,
        )
        self.assertTrue(
            any("source pool does not match registry" in finding for finding in findings)
        )

    def test_verified_source_requires_pool_specific_authority(self) -> None:
        episode, entry, source, policy = self._source_binding_fixture(
            "generated_business_world"
        )
        attacked_source = copy.deepcopy(source)
        attacked_source["provenance_kind"] = "expert_authorship"
        attacked_source["attested_scope"] = ["business_world"]
        _, _, _, findings = _source_binding_status(
            {episode["episode_id"]: episode},
            {entry["episode_id"]: entry},
            {"records": [attacked_source]},
            policy,
        )
        self.assertTrue(
            any("invalid provenance kind" in finding for finding in findings)
        )
        self.assertTrue(
            any("lacks required attested scope" in finding for finding in findings)
        )
        self.assertTrue(
            any("trusted source authority root" in finding for finding in findings)
        )

    def test_verified_source_needs_protected_external_attestation(
        self,
    ) -> None:
        episode, entry, source, policy = self._source_binding_fixture(
            "generated_business_world"
        )
        attacked_source = copy.deepcopy(source)
        attacked_source["verification_authority_ref"] = "ROOT-SOURCE-LOCAL"
        _, _, _, findings = _source_binding_status(
            {episode["episode_id"]: episode},
            {entry["episode_id"]: entry},
            {"records": [attacked_source]},
            policy,
            {
                "ROOT-SOURCE-LOCAL": {
                    "authorized_source_pools": [
                        "generated_business_world"
                    ]
                }
            },
            authorized_attestation_sha256s=set(),
        )
        self.assertTrue(
            any("protected external attestation" in item for item in findings)
        )

    def test_local_authority_receipt_cannot_create_a_trust_root(self) -> None:
        policy = _load_json(EVAL_ROOT / "gate3-eval-policy.json")
        attacked = copy.deepcopy(policy)
        with tempfile.TemporaryDirectory() as temp_root:
            workspace_root = Path(temp_root)
            receipt = workspace_root / "receipt.json"
            receipt.write_text('{"self":"signed"}\\n', encoding="utf-8")
            attacked["corpus_authority"]["source_authority_roots"] = [
                {
                    "authority_root_id": "ROOT-LOCAL-SOURCE",
                    "receipt_ref": "receipt.json",
                    "receipt_sha256": hashlib.sha256(
                        receipt.read_bytes()
                    ).hexdigest(),
                    "authorized_source_pools": [
                        "generated_business_world"
                    ],
                }
            ]
            roots, findings = _authority_roots(
                attacked, workspace_root=workspace_root
            )
            self.assertEqual({}, roots["source_authority_roots"])
            self.assertTrue(
                any("external admission verifier" in item for item in findings)
            )

    def test_manifest_and_predecessor_require_external_authorization(
        self,
    ) -> None:
        trust_schema = _load_json(TRUST_SCHEMA_PATH)
        root = {
            "ROOT-MANIFEST": {
                "authorized_artifact_types": ["promotion_manifest"]
            }
        }
        predecessor = copy.deepcopy(
            _load_json(
                EVAL_ROOT / "manifests" / "promotion-manifest.json"
            )
        )
        predecessor["authority_root_ref"] = "ROOT-MANIFEST"
        predecessor_hash = canonical_sha256(predecessor)
        with tempfile.TemporaryDirectory() as temp_root:
            workspace_root = Path(temp_root)
            predecessor_path = workspace_root / "promotion-v1.json"
            predecessor_path.write_text(
                json.dumps(predecessor), encoding="utf-8"
            )
            current = copy.deepcopy(predecessor)
            current["registry_epoch"] = 2
            current["status"] = "approved"
            current["authority_history"] = {
                "predecessor_ref": "promotion-v1.json",
                "predecessor_sha256": predecessor_hash,
            }
            current_hash = canonical_sha256(current)
            self.assertFalse(
                _manifest_authorized(current, root, set())
            )
            self.assertFalse(
                _has_valid_predecessor(
                    current,
                    authorized_manifest_sha256s={current_hash},
                    trust_schema=trust_schema,
                    workspace_root=workspace_root,
                )
            )
            authorized = {predecessor_hash, current_hash}
            self.assertTrue(
                _manifest_authorized(current, root, authorized)
            )
            self.assertTrue(
                _has_valid_predecessor(
                    current,
                    authorized_manifest_sha256s=authorized,
                    trust_schema=trust_schema,
                    workspace_root=workspace_root,
                )
            )
            successor_path = workspace_root / "promotion-v2.json"
            successor_path.write_text(
                json.dumps(current), encoding="utf-8"
            )
            successor = copy.deepcopy(current)
            successor["registry_epoch"] = 3
            successor["authority_history"] = {
                "predecessor_ref": "promotion-v2.json",
                "predecessor_sha256": current_hash,
            }
            successor_hash = canonical_sha256(successor)
            authorized.add(successor_hash)
            self.assertTrue(
                _has_valid_predecessor(
                    successor,
                    authorized_manifest_sha256s=authorized,
                    trust_schema=trust_schema,
                    workspace_root=workspace_root,
                )
            )
            rollback = copy.deepcopy(successor)
            rollback["status"] = "draft"
            rollback_hash = canonical_sha256(rollback)
            self.assertFalse(
                _has_valid_predecessor(
                    rollback,
                    authorized_manifest_sha256s=authorized
                    | {rollback_hash},
                    trust_schema=trust_schema,
                    workspace_root=workspace_root,
                )
            )
            epoch_jump = copy.deepcopy(successor)
            epoch_jump["registry_epoch"] = 4
            epoch_jump_hash = canonical_sha256(epoch_jump)
            self.assertFalse(
                _has_valid_predecessor(
                    epoch_jump,
                    authorized_manifest_sha256s=authorized
                    | {epoch_jump_hash},
                    trust_schema=trust_schema,
                    workspace_root=workspace_root,
                )
            )
            root_change = copy.deepcopy(successor)
            root_change["authority_root_ref"] = "ROOT-OTHER"
            root_change_hash = canonical_sha256(root_change)
            self.assertFalse(
                _has_valid_predecessor(
                    root_change,
                    authorized_manifest_sha256s=authorized
                    | {root_change_hash},
                    trust_schema=trust_schema,
                    workspace_root=workspace_root,
                )
            )
            predecessor_path.unlink()
            self.assertFalse(
                _has_valid_predecessor(
                    successor,
                    authorized_manifest_sha256s=authorized,
                    trust_schema=trust_schema,
                    workspace_root=workspace_root,
                )
            )

    def test_parent_policy_snapshot_and_hash_are_pinned(self) -> None:
        policy = _load_json(EVAL_ROOT / "gate3-eval-policy.json")
        attacked = copy.deepcopy(policy)
        attacked["parent_policy"]["canonical_sha256"] = "0" * 64
        attacked["parent_policy"]["minimum_catalog_snapshot"][
            "reviewed_base_episodes"
        ] = 1
        attacked["minimum_catalog"]["reviewed_base_episodes"] = 1
        findings = _policy_is_monotonic(attacked)
        self.assertTrue(any("pinned v1" in item for item in findings))

    def test_real_user_wording_must_exist_in_bound_source(self) -> None:
        episode, entry, source, policy = self._source_binding_fixture(
            "real_user_language"
        )
        with tempfile.TemporaryDirectory() as temp_root:
            workspace_root = Path(temp_root)
            artifact_path = workspace_root / "source.md"
            artifact_path.write_text(
                "Unrelated but authentic user material.\n", encoding="utf-8"
            )
            attacked_source = copy.deepcopy(source)
            attacked_source["source_artifacts"] = [
                {
                    "ref": "source.md",
                    "content_sha256": hashlib.sha256(
                        artifact_path.read_bytes()
                    ).hexdigest(),
                }
            ]
            _, _, _, findings = _source_binding_status(
                {episode["episode_id"]: episode},
                {entry["episode_id"]: entry},
                {"records": [attacked_source]},
                policy,
                workspace_root=workspace_root,
            )
        self.assertTrue(
            any("user wording is absent" in finding for finding in findings)
        )

    def test_three_layer_result_uses_strict_and(self) -> None:
        passing, authority = _passing_result()
        self.assertEqual([], validate_result(passing, authority=authority))
        attacked = copy.deepcopy(passing)
        attacked["layer_results"]["product_behavior"]["verdict"] = "fail"
        attacked["layer_results"]["product_behavior"]["check_results"][0][
            "verdict"
        ] = "fail"
        self.assertTrue(validate_result(attacked, authority=authority))

    def test_leakage_invalidates_a_claimed_pass(self) -> None:
        attacked, authority = _passing_result()
        attacked["leakage_detected"] = True
        self.assertTrue(
            any(
                "must be invalid" in finding
                for finding in validate_result(attacked, authority=authority)
            )
        )

    def test_child_failure_cannot_be_hidden_by_layer_pass(self) -> None:
        attacked, authority = _passing_result()
        attacked["layer_results"]["implementation"]["check_results"][0][
            "verdict"
        ] = "fail"
        self.assertTrue(
            any(
                "cannot pass" in finding
                for finding in validate_result(attacked, authority=authority)
            )
        )

    def test_result_rejects_forged_manifest_hash_and_dummy_checks(self) -> None:
        attacked, authority = _passing_result()
        attacked["run_manifest_sha256"] = "0" * 64
        attacked["layer_results"]["product_behavior"]["check_results"] = [
            {"check_id": "dummy", "verdict": "pass"}
        ]
        findings = validate_result(attacked, authority=authority)
        self.assertTrue(any("canonical manifest" in item for item in findings))
        self.assertTrue(any("check set" in item for item in findings))

    def test_passing_result_requires_runner_artifact_index(self) -> None:
        passing, authority = _passing_result()
        authority["artifact_index"] = None
        self.assertTrue(
            any(
                "runner-verified artifact index" in item
                for item in validate_result(passing, authority=authority)
            )
        )

    def test_readiness_is_derived_and_remains_blocked(self) -> None:
        readiness, findings = compute_readiness()
        self.assertEqual([], findings)
        self.assertEqual(readiness, _load_json(READINESS_PATH))
        self.assertEqual("blocked", readiness["derived_status"])
        self.assertEqual("deny_g3_1", readiness["entry_decision"])
        blocked_ids = {
            condition["condition_id"]
            for condition in readiness["condition_verdicts"]
            if condition["verdict"] == "blocked"
        }
        self.assertIn("external_admission_verified", blocked_ids)
        self.assertIn("source_registry_verified", blocked_ids)
        self.assertIn("independent_reviews_complete", blocked_ids)
        self.assertIn("truth_identifiability_reviewed", blocked_ids)
        self.assertIn("per_claim_ceiling_complete", blocked_ids)
        self.assertIn("counterfactual_mutations_executable", blocked_ids)
        self.assertIn("grader_calibrated", blocked_ids)
        self.assertIn("held_out_partition_sealed", blocked_ids)

    def test_episode_core_hash_excludes_authoring_provenance(self) -> None:
        episode = _load_json(CATALOG_PATH)["episodes"][0]
        core_hash = canonical_sha256(episode_core(episode))
        mutated = copy.deepcopy(episode)
        mutated["provenance"]["origin_note"] = "different authoring note"
        self.assertEqual(core_hash, canonical_sha256(episode_core(mutated)))

    def test_episode_core_hash_covers_source_and_coverage_authority(self) -> None:
        episode = _load_json(CATALOG_PATH)["episodes"][0]
        core_hash = canonical_sha256(episode_core(episode))
        for mutate in (
            lambda value: value.__setitem__(
                "source_pool", "real_user_language"
            ),
            lambda value: value["coverage_tags"][
                "decision_goals"
            ].append("forged_goal"),
            lambda value: value["provenance"].__setitem__(
                "source_record_ref", "SRC-FORGED"
            ),
        ):
            attacked = copy.deepcopy(episode)
            mutate(attacked)
            self.assertNotEqual(
                core_hash, canonical_sha256(episode_core(attacked))
            )


if __name__ == "__main__":
    unittest.main()
