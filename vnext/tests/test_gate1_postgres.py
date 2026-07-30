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
    make_answer,
    make_evidence,
    make_frame,
    make_objection,
    make_plan,
)
from waje_vnext.domain.authority import (
    AnalysisFrameRevision,
    AnswerStatus,
    ReviewerObjectionStatus,
)
from waje_vnext.storage import (
    AuthorityConflict,
    PostgresAuthorityStore,
    StaleHead,
    apply_gate1_migration,
)


DSN = os.environ.get("WAJE_VNEXT_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "storage/migrations/001_gate1_authority.sql"


@unittest.skipUnless(DSN, "WAJE_VNEXT_DATABASE_URL is not configured")
class PostgresAuthorityStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        first = apply_gate1_migration(DSN, migration_path=MIGRATION)
        second = apply_gate1_migration(DSN, migration_path=MIGRATION)
        if first != second:
            raise AssertionError("migration checksum changed across idempotent apply")

    def setUp(self) -> None:
        assert DSN is not None
        self.store = PostgresAuthorityStore.connect(DSN)

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
        first = replace(
            make_frame(frame_id="frame-concurrent-a"),
            case_id="case-concurrent",
            created_by_action_id="action-concurrent-a",
        )
        second = replace(
            make_frame(frame_id="frame-concurrent-b"),
            case_id="case-concurrent",
            created_by_action_id="action-concurrent-b",
        )
        barrier = threading.Barrier(2)

        def attempt(frame: AnalysisFrameRevision, event_id: str) -> str:
            assert DSN is not None
            store = PostgresAuthorityStore.connect(DSN)
            try:
                barrier.wait()
                store.accept_frame(
                    frame,
                    expected_head_version=0,
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
                        second,
                        "event-concurrent-b",
                    ),
                )
            )

        self.assertCountEqual(outcomes, ("accepted", "stale"))
        self.assertEqual(
            self.store.get_case("case-concurrent").head_version,
            1,
        )

    def test_full_authority_chain_and_append_only_storage(self) -> None:
        case = self.store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        frame = make_frame()
        case = self.store.accept_frame(
            frame,
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

        self.assertEqual(case.head_version, 3)
        self.assertEqual(case.accepted_answer_version_id, "answer-1")
        self.assertEqual(
            tuple(event.cursor for event in self.store.list_events("case-1")),
            (1, 2, 3, 4, 5),
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
            (1, 2, 3, 4, 5, 6, 7),
        )

        retried = self.store.accept_frame(
            frame,
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
        case = self.store.accept_frame(
            frame_2,
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
        self.assertEqual(case.head_version, 6)
        self.assertEqual(case.accepted_plan_revision_id, "plan-2")
        self.assertEqual(case.accepted_answer_version_id, "answer-2")
        self.assertEqual(
            tuple(event.cursor for event in self.store.list_events("case-1")),
            tuple(range(1, 12)),
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
                        6,
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
                        11,
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
                        6,
                        11,
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
                        11,
                        NULL,
                        'controller_wake',
                        'operation-outbox-1',
                        'cause-outbox-1',
                        'correlation-outbox-1',
                        1,
                        6,
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
