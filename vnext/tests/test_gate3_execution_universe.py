from __future__ import annotations

import copy
from collections import Counter
import json
import unittest

from tools.compile_gate3_execution_universe import (
    COMPILER_RELEASE_PATHS,
    READINESS_PATH,
    build_readiness,
    canonical_sha256,
    required_coordinates,
    required_episode_relation_groups,
    required_operator_scenario_universe,
    validate_paraphrase_authority,
    validate_scenario_authority,
)
from tools.gate3_execution_authority import (
    RUNNER_RELEASE_PATHS,
    _validate_full_run_universe,
    _validate_relation_groups,
    canonical_authority,
)


class Gate3ExecutionUniverseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = copy.deepcopy(canonical_authority())

    def readiness(self, **overrides):
        values = {
            "policy": self.authority["policy"],
            "catalog": self.authority["catalog"],
            "paraphrase_registry": self.authority["paraphrase_authority"],
            "operator_registry": self.authority["mutation_operators"],
            "scenario_registry": self.authority[
                "operator_scenario_authority"
            ],
            "trace_profiles": self.authority["trace_profiles"],
            "grader_registry": self.authority["grader_registry"],
            "source_run_manifest": self.authority["source_run_manifest"],
            "held_out_manifest": self.authority[
                "protected_held_out_manifest"
            ],
        }
        values.update(overrides)
        return build_readiness(**values)

    def test_checked_readiness_is_fresh_and_exact(self) -> None:
        expected = self.readiness()
        checked = json.loads(READINESS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(expected, checked)
        self.assertEqual(
            {
                "episode_count": 36,
                "case_variant_count": 156,
                "risk_variant_counts": {
                    "medium": 8,
                    "high": 95,
                    "critical": 53,
                },
                "required_coordinate_count": 1172,
                "required_coordinate_set_sha256": expected[
                    "universe_summary"
                ]["required_coordinate_set_sha256"],
                "required_episode_relation_group_count": 2011,
                "required_episode_relation_group_set_sha256": expected[
                    "universe_summary"
                ]["required_episode_relation_group_set_sha256"],
                "required_operator_scenario_coordinate_count": 0,
                "required_operator_scenario_coordinate_set_sha256": expected[
                    "universe_summary"
                ]["required_operator_scenario_coordinate_set_sha256"],
                "required_operator_scenario_relation_group_count": 0,
                "required_operator_scenario_relation_group_set_sha256": expected[
                    "universe_summary"
                ]["required_operator_scenario_relation_group_set_sha256"],
                "required_paraphrase_authority_count": 201,
                "required_operator_scenario_count": 38,
            },
            expected["universe_summary"],
        )

    def test_release_identity_includes_imported_compilers(self) -> None:
        self.assertIn(
            "validate_gate3_eval_catalog.py",
            {path.name for path in COMPILER_RELEASE_PATHS},
        )
        self.assertTrue(
            {
                "compile_gate3_eval_views.py",
                "validate_gate3_eval_catalog.py",
            }.issubset({path.name for path in RUNNER_RELEASE_PATHS})
        )

    def test_missing_authority_blocks_full_development_execution(self) -> None:
        readiness = self.readiness()
        self.assertEqual("blocked", readiness["development_status"])
        self.assertEqual(
            {
                "paraphrase_authority_missing": 201,
                "operator_scenario_authority_missing": 38,
                "operator_scenario_lane_missing": 26,
                "operator_scenario_executor_unavailable": 1,
                "operator_scenario_executor_unverified": 1,
                "operator_scenario_registry_not_reviewed": 1,
                "paraphrase_registry_not_reviewed": 1,
            },
            {
                blocker["code"]: blocker["count"]
                for blocker in readiness["development_blockers"]
            },
        )

    def test_lane_matrix_changes_recompile_the_exact_universe(self) -> None:
        baseline = self.readiness()
        policy = copy.deepcopy(self.authority["policy"])
        policy["run_policy"]["lane_matrix"]["high"]["full_authority"][
            "repeats"
        ] += 1
        changed = self.readiness(policy=policy)
        self.assertGreater(
            changed["universe_summary"]["required_coordinate_count"],
            baseline["universe_summary"]["required_coordinate_count"],
        )
        self.assertNotEqual(
            changed["universe_summary"]["required_coordinate_set_sha256"],
            baseline["universe_summary"]["required_coordinate_set_sha256"],
        )

    def test_hand_counted_high_risk_micro_universe(self) -> None:
        episode = copy.deepcopy(self.authority["catalog"]["episodes"][0])
        self.assertEqual("high", episode["decision_stakes"]["risk_level"])
        self.assertEqual(3, len(episode["counterfactual_siblings"]))
        catalog = {"episodes": [episode]}
        coordinates = required_coordinates(catalog, self.authority["policy"])
        self.assertEqual(20, len(coordinates))
        self.assertEqual(
            Counter(
                {
                    "semantic_frame": 4,
                    "full_authority": 16,
                }
            ),
            Counter(item["lane"] for item in coordinates),
        )
        operators = {
            item["operator_id"]: item
            for item in self.authority["mutation_operators"]["operators"]
        }
        groups = required_episode_relation_groups(
            catalog,
            self.authority["policy"],
            operators,
            coordinates,
        )
        self.assertEqual(37, len(groups))
        self.assertEqual(
            Counter(
                {
                    "episode_outcome": 20,
                    "meaning_preserving_paraphrase": 8,
                    "meaning_preserving_case_mutation": 3,
                    "material_semantic_change": 3,
                    "boundary_or_interaction_change": 3,
                }
            ),
            Counter(item["operator_ref"] for item in groups),
        )

    def test_paraphrase_authority_rejects_copy_and_hash_drift(self) -> None:
        episode = self.authority["catalog"]["episodes"][0]
        source_plan = episode["user_episode"]["messages"]
        message_plan = copy.deepcopy(source_plan)
        message_plan[0]["text"] += " 请按相同业务口径重新表述。"
        entry = {
            "paraphrase_authority_id": (
                f"PARA-{episode['episode_id']}-BASE-1"
            ),
            "episode_id": episode["episode_id"],
            "case_variant_ref": "base",
            "paraphrase_index": 1,
            "message_plan": message_plan,
            "message_plan_sha256": canonical_sha256(message_plan),
            "meaning_preservation_review": {
                "status": "pending",
                "reviewer_ref": None,
                "rubric_ref": None,
                "source_candidate_pair_sha256": None,
                "review_ref": None,
                "review_sha256": None,
            },
        }
        registry = {
            "artifact_type": "gate3_paraphrase_authority_registry",
            "artifact_version": "gate3.paraphrase-authority.v1",
            "registry_epoch": 1,
            "status": "candidate_authoring",
            "entries": [entry],
        }
        entries, findings = validate_paraphrase_authority(
            registry,
            self.authority["catalog"],
            {entry["paraphrase_authority_id"]},
        )
        self.assertEqual([], findings)
        self.assertIn(entry["paraphrase_authority_id"], entries)

        copied = copy.deepcopy(registry)
        copied["entries"][0]["message_plan"] = copy.deepcopy(source_plan)
        copied["entries"][0]["message_plan_sha256"] = canonical_sha256(
            source_plan
        )
        _, findings = validate_paraphrase_authority(
            copied,
            self.authority["catalog"],
            {entry["paraphrase_authority_id"]},
        )
        self.assertTrue(
            any(
                "does not change the evaluated visible wording" in item
                for item in findings
            )
        )

        drifted = copy.deepcopy(registry)
        drifted["entries"][0]["message_plan_sha256"] = "0" * 64
        _, findings = validate_paraphrase_authority(
            drifted,
            self.authority["catalog"],
            {entry["paraphrase_authority_id"]},
        )
        self.assertTrue(any("message plan hash drifted" in item for item in findings))

        self_signed = copy.deepcopy(registry)
        self_signed["entries"][0]["meaning_preservation_review"] = {
            "status": "reviewed",
            "reviewer_ref": "reviewer://self-asserted",
            "rubric_ref": "rubric://gate3/paraphrase/v1",
            "source_candidate_pair_sha256": "5" * 64,
            "review_ref": "review://self-asserted",
            "review_sha256": "6" * 64,
        }
        _, findings = validate_paraphrase_authority(
            self_signed,
            self.authority["catalog"],
            {entry["paraphrase_authority_id"]},
        )
        self.assertTrue(any("review pair binding drifted" in item for item in findings))

        multi_turn = next(
            item
            for item in self.authority["catalog"]["episodes"]
            if len(item["user_episode"]["messages"]) > 1
        )
        later_only_plan = copy.deepcopy(
            multi_turn["user_episode"]["messages"]
        )
        later_only_plan[-1]["text"] += " 只改后续轮次。"
        later_only_id = f"PARA-{multi_turn['episode_id']}-BASE-1"
        later_only = copy.deepcopy(registry)
        later_only["entries"] = [
            {
                **copy.deepcopy(entry),
                "paraphrase_authority_id": later_only_id,
                "episode_id": multi_turn["episode_id"],
                "message_plan": later_only_plan,
                "message_plan_sha256": canonical_sha256(later_only_plan),
            }
        ]
        _, findings = validate_paraphrase_authority(
            later_only,
            self.authority["catalog"],
            {later_only_id},
        )
        self.assertTrue(
            any("evaluated visible wording" in item for item in findings)
        )
        self.assertTrue(
            any("outside its visible wording slot" in item for item in findings)
        )

    def test_operator_scenario_binds_operator_stage_and_checks(self) -> None:
        operator = next(
            item
            for item in self.authority["mutation_operators"]["operators"]
            if item["operator_id"] == "time_offset_change"
        )
        relation_profiles = {
            item["expected_relation"]: item
            for item in self.authority["mutation_operators"][
                "relation_check_profiles"
            ]
        }
        scenario = {
            "scenario_id": "SCENARIO-G3-TIME-OFFSET-CHANGE",
            "operator_ref": operator["operator_id"],
            "operator_sha256": canonical_sha256(operator),
            "source_episode_id": "G3-USER-001",
            "source_case_variant_ref": "base",
            "lane": "semantic_frame",
            "stimulus_contract": {
                "stimulus_kind": "measurement_input_mutation",
                "target_stage_ids": ["typed_binding"],
                "input_contract_ref": "contract://gate3/time-offset-change/v1",
                "input_contract_sha256": "1" * 64,
                "mutation_artifact_ref": "artifact://gate3/time-offset-change/v1",
                "mutation_artifact_sha256": "2" * 64,
                "resolver_ref": "resolver://gate3/measurement-mutation/v1",
                "resolver_release_sha256": "3" * 64,
                "application_receipt_contract_sha256": "4" * 64,
                "expected_observation_kinds": ["analysis_frame_revision"],
            },
            "required_relation_check_ids": relation_profiles[
                operator["expected_relation"]
            ]["required_check_ids"],
            "review_status": "pending",
            "review_binding": {
                "reviewed_scenario_sha256": None,
                "review_ref": None,
                "review_sha256": None,
            },
        }
        registry = {
            "artifact_type": "gate3_operator_scenario_authority_registry",
            "artifact_version": "gate3.operator-scenario-authority.v1",
            "registry_epoch": 1,
            "status": "candidate_authoring",
            "executor_binding": {
                "status": "unavailable",
                "resolver_registry_ref": None,
                "resolver_registry_sha256": None,
                "application_receipt_contract_sha256": None,
                "authorized_resolvers": [],
            },
            "scenarios": [scenario],
        }
        by_operator, findings = validate_scenario_authority(
            registry,
            catalog=self.authority["catalog"],
            operators={
                item["operator_id"]: item
                for item in self.authority["mutation_operators"]["operators"]
            },
            automatic_operator_refs={
                "episode_outcome",
                "meaning_preserving_paraphrase",
                "meaning_preserving_case_mutation",
                "material_semantic_change",
                "boundary_or_interaction_change",
            },
            trace_profiles=self.authority["trace_profiles"],
            relation_profiles=relation_profiles,
        )
        self.assertEqual([], findings)
        self.assertIn(operator["operator_id"], by_operator)
        scenario_coordinates, scenario_groups, universe_findings = (
            required_operator_scenario_universe(
                coordinates=required_coordinates(
                    self.authority["catalog"],
                    self.authority["policy"],
                ),
                operators={
                    item["operator_id"]: item
                    for item in self.authority["mutation_operators"][
                        "operators"
                    ]
                },
                scenarios_by_operator=by_operator,
            )
        )
        self.assertEqual([], universe_findings)
        self.assertEqual(1, len(scenario_coordinates))
        self.assertEqual(
            scenario["scenario_id"],
            scenario_coordinates[0]["operator_scenario_ref"],
        )
        self.assertEqual(1, len(scenario_groups))
        self.assertEqual(
            scenario["scenario_id"],
            scenario_groups[0]["scenario_binding"]["scenario_ref"],
        )
        all_coordinates = required_coordinates(
            self.authority["catalog"],
            self.authority["policy"],
        )
        coordinate_by_id = {
            item["execution_cell_id"]: item
            for item in [*all_coordinates, *scenario_coordinates]
        }
        relation_cells = []
        for member in scenario_groups[0]["members"]:
            coordinate = coordinate_by_id[member["execution_cell_id"]]
            relation_cells.append(
                {
                    "execution_cell_id": coordinate["execution_cell_id"],
                    "episode_id": coordinate["episode_id"],
                    "lane": coordinate["lane"],
                    "case_variant": coordinate["case_variant"],
                    "wording_sha256": canonical_sha256(
                        coordinate["wording_authority_ref"]
                    ),
                    "visible_turn": coordinate["visible_turn"],
                    "paraphrase_index": coordinate["paraphrase_index"],
                    "repeat_index": coordinate["repeat_index"],
                    "operator_scenario_ref": coordinate.get(
                        "operator_scenario_ref"
                    ),
                }
            )
        authority_findings = _validate_relation_groups(
            {
                "cells": relation_cells,
                "relation_groups": scenario_groups,
            },
            operators={
                item["operator_id"]: item
                for item in self.authority["mutation_operators"]["operators"]
            },
            scenarios={scenario["scenario_id"]: scenario},
            episodes={
                episode["episode_id"]: episode
                for episode in self.authority["catalog"]["episodes"]
            },
            scenario_registry=registry,
        )
        self.assertTrue(
            any("scenario executor is unavailable" in item for item in authority_findings)
        )
        self.assertTrue(
            any("unreviewed scenario" in item for item in authority_findings)
        )

        drifted = copy.deepcopy(registry)
        drifted["scenarios"][0]["operator_sha256"] = "0" * 64
        _, findings = validate_scenario_authority(
            drifted,
            catalog=self.authority["catalog"],
            operators={
                item["operator_id"]: item
                for item in self.authority["mutation_operators"]["operators"]
            },
            automatic_operator_refs=set(),
            trace_profiles=self.authority["trace_profiles"],
            relation_profiles=relation_profiles,
        )
        self.assertTrue(any("operator hash drifted" in item for item in findings))

    def test_compiled_episode_relation_universe_is_structurally_satisfiable(
        self,
    ) -> None:
        coordinates = required_coordinates(
            self.authority["catalog"],
            self.authority["policy"],
        )
        operators = {
            item["operator_id"]: item
            for item in self.authority["mutation_operators"]["operators"]
        }
        groups = required_episode_relation_groups(
            self.authority["catalog"],
            self.authority["policy"],
            operators,
            coordinates,
        )
        cells = [
            {
                "execution_cell_id": coordinate["execution_cell_id"],
                "episode_id": coordinate["episode_id"],
                "lane": coordinate["lane"],
                "case_variant": coordinate["case_variant"],
                "visible_turn": coordinate["visible_turn"],
                "paraphrase_index": coordinate["paraphrase_index"],
                "repeat_index": coordinate["repeat_index"],
                "wording_sha256": canonical_sha256(
                    coordinate["wording_authority_ref"]
                ),
                "operator_scenario_ref": None,
            }
            for coordinate in coordinates
        ]
        self.assertEqual(
            [],
            _validate_relation_groups(
                {"cells": cells, "relation_groups": groups},
                operators=operators,
                scenarios={},
                episodes={
                    episode["episode_id"]: episode
                    for episode in self.authority["catalog"]["episodes"]
                },
                scenario_registry=self.authority[
                    "operator_scenario_authority"
                ],
            ),
        )

    def test_full_universe_rejects_coordinate_and_relation_shrink(self) -> None:
        coordinates = required_coordinates(
            self.authority["catalog"],
            self.authority["policy"],
        )
        operators = {
            item["operator_id"]: item
            for item in self.authority["mutation_operators"]["operators"]
        }
        relation_groups = required_episode_relation_groups(
            self.authority["catalog"],
            self.authority["policy"],
            operators,
            coordinates,
        )
        cells = [
            {
                **coordinate,
                "source_authority_kind": "candidate_episode",
            }
            for coordinate in coordinates
        ]
        covered_operators = {item["operator_ref"] for item in relation_groups}
        relation_groups.extend(
            {
                "relation_group_id": f"REL-SCENARIO-{operator_id}",
                "operator_ref": operator_id,
                "scenario_binding": None,
            }
            for operator_id in operators
            if operator_id != "episode_outcome"
            and operator_id not in covered_operators
        )
        manifest = {
            "execution_scope": "development",
            "run_mode": "full",
            "cells": cells,
            "relation_groups": relation_groups,
        }
        readiness = self.readiness()

        missing_cell = copy.deepcopy(manifest)
        missing_cell["cells"].pop()
        findings = _validate_full_run_universe(
            missing_cell,
            self.authority,
            universe_readiness=readiness,
        )
        self.assertIn(
            "full run execution coordinate set differs from compiled universe",
            findings,
        )

        missing_relation = copy.deepcopy(manifest)
        missing_relation["relation_groups"].pop(0)
        findings = _validate_full_run_universe(
            missing_relation,
            self.authority,
            universe_readiness=readiness,
        )
        self.assertIn(
            "full run Episode relation set differs from compiled universe",
            findings,
        )


if __name__ == "__main__":
    unittest.main()
