from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import psycopg
from gate1_fixtures import (
    NOW,
    accept_initial_question,
    make_answer,
    make_evidence,
    make_frame,
    make_objection,
    make_plan,
    make_resolution_admission,
    make_resolution_verifier,
    record_reviewed_frame,
)
from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerStatus,
    ReviewerObjectionStatus,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.identity import (
    compute_resolution_outcome_id,
    compute_typed_boundary_derivation_proof,
)
from waje_vnext.domain.measurement import (
    ClaimStrengthCeiling,
    EvidenceValidityRecord,
    EvidenceValidityStatus,
    MeasurementResolutionOutcome,
    ObligationExecutionDisposition,
    ObligationSatisfactionRecord,
    ObligationSatisfactionStatus,
    ResolvedEvidenceObligation,
    ResolutionOutcomeKind,
    SettlementPreconditionReport,
    SettlementPreconditionStatus,
    TypedResolutionBoundary,
)
from waje_vnext.storage import (
    AuthorityConflict,
    PostgresAuthorityStore,
    StaleHead,
    apply_gate1_migration,
    apply_gate3_1_migration,
    apply_gate3_2_migration,
)


DSN = os.environ.get("WAJE_VNEXT_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "storage/migrations/001_gate1_authority.sql"
MIGRATION_3 = (
    ROOT / "storage/migrations/003_gate3_1_measurement_authority.sql"
)
MIGRATION_4 = ROOT / "storage/migrations/004_gate3_2_runtime_sagas.sql"


@unittest.skipUnless(DSN, "WAJE_VNEXT_DATABASE_URL is not configured")
class PostgresAuthorityStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        first = apply_gate1_migration(DSN, migration_path=MIGRATION)
        second = apply_gate1_migration(DSN, migration_path=MIGRATION)
        if first != second:
            raise AssertionError("migration checksum changed across idempotent apply")
        apply_gate3_1_migration(DSN, migration_path=MIGRATION_3)
        apply_gate3_2_migration(DSN, migration_path=MIGRATION_4)

    def setUp(self) -> None:
        assert DSN is not None
        self.store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_resolution_verifier(),
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_concurrent_head_writers_are_serialized_by_cas(self) -> None:
        case = self.store.open_case(
            case_id="case-concurrent",
            thread_id="thread-concurrent",
            event_id="event-concurrent-open",
            opened_at=NOW,
        )
        self.assertEqual(case.head_version, 0)
        case, question = accept_initial_question(self.store, case)
        first = make_frame(
            frame_id="frame-concurrent-a",
            case_id="case-concurrent",
            question=question,
            action_id="action-concurrent-a",
        )
        proof_id = record_reviewed_frame(self.store, first)
        barrier = threading.Barrier(2)

        def attempt(frame: AnalysisFrameRevision, event_id: str) -> str:
            assert DSN is not None
            store = PostgresAuthorityStore.connect(DSN)
            try:
                barrier.wait()
                store.accept_frame(
                    frame,
                    frame_admission_proof_id=proof_id,
                    expected_head_version=1,
                    event_id=event_id,
                    recorded_at=frame.created_at,
                )
                return "accepted"
            except StaleHead:
                return "stale"
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                future.result()
                for future in (
                    executor.submit(
                        attempt,
                        first,
                        "event-concurrent-a",
                    ),
                    executor.submit(
                        attempt,
                        first,
                        "event-concurrent-b",
                    ),
                )
            )

        self.assertCountEqual(outcomes, ("accepted", "stale"))
        self.assertEqual(
            self.store.get_case("case-concurrent").head_version,
            2,
        )

    def test_gate3_1_derived_records_are_append_only_and_bound(self) -> None:
        case = self.store.open_case(
            case_id="case-g3-derived",
            thread_id="thread-g3-derived",
            event_id="case-g3-derived:event:open",
            opened_at=NOW,
        )
        case, question = accept_initial_question(self.store, case)
        frame = make_frame(
            case_id=case.case_id,
            question=question,
            frame_id="case-g3-derived:frame:1",
        )
        proof_id = record_reviewed_frame(self.store, frame)
        case = self.store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="case-g3-derived:event:frame",
            recorded_at=NOW,
        )
        plan = make_plan(
            case_id=case.case_id,
            frame_id=frame.frame_revision_id,
            plan_id="case-g3-derived:plan:1",
        )
        case = self.store.accept_plan(
            plan,
            expected_head_version=case.head_version,
            event_id="case-g3-derived:event:plan",
            recorded_at=NOW,
        )
        estimand = frame.measurement_design.estimands[0]
        requirement = frame.measurement_design.evidence_requirements[0]
        outcome = MeasurementResolutionOutcome(
            resolution_outcome_id="a" * 64,
            case_id=case.case_id,
            question_revision_id=question.question_revision_id,
            frame_revision_id=frame.frame_revision_id,
            estimand_id=estimand.estimand_id,
            semantic_measurement_id=frame.semantic_measurement_ids[0],
            authority_binding_id=frame.authority_binding_ids[0],
            kind=ResolutionOutcomeKind.TYPED_RESOLUTION_BOUNDARY,
            resolved_instance=None,
            boundary=TypedResolutionBoundary(
                boundary_code="incomplete_period",
                boundary_policy_ref=(
                    "waje-vnext://measurement-boundary-policy/"
                    "registry.v1"
                ),
                failed_requirement_ids=(
                    requirement.evidence_requirement_id,
                ),
                failed_contract_refs=("coverage:watermark:v1",),
                inspection_evidence_refs=("inspection:coverage:1",),
                allowed_claim_ceiling=(
                    ClaimStrengthCeiling.BOUNDARY_ONLY
                ),
                derivation_proof_sha256="b" * 64,
            ),
            requirement_boundaries=(),
            created_at=NOW,
        )
        outcome = replace(
            outcome,
            boundary=replace(
                outcome.boundary,
                derivation_proof_sha256=(
                    compute_typed_boundary_derivation_proof(outcome)
                ),
            ),
        )
        outcome = replace(
            outcome,
            resolution_outcome_id=compute_resolution_outcome_id(outcome),
        )
        admission = make_resolution_admission(outcome)
        self.store.record_measurement_resolution(
            outcome,
            admission=admission,
            expected_head_version=case.head_version,
            event_id="case-g3-derived:event:resolution",
        )
        self.assertEqual(
            self.store.get_measurement_resolution_admission(
                outcome.resolution_outcome_id
            ),
            admission,
        )
        obligation = ResolvedEvidenceObligation(
            obligation_id="c" * 64,
            case_id=case.case_id,
            frame_revision_id=frame.frame_revision_id,
            estimand_id=estimand.estimand_id,
            evidence_requirement_id=requirement.evidence_requirement_id,
            evidence_requirement_sha256=content_sha256(requirement),
            resolution_outcome_id=outcome.resolution_outcome_id,
            execution_disposition=(
                ObligationExecutionDisposition.TYPED_BOUNDARY
            ),
            boundary_code="incomplete_period",
            closure_definition_sha256="d" * 64,
            field_derivation_proof_sha256="e" * 64,
            created_at=NOW,
        )
        self.store.record_evidence_obligation(
            obligation,
            expected_head_version=case.head_version,
            event_id="case-g3-derived:event:obligation",
        )
        evidence = make_evidence(
            case_id=case.case_id,
            frame_id=frame.frame_revision_id,
            plan_id=plan.plan_revision_id,
            evidence_id="case-g3-derived:evidence:1",
        )
        self.store.record_evidence(
            evidence,
            expected_head_version=case.head_version,
            event_id="case-g3-derived:event:evidence",
            recorded_at=NOW,
        )
        validity = EvidenceValidityRecord(
            evidence_validity_record_id="case-g3-derived:validity:1",
            evidence_record_id=evidence.evidence_record_id,
            prior_validity_record_id=None,
            status=EvidenceValidityStatus.ADMITTED_VALID,
            reason_code="admission_passed",
            source_authority_ref=frame.frame_revision_id,
            verifier_policy_version="evidence-validity.v1",
            expected_prior_content_sha256=None,
            created_at=NOW,
        )
        self.store.record_evidence_validity(
            validity,
            event_id="case-g3-derived:event:validity",
        )
        satisfaction = ObligationSatisfactionRecord(
            satisfaction_record_id="case-g3-derived:satisfaction:1",
            obligation_id=obligation.obligation_id,
            status=ObligationSatisfactionStatus.OPEN,
            evidence_admission_record_ids=(),
            evidence_use_binding_ids=(),
            resolution_boundary_outcome_id=None,
            contradiction_disposition_refs=(),
            verifier_policy_version="obligation-satisfaction.v1",
            input_set_sha256="f" * 64,
            created_at=NOW,
        )
        self.store.record_obligation_satisfaction(
            satisfaction,
            event_id="case-g3-derived:event:satisfaction",
        )
        report = SettlementPreconditionReport(
            settlement_precondition_report_id="case-g3-derived:precondition:1",
            case_id=case.case_id,
            question_revision_id=question.question_revision_id,
            frame_revision_id=frame.frame_revision_id,
            plan_revision_id=plan.plan_revision_id,
            accepted_head_version=case.head_version,
            semantic_measurement_ids=frame.semantic_measurement_ids,
            authority_binding_ids=frame.authority_binding_ids,
            resolution_outcome_ids=(outcome.resolution_outcome_id,),
            logical_execution_ids=(),
            obligation_satisfaction_record_ids=(
                satisfaction.satisfaction_record_id,
            ),
            evidence_compatibility_proof_ids=(),
            objection_disposition_ids=(),
            trace_manifest_id="trace:g3-derived",
            verifier_policy_version="settlement-precondition.v1",
            status=SettlementPreconditionStatus.BLOCKED,
            fail_reason_codes=("obligation_open",),
            derived_input_sha256="1" * 64,
            created_at=NOW,
        )
        self.assertEqual(
            self.store.record_settlement_precondition(
                report,
                expected_head_version=case.head_version,
                event_id="case-g3-derived:event:precondition",
            ),
            report,
        )

    def test_full_authority_chain_and_append_only_storage(self) -> None:
        case = self.store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        case, _ = accept_initial_question(self.store, case)
        frame = make_frame()
        proof_id = record_reviewed_frame(self.store, frame)
        case = self.store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=case.head_version,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )
        plan = make_plan()
        case = self.store.accept_plan(
            plan,
            expected_head_version=case.head_version,
            event_id="event-plan",
            recorded_at=plan.created_at,
        )
        evidence = make_evidence()
        self.store.record_evidence(
            evidence,
            expected_head_version=case.head_version,
            event_id="event-evidence",
            recorded_at=evidence.created_at,
        )
        answer = make_answer(status=AnswerStatus.PROVISIONAL)
        case = self.store.accept_answer(
            answer,
            expected_head_version=case.head_version,
            event_id="event-answer",
            recorded_at=answer.created_at,
        )

        self.assertEqual(case.head_version, 4)
        self.assertEqual(case.accepted_answer_version_id, "answer-1")
        self.assertEqual(
            tuple(event.cursor for event in self.store.list_events("case-1")),
            (1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(self.store.get_frame("frame-1"), frame)
        self.assertEqual(self.store.get_plan("plan-1"), plan)
        self.assertEqual(self.store.get_evidence("evidence-1"), evidence)
        self.assertEqual(self.store.get_answer("answer-1"), answer)
        opened = make_objection()
        self.store.record_reviewer_objection(
            opened,
            event_id="event-objection-open",
        )
        resolved = make_objection(
            objection_id="objection-2",
            revision_number=2,
            prior_id=opened.objection_id,
            status=ReviewerObjectionStatus.RESOLVED,
        )
        self.store.record_reviewer_objection(
            resolved,
            event_id="event-objection-resolved",
        )
        self.assertEqual(
            tuple(event.cursor for event in self.store.list_events("case-1")),
            (1, 2, 3, 4, 5, 6, 7, 8),
        )

        retried = self.store.accept_frame(
            frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=0,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )
        self.assertEqual(retried, case)

        with self.assertRaises(StaleHead):
            self.store.accept_frame(
                make_frame(
                    revision_number=2,
                    frame_id="frame-2",
                    prior_id="frame-1",
                ),
                frame_admission_proof_id=proof_id,
                expected_head_version=0,
                event_id="event-frame-stale",
                recorded_at=NOW,
            )

        conflicting = replace(
            make_evidence(
                evidence_id="evidence-1",
                payload={"exposure_amount": 999.0},
            ),
            case_id="case-1",
        )
        with self.assertRaises(AuthorityConflict):
            self.store.record_evidence(
                conflicting,
                expected_head_version=case.head_version,
                event_id="event-evidence-conflict",
                recorded_at=conflicting.created_at,
            )

        frame_2 = make_frame(
            revision_number=2,
            frame_id="frame-2",
            prior_id="frame-1",
        )
        proof_2_id = record_reviewed_frame(self.store, frame_2)
        case = self.store.accept_frame(
            frame_2,
            frame_admission_proof_id=proof_2_id,
            expected_head_version=case.head_version,
            event_id="event-frame-2",
            recorded_at=frame_2.created_at,
        )
        plan_2 = make_plan(
            frame_id="frame-2",
            revision_number=2,
            plan_id="plan-2",
            prior_id="plan-1",
        )
        case = self.store.accept_plan(
            plan_2,
            expected_head_version=case.head_version,
            event_id="event-plan-2",
            recorded_at=plan_2.created_at,
        )
        evidence_2 = make_evidence(
            evidence_id="evidence-2",
            frame_id="frame-2",
            plan_id="plan-2",
        )
        self.store.record_evidence(
            evidence_2,
            expected_head_version=case.head_version,
            event_id="event-evidence-2",
            recorded_at=evidence_2.created_at,
        )
        answer_2 = make_answer(
            answer_id="answer-2",
            frame_id="frame-2",
            plan_id="plan-2",
            evidence_id="evidence-2",
            version_number=2,
            prior_id="answer-1",
        )
        case = self.store.accept_answer(
            answer_2,
            expected_head_version=case.head_version,
            event_id="event-answer-2",
            recorded_at=answer_2.created_at,
        )
        self.assertEqual(case.head_version, 7)
        self.assertEqual(case.accepted_plan_revision_id, "plan-2")
        self.assertEqual(case.accepted_answer_version_id, "answer-2")
        self.assertEqual(
            tuple(event.cursor for event in self.store.list_events("case-1")),
            tuple(range(1, 13)),
        )

        assert DSN is not None
        with psycopg.connect(
            DSN,
            options="-c statement_timeout=5000",
        ) as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO waje_vnext.context_packets (
                        packet_id,
                        case_id,
                        head_version,
                        content_sha256,
                        payload,
                        built_at
                    ) VALUES (
                        'packet-1',
                        'case-1',
                        7,
                        %s,
                        '{}'::jsonb,
                        %s
                    )
                    """,
                    ("a" * 64, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO waje_vnext.action_receipts (
                        case_id,
                        idempotency_key,
                        action_id,
                        request_sha256,
                        result_sha256,
                        event_cursor,
                        payload,
                        recorded_at
                    ) VALUES (
                        'case-1',
                        'receipt-key-1',
                        'action-receipt-1',
                        %s,
                        %s,
                        12,
                        '{}'::jsonb,
                        %s
                    )
                    """,
                    ("b" * 64, "c" * 64, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO waje_vnext.checkpoint_records (
                        checkpoint_id,
                        case_id,
                        head_version,
                        event_cursor,
                        context_packet_id,
                        context_sha256,
                        state_sha256,
                        payload,
                        created_at
                    ) VALUES (
                        'checkpoint-1',
                        'case-1',
                        7,
                        12,
                        'packet-1',
                        %s,
                        %s,
                        '{}'::jsonb,
                        %s
                    )
                    """,
                    ("a" * 64, "d" * 64, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO waje_vnext.outbox_messages (
                        outbox_message_id,
                        case_id,
                        source_event_cursor,
                        action_id,
                        job_kind,
                        operation_id,
                        causation_id,
                        correlation_id,
                        authority_revision,
                        expected_head_version,
                        expected_authority_epoch,
                        idempotency_key,
                        destination,
                        contract_ref,
                        payload_sha256,
                        payload,
                        created_at
                    ) VALUES (
                        'outbox-1',
                        'case-1',
                        12,
                        NULL,
                        'controller_wake',
                        'operation-outbox-1',
                        'cause-outbox-1',
                        'correlation-outbox-1',
                        1,
                        7,
                        1,
                        'outbox-key-1',
                        'case-controller',
                        'controller-wake.v1',
                        %s,
                        jsonb_build_object(
                            'operation',
                            jsonb_build_object('payload_sha256', %s::text),
                            'payload_sha256',
                            %s::text
                        ),
                        %s
                    )
                    """,
                    ("e" * 64, "e" * 64, "e" * 64, NOW),
                )
            with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE waje_vnext.evidence_records
                        SET payload = payload
                        WHERE evidence_record_id = 'evidence-1'
                        """
                    )
            with self.assertRaises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO waje_vnext.answer_versions (
                            answer_version_id,
                            case_id,
                            frame_revision_id,
                            plan_revision_id,
                            version_number,
                            prior_answer_version_id,
                            status,
                            content_sha256,
                            payload,
                            created_at
                        ) VALUES (
                            'answer-settled-bypass',
                            'case-1',
                            'frame-2',
                            'plan-2',
                            3,
                            'answer-2',
                            'settled',
                            %s,
                            '{}'::jsonb,
                            %s
                        )
                        """,
                        ("f" * 64, NOW),
                    )
            with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE waje_vnext.outbox_messages
                        SET payload = payload
                        WHERE outbox_message_id = 'outbox-1'
                        """
                    )


if __name__ == "__main__":
    unittest.main()
