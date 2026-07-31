from __future__ import annotations

import unittest
from dataclasses import replace

from gate3_5_business_world_harness import execute_business_world
from gate3_5_business_worlds import BUSINESS_WORLDS
from gate3_5_runtime_fixtures import (
    build_evidence_runtime_world,
    land_evidence_runtime_world,
)
from test_gate3_2_obligation_scheduler import NOW
from waje_vnext.controller import EvidenceRuntime
from waje_vnext.domain.answering import (
    AnswerCandidateStatus,
    AnswerStatus,
    ClaimPrecheckStatus,
)
from waje_vnext.domain.evidence import (
    EvidenceAdmissionProfile,
    EvidenceAdmissionStatus,
    EvidenceValidityStatus,
    ObligationSatisfactionStatus,
    build_physical_query_execution_provenance,
)
from waje_vnext.domain.measurement import ClaimStrengthCeiling
from waje_vnext.domain.workflow import (
    DeliveryState,
    PublicationState,
)


class Gate35BusinessWorldExecutionTest(unittest.TestCase):
    def test_all_twenty_four_worlds_execute_typed_result_space(self) -> None:
        for world in BUSINESS_WORLDS:
            with self.subTest(world_id=world.world_id):
                execution = execute_business_world(world)
                self.assertEqual(
                    execution.evidence_admission_status,
                    EvidenceAdmissionStatus.ACCEPTED,
                )
                self.assertEqual(
                    execution.evidence_validity_status,
                    EvidenceValidityStatus.ADMITTED_VALID,
                )
                self.assertEqual(
                    execution.obligation_status,
                    ObligationSatisfactionStatus.SATISFIED,
                )
                self.assertEqual(
                    execution.accepted_bundle.status,
                    AnswerCandidateStatus.ACCEPTED_PROVISIONAL,
                )
                answer = execution.accepted_bundle.answer
                self.assertIsNotNone(answer)
                assert answer is not None
                self.assertEqual(answer.status, AnswerStatus.PROVISIONAL)
                self.assertEqual(
                    execution.workflow.snapshot.case.publication_state,
                    PublicationState.PROVISIONAL,
                )
                self.assertEqual(
                    execution.workflow.snapshot.case.delivery_state,
                    DeliveryState.NOT_DELIVERED,
                )
                allowed_by_claim = {
                    item.claim_id: set(item.allowed_dispositions)
                    for item in world.claim_expectations
                }
                for claim in execution.claims:
                    self.assertIn(
                        claim.disposition,
                        allowed_by_claim[claim.claim_id],
                    )
                    if claim.mode == "supported":
                        self.assertEqual(
                            claim.first_precheck_status,
                            ClaimPrecheckStatus.ADMISSIBLE_SUPPORTED,
                        )
                        self.assertTrue(claim.appears_in_answer)
                    elif claim.mode == "boundary":
                        self.assertEqual(
                            claim.first_precheck_status,
                            ClaimPrecheckStatus.ADMISSIBLE_BOUNDARY,
                        )
                        self.assertTrue(claim.appears_in_answer)
                    else:
                        self.assertEqual(
                            claim.first_precheck_status,
                            ClaimPrecheckStatus.REJECTED,
                        )
                        self.assertFalse(claim.appears_in_answer)

    def test_production_lane_only_proves_fail_closed_boundaries(self) -> None:
        world = build_evidence_runtime_world(
            "case-gate35-production-boundary-lane",
            evidence_strength=ClaimStrengthCeiling.CAUSAL,
            limitation_refs=(),
        )
        digest = "a" * 64
        with self.assertRaisesRegex(ValueError, "disabled"):
            build_physical_query_execution_provenance(
                logical_execution_id=digest,
                binding=world.binding,
                query_spec_id=digest,
                query_spec_content_sha256=digest,
                capability_invocation_id=digest,
                capability_invocation_content_sha256=digest,
                provider_receipt_id=digest,
                provider_receipt_content_sha256=digest,
                compiler_contract_ref="compiler:gate4-unavailable",
            )

        receipt = land_evidence_runtime_world(world, received_at=NOW)
        production_runtime = EvidenceRuntime(
            store=world.store,
            owner_id="gate35-production-boundary-worker",
            profile=EvidenceAdmissionProfile.PRODUCTION,
        )
        with self.assertRaisesRegex(ValueError, "profile"):
            production_runtime.admit_result(
                receipt_id=receipt.capability_result_receipt_id,
                admitted_at=NOW,
            )
        self.assertEqual(
            world.store.list_evidence_admissions(
                case_id=world.schedule.case_id
            ),
            (),
        )

    def test_expected_result_space_does_not_construct_runtime_input(
        self,
    ) -> None:
        world = next(
            item
            for item in BUSINESS_WORLDS
            if item.world_id == "g3.5:payment_change:partial-gap"
        )
        baseline = execute_business_world(world)
        changed_expectations = replace(
            world,
            claim_expectations=tuple(
                replace(
                    expectation,
                    allowed_dispositions=("supported_provisional",),
                )
                for expectation in world.claim_expectations
            ),
        )
        replay = execute_business_world(changed_expectations)
        self.assertEqual(
            tuple(
                (
                    claim.claim_id,
                    claim.mode,
                    claim.first_precheck_status,
                    claim.appears_in_answer,
                )
                for claim in replay.claims
            ),
            tuple(
                (
                    claim.claim_id,
                    claim.mode,
                    claim.first_precheck_status,
                    claim.appears_in_answer,
                )
                for claim in baseline.claims
            ),
        )
        self.assertEqual(
            {claim.mode for claim in replay.claims},
            {"supported", "boundary", "blocked"},
        )


if __name__ == "__main__":
    unittest.main()
