from __future__ import annotations

import os
import unittest
from pathlib import Path

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
from waje_vnext.domain.controller import ControllerPhase
from waje_vnext.providers import ScriptedPrimaryAgentProvider
from waje_vnext.storage import (
    LeaseConflict,
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
        controller.advance("case-gate2-pg")
        waiting = controller.advance("case-gate2-pg")
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
            waiting.pending_outbox_message_id or ""
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
                run_id="run-b",
                owner_id="worker-b",
                now=NOW,
                expires_at=NOW.replace(hour=10),
            )
        self.store.release_lease(first)

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
                        proposal_sha256,
                        payload,
                        recorded_at
                    ) VALUES (
                        'action-gate2-immutable',
                        'case-gate2-lease',
                        0,
                        'action-gate2-immutable-key',
                        %s,
                        '{}'::jsonb,
                        %s
                    )
                    """,
                    ("a" * 64, NOW),
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


if __name__ == "__main__":
    unittest.main()
