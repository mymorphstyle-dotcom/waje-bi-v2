from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event

import psycopg
from tests.test_gate2_controller import (
    NOW,
    capability_proposal,
    frame_proposal,
    plan_proposal,
)
from waje_vnext.controller import (
    EffectExecutionResult,
    EffectTransientError,
    ScriptedEffectExecutor,
    WAJEController,
)
from waje_vnext.domain.async_runtime import MailboxMessageKind
from waje_vnext.domain.controller import ControllerPhase
from waje_vnext.domain.events import JournalEventType
from waje_vnext.providers import ScriptedPrimaryAgentProvider
from waje_vnext.storage import (
    LeaseConflict,
    LeaseFenceLost,
    PostgresAuthorityStore,
    apply_gate1_migration,
    apply_gate2_migration,
)


DSN = os.environ.get("WAJE_VNEXT_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
MIGRATION_1 = ROOT / "storage/migrations/001_gate1_authority.sql"
MIGRATION_2 = ROOT / "storage/migrations/002_gate2_controller.sql"


@unittest.skipUnless(DSN, "WAJE_VNEXT_DATABASE_URL is not configured")
class PostgresControllerStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        apply_gate1_migration(DSN, migration_path=MIGRATION_1)
        first = apply_gate2_migration(DSN, migration_path=MIGRATION_2)
        second = apply_gate2_migration(DSN, migration_path=MIGRATION_2)
        if first != second:
            raise AssertionError("Gate 2 migration is not idempotent")

    def setUp(self) -> None:
        assert DSN is not None
        self.store = PostgresAuthorityStore.connect(DSN)

    def tearDown(self) -> None:
        self.store.close()

    def test_controller_loop_recovers_from_postgres_checkpoint(self) -> None:
        provider = ScriptedPrimaryAgentProvider(
            (frame_proposal(), plan_proposal(), capability_proposal())
        )
        effects = ScriptedEffectExecutor(
            (
                EffectTransientError("temporary"),
                EffectExecutionResult(
                    payload={"rows": 24},
                    business_summary="Comparable windows were measured",
                ),
            )
        )
        controller = WAJEController(
            store=self.store,
            provider=provider,
            effect_executor=effects,
            owner_id="pg-worker-1",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-gate2-pg",
            thread_id="thread-gate2-pg",
            run_id="run-gate2-pg",
            user_message="月初付费金额是否更高？",
        )
        controller.advance("case-gate2-pg")
        controller.deliver_pending_llm("case-gate2-pg")
        controller.advance("case-gate2-pg")
        controller.deliver_pending_llm("case-gate2-pg")
        waiting = controller.advance("case-gate2-pg")
        self.assertEqual(waiting.phase, ControllerPhase.WAITING_FOR_LLM)
        waiting = controller.deliver_pending_llm("case-gate2-pg")
        self.assertEqual(waiting.phase, ControllerPhase.WAITING_FOR_EFFECT)
        head = self.store.get_case("case-gate2-pg").head_version

        retrying = controller.deliver_pending_effect("case-gate2-pg")
        self.assertEqual(retrying.phase, ControllerPhase.WAITING_FOR_EFFECT)
        self.assertEqual(
            self.store.get_case("case-gate2-pg").head_version,
            head,
        )
        ready = controller.deliver_pending_effect("case-gate2-pg")
        self.assertEqual(ready.phase, ControllerPhase.READY_FOR_AGENT)

        replacement_store = PostgresAuthorityStore.connect(DSN or "")
        try:
            replacement = WAJEController(
                store=replacement_store,
                provider=ScriptedPrimaryAgentProvider(()),
                effect_executor=ScriptedEffectExecutor(()),
                owner_id="pg-worker-2",
                clock=lambda: NOW,
            )
            self.assertEqual(
                replacement.resume("case-gate2-pg").content_sha256,
                ready.content_sha256,
            )
        finally:
            replacement_store.close()

        attempts = self.store.list_effect_attempts(
            waiting.pending_job_ids[0]
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            self.store.record_effect_attempt(attempts[-1]),
            attempts[-1],
        )

    def test_controller_lease_is_fenced_and_append_only_records_reject_update(
        self,
    ) -> None:
        self.store.open_case(
            case_id="case-gate2-lease",
            thread_id="thread-gate2-lease",
            event_id="event-gate2-lease-open",
            opened_at=NOW,
        )
        first = self.store.acquire_lease(
            case_id="case-gate2-lease",
            run_id="run-a",
            owner_id="worker-a",
            now=NOW,
            expires_at=NOW.replace(hour=10),
        )
        with self.assertRaises(LeaseConflict):
            self.store.acquire_lease(
                case_id="case-gate2-lease",
                run_id="run-a",
                owner_id="worker-a",
                now=NOW + timedelta(days=365),
                expires_at=NOW + timedelta(days=365, hours=1),
            )
        self.store.release_lease(first)
        second = self.store.acquire_lease(
            case_id="case-gate2-lease",
            run_id="run-b",
            owner_id="worker-b",
            now=NOW,
            expires_at=NOW.replace(hour=10),
        )
        self.assertEqual(first.fencing_token + 1, second.fencing_token)
        with self.assertRaises(LeaseFenceLost):
            self.store.release_lease(first)
        self.store.release_lease(second)

        assert DSN is not None
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO waje_vnext.action_records (
                        action_id,
                        case_id,
                        expected_head_version,
                        idempotency_key,
                        operation_id,
                        causation_id,
                        correlation_id,
                        authority_revision,
                        payload_sha256,
                        proposal_sha256,
                        payload,
                        recorded_at
                    ) VALUES (
                        'action-gate2-immutable',
                        'case-gate2-lease',
                        0,
                        'action-gate2-immutable-key',
                        'operation-gate2-immutable',
                        'cause-gate2-immutable',
                        'correlation-gate2-immutable',
                        0,
                        %s,
                        %s,
                        '{}'::jsonb,
                        %s
                    )
                    """,
                    ("b" * 64, "a" * 64, NOW),
                )
            with self.assertRaises(
                psycopg.errors.ObjectNotInPrerequisiteState
            ):
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE waje_vnext.action_records
                        SET payload = payload
                        WHERE action_id = 'action-gate2-immutable'
                        """
                    )

    def test_mailbox_retry_and_job_lease_match_in_memory_semantics(
        self,
    ) -> None:
        controller = WAJEController(
            store=self.store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="pg-worker-conformance",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-gate2-store-conformance",
            thread_id="thread-gate2-store-conformance",
            run_id="run-gate2-store-conformance",
            user_message="检查收入证据",
        )
        original = self.store.list_mailbox_messages(
            "case-gate2-store-conformance"
        )[0]
        replayed = self.store.append_mailbox_message(
            message_id=original.message_id,
            case_id=original.case_id,
            kind=original.kind,
            operation=original.operation,
            payload={"message": "检查收入证据"},
            created_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(original, replayed)

        wake = self.store.list_outbox_messages(
            case_id="case-gate2-store-conformance"
        )[0]
        first = self.store.acquire_job_lease(
            outbox_message_id=wake.outbox_message_id,
            owner_id="pg-worker-a",
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
        self.store.release_job_lease(first)
        second = self.store.acquire_job_lease(
            outbox_message_id=wake.outbox_message_id,
            owner_id="pg-worker-b",
            now=NOW + timedelta(seconds=10),
            expires_at=NOW + timedelta(minutes=2),
        )
        self.assertEqual(first.fencing_token + 1, second.fencing_token)
        with self.assertRaises(LeaseConflict):
            self.store.acquire_job_lease(
                outbox_message_id=wake.outbox_message_id,
                owner_id="pg-worker-b",
                now=NOW + timedelta(days=365),
                expires_at=NOW + timedelta(days=365, minutes=1),
            )
        renewed = self.store.heartbeat_job_lease(
            second,
            heartbeat_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
        with self.assertRaises(LeaseFenceLost):
            self.store.release_job_lease(second)
        self.store.release_job_lease(renewed)

        third = self.store.acquire_job_lease(
            outbox_message_id=wake.outbox_message_id,
            owner_id="pg-worker-c",
            now=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
        assert DSN is not None
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE waje_vnext.outbox_delivery_leases
                    SET acquired_at = clock_timestamp() - interval '3 seconds',
                        heartbeat_at = clock_timestamp() - interval '2 seconds',
                        expires_at = clock_timestamp() - interval '1 second'
                    WHERE outbox_message_id = %s
                    RETURNING acquired_at, heartbeat_at, expires_at
                    """,
                    (third.outbox_message_id,),
                ).fetchone()
        assert row is not None
        expired = replace(
            third,
            acquired_at=row[0],
            heartbeat_at=row[1],
            expires_at=row[2],
        )
        with self.assertRaises(LeaseFenceLost):
            self.store.heartbeat_job_lease(
                expired,
                heartbeat_at=NOW,
                expires_at=NOW + timedelta(minutes=1),
            )

    def test_concurrent_mailbox_and_first_job_lease_are_serialized(
        self,
    ) -> None:
        controller = WAJEController(
            store=self.store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="pg-ingress-seed",
            clock=lambda: NOW,
        )
        controller.start(
            case_id="case-gate2-concurrency",
            thread_id="thread-gate2-concurrency",
            run_id="run-gate2-concurrency",
            user_message="检查收入波动",
        )
        wake = self.store.list_outbox_messages(
            case_id="case-gate2-concurrency"
        )[0]

        assert DSN is not None
        left_store = PostgresAuthorityStore.connect(DSN)
        right_store = PostgresAuthorityStore.connect(DSN)
        ingress_barrier = Barrier(2)

        def ingress(
            store: PostgresAuthorityStore,
            owner_id: str,
        ):
            contender = WAJEController(
                store=store,
                provider=ScriptedPrimaryAgentProvider(()),
                effect_executor=ScriptedEffectExecutor(()),
                owner_id=owner_id,
                clock=lambda: NOW,
            )
            ingress_barrier.wait()
            return contender.ingress_message(
                case_id="case-gate2-concurrency",
                thread_id="thread-gate2-concurrency",
                run_id="run-gate2-concurrency",
                user_message="补充检查支付成功率",
                idempotency_key="same-concurrent-ingress",
            )

        try:
            start_barrier = Barrier(2)

            def start_case(
                store: PostgresAuthorityStore,
                owner_id: str,
            ):
                contender = WAJEController(
                    store=store,
                    provider=ScriptedPrimaryAgentProvider(()),
                    effect_executor=ScriptedEffectExecutor(()),
                    owner_id=owner_id,
                    clock=lambda: NOW,
                )
                start_barrier.wait()
                return contender.start(
                    case_id="case-gate2-concurrent-start",
                    thread_id="thread-gate2-concurrent-start",
                    run_id="run-gate2-concurrent-start",
                    user_message="并发创建同一调查",
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                started = tuple(
                    future.result()
                    for future in (
                        executor.submit(
                            start_case,
                            left_store,
                            "pg-start-left",
                        ),
                        executor.submit(
                            start_case,
                            right_store,
                            "pg-start-right",
                        ),
                    )
                )
            self.assertEqual(
                started[0].content_sha256,
                started[1].content_sha256,
            )
            self.assertEqual(
                1,
                self.store.get_mailbox_head(
                    "case-gate2-concurrent-start"
                ).last_sequence,
            )

            with ThreadPoolExecutor(max_workers=2) as executor:
                left_future = executor.submit(
                    ingress,
                    left_store,
                    "pg-ingress-left",
                )
                right_future = executor.submit(
                    ingress,
                    right_store,
                    "pg-ingress-right",
                )
                left_receipt = left_future.result()
                right_receipt = right_future.result()
            self.assertEqual(left_receipt, right_receipt)
            self.assertEqual(
                2,
                self.store.get_mailbox_head(
                    "case-gate2-concurrency"
                ).last_sequence,
            )

            lease_barrier = Barrier(2)

            def acquire(
                store: PostgresAuthorityStore,
                owner_id: str,
            ):
                lease_barrier.wait()
                return store.acquire_job_lease(
                    outbox_message_id=wake.outbox_message_id,
                    owner_id=owner_id,
                    now=NOW,
                    expires_at=NOW + timedelta(minutes=1),
                )

            outcomes: list[object] = []
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(acquire, left_store, "pg-lease-left"),
                    executor.submit(acquire, right_store, "pg-lease-right"),
                )
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except LeaseConflict as error:
                        outcomes.append(error)
            self.assertEqual(
                1,
                sum(
                    not isinstance(outcome, LeaseConflict)
                    for outcome in outcomes
                ),
            )
            self.assertEqual(
                1,
                sum(
                    isinstance(outcome, LeaseConflict)
                    for outcome in outcomes
                ),
            )
        finally:
            left_store.close()
            right_store.close()

    def test_correction_fence_is_linearized_with_authority_commit(self) -> None:
        final_check = Event()
        allow_commit = Event()

        class PausingFenceController(WAJEController):
            stale_checks = 0

            def _job_is_stale(self, message):
                result = super()._job_is_stale(message)
                self.stale_checks += 1
                if self.stale_checks == 3:
                    final_check.set()
                    if not allow_commit.wait(timeout=5):
                        raise AssertionError("authority test hook timed out")
                return result

        assert DSN is not None
        worker_store = PostgresAuthorityStore.connect(DSN)
        correction_store = PostgresAuthorityStore.connect(DSN)
        worker = PausingFenceController(
            store=worker_store,
            provider=ScriptedPrimaryAgentProvider((frame_proposal(),)),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="pg-fence-worker",
            clock=lambda: NOW,
        )
        correction = WAJEController(
            store=correction_store,
            provider=ScriptedPrimaryAgentProvider(()),
            effect_executor=ScriptedEffectExecutor(()),
            owner_id="pg-fence-correction",
            clock=lambda: NOW,
        )
        try:
            worker.start(
                case_id="case-gate2-fence-linearization",
                thread_id="thread-gate2-fence-linearization",
                run_id="run-gate2-fence-linearization",
                user_message="先按自然日解释收入",
            )
            worker.advance("case-gate2-fence-linearization")
            with ThreadPoolExecutor(max_workers=2) as executor:
                delivery = executor.submit(
                    worker.deliver_pending_llm,
                    "case-gate2-fence-linearization",
                )
                self.assertTrue(final_check.wait(timeout=5))
                correction_future = executor.submit(
                    correction.ingress_message,
                    case_id="case-gate2-fence-linearization",
                    thread_id="thread-gate2-fence-linearization",
                    run_id="run-gate2-fence-linearization",
                    user_message="改为按业务结算日",
                    kind=MailboxMessageKind.USER_CORRECTION,
                    idempotency_key="pg-fence-correction",
                )
                with self.assertRaises(FutureTimeout):
                    correction_future.result(timeout=0.2)
                allow_commit.set()
                delivered = delivery.result(timeout=5)
                correction_receipt = correction_future.result(timeout=5)

            self.assertIsNotNone(
                worker_store.get_case(
                    "case-gate2-fence-linearization"
                ).accepted_frame_revision_id
            )
            events = worker_store.list_events(
                "case-gate2-fence-linearization"
            )
            frame_cursor = next(
                event.cursor
                for event in events
                if event.event_type is JournalEventType.FRAME_ACCEPTED
            )
            correction_cursor = next(
                event.cursor
                for event in events
                if event.event_type is JournalEventType.MESSAGE_INGRESSED
                and event.authority_ref == correction_receipt.message_id
            )
            self.assertLess(frame_cursor, correction_cursor)
            self.assertEqual(1, delivered.authority_epoch)
            self.assertEqual(2, correction_receipt.authority_epoch)
        finally:
            allow_commit.set()
            worker_store.close()
            correction_store.close()


if __name__ == "__main__":
    unittest.main()
