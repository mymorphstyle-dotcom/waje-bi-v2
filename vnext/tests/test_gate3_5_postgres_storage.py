from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import psycopg

from gate3_5_runtime_fixtures import (
    build_evidence_runtime_world,
    forge_conformance_provenance_envelope,
)
from test_gate3_3_measurement_resolver import make_trusted_verifier
from waje_vnext.domain.actions import (
    ActionEnvelope,
    ActionKind,
    AgentActionProposal,
    ProposeAnswerPayload,
)
from waje_vnext.domain.answering import (
    AnswerCandidateStatus,
    EvidenceSelection,
    NarrativeBlockProposal,
    ProposedClaim,
    SettlementPreconditionStatus,
    build_provisional_answer_candidate,
)
from waje_vnext.domain.async_runtime import (
    MailboxMessageKind,
    OperationIdentity,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.evidence import (
    EvidenceAdmissionProfile,
    EvidenceAdmissionStatus,
    EvidenceValidityStatus,
    ObligationSatisfactionStatus,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationTerminalStatus,
)
from waje_vnext.domain.controller import PersistedAction
from waje_vnext.domain.measurement import ClaimStrengthCeiling
from waje_vnext.domain.planning import ExecutionRealm
from waje_vnext.domain.runtime_amendment import JobDisposition
from waje_vnext.controller import EvidenceRuntime
from waje_vnext.domain.workflow import ObligationState
from waje_vnext.storage.ports import (
    AuthorityConflict,
    InvalidAuthorityTransition,
    LeaseFenceLost,
    StaleHead,
)
from waje_vnext.storage.postgres import PostgresAuthorityStore


DSN = os.environ.get("WAJE_VNEXT_GATE35_MIGRATION_TEST_DSN")
NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)


@unittest.skipUnless(
    DSN,
    "WAJE_VNEXT_GATE35_MIGRATION_TEST_DSN is not configured",
)
class Gate35PostgresStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        assert DSN is not None
        self.store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
        )
        self.case_id = f"g35-storage-{uuid4().hex}"

    def _open_case(self) -> None:
        self.store.open_case(
            case_id=self.case_id,
            thread_id=f"thread:{self.case_id}",
            event_id=f"event:{self.case_id}:opened",
            opened_at=NOW,
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_workflow_projection_is_durable_idempotent_and_profile_fenced(
        self,
    ) -> None:
        self._open_case()
        initial = self.store.get_workflow_read_model(
            self.case_id,
            realm=ExecutionRealm.CONFORMANCE,
            evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
        )
        self.assertEqual(initial.head.version, 0)

        projected = self.store.project_workflow_read_model(
            self.case_id,
            realm=ExecutionRealm.CONFORMANCE,
            evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
            applied_at=NOW,
        )
        self.assertEqual(projected.head.version, 1)
        self.assertEqual(len(projected.application_receipts), 1)
        self.assertEqual(
            self.store.project_workflow_read_model(
                self.case_id,
                realm=ExecutionRealm.CONFORMANCE,
                evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
                applied_at=NOW,
            ),
            projected,
        )
        self.assertEqual(
            self.store.get_workflow_read_model(
                self.case_id,
                realm=ExecutionRealm.CONFORMANCE,
                evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
            ),
            projected,
        )
        with self.assertRaises(InvalidAuthorityTransition):
            self.store.get_workflow_read_model(
                self.case_id,
                realm=ExecutionRealm.CONFORMANCE,
                evidence_profile=EvidenceAdmissionProfile.PRODUCTION,
            )

    def test_t1_t2_evidence_admission_is_durable_and_idempotent(
        self,
    ) -> None:
        world = build_evidence_runtime_world(
            self.case_id,
            store=self.store,
            owner_id=f"worker:{self.case_id}",
        )

        first_lease = self.store.acquire_job_lease(
            outbox_message_id=world.envelope.outbox_message_id,
            owner_id=f"result-worker:{self.case_id}:1",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        try:
            receipt = world.runtime.land_result(
                envelope=world.envelope,
                job_lease=first_lease,
                received_at=NOW,
            )
        finally:
            self.store.release_job_lease(first_lease)
        replayed_at = NOW + timedelta(seconds=1)
        replay_lease = self.store.acquire_job_lease(
            outbox_message_id=world.envelope.outbox_message_id,
            owner_id=f"result-worker:{self.case_id}:2",
            now=replayed_at,
            expires_at=replayed_at + timedelta(minutes=5),
        )
        try:
            replayed_receipt = world.runtime.land_result(
                envelope=world.envelope,
                job_lease=replay_lease,
                received_at=replayed_at,
            )
        finally:
            self.store.release_job_lease(replay_lease)
        first = world.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )
        replayed = world.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )

        self.assertEqual(receipt, replayed_receipt)
        self.assertEqual(first, replayed)
        self.assertEqual(
            first.admission.status,
            EvidenceAdmissionStatus.ACCEPTED,
        )
        self.assertEqual(
            first.validity.status,
            EvidenceValidityStatus.ADMITTED_VALID,
        )
        self.assertEqual(
            first.satisfaction.status,
            ObligationSatisfactionStatus.SATISFIED,
        )
        self.assertEqual(
            first.completion.completion.status,
            ObligationTerminalStatus.EXECUTION_SUCCEEDED,
        )
        disposition = self.store.get_job_disposition(
            world.dispatch.outbox_message_id
        )
        assert disposition is not None
        self.assertEqual(
            disposition.disposition,
            JobDisposition.COMPLETED,
        )
        successor = self.store.transition_evidence_validity(
            evidence_record_id=(
                world.envelope.evidence_record.evidence_record_id
            ),
            status=EvidenceValidityStatus.REVOKED,
            reason_code="canonical_t2_replay_revocation",
            event_id=f"event:{self.case_id}:canonical-t2-replay",
            recorded_at=NOW + timedelta(seconds=2),
        )
        canonical_replay = world.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW + timedelta(seconds=3),
        )
        self.assertEqual(canonical_replay, first)
        self.assertEqual(
            self.store.latest_evidence_validity(
                world.envelope.evidence_record.evidence_record_id
            ),
            successor[0],
        )
        self.assertEqual(
            self.store.latest_obligation_satisfaction(
                world.obligation.obligation_id
            ),
            successor[1],
        )

    def test_t1_t2_concurrent_redelivery_returns_one_canonical_chain(
        self,
    ) -> None:
        assert DSN is not None
        world = build_evidence_runtime_world(
            self.case_id,
            store=self.store,
            owner_id=f"worker:{self.case_id}:primary",
        )
        secondary_store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
        )
        secondary_runtime = EvidenceRuntime(
            store=secondary_store,
            owner_id=f"worker:{self.case_id}:secondary",
            profile=EvidenceAdmissionProfile.CONFORMANCE,
        )
        lease = self.store.acquire_job_lease(
            outbox_message_id=world.envelope.outbox_message_id,
            owner_id=f"result-worker:{self.case_id}:shared",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        landing_barrier = Barrier(2)

        def land(runtime, received_at):
            landing_barrier.wait()
            return runtime.land_result(
                envelope=world.envelope,
                job_lease=lease,
                received_at=received_at,
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = (
                    pool.submit(land, world.runtime, NOW),
                    pool.submit(
                        land,
                        secondary_runtime,
                        NOW + timedelta(seconds=1),
                    ),
                )
                receipts = tuple(item.result() for item in futures)
        finally:
            self.store.release_job_lease(lease)
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(
            receipts[0].delivery_owner_id,
            lease.owner_id,
        )
        self.assertEqual(
            receipts[0].delivery_fencing_token,
            lease.fencing_token,
        )

        admission_barrier = Barrier(2)

        def admit(runtime, admitted_at):
            admission_barrier.wait()
            return runtime.admit_result(
                receipt_id=receipts[0].capability_result_receipt_id,
                admitted_at=admitted_at,
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = (
                    pool.submit(admit, world.runtime, NOW),
                    pool.submit(
                        admit,
                        secondary_runtime,
                        NOW + timedelta(seconds=1),
                    ),
                )
                outcomes = tuple(item.result() for item in futures)
        finally:
            secondary_store.close()
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(
            len(
                self.store.list_evidence_admissions(
                    case_id=self.case_id
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                self.store.list_obligation_completions(
                    world.schedule.schedule_id
                )
            ),
            1,
        )

    def test_postgres_rejects_forged_conformance_provenance(
        self,
    ) -> None:
        world = build_evidence_runtime_world(
            self.case_id,
            store=self.store,
            owner_id=f"worker:{self.case_id}",
        )
        forged = forge_conformance_provenance_envelope(world)
        lease = self.store.acquire_job_lease(
            outbox_message_id=forged.outbox_message_id,
            owner_id=f"forged-worker:{self.case_id}",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        try:
            with self.assertRaisesRegex(
                InvalidAuthorityTransition,
                "sealed execution provenance",
            ):
                world.runtime.land_result(
                    envelope=forged,
                    job_lease=lease,
                    received_at=NOW,
                )
        finally:
            self.store.release_job_lease(lease)

    def test_postgres_rejects_noncanonical_schedule_identity(self) -> None:
        world = build_evidence_runtime_world(
            self.case_id,
            store=self.store,
            owner_id=f"worker:{self.case_id}",
        )

        with self.assertRaisesRegex(
            InvalidAuthorityTransition,
            "schedule ID is not canonical",
        ):
            self.store.record_obligation_schedule(
                replace(
                    world.schedule,
                    schedule_id="forged-schedule-id",
                )
            )

    def test_postgres_rejects_expired_lease_with_backdated_receipt_time(
        self,
    ) -> None:
        world = build_evidence_runtime_world(
            self.case_id,
            store=self.store,
            owner_id=f"worker:{self.case_id}",
        )
        lease = self.store.acquire_job_lease(
            outbox_message_id=world.envelope.outbox_message_id,
            owner_id=f"expired-worker:{self.case_id}",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        assert DSN is not None
        with psycopg.connect(DSN) as connection:
            row = connection.execute(
                """
                UPDATE waje_vnext.outbox_delivery_leases
                SET acquired_at = CURRENT_TIMESTAMP - interval '3 seconds',
                    heartbeat_at = CURRENT_TIMESTAMP - interval '2 seconds',
                    expires_at = CURRENT_TIMESTAMP - interval '1 second'
                WHERE outbox_message_id = %s
                RETURNING acquired_at, heartbeat_at, expires_at
                """,
                (lease.outbox_message_id,),
            ).fetchone()
        assert row is not None
        expired = replace(
            lease,
            acquired_at=row[0],
            heartbeat_at=row[1],
            expires_at=row[2],
        )

        try:
            with self.assertRaises(LeaseFenceLost):
                world.runtime.land_result(
                    envelope=world.envelope,
                    job_lease=expired,
                    received_at=NOW - timedelta(days=30),
                )
            self.assertIsNone(
                self.store.find_capability_result_receipt_by_outbox(
                    world.envelope.outbox_message_id
                )
            )
        finally:
            self.store.release_job_lease(expired)

    def test_answer_and_workflow_follow_admitted_evidence(self) -> None:
        world = build_evidence_runtime_world(
            self.case_id,
            store=self.store,
            owner_id=f"worker:{self.case_id}",
        )
        lease = self.store.acquire_job_lease(
            outbox_message_id=world.envelope.outbox_message_id,
            owner_id=f"result-worker:{self.case_id}",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        try:
            receipt = world.runtime.land_result(
                envelope=world.envelope,
                job_lease=lease,
                received_at=NOW,
            )
        finally:
            self.store.release_job_lease(lease)
        world.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )
        requirement = world.binding.requirement_binding
        proposal = ProposedClaim(
            proposal_claim_key="payment-window-direction",
            statement="目标窗口的有效观察日归一化付费金额更高。",
            target_estimand_id=world.binding.estimand_id,
            obligation_ids=(world.obligation.obligation_id,),
            evidence_selections=(
                EvidenceSelection(
                    evidence_record_id=(
                        world.envelope.evidence_record.evidence_record_id
                    ),
                    role_ref="primary_estimate",
                ),
            ),
            applicability_scope=world.scope,
            requested_strength=ClaimStrengthCeiling.DESCRIPTIVE,
            boundary_satisfaction_record_ids=(),
            limitation_refs=(
                world.envelope.evidence_record.limitation_refs
            ),
            contradiction_refs=(),
            falsification_refs=requirement.linked_falsification_ids,
            reversal_refs=requirement.linked_reversal_ids,
            depends_on_proposal_claim_keys=(),
        )
        current_case = self.store.get_case(self.case_id)
        current_authority = self.store.get_authority_snapshot(
            self.case_id
        )
        adoption = self.store.get_plan_adoption(
            world.schedule.plan_revision_id
        )
        narrative_blocks = (
            NarrativeBlockProposal(
                block_key="finding",
                markdown=(
                    "在当前口径和数据覆盖范围内，"
                    "目标窗口的日均付费更高。"
                ),
                proposal_claim_keys=(
                    proposal.proposal_claim_key,
                ),
            ),
        )
        action_payload = ProposeAnswerPayload(
            claims=(proposal,),
            narrative_blocks=narrative_blocks,
        )
        action_proposal = AgentActionProposal(
            kind=ActionKind.PROPOSE_ANSWER,
            payload=action_payload,
        )
        action = ActionEnvelope(
            action_id=f"action:{self.case_id}:answer",
            case_id=self.case_id,
            kind=ActionKind.PROPOSE_ANSWER,
            expected_head_version=current_case.head_version,
            idempotency_key=f"idempotency:{self.case_id}:answer",
            issued_at=NOW,
            payload=action_payload,
            operation=OperationIdentity(
                operation_id=f"operation:{self.case_id}:answer",
                idempotency_key=(
                    f"idempotency:{self.case_id}:answer"
                ),
                causation_id=world.outbox.outbox_message_id,
                correlation_id=world.run_id,
                authority_revision=(
                    current_authority.mailbox_authority_epoch
                ),
                payload_sha256=action_proposal.content_sha256,
            ),
        )
        self.store.record_action(
            PersistedAction(
                action=action,
                proposal_sha256=action_proposal.content_sha256,
                recorded_at=NOW,
            )
        )
        candidate = build_provisional_answer_candidate(
            case_id=self.case_id,
            current_authority=current_authority,
            plan_adoption=adoption,
            version_number=1,
            prior_answer_version_id=None,
            claims=(proposal,),
            narrative_blocks=narrative_blocks,
            created_by_action_id=action.action_id,
            created_at=NOW,
        )
        accepted, accepted_case = (
            self.store.accept_provisional_answer_candidate(
                candidate=candidate,
                expected_head_version=current_case.head_version,
                event_id=f"event:{self.case_id}:answer",
                recorded_at=NOW,
                operation=action.operation,
            )
        )
        replayed_answer, replayed_case = (
            self.store.accept_provisional_answer_candidate(
                candidate=candidate,
                expected_head_version=current_case.head_version,
                event_id=f"event:{self.case_id}:answer",
                recorded_at=NOW,
                operation=action.operation,
            )
        )
        self.assertEqual(
            accepted.status,
            AnswerCandidateStatus.ACCEPTED_PROVISIONAL,
        )
        self.assertIsNotNone(accepted.answer)
        self.assertEqual(accepted, replayed_answer)
        self.assertEqual(accepted_case, replayed_case)
        assert accepted.answer is not None
        self.assertEqual(
            self.store.get_answer(accepted.answer.answer_version_id),
            accepted.answer,
        )
        manifest = world.controller.build_run_trace_manifest(
            self.case_id
        )
        settlement = self.store.derive_settlement_precondition(
            case_id=self.case_id,
            expected_head_version=self.store.get_case(
                self.case_id
            ).head_version,
            answer_version_id=accepted.answer.answer_version_id,
            objection_disposition_refs=(),
            unresolved_blocking_objection_refs=(),
            trace_manifest_id=manifest.trace_manifest_id,
            trace_manifest_content_sha256=content_sha256(manifest),
            trace_complete=True,
            event_id=f"event:{self.case_id}:settlement",
            recorded_at=NOW,
        )
        self.assertEqual(
            settlement.status,
            SettlementPreconditionStatus.BLOCKED,
        )
        self.assertIn(
            "production_evidence_unavailable",
            settlement.fail_reason_codes,
        )
        self.assertEqual(
            self.store.get_settlement_precondition(
                settlement.settlement_precondition_report_id
            ),
            settlement,
        )
        projected_before_revoke = (
            self.store.project_workflow_read_model(
                self.case_id,
                realm=ExecutionRealm.CONFORMANCE,
                evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
                applied_at=NOW,
            )
        )
        self.assertEqual(
            projected_before_revoke.snapshot.obligations[
                0
            ].obligation_state,
            ObligationState.SATISFIED,
        )
        revoked = (
            self.store.transition_evidence_validity(
                evidence_record_id=(
                    world.envelope.evidence_record.evidence_record_id
                ),
                status=EvidenceValidityStatus.REVOKED,
                reason_code="source_release_revoked",
                event_id=f"event:{self.case_id}:validity-revoked",
                recorded_at=NOW,
            )
        )
        _, revoked_satisfaction = revoked
        replayed = self.store.transition_evidence_validity(
            evidence_record_id=(
                world.envelope.evidence_record.evidence_record_id
            ),
            status=EvidenceValidityStatus.REVOKED,
            reason_code="source_release_revoked",
            event_id=f"event:{self.case_id}:validity-revoked",
            recorded_at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(replayed, revoked)
        with self.assertRaisesRegex(
            AuthorityConflict,
            "already has different content",
        ):
            self.store.transition_evidence_validity(
                evidence_record_id=(
                    world.envelope.evidence_record.evidence_record_id
                ),
                status=EvidenceValidityStatus.REVOKED,
                reason_code="different_reason",
                event_id=f"event:{self.case_id}:validity-revoked",
                recorded_at=NOW + timedelta(seconds=2),
            )
        self.assertEqual(
            revoked_satisfaction.status,
            ObligationSatisfactionStatus.BLOCKED,
        )
        projected_after_revoke = (
            self.store.project_workflow_read_model(
                self.case_id,
                realm=ExecutionRealm.CONFORMANCE,
                evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
                applied_at=NOW,
            )
        )
        self.assertEqual(
            projected_after_revoke.snapshot.obligations[
                0
            ].obligation_state,
            ObligationState.BLOCKED,
        )
        world.controller.ingress_message(
            case_id=self.case_id,
            thread_id=f"thread-{self.case_id}",
            run_id=world.run_id,
            user_message="请按新的业务口径重新调查。",
            kind=MailboxMessageKind.USER_CORRECTION,
            idempotency_key=f"idempotency:{self.case_id}:correction",
        )
        with self.assertRaisesRegex(
            StaleHead,
            "superseded after acceptance",
        ):
            self.store.accept_provisional_answer_candidate(
                candidate=accepted.candidate,
                expected_head_version=current_case.head_version,
                event_id=f"event:{self.case_id}:stale-answer-retry",
                recorded_at=NOW,
                operation=action.operation,
            )


if __name__ == "__main__":
    unittest.main()
