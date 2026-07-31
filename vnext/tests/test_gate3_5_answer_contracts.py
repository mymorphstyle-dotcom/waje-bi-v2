from __future__ import annotations

import unittest
from dataclasses import replace

import test_gate3_5_evidence_contracts as evidence_fixtures
from gate1_fixtures import NOW
from waje_vnext.domain.answering import (
    AnalysisCheckDisposition,
    AnalysisCheckKind,
    AnalysisCheckStatus,
    AnswerCandidateStatus,
    ClaimEvidenceSupport,
    ClaimPrecheckStatus,
    EvidenceSelection,
    NarrativeBlockProposal,
    ProposedClaim,
    SettlementPreconditionStatus,
    build_provisional_answer_candidate,
    build_analysis_check_disposition,
    compile_provisional_answer_bundle,
    derive_settlement_precondition_report,
    validate_provisional_answer_candidate,
)
from waje_vnext.domain.answering import AnswerStatus
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.evidence import (
    EvidenceValidityStatus,
    build_evidence_use_binding,
    build_evidence_validity_successor,
    build_obligation_satisfaction,
)
from waje_vnext.domain.measurement import ClaimStrengthCeiling


class Gate35AnswerContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture = evidence_fixtures.Gate35EvidenceContractsTest()
        fixture.setUp()
        self.fixture = fixture
        requirement = fixture.binding.requirement_binding
        self.proposal = ProposedClaim(
            proposal_claim_key="payment-change",
            statement="目标窗口的有效观察日归一化付费金额更高。",
            target_estimand_id=fixture.binding.estimand_id,
            obligation_ids=(fixture.obligation.obligation_id,),
            evidence_selections=(
                EvidenceSelection(
                    evidence_record_id=(
                        fixture.evidence.evidence_record_id
                    ),
                    role_ref="primary_estimate",
                ),
            ),
            applicability_scope=fixture.scope,
            requested_strength=ClaimStrengthCeiling.DESCRIPTIVE,
            boundary_satisfaction_record_ids=(),
            limitation_refs=fixture.evidence.limitation_refs,
            contradiction_refs=(),
            falsification_refs=requirement.linked_falsification_ids,
            reversal_refs=requirement.linked_reversal_ids,
            depends_on_proposal_claim_keys=(),
        )
        self.candidate = build_provisional_answer_candidate(
            case_id=fixture.case.case_id,
            current_authority=fixture.snapshot,
            plan_adoption=fixture.bundle.adoption,
            version_number=1,
            prior_answer_version_id=None,
            claims=(self.proposal,),
            narrative_blocks=(
                NarrativeBlockProposal(
                    block_key="finding",
                    markdown=(
                        "在当前口径和数据覆盖范围内，"
                        "目标窗口的日均付费更高。"
                    ),
                    proposal_claim_keys=("payment-change",),
                ),
            ),
            created_by_action_id="action:propose-answer",
            created_at=NOW,
        )
        self.check_dispositions = tuple(
            build_analysis_check_disposition(
                check_id=check_id,
                kind=kind,
                status=AnalysisCheckStatus.SATISFIED,
                source_authority_ref=fixture.evidence.evidence_record_id,
                source_authority_content_sha256=(
                    fixture.evidence.content_sha256
                ),
                limitation_ref=None,
            )
            for kind, values in (
                (
                    AnalysisCheckKind.FALSIFICATION,
                    requirement.linked_falsification_ids,
                ),
                (
                    AnalysisCheckKind.REVERSAL,
                    requirement.linked_reversal_ids,
                ),
            )
            for check_id in values
        )
        self.support, self.satisfaction = self._support_for(
            self.candidate,
            self.proposal,
            validity=fixture.validity,
        )

    def _compile(
        self,
        *,
        candidate=None,
        supports=None,
        satisfactions=None,
        check_dispositions=None,
    ):
        candidate = candidate or self.candidate
        proposal = candidate.claims[0]
        if supports is None or satisfactions is None:
            default_support, default_satisfaction = self._support_for(
                candidate,
                proposal,
                validity=self.fixture.validity,
            )
        return compile_provisional_answer_bundle(
            candidate=candidate,
            current_authority=self.fixture.snapshot,
            plan_adoption=self.fixture.bundle.adoption,
            supports_by_claim_key=(
                supports
                if supports is not None
                else {"payment-change": (default_support,)}
            ),
            satisfactions_by_claim_key=(
                satisfactions
                if satisfactions is not None
                else {
                    "payment-change": (default_satisfaction,)
                }
            ),
            check_dispositions_by_claim_key={
                "payment-change": (
                    self.check_dispositions
                    if check_dispositions is None
                    else check_dispositions
                )
            },
            checked_at=NOW,
        )

    def test_system_derives_claim_and_answer_identity(self) -> None:
        bundle = self._compile()
        self.assertEqual(
            bundle.status,
            AnswerCandidateStatus.ACCEPTED_PROVISIONAL,
        )
        self.assertIsNotNone(bundle.answer)
        answer = bundle.answer
        assert answer is not None
        self.assertEqual(answer.status, AnswerStatus.PROVISIONAL)
        self.assertEqual(
            answer.claims[0].evidence_use_binding_ids,
            (self.support.use_binding.evidence_use_binding_id,),
        )
        self.assertNotEqual(
            answer.claims[0].claim_id,
            self.proposal.proposal_claim_key,
        )
        self.assertEqual(
            answer.claims[0].claim_precheck_id,
            bundle.prechecks[0].claim_precheck_id,
        )

    def test_llm_cannot_submit_claim_identity(self) -> None:
        values = {
            name: getattr(self.proposal, name)
            for name in self.proposal.__dataclass_fields__
        }
        values["claim_id"] = "caller-owned"
        with self.assertRaises(TypeError):
            ProposedClaim(**values)

    def test_required_limitation_omission_rejects_candidate(self) -> None:
        proposal = replace(self.proposal, limitation_refs=())
        candidate = replace(
            self.candidate,
            answer_candidate_id=self._candidate_id_for(proposal),
            claims=(proposal,),
        )
        bundle = self._compile(candidate=candidate)
        self.assertEqual(bundle.status, AnswerCandidateStatus.REJECTED)
        self.assertIsNone(bundle.answer)
        self.assertEqual(
            bundle.prechecks[0].status,
            ClaimPrecheckStatus.REJECTED,
        )
        self.assertIn(
            "required_limitation_omitted",
            bundle.prechecks[0].reason_codes,
        )

    def test_forged_or_unbound_analysis_check_is_rejected(self) -> None:
        bound = build_analysis_check_disposition(
            check_id="check:unreferenced",
            kind=AnalysisCheckKind.CONTRADICTION,
            status=AnalysisCheckStatus.SATISFIED,
            source_authority_ref=(
                self.fixture.evidence.evidence_record_id
            ),
            source_authority_content_sha256=(
                self.fixture.evidence.content_sha256
            ),
            limitation_ref=None,
        )
        forged = replace(
            bound,
            status=AnalysisCheckStatus.TRIGGERED,
        )
        bundle = self._compile(
            check_dispositions=(forged,)
        )
        self.assertIs(bundle.status, AnswerCandidateStatus.REJECTED)
        self.assertIn(
            "analysis_check_disposition_forged",
            bundle.prechecks[0].reason_codes,
        )

        unbound = build_analysis_check_disposition(
            check_id=bound.check_id,
            kind=bound.kind,
            status=AnalysisCheckStatus.SATISFIED,
            source_authority_ref=content_sha256(
                {"evidence": "outside-claim"}
            ),
            source_authority_content_sha256=content_sha256(
                {"payload": "outside-claim"}
            ),
            limitation_ref=None,
        )
        bundle = self._compile(
            check_dispositions=(unbound,)
        )
        self.assertIs(bundle.status, AnswerCandidateStatus.REJECTED)
        self.assertIn(
            "analysis_check_source_not_admitted",
            bundle.prechecks[0].reason_codes,
        )

    def test_scope_or_strength_drift_rejects_candidate(self) -> None:
        stronger = replace(
            self.proposal,
            requested_strength=ClaimStrengthCeiling.CAUSAL,
        )
        candidate = replace(
            self.candidate,
            answer_candidate_id=self._candidate_id_for(stronger),
            claims=(stronger,),
        )
        bundle = self._compile(candidate=candidate)
        self.assertEqual(bundle.status, AnswerCandidateStatus.REJECTED)
        self.assertIn(
            "claim_strength_request_mismatch",
            bundle.prechecks[0].reason_codes,
        )

    def test_forged_or_stale_candidate_fails_closed(self) -> None:
        forged = replace(
            self.candidate,
            answer_candidate_id="f" * 64,
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_provisional_answer_candidate(
                candidate=forged,
                current_authority=self.fixture.snapshot,
                plan_adoption=self.fixture.bundle.adoption,
            )
        stale = replace(
            self.fixture.snapshot,
            head_version=self.fixture.snapshot.head_version + 1,
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_provisional_answer_candidate(
                candidate=self.candidate,
                current_authority=stale,
                plan_adoption=self.fixture.bundle.adoption,
            )

    def test_revoked_evidence_cannot_pass_claim_precheck(self) -> None:
        revoked = build_evidence_validity_successor(
            prior=self.fixture.validity,
            status=EvidenceValidityStatus.REVOKED,
            reason_code="source_contract_revoked",
            recorded_at=NOW,
        )
        support = replace(self.support, validity=revoked)
        bundle = self._compile(
            supports={"payment-change": (support,)}
            ,
            satisfactions={
                "payment-change": (self.satisfaction,)
            },
        )
        self.assertEqual(bundle.status, AnswerCandidateStatus.REJECTED)
        self.assertIn(
            "evidence_use_invalid",
            bundle.prechecks[0].reason_codes,
        )

    def test_conformance_answer_is_blocked_from_future_settlement(self) -> None:
        bundle = self._compile()
        answer = bundle.answer
        assert answer is not None
        trace_sha = content_sha256({"trace": "complete"})
        report = derive_settlement_precondition_report(
            answer=answer,
            candidate=bundle.candidate,
            prechecks=bundle.prechecks,
            supports=(self.support,),
            satisfactions=(self.satisfaction,),
            current_authority=self.fixture.snapshot,
            plan_adoption=self.fixture.bundle.adoption,
            objection_disposition_refs=(),
            unresolved_blocking_objection_refs=(),
            trace_manifest_id=trace_sha,
            trace_manifest_content_sha256=trace_sha,
            trace_complete=True,
            created_at=NOW,
        )
        self.assertEqual(
            report.status,
            SettlementPreconditionStatus.BLOCKED,
        )
        self.assertEqual(
            report.fail_reason_codes,
            ("production_evidence_unavailable",),
        )
        self.assertEqual(
            report.answer_version_id,
            answer.answer_version_id,
        )

    def test_open_objection_and_incomplete_trace_are_explicit(self) -> None:
        bundle = self._compile()
        answer = bundle.answer
        assert answer is not None
        trace_sha = content_sha256({"trace": "incomplete"})
        reviewer_advanced_authority = replace(
            self.fixture.snapshot,
            head_version=self.fixture.snapshot.head_version + 1,
            contradiction_state_version=(
                self.fixture.snapshot.contradiction_state_version + 1
            ),
        )
        report = derive_settlement_precondition_report(
            answer=answer,
            candidate=bundle.candidate,
            prechecks=bundle.prechecks,
            supports=(self.support,),
            satisfactions=(self.satisfaction,),
            current_authority=reviewer_advanced_authority,
            plan_adoption=self.fixture.bundle.adoption,
            objection_disposition_refs=("objection:one",),
            unresolved_blocking_objection_refs=("objection:one",),
            trace_manifest_id=trace_sha,
            trace_manifest_content_sha256=trace_sha,
            trace_complete=False,
            created_at=NOW,
        )
        self.assertEqual(
            set(report.fail_reason_codes),
            {
                "blocking_objection_open",
                "production_evidence_unavailable",
                "trace_incomplete",
            },
        )
        self.assertNotIn(
            "stale_answer_authority",
            report.fail_reason_codes,
        )

    def _candidate_id_for(self, proposal: ProposedClaim) -> str:
        candidate = build_provisional_answer_candidate(
            case_id=self.fixture.case.case_id,
            current_authority=self.fixture.snapshot,
            plan_adoption=self.fixture.bundle.adoption,
            version_number=1,
            prior_answer_version_id=None,
            claims=(proposal,),
            narrative_blocks=self.candidate.narrative_blocks,
            created_by_action_id=self.candidate.created_by_action_id,
            created_at=NOW,
        )
        return candidate.answer_candidate_id

    def _support_for(
        self,
        candidate,
        proposal,
        *,
        validity,
    ):
        use = build_evidence_use_binding(
            evidence=self.fixture.evidence,
            admission=self.fixture.admission,
            validity=validity,
            binding=self.fixture.binding,
            answer_candidate_id=candidate.answer_candidate_id,
            proposal_claim_key=proposal.proposal_claim_key,
            claim_scope=proposal.applicability_scope,
            requested_claim_strength=(
                proposal.requested_strength
                if proposal.requested_strength
                is not ClaimStrengthCeiling.CAUSAL
                else ClaimStrengthCeiling.DESCRIPTIVE
            ),
            bound_at=NOW,
        )
        satisfaction = build_obligation_satisfaction(
            obligation=self.fixture.obligation,
            admissions=(self.fixture.admission,),
            validities=(validity,),
            boundary_outcome=None,
            prior=None,
            recorded_at=NOW,
        )
        support = ClaimEvidenceSupport(
            evidence=self.fixture.evidence,
            admission=self.fixture.admission,
            validity=validity,
            query_binding=self.fixture.binding,
            use_binding=use,
        )
        return support, satisfaction


if __name__ == "__main__":
    unittest.main()
