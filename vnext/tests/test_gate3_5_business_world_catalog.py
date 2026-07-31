from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from gate3_5_business_worlds import (
    BUSINESS_WORLDS,
    BusinessWorld,
    ClaimDisposition,
    QuestionFamily,
    WorldVariant,
    worlds_for_family,
)


EXPECTED_FAMILIES: frozenset[QuestionFamily] = frozenset(
    {
        "payment_change",
        "recurring_pattern",
        "event_impact",
        "revenue_health",
        "factor_attribution",
        "anomaly_review",
        "baseline_comparison",
        "data_quality",
    }
)
EXPECTED_VARIANTS: frozenset[WorldVariant] = frozenset(
    {"supported", "partial_gap", "reversal_or_conflict"}
)
EXPECTED_CRASH_TAGS = frozenset(
    {
        "result_receipt_before_evidence",
        "evidence_before_admission",
        "admission_before_satisfaction",
        "satisfaction_before_answer",
        "answer_before_review",
        "review_before_settlement_report",
        "settlement_report_before_projection",
        "projection_before_outbox_ack",
    }
)
EXPECTED_RACE_TAGS = frozenset(
    {
        "parallel_obligations_out_of_order",
        "correction_vs_effect",
        "correction_vs_review",
        "correction_vs_answer",
        "validity_vs_publication",
    }
)
EXPECTED_DRIFT_TAGS = frozenset(
    {
        "question_frame_plan_heads",
        "comparison_window_identity",
        "exposure_definition",
        "scope_grain_unit",
        "evidence_validity",
        "answer_evidence_binding",
        "reviewed_answer_version",
        "correction_epoch",
    }
)
EXPECTED_REALM_TAGS = frozenset(
    {
        "conformance_only",
        "claim_scoped_mixed_sources",
        "realm_is_system_issued",
    }
)
LOCAL_BOUNDARY_DISPOSITIONS: frozenset[ClaimDisposition] = frozenset(
    {"bounded_provisional", "typed_boundary", "unverifiable"}
)


def _variant(
    worlds: tuple[BusinessWorld, ...],
    variant: WorldVariant,
) -> BusinessWorld:
    return next(world for world in worlds if world.variant == variant)


class Gate35BusinessWorldCatalogTest(unittest.TestCase):
    def test_catalog_is_complete_eight_by_three_matrix(self) -> None:
        self.assertIsInstance(BUSINESS_WORLDS, tuple)
        self.assertEqual(len(BUSINESS_WORLDS), 24)
        self.assertEqual(
            {world.family for world in BUSINESS_WORLDS},
            EXPECTED_FAMILIES,
        )
        self.assertEqual(
            len({world.world_id for world in BUSINESS_WORLDS}),
            len(BUSINESS_WORLDS),
        )
        for family in EXPECTED_FAMILIES:
            worlds = worlds_for_family(family)
            self.assertEqual(len(worlds), 3)
            self.assertEqual(
                {world.variant for world in worlds},
                EXPECTED_VARIANTS,
            )

    def test_every_family_is_multi_claim_and_mixed_strength(self) -> None:
        for family in EXPECTED_FAMILIES:
            worlds = worlds_for_family(family)
            claim_id_sets = [
                {claim.claim_id for claim in world.claim_targets}
                for world in worlds
            ]
            self.assertTrue(
                all(len(claim_ids) >= 4 for claim_ids in claim_id_sets),
                family,
            )
            self.assertTrue(
                all(
                    claim_ids == claim_id_sets[0]
                    for claim_ids in claim_id_sets[1:]
                ),
                family,
            )
            strengths = {
                claim.strength_ceiling
                for claim in worlds[0].claim_targets
            }
            self.assertGreaterEqual(len(strengths), 2, family)

    def test_partial_gap_is_local_and_preserves_supported_claims(
        self,
    ) -> None:
        for family in EXPECTED_FAMILIES:
            partial = _variant(
                worlds_for_family(family),
                "partial_gap",
            )
            allowed_by_claim = {
                expectation.claim_id: set(
                    expectation.allowed_dispositions
                )
                for expectation in partial.claim_expectations
            }
            self.assertTrue(
                any(
                    dispositions & LOCAL_BOUNDARY_DISPOSITIONS
                    for dispositions in allowed_by_claim.values()
                ),
                family,
            )
            self.assertTrue(
                any(
                    dispositions == {"supported_provisional"}
                    for dispositions in allowed_by_claim.values()
                ),
                family,
            )
            self.assertIn(
                "global_degradation_from_local_gap",
                partial.forbidden_outcomes,
            )
            affected_claims = {
                claim_id
                for relation in partial.evidence_relations
                if relation.kind in {"qualifies", "invalidates"}
                for claim_id in relation.claim_ids
            }
            self.assertTrue(affected_claims, family)
            self.assertTrue(
                all(
                    allowed_by_claim[claim_id]
                    & LOCAL_BOUNDARY_DISPOSITIONS
                    for claim_id in affected_claims
                ),
                family,
            )
            for relation in partial.evidence_relations:
                if relation.kind == "qualifies":
                    self.assertTrue(
                        all(
                            "typed_boundary" in allowed_by_claim[claim_id]
                            for claim_id in relation.claim_ids
                        ),
                        family,
                    )
                elif relation.kind == "invalidates":
                    self.assertTrue(
                        all(
                            "unverifiable" in allowed_by_claim[claim_id]
                            for claim_id in relation.claim_ids
                        ),
                        family,
                    )

    def test_claim_expectations_and_evidence_are_claim_scoped(
        self,
    ) -> None:
        for world in BUSINESS_WORLDS:
            claim_ids = {
                claim.claim_id for claim in world.claim_targets
            }
            expectation_ids = {
                expectation.claim_id
                for expectation in world.claim_expectations
            }
            self.assertEqual(expectation_ids, claim_ids, world.world_id)
            self.assertEqual(
                len(expectation_ids),
                len(world.claim_expectations),
                world.world_id,
            )
            for expectation in world.claim_expectations:
                self.assertTrue(
                    expectation.allowed_dispositions,
                    world.world_id,
                )
            relation_ids = [
                relation.relation_id
                for relation in world.evidence_relations
            ]
            self.assertEqual(
                len(relation_ids),
                len(set(relation_ids)),
                world.world_id,
            )
            for relation in world.evidence_relations:
                self.assertTrue(relation.claim_ids, world.world_id)
                self.assertLessEqual(
                    set(relation.claim_ids),
                    claim_ids,
                    world.world_id,
                )

    def test_result_space_keeps_gate_three_publication_closed(
        self,
    ) -> None:
        for world in BUSINESS_WORLDS:
            self.assertIn("settled_answer", world.forbidden_outcomes)
            self.assertIn("delivered_answer", world.forbidden_outcomes)
            self.assertIn(
                "completed_workflow_from_execution_success",
                world.forbidden_outcomes,
            )
            dispositions = {
                disposition
                for expectation in world.claim_expectations
                for disposition in expectation.allowed_dispositions
            }
            self.assertNotIn("settled", dispositions)
            self.assertNotIn("delivered", dispositions)

    def test_sibling_mutations_are_complete_and_minimal(self) -> None:
        for family in EXPECTED_FAMILIES:
            worlds = worlds_for_family(family)
            supported = _variant(worlds, "supported")
            partial = _variant(worlds, "partial_gap")
            conflict = _variant(worlds, "reversal_or_conflict")

            self.assertEqual(
                {
                    world.sibling_mutation.sibling_group
                    for world in worlds
                },
                {f"g3.5:{family}"},
            )
            self.assertTrue(
                all(
                    world.sibling_mutation.base_world_id
                    == supported.world_id
                    for world in worlds
                )
            )
            self.assertEqual(supported.sibling_mutation.kind, "base")
            self.assertEqual(
                supported.sibling_mutation.changed_business_facts,
                (),
            )
            self.assertEqual(
                supported.sibling_mutation.expected_changed_properties,
                (),
            )
            self.assertEqual(
                partial.sibling_mutation.kind,
                "local_contract_or_coverage_gap",
            )
            self.assertEqual(
                conflict.sibling_mutation.kind,
                "sensitivity_or_counterevidence",
            )
            for sibling in (partial, conflict):
                self.assertTrue(
                    sibling.sibling_mutation.changed_business_facts,
                    sibling.world_id,
                )
                self.assertTrue(
                    sibling.sibling_mutation.expected_changed_properties,
                    sibling.world_id,
                )
                self.assertEqual(
                    sibling.sibling_mutation.stable_properties,
                    supported.sibling_mutation.stable_properties,
                )
            self.assertTrue(
                any(
                    relation.kind == "contradicts"
                    for relation in conflict.evidence_relations
                ),
                family,
            )
            self.assertTrue(
                any(
                    set(expectation.allowed_dispositions)
                    & {"bounded_provisional", "revoked"}
                    for expectation in conflict.claim_expectations
                ),
                family,
            )

    def test_runtime_risk_tags_cover_all_required_boundaries(self) -> None:
        self.assertEqual(
            {
                tag
                for world in BUSINESS_WORLDS
                for tag in world.crash_tags
            },
            EXPECTED_CRASH_TAGS,
        )
        self.assertEqual(
            {
                tag
                for world in BUSINESS_WORLDS
                for tag in world.race_tags
            },
            EXPECTED_RACE_TAGS,
        )
        self.assertEqual(
            {
                tag
                for world in BUSINESS_WORLDS
                for tag in world.drift_tags
            },
            EXPECTED_DRIFT_TAGS,
        )
        self.assertEqual(
            {
                tag
                for world in BUSINESS_WORLDS
                for tag in world.realm_tags
            },
            EXPECTED_REALM_TAGS,
        )

    def test_catalog_keeps_analysis_design_open(self) -> None:
        required_freedoms = {
            "measurement_window_when_not_user_fixed",
            "comparison_or_estimator",
            "investigation_order",
            "evidence_gathering_route",
            "driver_ranking",
        }
        forbidden_implementation_tokens = (
            "select ",
            "queryspec",
            "call_capability",
            "run_probe",
            "1-7",
            "8-月底",
            "各7天",
            "7日窗口",
            "top 3 =",
        )
        for world in BUSINESS_WORLDS:
            self.assertEqual(
                set(world.open_design_dimensions),
                required_freedoms,
                world.world_id,
            )
            serialized_business_text = " ".join(
                (
                    world.user_question,
                    world.decision_stake,
                    *(
                        claim.business_meaning
                        for claim in world.claim_targets
                    ),
                    *(
                        relation.business_observation
                        for relation in world.evidence_relations
                    ),
                    *world.forbidden_outcomes,
                )
            ).lower()
            for token in forbidden_implementation_tokens:
                self.assertNotIn(
                    token,
                    serialized_business_text,
                    world.world_id,
                )

    def test_fixture_objects_are_frozen(self) -> None:
        world = BUSINESS_WORLDS[0]
        with self.assertRaises(FrozenInstanceError):
            world.variant = "partial_gap"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            world.claim_targets[0].business_meaning = (  # type: ignore[misc]
                "mutated"
            )


if __name__ == "__main__":
    unittest.main()
