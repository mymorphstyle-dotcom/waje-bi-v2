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
    _load_json_strict,
    _render,
)
from compile_gate3_replacement_expectations import (  # noqa: E402
    ReplacementCompilationError,
    compile_catalog_replacement_expectations,
    derive_base_claim_replacement_expectation,
)
from compile_gate3_eval_views import (  # noqa: E402
    agent_materialized_source_refs,
    compile_views,
)
from materialize_gate3_controlled_case_files import (  # noqa: E402
    controlled_authority_ids,
    materialize as materialize_controlled_case_files,
)
from validate_gate3_eval_catalog import (  # noqa: E402
    _validate_episode_semantics,
    _validate_required_suite,
    canonical_sha256,
    counterfactual_materialization_core,
    episode_core,
    materialize_counterfactual_episode,
    replacement_expectation_content_core,
    validate_catalog,
    validate_counterfactual_materialization,
    validate_replacement_expectation,
)
from validate_gate3_eval_result import validate_result  # noqa: E402
from verify_gate3_e0 import (  # noqa: E402
    TRUST_ARTIFACT_PATHS,
    _authority_roots,
    _calibration_label_findings,
    _calibration_sample_findings,
    _case_file_readiness_gaps,
    _evaluator_profile_findings,
    _has_valid_predecessor,
    _held_out_chain_findings,
    _manifest_authorized,
    _source_binding_status,
    _validate_authority_package,
    _valid_calibration_reviews,
    _valid_reviews,
    case_file_authority_content_core,
    compute_readiness,
    expected_run_variant_keys,
)


EVAL_ROOT = VNEXT_ROOT / "evals" / "gate3"
CATALOG_PATH = EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
LEDGER_PATH = EVAL_ROOT / "coverage-ledger.json"
CANDIDATE_ROOT = EVAL_ROOT / "candidates"
CORPUS_PATH = EVAL_ROOT / "registries" / "corpus-registry.json"
SOURCE_REGISTRY_PATH = EVAL_ROOT / "registries" / "source-registry.json"
REVIEW_REGISTRY_PATH = EVAL_ROOT / "registries" / "review-registry.json"
TRUST_SCHEMA_PATH = EVAL_ROOT / "gate3-e0-trust.schema.json"
POLICY_PATH = EVAL_ROOT / "gate3-eval-policy.json"
POLICY_SCHEMA_PATH = EVAL_ROOT / "gate3-eval-policy.schema.json"
GRADER_REGISTRY_PATH = (
    EVAL_ROOT / "registries" / "grader-registry.json"
)
GRADER_RUBRIC_PATH = EVAL_ROOT / "grader-rubric.json"
READINESS_PATH = EVAL_ROOT / "gate3-e0-readiness.json"
REVIEW_PACKAGES_PATH = EVAL_ROOT / "promotion" / "review-packages.json"
CASE_FILE_AUTHORITY_SCHEMA_PATH = (
    EVAL_ROOT / "case-files" / "case-file-authority.schema.json"
)
CASE_FILE_AUTHORITIES_PATH = (
    EVAL_ROOT / "case-files" / "case-file-authorities.json"
)
CONTROLLED_BUSINESS_FIXTURE_SCHEMA_PATH = (
    EVAL_ROOT / "authority" / "controlled-business-fixture.schema.json"
)
REAL_SNAPSHOT_MATERIALIZATION_SCHEMA_PATH = (
    EVAL_ROOT / "authority" / "real-snapshot-materialization.schema.json"
)
AUTHORITY_FIXTURE_ROOT = EVAL_ROOT / "authority" / "fixtures"
LAUNCH_EPISODES_PATH = (
    CANDIDATE_ROOT / "wajegame_launch_question_episodes.json"
)
AUTHORITY_STRESS_EPISODES_PATH = (
    CANDIDATE_ROOT / "wajegame_authority_stress_episodes.json"
)
BUSINESS_CHAIN_EPISODES_PATH = (
    CANDIDATE_ROOT / "wajegame_business_chain_episodes.json"
)
MEASUREMENT_REGRESSION_EPISODES_PATH = (
    CANDIDATE_ROOT / "wajegame_measurement_regression_episodes.json"
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_temp_catalog(catalog: dict) -> Path:
    path = Path(tempfile.mkdtemp()) / "catalog.json"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    return path


def _apply_counterfactual_mutation(
    episode: dict, sibling: dict
) -> dict:
    materialized = copy.deepcopy(episode)
    operation = sibling["mutation_operation"]
    for patch in operation["patches"]:
        tokens = patch["path"].removeprefix("/").split("/")
        current: object = materialized
        for token in tokens[:-1]:
            current = (
                current[int(token)]
                if isinstance(current, list)
                else current[token]
            )
        final = tokens[-1]
        if patch["operation"] == "replace":
            if isinstance(current, list):
                current[int(final)] = patch["after"]
            else:
                current[final] = patch["after"]
        else:
            raise AssertionError("test helper currently supports replace only")
    return materialized


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
        "predicates": [
            {
                "predicate_id": "product_check",
                "layer": "product_behavior",
            }
        ],
        "profiles": [
            {
                "profile_id": "GRADER-PRODUCT-TEST",
                "layer": "product_behavior",
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
        "case_variant": {"kind": "base"},
    }
    run_manifest = {
        "status": "frozen",
        "grader_registry_sha256": canonical_sha256(grader_registry),
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
        "case_variant": run_cell["case_variant"],
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
    def test_duplicate_json_keys_are_rejected_before_schema_validation(
        self,
    ) -> None:
        path = Path(tempfile.mkdtemp()) / "duplicate.json"
        path.write_text(
            '{"episodes": [], "episodes": [{"episode_id": "hidden"}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            _load_json_strict(path)

    def test_every_authoring_catalog_satisfies_v4_contract(self) -> None:
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
        policy = _load_json(EVAL_ROOT / "gate3-eval-policy.json")
        expected = [
            episode
            for name in policy["required_suite"]["required_candidate_files"]
            for path in [CANDIDATE_ROOT / name]
            for episode in _load_json(path)["episodes"]
        ]
        self.assertEqual(expected, catalog["episodes"])
        ids = [episode["episode_id"] for episode in expected]
        self.assertEqual(len(ids), len(set(ids)))

    def test_launch_claim_cases_exactly_cover_every_claim_target(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        self.assertEqual(8, len(launch["episodes"]))
        for episode in launch["episodes"]:
            with self.subTest(episode=episode["episode_id"]):
                self.assertEqual(
                    "claim_cases_complete",
                    episode["support_expectation"]["authoring_status"],
                )
                self.assertEqual(
                    {
                        target["claim_target_id"]
                        for target in episode["acceptable_outcome"][
                            "claim_targets"
                        ]
                    },
                    {
                        case["claim_target_id"]
                        for case in episode["support_expectation"][
                            "claim_cases"
                        ]
                    },
                )

    def test_business_chain_claim_gold_and_counterfactuals_are_complete(
        self,
    ) -> None:
        shard = _load_json(BUSINESS_CHAIN_EPISODES_PATH)
        self.assertEqual(10, len(shard["episodes"]))
        siblings = []
        for episode in shard["episodes"]:
            with self.subTest(episode=episode["episode_id"]):
                self.assertEqual(
                    "claim_cases_complete",
                    episode["support_expectation"]["authoring_status"],
                )
                self.assertEqual(
                    {
                        target["claim_target_id"]
                        for target in episode["acceptable_outcome"][
                            "claim_targets"
                        ]
                    },
                    {
                        case["claim_target_id"]
                        for case in episode["support_expectation"][
                            "claim_cases"
                        ]
                    },
                )
                self.assertTrue(
                    all(
                        truth["identifiability"]
                        == "pending_independent_review"
                        for truth in episode["business_world"][
                            "truth_facts"
                        ]
                    )
                )
                siblings.extend(
                    (episode, sibling)
                    for sibling in episode["counterfactual_siblings"]
                )
        self.assertEqual(33, len(siblings))
        for episode, sibling in siblings:
            with self.subTest(sibling=sibling["sibling_id"]):
                self.assertEqual(
                    "executable_verified",
                    sibling["mutation_operation"]["execution_status"],
                )
                self.assertEqual(
                    [],
                    validate_counterfactual_materialization(
                        episode,
                        sibling,
                    ),
                )
                self.assertFalse(
                    any(
                        patch["path"]
                        in {
                            "/user_episode/messages",
                            "/business_world/available_contracts",
                            "/business_world/data_conditions",
                            "/business_world/truth_facts",
                            "/acceptable_outcome/valid_design_space",
                            "/acceptable_outcome/must_preserve",
                            "/data_source_bindings",
                        }
                        for patch in sibling["mutation_operation"][
                            "patches"
                        ]
                    )
                )
                if (
                    sibling["expected_relation"]
                    == "measurement_changing"
                    or sibling["mutation_dimension"]
                    in {"decision_goal", "metric_definition", "scope"}
                ):
                    self.assertIn(
                        "replacement_expectation",
                        sibling,
                    )

    def test_measurement_regression_gold_and_counterfactuals_are_complete(
        self,
    ) -> None:
        shard = _load_json(MEASUREMENT_REGRESSION_EPISODES_PATH)
        self.assertEqual(10, len(shard["episodes"]))
        siblings = []
        for episode in shard["episodes"]:
            with self.subTest(episode=episode["episode_id"]):
                self.assertEqual(
                    "claim_cases_complete",
                    episode["support_expectation"]["authoring_status"],
                )
                self.assertEqual(
                    {
                        target["claim_target_id"]
                        for target in episode["acceptable_outcome"][
                            "claim_targets"
                        ]
                    },
                    {
                        case["claim_target_id"]
                        for case in episode["support_expectation"][
                            "claim_cases"
                        ]
                    },
                )
                self.assertTrue(
                    all(
                        truth["identifiability"]
                        == "pending_independent_review"
                        for truth in episode["business_world"][
                            "truth_facts"
                        ]
                    )
                )
                siblings.extend(
                    (episode, sibling)
                    for sibling in episode["counterfactual_siblings"]
                )
        self.assertEqual(39, len(siblings))
        for episode, sibling in siblings:
            with self.subTest(sibling=sibling["sibling_id"]):
                self.assertEqual(
                    "executable_verified",
                    sibling["mutation_operation"]["execution_status"],
                )
                self.assertEqual(
                    [],
                    validate_counterfactual_materialization(
                        episode,
                        sibling,
                    ),
                )

    def test_business_chain_source_authorities_are_clock_and_scope_bound(
        self,
    ) -> None:
        shard = _load_json(BUSINESS_CHAIN_EPISODES_PATH)
        authorities = {
            authority["authority_id"]: authority
            for authority in _load_json(CASE_FILE_AUTHORITIES_PATH)[
                "authorities"
            ]
        }
        for episode in shard["episodes"]:
            clock = episode["business_world"]["evaluation_clock"]
            claim_ids = {
                target["claim_target_id"]
                for target in episode["acceptable_outcome"][
                    "claim_targets"
                ]
            }
            for binding in episode["data_source_bindings"]:
                if binding["source_mode"] == "known_contract_gap":
                    continue
                authority_id = binding["authority_ref"].rsplit("#", 1)[-1]
                authority = authorities[authority_id]
                with self.subTest(
                    episode=episode["episode_id"],
                    binding=binding["binding_id"],
                ):
                    self.assertEqual("authoring", authority["status"])
                    self.assertEqual(
                        clock["as_of_instant"],
                        authority["evaluation_clock"]["as_of_instant"],
                    )
                    self.assertEqual(
                        clock["default_business_timezone"],
                        authority["evaluation_clock"][
                            "business_timezone"
                        ],
                    )
                    used_claim_ids = {
                        case["claim_target_id"]
                        for case in episode["support_expectation"][
                            "claim_cases"
                        ]
                        if any(
                            use["binding_id"] == binding["binding_id"]
                            for use in case["source_uses"]
                        )
                    }
                    self.assertTrue(used_claim_ids <= claim_ids)
                    self.assertTrue(
                        used_claim_ids
                        <= set(authority["scope_claim_target_ids"])
                    )

    def test_all_launch_counterfactuals_are_executable(self) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        siblings = [
            (episode, sibling)
            for episode in launch["episodes"]
            for sibling in episode["counterfactual_siblings"]
        ]
        self.assertEqual(24, len(siblings))
        for episode, sibling in siblings:
            with self.subTest(sibling=sibling["sibling_id"]):
                self.assertEqual(
                    "executable_verified",
                    sibling["mutation_operation"]["execution_status"],
                )
                self.assertEqual(
                    [],
                    validate_counterfactual_materialization(
                        episode, sibling
                    ),
                )

    def test_substantive_launch_changes_bind_compiled_replacement_claims(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        expected_siblings = {
            "G3-USER-001-CF02",
            "G3-USER-003-CF02",
            "G3-USER-004-CF02",
            "G3-USER-005-CF02",
        }
        observed_siblings: set[str] = set()
        for episode in launch["episodes"]:
            for sibling in episode["counterfactual_siblings"]:
                if sibling["mutation_dimension"] not in {
                    "decision_goal",
                    "metric_definition",
                    "scope",
                }:
                    continue
                observed_siblings.add(sibling["sibling_id"])
                self.assertEqual(
                    derive_base_claim_replacement_expectation(
                        episode,
                        sibling,
                    ),
                    sibling["replacement_expectation"],
                )
                materialized = materialize_counterfactual_episode(
                    episode,
                    sibling,
                )
                self.assertEqual(
                    [],
                    validate_replacement_expectation(
                        episode,
                        sibling,
                        materialized,
                    ),
                )
        self.assertEqual(expected_siblings, observed_siblings)

    def test_replacement_compiler_is_deterministic_and_gold_limited(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        compiled, gaps = compile_catalog_replacement_expectations(
            launch
        )
        self.assertEqual([], gaps)
        self.assertEqual(launch, compiled)

        episode = copy.deepcopy(launch["episodes"][2])
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-003-CF02"
        )
        for effect in sibling["claim_effects"]:
            effect["claim_case_disposition"] = "supersede_or_omit"
        with self.assertRaisesRegex(
            ReplacementCompilationError,
            "author variant gold",
        ):
            derive_base_claim_replacement_expectation(
                episode,
                sibling,
            )

    def test_replacement_compiler_preserves_valid_variant_authored_gold(
        self,
    ) -> None:
        catalog = _load_json(AUTHORITY_STRESS_EPISODES_PATH)
        compiled, gaps = compile_catalog_replacement_expectations(
            catalog
        )
        self.assertEqual([], gaps)
        source_expectations = {
            sibling["sibling_id"]: sibling["replacement_expectation"]
            for episode in catalog["episodes"]
            for sibling in episode["counterfactual_siblings"]
            if sibling.get("replacement_expectation", {}).get(
                "derivation"
            )
            == "variant_authored_gold"
        }
        compiled_expectations = {
            sibling["sibling_id"]: sibling["replacement_expectation"]
            for episode in compiled["episodes"]
            for sibling in episode["counterfactual_siblings"]
            if sibling["sibling_id"] in source_expectations
        }
        self.assertEqual(source_expectations, compiled_expectations)

    def test_substantive_change_without_replacement_claim_fails_closed(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        attacked = copy.deepcopy(launch)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-003"
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-003-CF02"
        )
        del sibling["replacement_expectation"]

        findings, _ = validate_catalog(
            _write_temp_catalog(attacked),
            require_policy_ready=False,
        )

        self.assertTrue(
            any(
                "substantive decision_goal change requires replacement expectation"
                in finding
                for finding in findings
            )
        )

    def test_replacement_expectation_hashes_base_gold_and_intervention(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        attacked = copy.deepcopy(launch)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-004"
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-004-CF02"
        )
        sibling["replacement_expectation"]["base_claim_refs"][0][
            "base_claim_case_sha256"
        ] = "0" * 64
        sibling["replacement_expectation"]["content_sha256"] = (
            canonical_sha256(
                replacement_expectation_content_core(
                    sibling["replacement_expectation"]
                )
            )
        )

        findings, _ = validate_catalog(
            _write_temp_catalog(attacked),
            require_policy_ready=False,
        )

        self.assertTrue(
            any(
                "has stale base claim-case binding" in finding
                for finding in findings
            )
        )

    def test_scope_changes_can_mix_recompute_and_supersession(self) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        for episode_id, sibling_id in (
            ("G3-USER-003", "G3-USER-003-CF02"),
            ("G3-USER-005", "G3-USER-005-CF02"),
            ("G3-USER-006", "G3-USER-006-CF02"),
        ):
            episode = next(
                item
                for item in launch["episodes"]
                if item["episode_id"] == episode_id
            )
            sibling = next(
                item
                for item in episode["counterfactual_siblings"]
                if item["sibling_id"] == sibling_id
            )
            with self.subTest(sibling=sibling_id):
                self.assertEqual(
                    "mixed",
                    sibling["expected_authority_effects"][
                        "prior_evidence"
                    ],
                )
                self.assertEqual(
                    "mixed",
                    sibling["expected_authority_effects"][
                        "claim_case_disposition"
                    ],
                )
                dispositions = {
                    effect["claim_case_disposition"]
                    for effect in sibling["claim_effects"]
                }
                self.assertEqual(
                    {"recompute", "supersede_or_omit"},
                    dispositions,
                )
                removed = [
                    effect
                    for effect in sibling["claim_effects"]
                    if effect["claim_case_disposition"]
                    == "supersede_or_omit"
                ]
                self.assertTrue(removed)
                self.assertTrue(
                    all(
                        effect["support_state"] == "not_applicable"
                        and effect["claim_ceiling"] == "not_applicable"
                        and effect["boundary_codes"] == "clear"
                        for effect in removed
                    )
                )

    def test_mixed_summary_requires_nonuniform_claim_effects(self) -> None:
        catalog = _load_json(LAUNCH_EPISODES_PATH)
        attacked = copy.deepcopy(catalog)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-001-CF02"
        )
        sibling["expected_authority_effects"]["prior_evidence"] = "mixed"
        sibling["expected_authority_effects"][
            "claim_case_disposition"
        ] = "mixed"
        findings, _ = validate_catalog(
            _write_temp_catalog(attacked), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "declares mixed prior_evidence but claim effects are uniform"
                in finding
                for finding in findings
            )
        )
        self.assertTrue(
            any(
                "declares mixed claim_case_disposition but claim effects are uniform"
                in finding
                for finding in findings
            )
        )

    def test_mixed_summary_cannot_hide_an_incoherent_claim_effect(
        self,
    ) -> None:
        catalog = _load_json(LAUNCH_EPISODES_PATH)
        attacked = copy.deepcopy(catalog)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-005"
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-005-CF02"
        )
        effect = next(
            item
            for item in sibling["claim_effects"]
            if item["claim_target_id"] == "per_dimension_leader_claim"
        )
        effect["prior_evidence"] = "reject"
        effect["claim_case_disposition"] = "supersede_or_omit"

        findings, _ = validate_catalog(
            _write_temp_catalog(attacked), require_policy_ready=False
        )

        self.assertTrue(
            any(
                "has invalid authority effect tuple" in finding
                for finding in findings
            )
        )

    def test_preserved_boundary_refs_remain_valid_in_materialized_sibling(
        self,
    ) -> None:
        catalog = _load_json(LAUNCH_EPISODES_PATH)
        attacked = copy.deepcopy(catalog)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-005"
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-005-CF03"
        )
        original_condition = copy.deepcopy(
            episode["business_world"]["data_conditions"][1]
        )
        replacement_condition = {
            **original_condition,
            "condition_id": "DC-UNRELATED-DIMENSION-CONDITION",
            "description": "与维度覆盖无关的条件。",
        }
        sibling["mutation_operation"]["patches"] = [
            {
                "path": "/business_world/data_conditions/1",
                "operation": "replace",
                "before": original_condition,
                "after": replacement_condition,
            }
        ]
        materialized = _apply_counterfactual_mutation(episode, sibling)
        sibling["mutation_operation"]["materialized_sibling_sha256"] = (
            canonical_sha256(
                counterfactual_materialization_core(materialized)
            )
        )

        findings, _ = validate_catalog(
            _write_temp_catalog(attacked), require_policy_ready=False
        )

        self.assertTrue(
            any(
                "boundary dimension_coverage_gap cites unknown fact"
                in finding
                for finding in findings
            )
        )

    def test_contract_changes_bind_new_case_file_authority(self) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        authorities = {
            item["authority_id"]: item
            for item in _load_json(CASE_FILE_AUTHORITIES_PATH)[
                "authorities"
            ]
        }
        expectations = (
            (
                "G3-USER-003",
                "G3-USER-003-CF03",
                "fixture_operations_assignment",
                "FIXTURE-WAJE-CREATIVE-VERSION-ASSIGNMENT-INCOMPLETE-V1",
            ),
            (
                "G3-USER-004",
                "G3-USER-004-CF03",
                "gap_canonical_payer",
                "FIXTURE-WAJE-CANONICAL-PAYER-IDENTITY-V1",
            ),
            (
                "G3-USER-008",
                "G3-USER-008-CF03",
                "fixture_repair",
                "FIXTURE-WAJE-RUN-SUMMARY-ONLY-V1",
            ),
        )
        for episode_id, sibling_id, binding_id, authority_id in expectations:
            episode = next(
                item
                for item in launch["episodes"]
                if item["episode_id"] == episode_id
            )
            sibling = next(
                item
                for item in episode["counterfactual_siblings"]
                if item["sibling_id"] == sibling_id
            )
            materialized = _apply_counterfactual_mutation(
                episode, sibling
            )
            source_binding = next(
                item
                for item in materialized["data_source_bindings"]
                if item["binding_id"] == binding_id
            )
            with self.subTest(sibling=sibling_id):
                self.assertEqual(authority_id, source_binding["source_ref"])
                self.assertTrue(
                    source_binding["authority_ref"].endswith(
                        "#" + authority_id
                    )
                )
                self.assertIn(authority_id, authorities)

    def test_counterfactual_only_authorities_enter_readiness(self) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        authorities = _load_json(CASE_FILE_AUTHORITIES_PATH)
        (
            missing_authorities,
            pending_authorities,
            pending_materializations,
            integrity_gaps,
        ) = _case_file_readiness_gaps(launch, authorities)
        self.assertEqual([], missing_authorities)
        self.assertEqual([], integrity_gaps)
        for authority_id in (
            "FIXTURE-WAJE-CREATIVE-VERSION-ASSIGNMENT-INCOMPLETE-V1",
            "FIXTURE-WAJE-CANONICAL-PAYER-IDENTITY-V1",
            "FIXTURE-WAJE-RUN-SUMMARY-ONLY-V1",
        ):
            self.assertIn(authority_id, pending_authorities)
        self.assertEqual([], pending_materializations)

    def test_business_chain_controlled_case_files_are_hash_bound(self) -> None:
        self.assertEqual([], materialize_controlled_case_files(check=True))
        authorities = _load_json(CASE_FILE_AUTHORITIES_PATH)
        business_chain = _load_json(BUSINESS_CHAIN_EPISODES_PATH)
        bound_authority_ids = controlled_authority_ids(
            business_chain
        )
        controlled = [
            authority
            for authority in authorities["authorities"]
            if authority["authority_id"] in bound_authority_ids
            and authority["source_mode"] == "controlled_synthetic_fixture"
        ]
        self.assertEqual(14, len(controlled))
        self.assertTrue(
            all(
                authority["status"] == "authoring"
                and len(authority["materializations"]) == 1
                and not authority.get("review_records")
                for authority in controlled
            )
        )

    def test_measurement_controlled_case_files_are_hash_bound(self) -> None:
        self.assertEqual([], materialize_controlled_case_files(check=True))
        authorities = _load_json(CASE_FILE_AUTHORITIES_PATH)
        shard = _load_json(MEASUREMENT_REGRESSION_EPISODES_PATH)
        bound_authority_ids = controlled_authority_ids(shard)
        controlled = [
            authority
            for authority in authorities["authorities"]
            if authority["authority_id"] in bound_authority_ids
            and authority["source_mode"] == "controlled_synthetic_fixture"
        ]
        self.assertEqual(10, len(controlled))
        validator = Draft202012Validator(
            _load_json(CONTROLLED_BUSINESS_FIXTURE_SCHEMA_PATH)
        )
        for authority in controlled:
            with self.subTest(authority=authority["authority_id"]):
                self.assertEqual("authoring", authority["status"])
                self.assertFalse(authority.get("review_records"))
                self.assertEqual(1, len(authority["materializations"]))
                materialization = authority["materializations"][0]
                artifact = _load_json(
                    VNEXT_ROOT.parent / materialization["artifact_ref"]
                )
                self.assertEqual([], list(validator.iter_errors(artifact)))
                self.assertEqual(
                    materialization["artifact_content_sha256"],
                    canonical_sha256(artifact),
                )
                self.assertEqual(
                    "pending_independent_review",
                    artifact["evaluator_oracle"]["review_status"],
                )
                self.assertTrue(
                    all(
                        truth["identifiability"]
                        == "pending_independent_review"
                        for truth in artifact["evaluator_oracle"][
                            "truth_facts"
                        ]
                    )
                )

    def test_authority_slot_rejects_conflicting_visible_alternatives(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        attacked = copy.deepcopy(launch)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-003"
        )
        operations_binding = next(
            item
            for item in episode["data_source_bindings"]
            if item["binding_id"] == "fixture_operations"
        )
        operations_binding["source_ref"] = (
            "FIXTURE-WAJE-CREATIVE-VERSION-ASSIGNMENT-V1"
        )
        operations_binding["authority_ref"] = (
            "vnext/evals/gate3/case-files/"
            "case-file-authorities.json"
            "#FIXTURE-WAJE-CREATIVE-VERSION-ASSIGNMENT-V1"
        )

        *_, integrity_gaps = _case_file_readiness_gaps(
            attacked, _load_json(CASE_FILE_AUTHORITIES_PATH)
        )

        self.assertTrue(
            any(
                "G3-USER-003:G3-USER-003-CF03 exposes conflicting "
                "authorities" in finding
                and "creative_version_assignment" in finding
                for finding in integrity_gaps
            )
        )

    def test_shared_fixture_authority_is_split_by_claim_use(self) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        authorities = {
            item["authority_id"]: item
            for item in _load_json(CASE_FILE_AUTHORITIES_PATH)[
                "authorities"
            ]
        }
        episode = next(
            item
            for item in launch["episodes"]
            if item["episode_id"] == "G3-USER-003"
        )
        source_use_by_claim = {
            item["claim_target_id"]: {
                use["binding_id"] for use in item["source_uses"]
            }
            for item in episode["support_expectation"]["claim_cases"]
        }
        self.assertIn(
            "fixture_operations",
            source_use_by_claim["joint_activity_budget_effect_claim"],
        )
        for claim_target_id in (
            "creative_effect_claim",
            "version_effect_claim",
        ):
            self.assertEqual(
                {"fixture_operations_assignment"},
                source_use_by_claim[claim_target_id],
            )
        self.assertEqual(
            {
                "activity_effect_claim",
                "budget_effect_claim",
                "joint_activity_budget_effect_claim",
            },
            set(
                authorities["FIXTURE-WAJE-OPERATIONS-ASSIGNMENT-V1"][
                    "scope_claim_target_ids"
                ]
            ),
        )
        self.assertEqual(
            {"creative_effect_claim", "version_effect_claim"},
            set(
                authorities[
                    "FIXTURE-WAJE-CREATIVE-VERSION-ASSIGNMENT-V1"
                ]["scope_claim_target_ids"]
            ),
        )

    def test_coverage_counterfactual_exposes_one_creative_version_authority(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        episode = next(
            item
            for item in launch["episodes"]
            if item["episode_id"] == "G3-USER-003"
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-003-CF03"
        )
        materialized = _apply_counterfactual_mutation(episode, sibling)
        views = compile_views(
            materialized,
            {
                "episode_core_sha256": "0" * 64,
                "source_record_ref": "test-source",
                "product_grader_profile_ref": "test-product-profile",
                "authority_profile_ref": "test-authority-profile",
            },
        )
        discoverable_refs = {
            ref
            for surface in views["agent_world_view"][
                "inspection_surfaces"
            ]
            for ref in surface["discoverable_refs"]
        }

        self.assertIn(
            "FIXTURE-WAJE-CREATIVE-VERSION-ASSIGNMENT-INCOMPLETE-V1",
            discoverable_refs,
        )
        self.assertIn(
            "fixture://wajegame/creative-version-assignment-incomplete/v1",
            discoverable_refs,
        )
        self.assertNotIn(
            "FIXTURE-WAJE-CREATIVE-VERSION-ASSIGNMENT-V1",
            discoverable_refs,
        )
        self.assertNotIn(
            "fixture://wajegame/creative-version-assignment/v1",
            discoverable_refs,
        )

    def test_new_metric_and_causal_requests_have_typed_boundaries(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        expectations = (
            (
                "G3-USER-001",
                "refund_or_reversal_contract_missing",
                "gap_refund",
                "G3-USER-001-CF02",
            ),
            (
                "G3-USER-008",
                "causal_identification_missing",
                "gap_causal_exposure",
                "G3-USER-008-CF02",
            ),
        )
        for episode_id, boundary_code, binding_id, sibling_id in expectations:
            episode = next(
                item
                for item in launch["episodes"]
                if item["episode_id"] == episode_id
            )
            sibling = next(
                item
                for item in episode["counterfactual_siblings"]
                if item["sibling_id"] == sibling_id
            )
            with self.subTest(sibling=sibling_id):
                self.assertIn(
                    boundary_code,
                    episode["acceptable_outcome"][
                        "allowed_boundary_codes"
                    ],
                )
                self.assertTrue(
                    any(
                        item["boundary_code"] == boundary_code
                        for item in episode["support_expectation"][
                            "boundary_cases"
                        ]
                    )
                )
                source_binding = next(
                    item
                    for item in episode["data_source_bindings"]
                    if item["binding_id"] == binding_id
                )
                self.assertEqual(
                    "known_contract_gap",
                    source_binding["source_mode"],
                )
                self.assertNotEqual(
                    "recompute",
                    sibling["expected_authority_effects"][
                        "claim_case_disposition"
                    ],
                )

    def test_multiple_authority_surfaces_require_composite_binding(
        self,
    ) -> None:
        catalog = _load_json(LAUNCH_EPISODES_PATH)
        attacked = copy.deepcopy(catalog)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-008"
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-008-CF03"
        )
        self.assertEqual(
            "composite_authority",
            sibling["mutation_operation"]["authority_surface"],
        )
        sibling["mutation_operation"]["authority_surface"] = "world_fixture"
        findings, _ = validate_catalog(
            _write_temp_catalog(attacked), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "requires composite authority" in finding
                for finding in findings
            )
        )

    def test_promoted_run_matrix_requires_base_and_every_sibling(self) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        episodes_by_id = {
            episode["episode_id"]: episode
            for episode in launch["episodes"]
        }
        keys = expected_run_variant_keys(
            set(episodes_by_id), episodes_by_id
        )
        self.assertEqual(32, len(keys))
        for episode in launch["episodes"]:
            self.assertIn(
                (episode["episode_id"], "base", ""),
                keys,
            )
            for sibling in episode["counterfactual_siblings"]:
                self.assertIn(
                    (
                        episode["episode_id"],
                        "counterfactual",
                        sibling["sibling_id"],
                    ),
                    keys,
                )

    def test_every_launch_sibling_changes_the_agent_visible_case(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        corpus = _load_json(CORPUS_PATH)
        entries = {
            entry["episode_id"]: entry for entry in corpus["entries"]
        }
        for episode in launch["episodes"]:
            base = compile_views(
                episode, entries[episode["episode_id"]]
            )["agent_world_view"]
            for sibling in episode["counterfactual_siblings"]:
                materialized = _apply_counterfactual_mutation(
                    episode, sibling
                )
                changed = compile_views(
                    materialized, entries[episode["episode_id"]]
                )["agent_world_view"]
                with self.subTest(sibling=sibling["sibling_id"]):
                    self.assertNotEqual(
                        base["view_sha256"], changed["view_sha256"]
                    )

    def test_case_file_authority_registry_matches_schema(self) -> None:
        schema = _load_json(CASE_FILE_AUTHORITY_SCHEMA_PATH)
        authority = _load_json(CASE_FILE_AUTHORITIES_PATH)
        errors = list(
            Draft202012Validator(schema).iter_errors(authority)
        )
        self.assertEqual([], errors)
        self.assertEqual(
            {
                "controlled_synthetic_fixture",
                "frozen_real_snapshot",
            },
            {
                item["source_mode"]
                for item in authority["authorities"]
            },
        )

    def test_case_file_materialization_artifacts_match_schemas(self) -> None:
        controlled_schema = _load_json(
            CONTROLLED_BUSINESS_FIXTURE_SCHEMA_PATH
        )
        real_schema = _load_json(
            REAL_SNAPSHOT_MATERIALIZATION_SCHEMA_PATH
        )
        real_paths = sorted(
            AUTHORITY_FIXTURE_ROOT.glob("g3-real-*.json")
        )
        self.assertGreaterEqual(len(real_paths), 3)
        for path in real_paths:
            with self.subTest(path=path.name):
                self.assertEqual(
                    [],
                    list(
                        Draft202012Validator(real_schema).iter_errors(
                            _load_json(path)
                        )
                    ),
                )
        controlled_paths = [
            path
            for path in sorted(AUTHORITY_FIXTURE_ROOT.glob("*.json"))
            if not path.name.startswith("g3-real-")
            and path.name != "g3-user-008-prior-authority.v1.json"
        ]
        self.assertGreaterEqual(len(controlled_paths), 7)
        for path in controlled_paths:
            with self.subTest(path=path.name):
                self.assertEqual(
                    [],
                    list(
                        Draft202012Validator(
                            controlled_schema
                        ).iter_errors(_load_json(path))
                    ),
                )

    def test_verified_case_files_have_no_readiness_gap(self) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        authorities = _load_json(CASE_FILE_AUTHORITIES_PATH)
        artifact_root = Path(tempfile.mkdtemp())
        bindings_by_authority: dict[str, list[dict]] = {}
        for episode in launch["episodes"]:
            for binding in episode["data_source_bindings"]:
                if binding["source_mode"] != "known_contract_gap":
                    binding["materialization_status"] = "verified"
                    authority_id = binding["authority_ref"].split("#", 1)[1]
                    bindings_by_authority.setdefault(
                        authority_id, []
                    ).append(binding)
            for sibling in episode["counterfactual_siblings"]:
                for item_patch in sibling["mutation_operation"]["patches"]:
                    if not item_patch["path"].startswith(
                        "/data_source_bindings/"
                    ):
                        continue
                    binding_index = int(
                        item_patch["path"].split("/")[2]
                    )
                    base_binding = episode["data_source_bindings"][
                        binding_index
                    ]
                    item_patch["before"] = copy.deepcopy(base_binding)
                    replacement_binding = item_patch["after"]
                    replacement_binding["materialization_status"] = (
                        "verified"
                    )
                    authority_id = replacement_binding[
                        "authority_ref"
                    ].split("#", 1)[1]
                    bindings_by_authority.setdefault(
                        authority_id, []
                    ).append(replacement_binding)
        for authority in authorities["authorities"]:
            authority["status"] = "independently_reviewed"
            identity_fields_by_source: dict[str, set[str]] = {}
            for binding in bindings_by_authority.get(
                authority["authority_id"], []
            ):
                identity_fields_by_source.setdefault(
                    binding["source_ref"], set()
                ).update(binding["required_identity_fields"])
            authority["materializations"] = [
                self._case_file_materialization(
                    artifact_root,
                    authority,
                    source_ref,
                    bindings_by_authority[authority["authority_id"]][0][
                        "source_mode"
                    ],
                    identity_fields,
                )
                for source_ref, identity_fields in sorted(
                    identity_fields_by_source.items()
                )
            ]
            authority["content_sha256"] = canonical_sha256(
                case_file_authority_content_core(authority)
            )
            authority["review_records"] = [
                {
                    "review_id": "{}-{}".format(
                        authority["authority_id"], role
                    ),
                    "role": role,
                    "principal_id": "principal-{}".format(role),
                    "verdict": "approved",
                    "reviewed_content_sha256": authority[
                        "content_sha256"
                    ],
                }
                for role in authority["required_reviews"]
            ]
        authorized_reviews = {
            canonical_sha256(record)
            for authority in authorities["authorities"]
            for record in authority["review_records"]
        }
        self.assertTrue(
            any(
                "lack protected external authorization" in gap
                for gap in _case_file_readiness_gaps(
                    launch,
                    authorities,
                    workspace_root=artifact_root,
                )[3]
            )
        )
        self.assertEqual(
            ([], [], [], []),
            _case_file_readiness_gaps(
                launch,
                authorities,
                workspace_root=artifact_root,
                authorized_review_sha256s=authorized_reviews,
            ),
        )
        first_materialization = authorities["authorities"][0][
            "materializations"
        ][0]
        (artifact_root / first_materialization["artifact_ref"]).write_text(
            '{"tampered":true}',
            encoding="utf-8",
        )
        self.assertTrue(
            any(
                "artifact hash is stale" in gap
                for gap in _case_file_readiness_gaps(
                    launch,
                    authorities,
                    workspace_root=artifact_root,
                    authorized_review_sha256s=authorized_reviews,
                )[3]
            )
        )
        tampered_artifact = {"tampered": True}
        tampered_sha256 = canonical_sha256(tampered_artifact)
        first_materialization["artifact_content_sha256"] = tampered_sha256
        if "fixture_content_sha256" in first_materialization[
            "identity_values"
        ]:
            first_materialization["identity_values"][
                "fixture_content_sha256"
            ] = tampered_sha256
        self.assertTrue(
            any(
                "unrecognized artifact contract" in gap
                for gap in _case_file_readiness_gaps(
                    launch,
                    authorities,
                    workspace_root=artifact_root,
                    authorized_review_sha256s=authorized_reviews,
                )[3]
            )
        )
        authority = authorities["authorities"][0]
        authority["agent_projection"] += " changed"
        self.assertTrue(
            _case_file_readiness_gaps(
                launch,
                authorities,
                workspace_root=artifact_root,
                authorized_review_sha256s=authorized_reviews,
            )[3]
        )

    @staticmethod
    def _case_file_materialization(
        artifact_root: Path,
        authority: dict,
        source_ref: str,
        source_mode: str,
        identity_fields: set[str],
    ) -> dict:
        artifact_name = "{}.json".format(source_ref)
        fixture_version_ref = "fixture://{}".format(source_ref)
        if source_mode == "frozen_real_snapshot":
            identity_values = {
                field: "{}://{}".format(field, source_ref)
                for field in sorted(identity_fields)
            }
            artifact = {
                "artifact_type": "gate3_real_snapshot_materialization",
                "authority_id": authority["authority_id"],
                "evaluation_clock": authority["evaluation_clock"],
                "sources": [
                    {
                        "source_ref": source_ref,
                        **identity_values,
                    }
                ],
            }
        else:
            artifact = {
                "artifact_type": "gate3_controlled_business_fixture",
                "fixture_id": authority["authority_id"],
                "fixture_version_ref": fixture_version_ref,
            }
        artifact_path = artifact_root / artifact_name
        artifact_path.write_text(
            json.dumps(artifact, ensure_ascii=False),
            encoding="utf-8",
        )
        artifact_sha256 = canonical_sha256(artifact)
        return {
            "source_ref": source_ref,
            "artifact_ref": artifact_name,
            "artifact_content_sha256": artifact_sha256,
            "identity_values": {
                field: (
                    artifact_sha256
                    if field == "fixture_content_sha256"
                    else fixture_version_ref
                    if field == "fixture_version_ref"
                    else "{}://{}".format(field, source_ref)
                )
                for field in sorted(identity_fields)
            },
        }

    def test_case_file_readiness_rejects_duplicate_authority_ids(self) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        authorities = _load_json(CASE_FILE_AUTHORITIES_PATH)
        authorities["authorities"].append(
            copy.deepcopy(authorities["authorities"][0])
        )
        self.assertTrue(
            any(
                "duplicate authority id" in gap
                for gap in _case_file_readiness_gaps(
                    launch, authorities
                )[3]
            )
        )

    def test_transfer_research_is_unreachable_from_required_inputs(self) -> None:
        policy = _load_json(EVAL_ROOT / "gate3-eval-policy.json")
        required_paths = {
            CANDIDATE_ROOT / name
            for name in policy["required_suite"]["required_candidate_files"]
        }
        self.assertEqual(required_paths, set(CANDIDATE_ROOT.glob("*.json")))
        transfer = _load_json(EVAL_ROOT / "research" / "transfer-probes.json")
        self.assertTrue(transfer["non_gating"])
        self.assertEqual(
            "forbidden", transfer["gate_artifact_reachability"]
        )
        required_ids = {
            episode["episode_id"]
            for episode in _load_json(CATALOG_PATH)["episodes"]
        }
        transfer_ids = {
            episode["episode_id"] for episode in transfer["episodes"]
        }
        self.assertTrue(required_ids.isdisjoint(transfer_ids))

    def test_contract_release_is_visible_only_after_its_user_turn(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = next(
            item
            for item in catalog["episodes"]
            if item["episode_id"] == "G3-EXP-007"
        )
        entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == "G3-EXP-007"
        )
        initial = compile_views(episode, entry, visible_turn=1)
        resumed = compile_views(episode, entry, visible_turn=3)
        diagnostic_ref = (
            "contract://wajegame/payment-failure-diagnostic/v1"
        )
        self.assertEqual(
            [], initial["agent_world_view"]["injected_events"]
        )
        self.assertNotIn(
            diagnostic_ref,
            initial["agent_world_view"]["inspection_surfaces"][0][
                "discoverable_refs"
            ],
        )
        self.assertIn(
            diagnostic_ref,
            resumed["agent_world_view"]["inspection_surfaces"][0][
                "discoverable_refs"
            ],
        )
        self.assertNotIn(
            "authority_expectation",
            json.dumps(
                resumed["agent_world_view"], ensure_ascii=False
            ),
        )
        self.assertEqual(
            "resume_case",
            resumed["evaluator_oracle_view"]["scheduled_events"][0][
                "authority_expectation"
            ],
        )

    def test_generated_corpus_and_review_packages_are_fresh(self) -> None:
        for path, expected in _expected_artifacts().items():
            with self.subTest(path=path.name):
                self.assertTrue(path.exists())
                self.assertEqual(_render(expected), path.read_text(encoding="utf-8"))
        self.assertEqual(
            36, len(_load_json(REVIEW_PACKAGES_PATH)["packages"])
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
        self.assertEqual(36, report["episode_count"])
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

    def test_gap_source_cannot_be_used_as_quantified_evidence(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["support_expectation"]["authoring_status"]
            == "claim_cases_complete"
            and any(
                use["binding_id"]
                in {
                    binding["binding_id"]
                    for binding in item["data_source_bindings"]
                    if binding["source_mode"] == "known_contract_gap"
                }
                for case in item["support_expectation"]["claim_cases"]
                for use in case["source_uses"]
            )
        )
        gap_id = next(
            binding["binding_id"]
            for binding in episode["data_source_bindings"]
            if binding["source_mode"] == "known_contract_gap"
            and any(
                use["binding_id"] == binding["binding_id"]
                for case in episode["support_expectation"]["claim_cases"]
                for use in case["source_uses"]
            )
        )
        claim_case = next(
            case
            for case in episode["support_expectation"]["claim_cases"]
            if any(
                use["binding_id"] == gap_id
                for use in case["source_uses"]
            )
        )
        next(
            use
            for use in claim_case["source_uses"]
            if use["binding_id"] == gap_id
        )["requirement"] = "required"
        path = _write_temp_catalog(mutated)
        findings, _ = validate_catalog(path, require_policy_ready=False)
        self.assertTrue(
            any(
                "uses gap" in finding
                and "as quantified evidence" in finding
                for finding in findings
            )
        )

    def test_every_completed_binding_resolves_its_own_authority(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-002"
        )
        episode["data_source_bindings"][0][
            "authority_ref"
        ] = (
            "vnext/evals/gate3/case-files/"
            "case-file-authorities.json#UNKNOWN-AUTHORITY"
        )
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "cites unknown case-file authority UNKNOWN-AUTHORITY"
                in finding
                for finding in findings
            )
        )

    def test_every_claim_source_use_is_checked_against_authority_scope(
        self,
    ) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-003"
        )
        claim = next(
            item
            for item in episode["support_expectation"]["claim_cases"]
            if item["claim_target_id"] == "activity_effect_claim"
        )
        next(
            source
            for source in claim["source_uses"]
            if source["binding_id"] == "fixture_operations"
        )["binding_id"] = "fixture_incident"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "activity_effect_claim is outside authority "
                "FIXTURE-WAJE-PAYMENT-INCIDENT-HOURLY-V1 scope"
                in finding
                for finding in findings
            )
        )

    def test_gap_binding_requires_a_real_backlog_item(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if any(
                binding["source_mode"] == "known_contract_gap"
                for binding in item["data_source_bindings"]
            )
        )
        binding = next(
            item
            for item in episode["data_source_bindings"]
            if item["source_mode"] == "known_contract_gap"
        )
        binding["authority_ref"] = (
            "vnext/contracts/backlog/missing-contracts.yaml#backlog.forged_gap"
        )
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "lacks resolvable backlog authority" in finding
                for finding in findings
            )
        )
        attacked = copy.deepcopy(catalog)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-002"
        )
        binding = next(
            item
            for item in episode["data_source_bindings"]
            if item["source_mode"] == "known_contract_gap"
        )
        next(
            contract
            for contract in episode["business_world"][
                "available_contracts"
            ]
            if contract["contract_ref"] == binding["source_ref"]
        )["state"] = "available"
        findings, _ = validate_catalog(
            _write_temp_catalog(attacked), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "must resolve to a missing world contract" in finding
                for finding in findings
            )
        )
        attacked = copy.deepcopy(catalog)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-002"
        )
        binding = next(
            item
            for item in episode["data_source_bindings"]
            if item["source_mode"] == "known_contract_gap"
        )
        binding["authority_ref"] = (
            "vnext/contracts/backlog/missing-contracts.yaml#"
            "backlog.physical_source_binding"
        )
        findings, _ = validate_catalog(
            _write_temp_catalog(attacked), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "lacks resolvable backlog authority" in finding
                for finding in findings
            )
        )

    def test_required_source_needs_agent_observable_prerequisites(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        episode["support_expectation"]["claim_cases"][0][
            "required_observation_refs"
        ] = []
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "requires sources but no observable prerequisites"
                in finding
                for finding in findings
            )
        )
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        claim = episode["support_expectation"]["claim_cases"][0]
        claim["required_observation_refs"].remove("paid_order_success")
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("omits source observations" in item for item in findings)
        )

    def test_claim_cannot_use_a_source_before_its_visible_turn(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        episode["data_source_bindings"][0]["agent_access"][
            "available_from_turn"
        ] = 2
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("uses source real_paid_order_success before turn 2" in item for item in findings)
        )

    def test_claim_support_and_applicability_cannot_contradict_sources(
        self,
    ) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        claim = episode["support_expectation"]["claim_cases"][0]
        claim["support_state"]["data_contract_state"] = "missing"
        claim["support_state"]["business_evidence_state"] = "supported"
        claim["applicability"] = "fixture_only_scope"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "cannot have supported business evidence" in finding
                for finding in findings
            )
        )
        self.assertTrue(
            any(
                "applicability fixture_only_scope disagrees"
                in finding
                for finding in findings
            )
        )

    def test_gap_only_claim_cannot_self_resolve_or_settle(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-002"
        )
        claim = next(
            item
            for item in episode["support_expectation"]["claim_cases"]
            if item["claim_target_id"] == "intraday_pattern_claim"
        )
        claim["disposition"] = {
            "resolution": "resolved_instance",
            "verifier": "accepted",
            "settlement_precondition": "blocked",
        }
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("gap-only support" in item for item in findings)
        )

        claim["support_state"] = {
            "data_contract_state": "supported",
            "business_evidence_state": "supported",
        }
        claim["disposition"]["settlement_precondition"] = (
            "eligible_for_future_settlement"
        )
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("gap-only support" in item for item in findings)
        )

    def test_boundary_observations_must_exist_at_claim_turn(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-002"
        )
        claim = next(
            item
            for item in episode["support_expectation"]["claim_cases"]
            if item["claim_target_id"] == "intraday_pattern_claim"
        )
        gap_binding = next(
            binding
            for binding in episode["data_source_bindings"]
            if binding["source_ref"] in claim["required_observation_refs"]
        )
        gap_binding["agent_access"]["available_from_turn"] = 2
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "claim intraday_pattern_claim uses source"
                in item
                and "before turn 2" in item
                for item in findings
            )
        )

    def test_required_counterfactual_roles_are_hard_acceptance(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        for episode in mutated["episodes"]:
            for sibling in episode["counterfactual_siblings"]:
                if sibling["expected_relation"] == "meaning_preserving":
                    sibling["expected_relation"] = "boundary_changing"
                    break
        findings = _validate_required_suite(
            mutated["episodes"],
            taxonomy=_load_json(
                EVAL_ROOT / "taxonomy" / "coverage-taxonomy.json"
            ),
            policy=_load_json(EVAL_ROOT / "gate3-eval-policy.json"),
        )
        self.assertTrue(
            any(
                "lacks required counterfactual roles" in item
                for item in findings
            )
        )

    def test_counterfactual_gold_is_claim_scoped_and_base_bound(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        sibling = episode["counterfactual_siblings"][0]
        sibling["claim_effects"].pop()
        sibling["unaffected_claim_target_ids"] = []
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "claim effects must exactly cover affected claims"
                in finding
                for finding in findings
            )
        )
        attacked = copy.deepcopy(catalog)
        episode = next(
            item
            for item in attacked["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        episode["counterfactual_siblings"][0]["claim_effects"][0][
            "base_claim_case_sha256"
        ] = "0" * 64
        findings, _ = validate_catalog(
            _write_temp_catalog(attacked), require_policy_ready=False
        )
        self.assertTrue(
            any("has stale base claim binding" in finding for finding in findings)
        )

    def test_materialized_episode_does_not_rebind_base_counterfactual_gold(
        self,
    ) -> None:
        catalog = _load_json(CATALOG_PATH)
        taxonomy = _load_json(
            EVAL_ROOT / "taxonomy" / "coverage-taxonomy.json"
        )
        episode = copy.deepcopy(
            next(
                item
                for item in catalog["episodes"]
                if item["episode_id"] == "G3-USER-002"
            )
        )
        episode["support_expectation"]["claim_cases"][0][
            "reversal_conditions"
        ].append("物化 sibling 的 claim-local 反转条件。")
        base_owned_findings = _validate_episode_semantics(
            episode,
            taxonomy,
            validate_materializations=False,
        )
        self.assertTrue(
            any(
                "stale base claim binding" in item
                for item in base_owned_findings
            )
        )
        materialized_findings = _validate_episode_semantics(
            episode,
            taxonomy,
            validate_materializations=False,
            validate_counterfactual_claim_bindings=False,
        )
        self.assertFalse(
            any(
                "stale base claim binding" in item
                for item in materialized_findings
            )
        )

    def test_counterfactual_relation_and_surface_cannot_be_relabelled(
        self,
    ) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        sibling = episode["counterfactual_siblings"][0]
        sibling["mutation_operation"]["authority_surface"] = "world_fixture"
        sibling["expected_authority_effects"][
            "claim_case_disposition"
        ] = "degrade_or_omit"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "patches require authority surface user_message" in item
                for item in findings
            )
        )
        self.assertTrue(
            any("meaning-preserving effects are inconsistent" in item for item in findings)
        )

    def test_hidden_truth_cannot_masquerade_as_meaning_preserving(
        self,
    ) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        sibling = episode["counterfactual_siblings"][0]
        sibling["mutation_dimension"] = "hidden_business_truth"
        patch = sibling["mutation_operation"]["patches"][0]
        patch.update(
            {
                "path": "/business_world/truth_facts/0/statement",
                "before": episode["business_world"]["truth_facts"][0][
                    "statement"
                ],
                "after": "The hidden business outcome reverses.",
            }
        )
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any(
                "incompatible with mutation dimension hidden_business_truth"
                in item
                for item in findings
            )
        )

    def test_executable_siblings_cannot_duplicate_authority_state(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-001"
        )
        first = episode["counterfactual_siblings"][0]["mutation_operation"]
        second = episode["counterfactual_siblings"][1]["mutation_operation"]
        second["semantic_intervention_id"] = first[
            "semantic_intervention_id"
        ]
        second["materialized_sibling_sha256"] = first[
            "materialized_sibling_sha256"
        ]
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("reuse semantic intervention ids" in item for item in findings)
        )
        self.assertTrue(
            any("duplicate authority states" in item for item in findings)
        )

    def test_accepted_claim_cannot_resolve_latent_truth(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["episode_id"] == "G3-USER-003"
        )
        truth_ref = next(
            claim["oracle_truth_refs"][0]
            for claim in episode["support_expectation"]["claim_cases"]
            if claim["disposition"]["verifier"] == "accepted"
            and claim["oracle_truth_refs"]
        )
        next(
            truth
            for truth in episode["business_world"]["truth_facts"]
            if truth["truth_id"] == truth_ref
        )["identifiability"] = "latent_unidentifiable"
        findings, _ = validate_catalog(
            _write_temp_catalog(mutated), require_policy_ready=False
        )
        self.assertTrue(
            any("latent unidentifiable truth" in item for item in findings)
        )

    def test_claim_cannot_use_future_truth_identification_support(
        self,
    ) -> None:
        catalog = _load_json(CATALOG_PATH)
        taxonomy = _load_json(
            EVAL_ROOT / "taxonomy" / "coverage-taxonomy.json"
        )
        episode = copy.deepcopy(
            next(
                item
                for item in catalog["episodes"]
                if item["episode_id"] == "G3-USER-003"
            )
        )
        future_ref = "DC-FUTURE-ASSIGNMENT"
        contract = copy.deepcopy(
            episode["business_world"]["available_contracts"][0]
        )
        contract.update(
            {
                "contract_ref": future_ref,
                "description": "第二轮用户消息后才释放的 assignment 合同。",
                "state": "available",
                "access_state": "accessible",
                "discoverability": (
                    "discoverable_by_semantic_inspection"
                ),
            }
        )
        episode["business_world"]["available_contracts"].append(contract)
        episode["user_episode"]["messages"].append(
            {
                "turn": 2,
                "speaker": "user",
                "text": "继续，并使用刚释放的 assignment 合同。",
                "communication_function": "correction",
                "trigger": {
                    "kind": "while_investigation_pending",
                    "fallback": "inject_when_observable_or_mark_unreached",
                },
            }
        )
        episode["business_world"].setdefault("scheduled_events", []).append(
            {
                "event_id": "EVENT-FUTURE-ASSIGNMENT",
                "event_type": "contract_release",
                "after_user_turn": 2,
                "public_payload": "assignment 合同已释放。",
                "affected_ref": future_ref,
                "authority_expectation": "reenter_loop",
            }
        )
        truth = next(
            item
            for item in episode["business_world"]["truth_facts"]
            if item["truth_id"] == "TRUTH-G3-USER-003-02"
        )
        truth.update(
            {
                "identifiability": "identifiable_from_world",
                "support_refs": [future_ref],
                "identification_basis": "由释放后的 assignment 合同识别。",
            }
        )

        findings = _validate_episode_semantics(
            episode, taxonomy, validate_materializations=False
        )
        self.assertTrue(
            any(
                "before its identification support is visible" in item
                for item in findings
            )
        )

    def test_boundary_requires_exact_codes_and_world_facts(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["support_expectation"]["boundary_cases"]
        )
        episode["support_expectation"]["boundary_cases"][0][
            "boundary_code"
        ] = "forged_boundary"
        episode["support_expectation"]["boundary_cases"][0][
            "allowed_when_refs"
        ] = ["DC-NOT-IN-WORLD"]
        path = _write_temp_catalog(mutated)
        findings, _ = validate_catalog(path, require_policy_ready=False)
        self.assertTrue(
            any("boundary case mismatch" in finding for finding in findings)
        )
        self.assertTrue(
            any("cites unknown fact" in finding for finding in findings)
        )

    def test_boundary_ceiling_is_checked_per_claim_target(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = next(
            item
            for item in mutated["episodes"]
            if item["support_expectation"]["boundary_cases"]
        )
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
                "design_claim_ceiling": "causal",
            },
            {
                "claim_target_id": "weak_claim",
                "estimand_id": "weak_pattern",
                "target_description": "Descriptive claim",
                "design_claim_ceiling": "descriptive",
            },
        ]
        if (
            "multi_estimand"
            not in episode["coverage_tags"]["measurement_challenges"]
        ):
            episode["coverage_tags"]["measurement_challenges"].append(
                "multi_estimand"
            )
        boundary = episode["support_expectation"]["boundary_cases"][0]
        boundary["claim_target_ids"] = ["weak_claim"]
        boundary["maximum_claim_ceiling"] = "causal"
        episode["support_expectation"]["boundary_cases"] = [boundary]
        episode["acceptable_outcome"]["allowed_boundary_codes"] = [
            boundary["boundary_code"]
        ]
        episode["support_expectation"]["claim_cases"] = []
        episode["support_expectation"][
            "authoring_status"
        ] = "pending_claim_cases"
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
                "design_claim_ceiling": "descriptive",
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

    def test_semantically_discoverable_condition_is_agent_visible(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = copy.deepcopy(catalog["episodes"][0])
        condition = episode["business_world"]["data_conditions"][0]
        condition[
            "discoverability"
        ] = "discoverable_by_semantic_inspection"
        entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == episode["episode_id"]
        )
        bundle = compile_views(episode, entry)
        semantic_surface = next(
            surface
            for surface in bundle["agent_world_view"][
                "inspection_surfaces"
            ]
            if surface["kind"] == "semantic_contract"
        )
        self.assertIn(
            condition["condition_id"],
            semantic_surface["discoverable_refs"],
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

    def test_executable_counterfactual_is_replayed_before_acceptance(self) -> None:
        episode = copy.deepcopy(_load_json(CATALOG_PATH)["episodes"][0])
        sibling = episode["counterfactual_siblings"][0]
        operation = sibling["mutation_operation"]
        patch = operation["patches"][0]
        before = episode["user_episode"]["messages"][0]["text"]
        after = before + "（同义改写）"
        operation.update(
            {
                "semantic_intervention_id": "wording_paraphrase",
                "execution_status": "executable_verified",
                "materialized_sibling_sha256": "0" * 64,
            }
        )
        patch.update(
            {
                "path": "/user_episode/messages/0/text",
                "operation": "replace",
                "before": before,
                "after": after,
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
        patch["before"] = None
        operation["materialized_sibling_sha256"] = "f" * 64
        self.assertTrue(
            validate_counterfactual_materialization(episode, sibling)
        )

    def test_executable_counterfactual_rejects_broad_collection_replace(
        self,
    ) -> None:
        episode = copy.deepcopy(_load_json(CATALOG_PATH)["episodes"][0])
        sibling = episode["counterfactual_siblings"][0]
        sibling["mutation_dimension"] = "conversation_history"
        patch = sibling["mutation_operation"]["patches"][0]
        before = copy.deepcopy(episode["user_episode"]["messages"])
        after = copy.deepcopy(before)
        after[0]["text"] += "（同义改写）"
        patch.update(
            {
                "path": "/user_episode/messages",
                "operation": "replace",
                "before": before,
                "after": after,
            }
        )
        materialized = materialize_counterfactual_episode(
            episode,
            sibling,
        )
        sibling["mutation_operation"][
            "materialized_sibling_sha256"
        ] = canonical_sha256(
            counterfactual_materialization_core(materialized)
        )

        self.assertTrue(
            any(
                "broad collection mutation /user_episode/messages"
                in finding
                for finding in validate_counterfactual_materialization(
                    episode,
                    sibling,
                )
            )
        )

    def test_physical_source_change_requires_new_authority_identity(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        episode = copy.deepcopy(
            next(
                item
                for item in launch["episodes"]
                if item["episode_id"] == "G3-USER-003"
            )
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-003-CF03"
        )
        source_patch = next(
            patch
            for patch in sibling["mutation_operation"]["patches"]
            if patch["path"] == "/data_source_bindings/7"
        )
        source_patch["after"]["authority_ref"] = source_patch["before"][
            "authority_ref"
        ]
        materialized = materialize_counterfactual_episode(
            episode,
            sibling,
        )
        sibling["mutation_operation"][
            "materialized_sibling_sha256"
        ] = canonical_sha256(
            counterfactual_materialization_core(materialized)
        )

        self.assertTrue(
            any(
                "changed without a new authority identity" in finding
                for finding in validate_counterfactual_materialization(
                    episode,
                    sibling,
                )
            )
        )

    def test_contract_gap_to_causal_source_requires_atomic_authority_change(
        self,
    ) -> None:
        catalog = _load_json(AUTHORITY_STRESS_EPISODES_PATH)
        episode = copy.deepcopy(
            next(
                item
                for item in catalog["episodes"]
                if item["episode_id"] == "G3-ADV-008"
            )
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-ADV-008-CF03"
        )
        source_patch = next(
            patch
            for patch in sibling["mutation_operation"]["patches"]
            if patch["path"] == "/data_source_bindings/1"
        )
        self.assertEqual(
            "known_contract_gap", source_patch["before"]["source_mode"]
        )
        self.assertEqual(
            "controlled_synthetic_fixture",
            source_patch["after"]["source_mode"],
        )
        self.assertNotEqual(
            source_patch["before"]["authority_ref"],
            source_patch["after"]["authority_ref"],
        )

        source_patch["after"]["authority_ref"] = source_patch["before"][
            "authority_ref"
        ]
        materialized = materialize_counterfactual_episode(
            episode, sibling
        )
        sibling["mutation_operation"][
            "materialized_sibling_sha256"
        ] = canonical_sha256(
            counterfactual_materialization_core(materialized)
        )
        findings = validate_counterfactual_materialization(
            episode, sibling
        )
        self.assertTrue(
            any(
                "changed mode without atomic source and authority replacement"
                in finding
                for finding in findings
            )
        )

    def test_variant_replacement_claim_source_must_exist_at_its_turn(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        episode = copy.deepcopy(
            next(
                item
                for item in launch["episodes"]
                if item["episode_id"] == "G3-USER-003"
            )
        )
        sibling = next(
            item
            for item in episode["counterfactual_siblings"]
            if item["sibling_id"] == "G3-USER-003-CF02"
        )
        materialized = materialize_counterfactual_episode(
            episode,
            sibling,
        )
        incident_binding = next(
            binding
            for binding in materialized["data_source_bindings"]
            if binding["binding_id"] == "fixture_incident"
        )
        incident_binding["agent_access"]["available_from_turn"] = 2
        expectation = sibling["replacement_expectation"]
        source_case = next(
            case
            for case in episode["support_expectation"]["claim_cases"]
            if case["claim_target_id"]
            == "payment_outage_attempt_claim"
        )
        variant_case = copy.deepcopy(source_case)
        variant_case["claim_target_id"] = "variant_outage_claim"
        expectation["derivation"] = "variant_authored_gold"
        expectation["variant_estimands"] = [
            {
                "estimand_id": "variant_outage_estimand",
                "target_description": "Variant outage target.",
            }
        ]
        expectation["variant_claim_targets"] = [
            {
                "claim_target_id": "variant_outage_claim",
                "estimand_id": "variant_outage_estimand",
                "target_description": "Variant outage claim.",
                "design_claim_ceiling": "descriptive",
            }
        ]
        expectation["variant_claim_cases"] = [variant_case]
        expectation["content_sha256"] = canonical_sha256(
            replacement_expectation_content_core(expectation)
        )

        self.assertTrue(
            any(
                "replacement claim variant_outage_claim uses source "
                "fixture_incident before turn 2" in finding
                for finding in validate_replacement_expectation(
                    episode,
                    sibling,
                    materialized,
                )
            )
        )

    def test_materialized_counterfactual_must_remain_schema_valid(
        self,
    ) -> None:
        episode = copy.deepcopy(_load_json(CATALOG_PATH)["episodes"][0])
        sibling = episode["counterfactual_siblings"][0]
        sibling["mutation_dimension"] = "claim_strength_request"
        operation = sibling["mutation_operation"]
        patch = operation["patches"][0]
        before = episode["acceptable_outcome"]["claim_targets"][0][
            "design_claim_ceiling"
        ]
        operation.update(
            {
                "semantic_intervention_id": "invalid_claim_ceiling",
                "execution_status": "executable_verified",
                "materialized_sibling_sha256": "0" * 64,
            }
        )
        patch.update(
            {
                "path": (
                    "/acceptable_outcome/claim_targets/0/"
                    "design_claim_ceiling"
                ),
                "operation": "replace",
                "before": before,
                "after": "not_a_valid_ceiling",
            }
        )
        materialized = copy.deepcopy(episode)
        materialized["acceptable_outcome"]["claim_targets"][0][
            "design_claim_ceiling"
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
        patch = operation["patches"][0]
        operation.update(
            {
                "semantic_intervention_id": "misclassified_mutation",
                "execution_status": "executable_verified",
                "materialized_sibling_sha256": "0" * 64,
            }
        )
        patch.update(
            {
                "path": "/business_world/truth_facts/0/statement",
                "operation": "replace",
                "before": episode["business_world"]["truth_facts"][0][
                    "statement"
                ],
                "after": "changed hidden truth",
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
        patch.update(
            {
                "path": (
                    "/acceptable_outcome/claim_targets/0/"
                    "design_claim_ceiling"
                ),
                "before": episode["acceptable_outcome"][
                    "claim_targets"
                ][0]["design_claim_ceiling"],
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

    def test_future_release_cannot_leak_through_public_context(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = copy.deepcopy(
            next(
                item
                for item in catalog["episodes"]
                if item["episode_id"] == "G3-EXP-007"
            )
        )
        entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == "G3-EXP-007"
        )
        diagnostic_ref = (
            "contract://wajegame/payment-failure-diagnostic/v1"
        )
        contract = next(
            item
            for item in episode["business_world"]["available_contracts"]
            if item["contract_ref"] == diagnostic_ref
        )
        contract["discoverability"] = "provided_to_agent"

        initial = compile_views(episode, entry, visible_turn=1)
        resumed = compile_views(episode, entry, visible_turn=3)
        initial_refs = {
            item["ref"]
            for item in initial["agent_world_view"]["public_context"]
        }
        resumed_refs = {
            item["ref"]
            for item in resumed["agent_world_view"]["public_context"]
        }
        self.assertNotIn(diagnostic_ref, initial_refs)
        self.assertIn(diagnostic_ref, resumed_refs)

    def test_source_binding_visibility_uses_its_own_turn(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = copy.deepcopy(
            next(
                item
                for item in catalog["episodes"]
                if item["episode_id"] == "G3-USER-002"
            )
        )
        entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == "G3-USER-002"
        )
        binding = next(
            item
            for item in episode["data_source_bindings"]
            if item["source_mode"] == "known_contract_gap"
        )
        binding_ref = binding["source_ref"]
        binding["agent_access"]["available_from_turn"] = 2

        initial = compile_views(episode, entry, visible_turn=1)
        resumed = compile_views(episode, entry, visible_turn=2)
        initial_source_refs = {
            observation["ref"]
            for surface in initial["agent_world_view"][
                "inspection_surfaces"
            ]
            for observation in surface["observations"]
            if observation["fact_kind"] == "source_binding"
        }
        resumed_source_refs = {
            observation["ref"]
            for surface in resumed["agent_world_view"][
                "inspection_surfaces"
            ]
            for observation in surface["observations"]
            if observation["fact_kind"] == "source_binding"
        }
        self.assertNotIn(binding_ref, initial_source_refs)
        self.assertIn(binding_ref, resumed_source_refs)

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

    def test_source_observations_distinguish_verified_planned_and_gap(
        self,
    ) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = next(
            item
            for item in catalog["episodes"]
            if item["episode_id"] == "G3-USER-008"
        )
        entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == "G3-USER-008"
        )
        views = compile_views(episode, entry)
        observations = {
            observation["ref"]: observation
            for surface in views["agent_world_view"]["inspection_surfaces"]
            for observation in surface["observations"]
            if observation["fact_kind"] == "source_binding"
        }
        self.assertEqual(
            ("available", "verified"),
            (
                observations["FIXTURE-WAJE-RUN-EVIDENCE-REPAIR-V1"][
                    "state"
                ],
                observations["FIXTURE-WAJE-RUN-EVIDENCE-REPAIR-V1"][
                    "materialization_status"
                ],
            ),
        )
        self.assertEqual(
            ("available", "verified"),
            (
                observations["paid_order_success"]["state"],
                observations["paid_order_success"][
                    "materialization_status"
                ],
            ),
        )
        self.assertEqual(
            {
                "FIXTURE-WAJE-PAYMENT-RELEASE-V1",
                "FIXTURE-WAJE-RUN-EVIDENCE-REPAIR-V1",
                "paid_order_success",
                "payment_final_outcome",
            },
            agent_materialized_source_refs(episode),
        )
        completed_episode = next(
            item
            for item in catalog["episodes"]
            if item["episode_id"] == "G3-ADV-002"
        )
        completed_entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == "G3-ADV-002"
        )
        completed_views = compile_views(completed_episode, completed_entry)
        completed_observations = {
            observation["ref"]: observation
            for surface in completed_views["agent_world_view"][
                "inspection_surfaces"
            ]
            for observation in surface["observations"]
            if observation["fact_kind"] == "source_binding"
        }
        self.assertEqual(
            ("available", "verified"),
            (
                completed_observations["FIXTURE-G3-ADV-002-BASE-V1"][
                    "state"
                ],
                completed_observations["FIXTURE-G3-ADV-002-BASE-V1"][
                    "materialization_status"
                ],
            ),
        )
        planned_episode = copy.deepcopy(completed_episode)
        next(
            binding
            for binding in planned_episode["data_source_bindings"]
            if binding["source_ref"] == "FIXTURE-G3-ADV-002-BASE-V1"
        )["materialization_status"] = "planned"
        planned_views = compile_views(planned_episode, completed_entry)
        planned_observations = {
            observation["ref"]: observation
            for surface in planned_views["agent_world_view"][
                "inspection_surfaces"
            ]
            for observation in surface["observations"]
            if observation["fact_kind"] == "source_binding"
        }
        self.assertEqual(
            ("missing", "planned"),
            (
                planned_observations["FIXTURE-G3-ADV-002-BASE-V1"][
                    "state"
                ],
                planned_observations["FIXTURE-G3-ADV-002-BASE-V1"][
                    "materialization_status"
                ],
            ),
        )
        gap_episode = next(
            item
            for item in catalog["episodes"]
            if item["episode_id"] == "G3-USER-002"
        )
        gap_entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == "G3-USER-002"
        )
        gap_views = compile_views(gap_episode, gap_entry)
        gap_observations = [
            observation
            for surface in gap_views["agent_world_view"][
                "inspection_surfaces"
            ]
            for observation in surface["observations"]
            if observation.get("source_mode") == "known_contract_gap"
        ]
        self.assertTrue(gap_observations)
        self.assertTrue(
            all(
                observation["state"] == "missing"
                and observation["materialization_status"]
                == "missing_by_design"
                for observation in gap_observations
            )
        )

    def test_evaluator_only_condition_is_not_discoverable(self) -> None:
        condition = self.episode["business_world"]["data_conditions"][0]
        condition["discoverability"] = "evaluator_only"
        bundle = compile_views(self.episode, self.entry)
        rendered = json.dumps(
            bundle["agent_world_view"]["inspection_surfaces"],
            ensure_ascii=False,
        )
        self.assertNotIn(condition["condition_id"], rendered)

    def test_known_contract_gap_is_discoverable_to_agent(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = next(
            item
            for item in catalog["episodes"]
            if any(
                binding["source_mode"] == "known_contract_gap"
                for binding in item["data_source_bindings"]
            )
        )
        entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == episode["episode_id"]
        )
        bundle = compile_views(episode, entry)
        discoverable_refs = {
            ref
            for surface in bundle["agent_world_view"][
                "inspection_surfaces"
            ]
            for ref in surface["discoverable_refs"]
        }
        self.assertTrue(
            {
                binding["source_ref"]
                for binding in episode["data_source_bindings"]
                if binding["source_mode"] == "known_contract_gap"
            }.issubset(discoverable_refs)
        )

    def test_user008_prior_authority_is_available_by_lookup(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        corpus = _load_json(CORPUS_PATH)
        episode = next(
            item
            for item in catalog["episodes"]
            if item["episode_id"] == "G3-USER-008"
        )
        entry = next(
            item
            for item in corpus["entries"]
            if item["episode_id"] == episode["episode_id"]
        )
        bundle = compile_views(episode, entry)
        authority_surface = next(
            surface
            for surface in bundle["agent_world_view"][
                "inspection_surfaces"
            ]
            if surface["kind"] == "authority_lookup"
        )
        self.assertIn(
            "FIXTURE-WAJE-RUN-EVIDENCE-REPAIR-V1",
            authority_surface["discoverable_refs"],
        )

    def test_semantic_contract_observation_exposes_safe_status(self) -> None:
        bundle = compile_views(self.episode, self.entry)
        semantic_surface = next(
            surface
            for surface in bundle["agent_world_view"][
                "inspection_surfaces"
            ]
            if surface["kind"] == "semantic_contract"
        )
        contract_observations = [
            observation
            for observation in semantic_surface["observations"]
            if observation["fact_kind"] == "contract_status"
        ]
        self.assertTrue(contract_observations)
        for observation in contract_observations:
            self.assertIn("state", observation)
            self.assertIn("access_state", observation)
            self.assertIn("summary", observation)

    def test_launch_boundary_mutations_change_agent_observation(
        self,
    ) -> None:
        launch = _load_json(LAUNCH_EPISODES_PATH)
        corpus = _load_json(CORPUS_PATH)
        entries = {
            item["episode_id"]: item for item in corpus["entries"]
        }
        boundary_siblings = [
            (episode, sibling)
            for episode in launch["episodes"]
            for sibling in episode["counterfactual_siblings"]
            if sibling["expected_relation"]
            in {"boundary_changing", "interaction_changing"}
        ]
        self.assertEqual(8, len(boundary_siblings))
        for episode, sibling in boundary_siblings:
            entry = entries[episode["episode_id"]]
            base = compile_views(episode, entry)["agent_world_view"]
            materialized = compile_views(
                _apply_counterfactual_mutation(episode, sibling),
                entry,
            )["agent_world_view"]
            with self.subTest(sibling=sibling["sibling_id"]):
                self.assertNotEqual(
                    base["view_sha256"],
                    materialized["view_sha256"],
                )


class Gate3TrustAndVerdictTests(unittest.TestCase):
    def test_authority_observation_package_is_executable(self) -> None:
        summary, findings = _validate_authority_package()
        self.assertEqual([], findings)
        self.assertEqual(
            {
                "schemas": 4,
                "milestones": 19,
                "observations": 19,
                "claim_dispositions": 8,
                "negative_cases": 11,
            },
            summary,
        )

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
            _, _, findings = _source_binding_status(
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
        _, _, findings = _source_binding_status(
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
        _, _, findings = _source_binding_status(
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
        _, _, findings = _source_binding_status(
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
                any("protected CI admission" in item for item in findings)
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

    def test_v4_policy_is_a_clean_wajegame_authority_epoch(self) -> None:
        policy = _load_json(EVAL_ROOT / "gate3-eval-policy.json")
        self.assertNotIn("parent_policy", policy)
        self.assertNotIn("minimum_catalog", policy)
        self.assertEqual(
            "wajegame",
            policy["required_suite"]["required_business_domain"],
        )
        self.assertEqual(
            36,
            policy["required_suite"]["required_catalog_episodes"],
        )
        self.assertNotIn(
            "source_pool_minimums", policy["required_suite"]
        )

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
            _, _, findings = _source_binding_status(
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
                "verdict must be fail" in finding
                for finding in validate_result(attacked, authority=authority)
            )
        )

    def test_product_grader_layer_and_profile_ids_are_authoritative(
        self,
    ) -> None:
        passing, authority = _passing_result()
        authority["grader_registry"]["profiles"][0][
            "layer"
        ] = "authority_conformance"
        findings = validate_result(passing, authority=authority)
        self.assertTrue(any("wrong layer" in item for item in findings))

        passing, authority = _passing_result()
        duplicate = copy.deepcopy(
            authority["grader_registry"]["profiles"][0]
        )
        authority["grader_registry"]["profiles"].append(duplicate)
        findings = validate_result(passing, authority=authority)
        self.assertTrue(
            any("duplicate profile ids" in item for item in findings)
        )

        passing, authority = _passing_result()
        authority["grader_registry"]["profiles"][0][
            "required_predicate_ids"
        ] = ["unregistered_check"]
        passing["layer_results"]["product_behavior"]["check_results"] = [
            {"check_id": "unregistered_check", "verdict": "pass"}
        ]
        findings = validate_result(passing, authority=authority)
        self.assertTrue(
            any(
                "unregistered product predicates" in item
                for item in findings
            )
        )

    def test_layer_cannot_claim_blocked_when_all_checks_pass(self) -> None:
        attacked, authority = _passing_result()
        attacked["layer_results"]["implementation"]["verdict"] = "blocked"
        attacked["derived_final_verdict"] = "blocked"
        self.assertTrue(
            any(
                "verdict must be pass" in item
                for item in validate_result(attacked, authority=authority)
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

    def test_run_manifest_must_bind_the_grader_registry(self) -> None:
        attacked, authority = _passing_result()
        authority["run_manifest"]["grader_registry_sha256"] = "0" * 64
        attacked["run_manifest_sha256"] = canonical_sha256(
            authority["run_manifest"]
        )
        self.assertTrue(
            any(
                "canonical grader registry" in item
                for item in validate_result(attacked, authority=authority)
            )
        )

    def test_reviewer_principal_and_calibration_authority_are_reachable(
        self,
    ) -> None:
        trust_schema = _load_json(TRUST_SCHEMA_PATH)
        review_registry = {
            "artifact_type": "review_registry",
            "artifact_version": "gate3.review-registry.v2",
            "registry_epoch": 1,
            "principals": [
                {
                    "principal_id": "REVIEWER-CAL-001",
                    "roles": ["calibration_reviewer"],
                    "status": "active",
                    "authority_root_ref": "ROOT-CAL-001",
                }
            ],
            "records": [],
            "calibration_records": [],
        }
        self.assertEqual(
            [],
            [
                error.message
                for error in Draft202012Validator(
                    trust_schema
                ).iter_errors(review_registry)
            ],
        )

        policy = _load_json(POLICY_PATH)
        policy["corpus_authority"]["reviewer_authority_roots"] = [
            {
                "authority_root_id": "ROOT-CAL-001",
                "receipt_ref": "receipts/calibration.json",
                "receipt_sha256": "a" * 64,
                "principal_id": "REVIEWER-CAL-001",
                "authorized_roles": ["calibration_reviewer"],
            }
        ]
        policy_schema = _load_json(POLICY_SCHEMA_PATH)
        self.assertEqual(
            [],
            [
                error.message
                for error in Draft202012Validator(
                    policy_schema
                ).iter_errors(policy)
            ],
        )

    def test_selected_evaluator_profiles_bind_configuration_and_files(
        self,
    ) -> None:
        registry = _load_json(GRADER_REGISTRY_PATH)
        policy = _load_json(POLICY_PATH)
        profiles, findings = _evaluator_profile_findings(
            registry, policy["calibration_policy"]
        )
        self.assertEqual([], findings)
        self.assertEqual(
            {
                (
                    "primary_business_analysis_agent",
                    "deepseek-v4-pro",
                    "enabled",
                ),
                (
                    "runtime_reviewer",
                    "deepseek-v4-pro",
                    "disabled",
                ),
                (
                    "evaluation_reviewer",
                    "deepseek-v4-flash",
                    "enabled",
                ),
            },
            {
                (
                    profile["role"],
                    profile["model"],
                    profile["thinking"],
                )
                for profile in profiles.values()
            },
        )
        self.assertTrue(
            all(
                profile["lifecycle_status"] == "quality_probe_only"
                for profile in profiles.values()
            )
        )

        attacked = copy.deepcopy(registry)
        attacked["evaluator_profiles"][0]["prompt_sha256"] = "0" * 64
        _, findings = _evaluator_profile_findings(
            attacked, policy["calibration_policy"]
        )
        self.assertTrue(
            any("prompt hash is stale" in finding for finding in findings)
        )

        workspace_root = Path(tempfile.mkdtemp())
        attacked_rubric = copy.deepcopy(_load_json(GRADER_RUBRIC_PATH))
        attacked_contract = attacked_rubric["role_contracts"][
            registry["evaluator_profiles"][0]["prompt_contract_id"]
        ]
        attacked_contract["input_contract"] = {"type": "unknown-type"}
        rubric_path = workspace_root / (
            registry["evaluator_profiles"][0]["rubric_ref"]
        )
        rubric_path.parent.mkdir(parents=True)
        rubric_path.write_text(
            json.dumps(attacked_rubric, ensure_ascii=False),
            encoding="utf-8",
        )
        runner_path = workspace_root / (
            registry["evaluator_profiles"][0]["runner_ref"]
        )
        runner_path.parent.mkdir(parents=True)
        runner_path.write_bytes(
            (
                VNEXT_ROOT.parent
                / registry["evaluator_profiles"][0]["runner_ref"]
            ).read_bytes()
        )
        invalid_schema_registry = copy.deepcopy(registry)
        attacked_rubric_sha256 = canonical_sha256(attacked_rubric)
        for profile in invalid_schema_registry["evaluator_profiles"]:
            profile["rubric_sha256"] = attacked_rubric_sha256
            contract = attacked_rubric["role_contracts"][
                profile["prompt_contract_id"]
            ]
            profile["input_contract_sha256"] = canonical_sha256(
                contract["input_contract"]
            )
            profile["output_contract_sha256"] = canonical_sha256(
                contract["output_contract"]
            )
        _, findings = _evaluator_profile_findings(
            invalid_schema_registry,
            policy["calibration_policy"],
            workspace_root=workspace_root,
        )
        self.assertTrue(
            any(
                "input_contract is not valid JSON Schema" in finding
                for finding in findings
            )
        )

    def test_calibration_sample_requires_risk_verdict_and_variant_mix(
        self,
    ) -> None:
        policy = _load_json(POLICY_PATH)["calibration_policy"]
        all_pass = [
            {
                "critical": False,
                "human_verdict": "pass",
                "case_variant": {"kind": "base"},
            }
            for _ in range(12)
        ]
        findings = _calibration_sample_findings(all_pass, policy)
        self.assertTrue(
            any("critical episodes" in finding for finding in findings)
        )
        self.assertTrue(
            any("counterfactual variants" in finding for finding in findings)
        )
        self.assertTrue(
            any("human verdicts" in finding for finding in findings)
        )

        stratified = []
        for index in range(12):
            verdict = (
                "fail"
                if index in {0, 1}
                else "blocked"
                if index in {2, 3}
                else "pass"
            )
            stratified.append(
                {
                    "critical": index < 3,
                    "human_verdict": verdict,
                    "case_variant": {
                        "kind": (
                            "base" if index < 6 else "counterfactual"
                        )
                    },
                }
            )
        self.assertEqual(
            [], _calibration_sample_findings(stratified, policy)
        )

    def test_calibration_reviewer_is_role_dedicated_and_independent(
        self,
    ) -> None:
        policy = _load_json(POLICY_PATH)["calibration_policy"]
        record = {
            "calibration_review_record_id": "CAL-REVIEW-001",
            "episode_id": "G3-TEST-001",
            "episode_core_sha256": "a" * 64,
            "principal_id": "REVIEWER-CAL-001",
            "run_cell_id": "CELL-001",
            "case_variant": {"kind": "base"},
            "evaluator_profile_ref": (
                "EVALUATOR-DEEPSEEK-FLASH-THINK-V1"
            ),
            "evaluator_profile_sha256": "b" * 64,
            "grader_result_sha256": "c" * 64,
            "human_verdict": "pass",
        }
        principal = {
            "principal_id": "REVIEWER-CAL-001",
            "roles": ["calibration_reviewer"],
            "status": "active",
            "authority_root_ref": "ROOT-CAL-001",
        }
        authority_roots = {
            "ROOT-CAL-001": {
                "authority_root_id": "ROOT-CAL-001",
                "principal_id": "REVIEWER-CAL-001",
                "authorized_roles": ["calibration_reviewer"],
            }
        }
        registry = {
            "principals": [principal],
            "records": [],
            "calibration_records": [record],
        }
        authorized = {canonical_sha256(record)}
        valid, findings = _valid_calibration_reviews(
            registry, authority_roots, authorized, policy
        )
        self.assertEqual([], findings)
        self.assertIn(record["calibration_review_record_id"], valid)

        registry["records"] = [
            {
                "episode_id": record["episode_id"],
                "principal_id": record["principal_id"],
                "decision": "approved",
            }
        ]
        valid, findings = _valid_calibration_reviews(
            registry, authority_roots, authorized, policy
        )
        self.assertEqual({}, valid)
        self.assertTrue(
            any(
                "not independent from Episode reviewers" in finding
                for finding in findings
            )
        )

    def test_held_out_requires_externally_authorized_promotion_chain(
        self,
    ) -> None:
        workspace_root = Path(tempfile.mkdtemp())
        promotion_receipt = {
            "artifact_type": "held_out_promotion_receipt",
            "artifact_version": "gate3.held-out-promotion-receipt.v1",
            "promotion_ref": "HELDOUT-PROMOTION-001",
            "opaque_episode_id": "HELDOUT-EPISODE-001",
            "episode_core_sha256": "a" * 64,
            "source_record_ref": "SRC-HELDOUT-001",
            "source_attestation_sha256": "b" * 64,
            "independent_source_key": "held-out-source:001",
            "business_review_ref": "REVIEW-HELDOUT-BUSINESS-001",
            "business_review_attestation_sha256": "c" * 64,
            "business_reviewer_principal_id": "REVIEWER-BUSINESS-001",
            "measurement_review_ref": (
                "REVIEW-HELDOUT-MEASUREMENT-001"
            ),
            "measurement_review_attestation_sha256": "d" * 64,
            "measurement_reviewer_principal_id": (
                "REVIEWER-MEASUREMENT-001"
            ),
            "decision": "approved",
        }
        promotion_path = workspace_root / "promotion.json"
        promotion_path.write_text(
            json.dumps(promotion_receipt), encoding="utf-8"
        )
        promotion_sha256 = canonical_sha256(promotion_receipt)
        entry = {
            "opaque_episode_id": "HELDOUT-EPISODE-001",
            "encrypted_object_ref": "s3://held-out/episode-001.bin",
            "ciphertext_sha256": "e" * 64,
            "promotion_ref": "HELDOUT-PROMOTION-001",
            "promotion_receipt_ref": promotion_path.name,
            "promotion_receipt_sha256": promotion_sha256,
            "access_realm": "held_out_runner_only",
            "object_receipt_ref": "object-receipt.json",
            "object_receipt_sha256": "",
        }
        object_receipt = {
            "opaque_episode_id": entry["opaque_episode_id"],
            "encrypted_object_ref": entry["encrypted_object_ref"],
            "ciphertext_sha256": entry["ciphertext_sha256"],
            "promotion_ref": entry["promotion_ref"],
            "promotion_receipt_ref": entry["promotion_receipt_ref"],
            "promotion_receipt_sha256": promotion_sha256,
            "access_realm": entry["access_realm"],
            "registry_epoch": 2,
        }
        object_path = workspace_root / "object-receipt.json"
        object_path.write_text(
            json.dumps(object_receipt), encoding="utf-8"
        )
        entry["object_receipt_sha256"] = hashlib.sha256(
            object_path.read_bytes()
        ).hexdigest()
        manifest = {
            "registry_epoch": 2,
            "entries": [entry],
        }
        authorized = {
            promotion_sha256,
            promotion_receipt["source_attestation_sha256"],
            promotion_receipt["business_review_attestation_sha256"],
            promotion_receipt[
                "measurement_review_attestation_sha256"
            ],
        }
        findings, sources = _held_out_chain_findings(
            manifest,
            trust_schema=_load_json(TRUST_SCHEMA_PATH),
            authorized_attestation_sha256s=authorized,
            workspace_root=workspace_root,
        )
        self.assertEqual([], findings)
        self.assertEqual({"held-out-source:001"}, sources)

        authorized.remove(
            promotion_receipt[
                "measurement_review_attestation_sha256"
            ]
        )
        findings, sources = _held_out_chain_findings(
            manifest,
            trust_schema=_load_json(TRUST_SCHEMA_PATH),
            authorized_attestation_sha256s=authorized,
            workspace_root=workspace_root,
        )
        self.assertEqual(set(), sources)
        self.assertTrue(
            any(
                "double-review attestations" in finding
                for finding in findings
            )
        )

    def test_calibration_label_binds_review_result_and_runner_index(
        self,
    ) -> None:
        result, authority = _passing_result()
        workspace_root = Path(tempfile.mkdtemp())
        result_path = workspace_root / "result.json"
        index_path = workspace_root / "artifact-index.json"
        result_path.write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        artifact_index = {
            "artifact_type": "runner_artifact_index",
            "artifact_version": "gate3.runner-artifact-index.v1",
            "run_manifest_sha256": canonical_sha256(
                authority["run_manifest"]
            ),
            "run_cells": authority["artifact_index"],
        }
        index_path.write_text(
            json.dumps(artifact_index, ensure_ascii=False),
            encoding="utf-8",
        )
        evaluator_profile = {
            "profile_id": "EVALUATOR-DEEPSEEK-FLASH-THINK-V1",
            "role": "evaluation_reviewer",
            "lifecycle_status": "calibration_eligible",
        }
        evaluator_profile_sha256 = canonical_sha256(
            evaluator_profile
        )
        review = {
            "calibration_review_record_id": "CAL-REVIEW-001",
            "episode_id": result["episode_id"],
            "episode_core_sha256": result["episode_core_sha256"],
            "principal_id": "REVIEWER-CAL-001",
            "run_cell_id": result["run_cell_id"],
            "case_variant": result["case_variant"],
            "evaluator_profile_ref": evaluator_profile["profile_id"],
            "evaluator_profile_sha256": evaluator_profile_sha256,
            "grader_result_sha256": canonical_sha256(result),
            "human_verdict": "pass",
        }
        label = {
            "episode_id": result["episode_id"],
            "episode_core_sha256": result["episode_core_sha256"],
            "human_verdict": "pass",
            "grader_verdict": "pass",
            "human_review_ref": review[
                "calibration_review_record_id"
            ],
            "run_cell_id": result["run_cell_id"],
            "case_variant": result["case_variant"],
            "evaluator_profile_ref": evaluator_profile["profile_id"],
            "evaluator_profile_sha256": evaluator_profile_sha256,
            "grader_result_ref": result_path.name,
            "grader_result_sha256": canonical_sha256(result),
            "runner_artifact_index_ref": index_path.name,
            "runner_artifact_index_sha256": canonical_sha256(
                artifact_index
            ),
            "critical": True,
        }
        episodes = {
            result["episode_id"]: {
                "decision_stakes": {"risk_level": "critical"}
            }
        }
        arguments = {
            "valid_calibration_reviews": {
                review["calibration_review_record_id"]: review
            },
            "evaluator_profiles_by_id": {
                evaluator_profile["profile_id"]: evaluator_profile
            },
            "episodes_by_id": episodes,
            "run_manifest": authority["run_manifest"],
            "grader_registry": authority["grader_registry"],
            "authority_profiles": authority["authority_profiles"],
            "world_profiles": authority["world_profiles"],
            "workspace_root": workspace_root,
        }
        self.assertEqual(
            [], _calibration_label_findings([label], **arguments)
        )

        probe_only_profile = copy.deepcopy(evaluator_profile)
        probe_only_profile["lifecycle_status"] = "quality_probe_only"
        probe_only_profile_sha256 = canonical_sha256(probe_only_profile)
        probe_only_label = copy.deepcopy(label)
        probe_only_label[
            "evaluator_profile_sha256"
        ] = probe_only_profile_sha256
        probe_only_review = copy.deepcopy(review)
        probe_only_review[
            "evaluator_profile_sha256"
        ] = probe_only_profile_sha256
        probe_only_arguments = dict(arguments)
        probe_only_arguments["valid_calibration_reviews"] = {
            probe_only_review[
                "calibration_review_record_id"
            ]: probe_only_review
        }
        probe_only_arguments["evaluator_profiles_by_id"] = {
            probe_only_profile["profile_id"]: probe_only_profile
        }
        findings = _calibration_label_findings(
            [probe_only_label], **probe_only_arguments
        )
        self.assertTrue(
            any(
                "ineligible or stale evaluator profile" in item
                for item in findings
            )
        )

        attacked = copy.deepcopy(label)
        attacked["episode_core_sha256"] = "e" * 64
        findings = _calibration_label_findings([attacked], **arguments)
        self.assertTrue(
            any("human calibration review" in item for item in findings)
        )
        self.assertTrue(
            any("bound grader result" in item for item in findings)
        )

        tampered = copy.deepcopy(label)
        tampered["grader_result_sha256"] = "f" * 64
        findings = _calibration_label_findings([tampered], **arguments)
        self.assertTrue(
            any("grader result hash is stale" in item for item in findings)
        )

        invalid_result: dict = {}
        result_path.write_text("{}", encoding="utf-8")
        invalid_label = copy.deepcopy(label)
        invalid_label["grader_result_sha256"] = canonical_sha256(
            invalid_result
        )
        invalid_review = copy.deepcopy(review)
        invalid_review["grader_result_sha256"] = invalid_label[
            "grader_result_sha256"
        ]
        invalid_arguments = dict(arguments)
        invalid_arguments["valid_calibration_reviews"] = {
            invalid_review["calibration_review_record_id"]: invalid_review
        }
        findings = _calibration_label_findings(
            [invalid_label], **invalid_arguments
        )
        self.assertTrue(
            any("invalid grader result" in item for item in findings)
        )

    def test_result_must_bind_exact_counterfactual_variant(self) -> None:
        attacked, authority = _passing_result()
        attacked["case_variant"] = {
            "kind": "counterfactual",
            "sibling_id": "G3-TEST-001-CF01",
            "materialized_sibling_sha256": "d" * 64,
        }
        self.assertTrue(
            any(
                "case_variant does not match frozen run cell"
                in finding
                for finding in validate_result(attacked, authority=authority)
            )
        )

    def test_passing_result_requires_runner_artifact_index(self) -> None:
        passing, authority = _passing_result()
        authority["artifact_index"] = None
        self.assertTrue(
            any(
                "runner-verified artifact index" in item
                for item in validate_result(passing, authority=authority)
            )
        )

    def test_failing_result_cannot_claim_unindexed_artifacts(self) -> None:
        failing, authority = _passing_result()
        failing["layer_results"]["product_behavior"]["verdict"] = "fail"
        failing["layer_results"]["product_behavior"]["check_results"][0][
            "verdict"
        ] = "fail"
        failing["layer_results"]["product_behavior"][
            "artifact_sha256s"
        ] = ["f" * 64]
        failing["derived_final_verdict"] = "fail"
        self.assertTrue(
            any(
                "artifacts do not match runner index" in item
                for item in validate_result(failing, authority=authority)
            )
        )

    def test_critical_veto_must_bind_a_failed_check_and_artifact(
        self,
    ) -> None:
        attacked, authority = _passing_result()
        attacked["critical_vetoes"] = [
            {
                "veto_id": "made_up_veto",
                "layer": "product_behavior",
                "check_id": "product_check",
                "artifact_sha256": "f" * 64,
                "reason_code": "made_up_reason",
            }
        ]
        attacked["derived_final_verdict"] = "fail"
        findings = validate_result(attacked, authority=authority)
        self.assertTrue(
            any("matching failed check" in item for item in findings)
        )
        self.assertTrue(
            any("matching layer artifact" in item for item in findings)
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
        self.assertIn("claim_case_files_ready", blocked_ids)
        self.assertIn("source_registry_verified", blocked_ids)
        self.assertIn("independent_reviews_complete", blocked_ids)
        self.assertIn("truth_identifiability_reviewed", blocked_ids)
        self.assertIn("grader_calibrated", blocked_ids)
        self.assertIn("held_out_partition_sealed", blocked_ids)
        passed_ids = {
            condition["condition_id"]
            for condition in readiness["condition_verdicts"]
            if condition["verdict"] == "pass"
        }
        self.assertIn("per_claim_ceiling_complete", passed_ids)
        self.assertIn("counterfactual_mutations_executable", passed_ids)

    def test_schema_invalid_catalog_returns_blocked_readiness(
        self,
    ) -> None:
        invalid_path = Path(tempfile.mkdtemp()) / "invalid-catalog.json"
        invalid_path.write_text(
            json.dumps({"episodes": [{}]}), encoding="utf-8"
        )
        readiness, findings = compute_readiness(
            authoring_catalog_path=invalid_path
        )
        self.assertTrue(findings)
        self.assertEqual("blocked", readiness["derived_status"])
        self.assertEqual("deny_g3_1", readiness["entry_decision"])
        self.assertEqual(
            "authoring_catalog_valid",
            readiness["condition_verdicts"][0]["condition_id"],
        )

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
                "source_pool", "generated_business_world"
            ),
            lambda value: value["suite_binding"].__setitem__(
                "coverage_group", "authority_stress"
            ),
            lambda value: value["data_source_bindings"][0].__setitem__(
                "source_mode", "controlled_synthetic_fixture"
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
