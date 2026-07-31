from __future__ import annotations

import unittest
from datetime import timedelta

import test_gate3_5_evidence_runtime as evidence_runtime_fixtures
from waje_vnext.controller import (
    ScriptedEffectExecutor,
    WAJEController,
)
from waje_vnext.domain.actions import (
    ActionKind,
    AgentActionProposal,
    ProposeAnswerPayload,
    RecordInterpretationPayload,
)
from waje_vnext.domain.answering import (
    EvidenceSelection,
    NarrativeBlockProposal,
    ProposedClaim,
    SettlementPreconditionStatus,
    build_provisional_answer_candidate,
)
from waje_vnext.domain.action_codec import decode_agent_action_proposal
from waje_vnext.domain.async_runtime import (
    AsyncJobKind,
    MailboxMessageKind,
)
from waje_vnext.domain.answering import AnswerStatus
from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.controller import ControllerPhase
from waje_vnext.domain.measurement import ClaimStrengthCeiling
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.evidence import EvidenceValidityStatus
from waje_vnext.providers import ScriptedPrimaryAgentProvider
from waje_vnext.storage import InvalidAuthorityTransition, StaleHead


NOW = evidence_runtime_fixtures.NOW


class Gate35AnswerRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture = (
            evidence_runtime_fixtures.Gate35EvidenceRuntimeTest(
                methodName="test_t1_is_durable_without_making_job_terminal"
            )
        )
        fixture.setUp()
        receipt = fixture._land()
        fixture.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )
        self.controller = fixture.controller
        self.store = fixture.store
        self.run_id = fixture.run_id
        self.case_id = fixture.schedule.case_id
        self.evidence = fixture.envelope.evidence_record
        self.scope = fixture.scope
        self.obligation = fixture.obligation
        prior = self.controller.resume(self.case_id)
        prior_packet = self.store.get_context_packet(
            prior.context_packet_id
        )
        self.current = self.controller._checkpoint(
            run_id=prior.run_id,
            case_id=prior.case_id,
            phase=ControllerPhase.READY_FOR_AGENT,
            step_number=prior.step_number,
            latest_user_message=prior.latest_user_message,
            pending_action_id=None,
            pending_job_ids=(),
            pending_decision_request_id=None,
            consecutive_rejections=prior.consecutive_rejections,
            now=NOW,
            authority_epoch=prior.authority_epoch,
            mailbox_cursor=prior.mailbox_cursor,
            context_user_messages=prior_packet.user_messages,
        )

    def _proposal(
        self,
        *,
        requested_strength: ClaimStrengthCeiling,
    ) -> AgentActionProposal:
        claim = ProposedClaim(
            proposal_claim_key="payment-window-direction",
            statement=(
                "目标窗口按有效观察日归一化的付费金额更高。"
            ),
            target_estimand_id=self.evidence.estimand_id,
            obligation_ids=(self.obligation.obligation_id,),
            evidence_selections=(
                EvidenceSelection(
                    evidence_record_id=(
                        self.evidence.evidence_record_id
                    ),
                    role_ref="primary-estimate",
                ),
            ),
            applicability_scope=self.scope,
            requested_strength=requested_strength,
            boundary_satisfaction_record_ids=(),
            limitation_refs=self.evidence.limitation_refs,
            contradiction_refs=(),
            falsification_refs=(),
            reversal_refs=(),
            depends_on_proposal_claim_keys=(),
        )
        payload = ProposeAnswerPayload(
            claims=(claim,),
            narrative_blocks=(
                NarrativeBlockProposal(
                    block_key="finding",
                    markdown=claim.statement,
                    proposal_claim_keys=(
                        claim.proposal_claim_key,
                    ),
                ),
            ),
        )
        return AgentActionProposal(
            kind=ActionKind.PROPOSE_ANSWER,
            payload=payload,
        )

    def _deliver(
        self,
        *,
        requested_strength: ClaimStrengthCeiling,
    ):
        provider = ScriptedPrimaryAgentProvider(
            (
                self._proposal(
                    requested_strength=requested_strength
                ),
            )
        )
        controller = WAJEController(
            store=self.store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id=(
                f"gate35-answer-{requested_strength.value}-worker"
            ),
            clock=lambda: NOW,
        )
        waiting = controller.advance(self.case_id)
        self.assertEqual(
            waiting.phase,
            ControllerPhase.WAITING_FOR_LLM,
        )
        return controller.deliver_pending_llm(self.case_id)

    def test_accepted_provisional_answer_waits_for_review(self) -> None:
        state = self._deliver(
            requested_strength=ClaimStrengthCeiling.DESCRIPTIVE,
        )

        self.assertEqual(
            state.phase,
            ControllerPhase.WAITING_FOR_REVIEW,
        )
        self.assertNotEqual(state.phase, ControllerPhase.COMPLETED)
        self.assertEqual(len(state.pending_job_ids), 1)
        answer = self.store.get_answer(
            state.accepted_answer_version_id or ""
        )
        self.assertEqual(answer.status, AnswerStatus.PROVISIONAL)
        review_job = self.store.get_outbox_message(
            state.pending_job_ids[0]
        )
        self.assertEqual(review_job.job_kind, AsyncJobKind.REVIEWER)
        self.assertEqual(
            review_job.contract_ref,
            "waje-vnext://runtime/provisional-answer-review-job.v1",
        )
        self.assertEqual(
            review_job.payload["answer_version_id"],
            answer.answer_version_id,
        )
        manifest = self.controller.build_run_trace_manifest(
            self.case_id
        )
        report = self.store.derive_settlement_precondition(
            case_id=self.case_id,
            expected_head_version=self.store.get_case(
                self.case_id
            ).head_version,
            answer_version_id=answer.answer_version_id,
            objection_disposition_refs=(),
            unresolved_blocking_objection_refs=(),
            trace_manifest_id=manifest.trace_manifest_id,
            trace_manifest_content_sha256=content_sha256(manifest),
            trace_complete=True,
            event_id="event:gate35:settlement-precondition",
            recorded_at=NOW,
        )
        self.assertEqual(
            report.status,
            SettlementPreconditionStatus.BLOCKED,
        )
        self.assertIn(
            "production_evidence_unavailable",
            report.fail_reason_codes,
        )
        self.assertEqual(
            self.store.get_settlement_precondition(
                report.settlement_precondition_report_id
            ),
            report,
        )
        replay = self.store.derive_settlement_precondition(
            case_id=self.case_id,
            expected_head_version=self.store.get_case(
                self.case_id
            ).head_version,
            answer_version_id=answer.answer_version_id,
            objection_disposition_refs=(),
            unresolved_blocking_objection_refs=(),
            trace_manifest_id=manifest.trace_manifest_id,
            trace_manifest_content_sha256=content_sha256(manifest),
            trace_complete=True,
            event_id="event:gate35:settlement-precondition",
            recorded_at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(replay, report)
        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "trace manifest identity",
        ):
            self.store.derive_settlement_precondition(
                case_id=self.case_id,
                expected_head_version=self.store.get_case(
                    self.case_id
                ).head_version,
                answer_version_id=answer.answer_version_id,
                objection_disposition_refs=(),
                unresolved_blocking_objection_refs=(),
                trace_manifest_id=manifest.trace_manifest_id,
                trace_manifest_content_sha256="f" * 64,
                trace_complete=True,
                event_id=(
                    "event:gate35:settlement-precondition-forged"
                ),
                recorded_at=NOW,
            )
        self.store.transition_evidence_validity(
            evidence_record_id=self.evidence.evidence_record_id,
            status=EvidenceValidityStatus.REVOKED,
            reason_code="data_contract_revoked",
            event_id="event:gate35:revoke-after-answer",
            recorded_at=NOW + timedelta(seconds=2),
        )
        changed_manifest = self.controller.build_run_trace_manifest(
            self.case_id
        )
        changed_report = self.store.derive_settlement_precondition(
            case_id=self.case_id,
            expected_head_version=self.store.get_case(
                self.case_id
            ).head_version,
            answer_version_id=answer.answer_version_id,
            objection_disposition_refs=(),
            unresolved_blocking_objection_refs=(),
            trace_manifest_id=changed_manifest.trace_manifest_id,
            trace_manifest_content_sha256=content_sha256(
                changed_manifest
            ),
            trace_complete=True,
            event_id="event:gate35:settlement-after-revocation",
            recorded_at=NOW + timedelta(seconds=3),
        )
        self.assertIn(
            "obligation_closure_changed",
            changed_report.fail_reason_codes,
        )
        self.assertIn(
            "evidence_not_currently_valid",
            changed_report.fail_reason_codes,
        )
        candidate = self.store.get_answer_candidate(
            answer.answer_candidate_id
        )
        self.controller.ingress_message(
            case_id=self.case_id,
            thread_id=f"thread-{self.case_id}",
            run_id=self.run_id,
            user_message="请按新的业务口径重新调查。",
            kind=MailboxMessageKind.USER_CORRECTION,
            idempotency_key="gate35-answer-correction",
        )
        with self.assertRaisesRegex(
            StaleHead,
            "superseded after acceptance",
        ):
            self.store.accept_provisional_answer_candidate(
                candidate=candidate,
                expected_head_version=(
                    candidate.authority_snapshot.head_version
                ),
                event_id="event:gate35:stale-answer-retry",
                recorded_at=NOW,
                operation=self.store.get_action(
                    candidate.created_by_action_id
                ).action.operation,
            )

    def test_precheck_rejection_returns_to_agent_without_answer(
        self,
    ) -> None:
        state = self._deliver(
            requested_strength=ClaimStrengthCeiling.CAUSAL,
        )

        self.assertEqual(
            state.phase,
            ControllerPhase.READY_FOR_AGENT,
        )
        self.assertEqual(state.pending_job_ids, ())
        self.assertIsNone(state.accepted_answer_version_id)
        self.assertEqual(
            state.consecutive_rejections,
            self.current.consecutive_rejections + 1,
        )
        self.assertIsNone(
            self.store.latest_answer(self.case_id)
        )

    def test_storage_rejects_candidate_from_non_answer_action(
        self,
    ) -> None:
        proposal = self._proposal(
            requested_strength=ClaimStrengthCeiling.DESCRIPTIVE
        )
        case = self.store.get_case(self.case_id)
        frame = self.store.get_frame(
            case.accepted_frame_revision_id or ""
        )
        adoption = self.store.get_plan_adoption(
            case.accepted_plan_revision_id or ""
        )
        candidate = build_provisional_answer_candidate(
            case_id=self.case_id,
            current_authority=self.store.get_authority_snapshot(
                self.case_id
            ),
            plan_adoption=adoption,
            version_number=1,
            prior_answer_version_id=None,
            claims=proposal.payload.claims,
            narrative_blocks=proposal.payload.narrative_blocks,
            created_by_action_id=frame.created_by_action_id,
            created_at=NOW,
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "propose_answer action",
        ):
            self.store.accept_provisional_answer_candidate(
                candidate=candidate,
                expected_head_version=case.head_version,
                event_id="event:forged-answer-action",
                recorded_at=NOW,
            )

    def test_action_codec_uses_only_the_current_answer_contract(
        self,
    ) -> None:
        proposal = self._proposal(
            requested_strength=ClaimStrengthCeiling.DESCRIPTIVE
        )
        encoded = to_jsonable(proposal)

        self.assertEqual(
            decode_agent_action_proposal(encoded),
            proposal,
        )
        legacy = {
            "kind": ActionKind.PROPOSE_ANSWER.value,
            "payload": {
                "claims": [
                    {
                        "claim_id": "legacy-caller-owned-id",
                        "statement": "legacy",
                        "applicability": "legacy-free-text",
                        "evidence_record_ids": [],
                        "boundary_ref": "legacy",
                        "limitations": [],
                    }
                ],
                "narrative_markdown": "legacy",
            },
        }
        with self.assertRaises(ValueError):
            decode_agent_action_proposal(legacy)

    def test_interpretation_binds_current_admission_and_validity(
        self,
    ) -> None:
        provider = ScriptedPrimaryAgentProvider(
            (
                AgentActionProposal(
                    kind=ActionKind.RECORD_INTERPRETATION,
                    payload=RecordInterpretationPayload(
                        evidence_record_ids=(
                            self.evidence.evidence_record_id,
                        ),
                        interpretation=(
                            "方向结论只适用于已接受窗口与当前数据版本。"
                        ),
                    ),
                ),
            )
        )
        controller = WAJEController(
            store=self.store,
            provider=provider,
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="gate35-interpretation-worker",
            clock=lambda: NOW,
        )

        controller.advance(self.case_id)
        state = controller.deliver_pending_llm(self.case_id)

        self.assertEqual(state.phase, ControllerPhase.READY_FOR_AGENT)
        events = self.store.list_events(self.case_id)
        self.assertTrue(
            any(
                item.event_type
                is JournalEventType.INTERPRETATION_RECORDED
                for item in events
            )
        )


if __name__ == "__main__":
    unittest.main()
