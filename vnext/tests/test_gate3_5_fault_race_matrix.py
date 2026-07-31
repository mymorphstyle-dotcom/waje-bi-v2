from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import os
from threading import Barrier, Event
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from gate3_5_runtime_fixtures import (
    build_evidence_runtime_world,
    land_evidence_runtime_world,
)
import test_gate3_5_answer_contracts as answer_contract_fixtures
from test_gate3_2_obligation_scheduler import (
    accepted_single_obligation_runtime,
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
from waje_vnext.domain.authority import (
    ReviewerObjection,
    ReviewerObjectionStatus,
    ReviewerSeverity,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.controller import PersistedAction
from waje_vnext.domain.evidence import (
    EvidenceAdmissionProfile,
    EvidenceValidityStatus,
)
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.measurement import ClaimStrengthCeiling
from waje_vnext.domain.planning import ExecutionRealm
from waje_vnext.domain.workflow import apply_workflow_fact
from waje_vnext.domain.workflow_adapter import (
    journal_event_to_workflow_fact,
)
from waje_vnext.storage.ports import (
    InvalidAuthorityTransition,
    StaleHead,
)
from waje_vnext.storage.postgres import PostgresAuthorityStore


DSN = os.environ.get("WAJE_VNEXT_GATE35_MIGRATION_TEST_DSN")
NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _PreparedAnswer:
    world: object
    candidate: object
    operation: OperationIdentity


def _prepare_answer(case_id: str) -> _PreparedAnswer:
    world = build_evidence_runtime_world(case_id)
    receipt = land_evidence_runtime_world(world, received_at=NOW)
    world.runtime.admit_result(
        receipt_id=receipt.capability_result_receipt_id,
        admitted_at=NOW,
    )
    evidence = world.envelope.evidence_record
    claim = ProposedClaim(
        proposal_claim_key="normalized-window-direction",
        statement="目标窗口按有效观察日归一化的指标更高。",
        target_estimand_id=evidence.estimand_id,
        obligation_ids=(world.obligation.obligation_id,),
        evidence_selections=(
            EvidenceSelection(
                evidence_record_id=evidence.evidence_record_id,
                role_ref="primary-estimate",
            ),
        ),
        applicability_scope=world.scope,
        requested_strength=ClaimStrengthCeiling.DESCRIPTIVE,
        boundary_satisfaction_record_ids=(),
        limitation_refs=evidence.limitation_refs,
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
                proposal_claim_keys=(claim.proposal_claim_key,),
            ),
        ),
    )
    proposal = AgentActionProposal(
        kind=ActionKind.PROPOSE_ANSWER,
        payload=payload,
    )
    store = world.store
    case = store.get_case(case_id)
    authority = store.get_authority_snapshot(case_id)
    operation = OperationIdentity(
        operation_id=f"operation:{case_id}:answer",
        idempotency_key=f"idempotency:{case_id}:answer",
        causation_id=world.outbox.outbox_message_id,
        correlation_id=world.run_id,
        authority_revision=authority.mailbox_authority_epoch,
        payload_sha256=proposal.content_sha256,
    )
    action = ActionEnvelope(
        action_id=f"action:{case_id}:answer",
        case_id=case_id,
        kind=ActionKind.PROPOSE_ANSWER,
        expected_head_version=case.head_version,
        idempotency_key=operation.idempotency_key,
        operation=operation,
        issued_at=NOW,
        payload=payload,
    )
    store.record_action(
        PersistedAction(
            action=action,
            proposal_sha256=proposal.content_sha256,
            recorded_at=NOW,
        )
    )
    adoption = store.get_plan_adoption(world.schedule.plan_revision_id)
    candidate = build_provisional_answer_candidate(
        case_id=case_id,
        current_authority=authority,
        plan_adoption=adoption,
        version_number=1,
        prior_answer_version_id=None,
        claims=(claim,),
        narrative_blocks=payload.narrative_blocks,
        created_by_action_id=action.action_id,
        created_at=NOW,
    )
    return _PreparedAnswer(
        world=world,
        candidate=candidate,
        operation=operation,
    )


def _capture(callable_):
    try:
        return callable_()
    except Exception as error:  # noqa: BLE001 - race outcome is asserted by type
        return error


def _run_ordered_threads(first, second):
    """Exercise one legal serialization through two real worker threads."""

    ready = Barrier(2)
    first_done = Event()

    def run_first():
        ready.wait()
        try:
            return _capture(first)
        finally:
            first_done.set()

    def run_second():
        ready.wait()
        first_done.wait()
        return _capture(second)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run_first)
        second_future = pool.submit(run_second)
        return first_future.result(), second_future.result()


@contextmanager
def _reject_insert(
    dsn: str,
    *,
    table: str,
    event_type: str | None = None,
):
    """Install one test-owned failpoint and always remove it."""

    suffix = uuid4().hex
    function_name = f"gate35_failpoint_function_{suffix}"
    trigger_name = f"gate35_failpoint_trigger_{suffix}"
    predicate = sql.SQL("")
    if event_type is not None:
        predicate = sql.SQL(" WHEN (NEW.event_type = {})").format(
            sql.Literal(event_type)
        )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL(
                """
                CREATE FUNCTION public.{}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    RAISE EXCEPTION 'injected Gate 3.5 write failure'
                        USING ERRCODE = 'P0001';
                END;
                $$
                """
            ).format(sql.Identifier(function_name))
        )
        connection.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT ON waje_vnext.{} "
                "FOR EACH ROW{} EXECUTE FUNCTION public.{}()"
            ).format(
                sql.Identifier(trigger_name),
                sql.Identifier(table),
                predicate,
                sql.Identifier(function_name),
            )
        )
    try:
        yield
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {} ON waje_vnext.{}").format(
                    sql.Identifier(trigger_name),
                    sql.Identifier(table),
                )
            )
            connection.execute(
                sql.SQL("DROP FUNCTION IF EXISTS public.{}()").format(
                    sql.Identifier(function_name)
                )
            )


@unittest.skipUnless(
    DSN,
    "WAJE_VNEXT_GATE35_MIGRATION_TEST_DSN is not configured",
)
class Gate35PostgresFaultInjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        assert DSN is not None
        self.store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
        )

    def tearDown(self) -> None:
        self.store.close()

    def _t1_state(self, world) -> tuple[int, ...]:
        assert DSN is not None
        evidence = world.envelope.evidence_record
        with psycopg.connect(DSN) as connection:
            return connection.execute(
                """
                SELECT
                    (
                        SELECT count(*)
                        FROM waje_vnext.evidence_records
                        WHERE evidence_record_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.capability_result_envelopes
                        WHERE capability_result_envelope_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.capability_result_receipts
                        WHERE outbox_message_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.event_journal
                        WHERE case_id = %s
                          AND event_type IN (%s, %s)
                    )
                """,
                (
                    evidence.evidence_record_id,
                    world.envelope.capability_result_envelope_id,
                    world.envelope.outbox_message_id,
                    world.schedule.case_id,
                    JournalEventType.CAPABILITY_RESULT_LANDED.value,
                    JournalEventType.EVIDENCE_RECORDED.value,
                ),
            ).fetchone()

    def _t2_state(self, world) -> tuple[int, ...]:
        assert DSN is not None
        evidence = world.envelope.evidence_record
        with psycopg.connect(DSN) as connection:
            return connection.execute(
                """
                SELECT
                    (
                        SELECT count(*)
                        FROM waje_vnext.evidence_admission_records
                        WHERE evidence_record_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.evidence_validity_records
                        WHERE evidence_record_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.obligation_satisfaction_records
                        WHERE obligation_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.obligation_completion_records
                        WHERE schedule_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.job_disposition_records
                        WHERE outbox_message_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.obligation_schedule_checkpoints
                        WHERE schedule_id = %s
                    ),
                    (
                        SELECT count(*)
                        FROM waje_vnext.event_journal
                        WHERE case_id = %s
                    )
                """,
                (
                    evidence.evidence_record_id,
                    evidence.evidence_record_id,
                    world.obligation.obligation_id,
                    world.schedule.schedule_id,
                    world.envelope.outbox_message_id,
                    world.schedule.schedule_id,
                    world.schedule.case_id,
                ),
            ).fetchone()

    def test_t1_each_durable_write_class_rolls_back_and_retries_once(
        self,
    ) -> None:
        assert DSN is not None
        failpoints = (
            ("evidence_records", None),
            ("capability_result_envelopes", None),
            ("capability_result_receipts", None),
            (
                "event_journal",
                JournalEventType.CAPABILITY_RESULT_LANDED.value,
            ),
            (
                "event_journal",
                JournalEventType.EVIDENCE_RECORDED.value,
            ),
        )
        for index, (table, event_type) in enumerate(failpoints):
            with self.subTest(table=table, event_type=event_type):
                world = build_evidence_runtime_world(
                    f"gate35-t1-failpoint-{index}-{uuid4().hex}",
                    store=self.store,
                )
                before = self._t1_state(world)
                self.assertEqual(before[:3], (0, 0, 0))
                with _reject_insert(
                    DSN,
                    table=table,
                    event_type=event_type,
                ):
                    with self.assertRaises(psycopg.errors.RaiseException):
                        land_evidence_runtime_world(
                            world,
                            received_at=NOW,
                        )
                self.assertEqual(self._t1_state(world), before)

                receipt = land_evidence_runtime_world(
                    world,
                    received_at=NOW + timedelta(seconds=1),
                )
                committed = self._t1_state(world)
                self.assertEqual(committed[:3], (1, 1, 1))
                self.assertEqual(committed[3] - before[3], 2)
                replayed = land_evidence_runtime_world(
                    world,
                    received_at=NOW + timedelta(seconds=2),
                )
                self.assertEqual(replayed, receipt)
                self.assertEqual(self._t1_state(world), committed)

    def test_t2_each_durable_write_class_rolls_back_and_retries_once(
        self,
    ) -> None:
        assert DSN is not None
        failpoints = (
            ("evidence_admission_records", None),
            ("evidence_validity_records", None),
            ("obligation_satisfaction_records", None),
            (
                "event_journal",
                JournalEventType.EVIDENCE_ADMISSION_RECORDED.value,
            ),
            ("obligation_completion_records", None),
            (
                "event_journal",
                JournalEventType.OBLIGATION_COMPLETION_ADMITTED.value,
            ),
            ("job_disposition_records", None),
            ("obligation_schedule_checkpoints", None),
            (
                "event_journal",
                JournalEventType.OBLIGATION_SCHEDULE_CHECKPOINTED.value,
            ),
        )
        for index, (table, event_type) in enumerate(failpoints):
            with self.subTest(table=table, event_type=event_type):
                world = build_evidence_runtime_world(
                    f"gate35-t2-failpoint-{index}-{uuid4().hex}",
                    store=self.store,
                )
                receipt = land_evidence_runtime_world(world, received_at=NOW)
                before = self._t2_state(world)
                self.assertEqual(before[:5], (0, 0, 0, 0, 0))
                with _reject_insert(
                    DSN,
                    table=table,
                    event_type=event_type,
                ):
                    with self.assertRaises(psycopg.errors.RaiseException):
                        world.runtime.admit_result(
                            receipt_id=(
                                receipt.capability_result_receipt_id
                            ),
                            admitted_at=NOW,
                        )
                self.assertEqual(self._t2_state(world), before)

                outcome = world.runtime.admit_result(
                    receipt_id=receipt.capability_result_receipt_id,
                    admitted_at=NOW + timedelta(seconds=1),
                )
                committed = self._t2_state(world)
                self.assertEqual(committed[:5], (1, 1, 1, 1, 1))
                replayed = world.runtime.admit_result(
                    receipt_id=receipt.capability_result_receipt_id,
                    admitted_at=NOW + timedelta(seconds=2),
                )
                self.assertEqual(replayed, outcome)
                self.assertEqual(self._t2_state(world), committed)

    def test_t1_and_t2_ack_loss_recover_from_independent_connection(
        self,
    ) -> None:
        assert DSN is not None
        case_id = f"gate35-ack-loss-{uuid4().hex}"
        world = build_evidence_runtime_world(case_id, store=self.store)
        landed_before_ack = land_evidence_runtime_world(world, received_at=NOW)

        recovery_store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
        )
        try:
            recovery_runtime = type(world.runtime)(
                store=recovery_store,
                owner_id=f"recovery-worker:{case_id}",
                profile=world.envelope.evidence_record.profile,
            )
            recovery_lease = recovery_store.acquire_job_lease(
                outbox_message_id=world.envelope.outbox_message_id,
                owner_id=f"recovery-result-worker:{case_id}",
                now=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(minutes=6),
            )
            try:
                replayed_t1 = recovery_runtime.land_result(
                    envelope=world.envelope,
                    job_lease=recovery_lease,
                    received_at=NOW + timedelta(seconds=1),
                )
            finally:
                recovery_store.release_job_lease(recovery_lease)
            self.assertEqual(replayed_t1, landed_before_ack)
            admitted_before_ack = recovery_runtime.recover_outbox(
                outbox_message_id=world.envelope.outbox_message_id,
                admitted_at=NOW + timedelta(seconds=2),
            )
            self.assertIsNotNone(admitted_before_ack)
        finally:
            recovery_store.close()

        second_recovery_store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
        )
        try:
            second_runtime = type(world.runtime)(
                store=second_recovery_store,
                owner_id=f"second-recovery-worker:{case_id}",
                profile=world.envelope.evidence_record.profile,
            )
            replayed_t2 = second_runtime.recover_outbox(
                outbox_message_id=world.envelope.outbox_message_id,
                admitted_at=NOW + timedelta(seconds=3),
            )
            self.assertEqual(replayed_t2, admitted_before_ack)
        finally:
            second_recovery_store.close()

    def test_journal_projector_cas_and_ack_loss_cross_connections(
        self,
    ) -> None:
        assert DSN is not None
        case_id = f"gate35-pg-projector-race-{uuid4().hex}"
        world = build_evidence_runtime_world(case_id, store=self.store)
        initial = self.store.get_workflow_read_model(
            case_id,
            realm=ExecutionRealm.CONFORMANCE,
            evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
        )
        source_event = self.store.list_events(case_id)[0]
        proposed = apply_workflow_fact(
            initial,
            journal_event_to_workflow_fact(
                source_event,
                current=initial,
                authority_resolver=self.store,
            ),
        )
        second_store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
        )
        ready = Barrier(2)

        def commit_from(store):
            ready.wait()
            return store.commit_workflow_read_model(
                proposed,
                expected_head_version=initial.head.version,
                applied_at=NOW,
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(commit_from, self.store)
                second = pool.submit(commit_from, second_store)
                self.assertEqual(first.result(), proposed)
                self.assertEqual(second.result(), proposed)
        finally:
            second_store.close()

        # A third process receives no ACK, reloads durable state, and replays.
        recovery_store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
        )
        try:
            replayed = recovery_store.commit_workflow_read_model(
                proposed,
                expected_head_version=initial.head.version,
                applied_at=NOW + timedelta(seconds=1),
            )
            self.assertEqual(replayed, proposed)
            projected = recovery_store.project_workflow_read_model(
                case_id,
                realm=ExecutionRealm.CONFORMANCE,
                evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
                applied_at=NOW + timedelta(seconds=2),
            )
            rebuilt = initial
            for event in recovery_store.list_events(case_id):
                rebuilt = apply_workflow_fact(
                    rebuilt,
                    journal_event_to_workflow_fact(
                        event,
                        current=rebuilt,
                        authority_resolver=recovery_store,
                    ),
                )
            self.assertEqual(projected, rebuilt)
            self.assertEqual(
                projected.head.last_applied_cursor,
                len(recovery_store.list_events(case_id)),
            )
        finally:
            recovery_store.close()


class Gate35InMemoryRaceMatrixTest(unittest.TestCase):
    def _accept_answer(
        self,
        prepared: _PreparedAnswer,
        *,
        event_suffix: str,
    ):
        case_id = prepared.world.schedule.case_id
        return prepared.world.store.accept_provisional_answer_candidate(
            candidate=prepared.candidate,
            expected_head_version=(
                prepared.candidate.authority_snapshot.head_version
            ),
            event_id=f"event:{case_id}:answer:{event_suffix}",
            recorded_at=NOW,
            operation=prepared.operation,
        )

    def _correct(
        self,
        prepared: _PreparedAnswer,
        *,
        suffix: str,
    ):
        case_id = prepared.world.schedule.case_id
        return prepared.world.controller.ingress_message(
            case_id=case_id,
            thread_id=f"thread-{case_id}",
            run_id=prepared.world.run_id,
            user_message="请按新的业务定义修订当前调查。",
            kind=MailboxMessageKind.USER_CORRECTION,
            idempotency_key=(
                f"idempotency:{case_id}:correction:{suffix}"
            ),
        )

    def _derive_settlement(
        self,
        prepared: _PreparedAnswer,
        *,
        answer_version_id: str,
        event_suffix: str,
        manifest=None,
        objection_disposition_refs: tuple[str, ...] = (),
        unresolved_blocking_objection_refs: tuple[str, ...] = (),
    ):
        case_id = prepared.world.schedule.case_id
        if manifest is None:
            manifest = (
                prepared.world.controller.build_run_trace_manifest(
                    case_id
                )
            )
        store = prepared.world.store
        return store.derive_settlement_precondition(
            case_id=case_id,
            expected_head_version=store.get_case(case_id).head_version,
            answer_version_id=answer_version_id,
            objection_disposition_refs=objection_disposition_refs,
            unresolved_blocking_objection_refs=(
                unresolved_blocking_objection_refs
            ),
            trace_manifest_id=manifest.trace_manifest_id,
            trace_manifest_content_sha256=content_sha256(manifest),
            trace_complete=True,
            event_id=f"event:{case_id}:settlement:{event_suffix}",
            recorded_at=NOW,
        )

    def _revoke(self, prepared: _PreparedAnswer, *, suffix: str):
        case_id = prepared.world.schedule.case_id
        return prepared.world.store.transition_evidence_validity(
            evidence_record_id=(
                prepared.world.envelope.evidence_record.evidence_record_id
            ),
            status=EvidenceValidityStatus.REVOKED,
            reason_code="source_release_revoked",
            event_id=f"event:{case_id}:revoke:{suffix}",
            recorded_at=NOW,
        )

    def test_correction_vs_answer_covers_both_serializations(self) -> None:
        correction_first = _prepare_answer(
            f"gate35-correction-answer-first-{uuid4().hex}"
        )
        correction, answer = _run_ordered_threads(
            lambda: self._correct(
                correction_first,
                suffix="correction-first",
            ),
            lambda: self._accept_answer(
                correction_first,
                event_suffix="after-correction",
            ),
        )
        self.assertNotIsInstance(correction, Exception)
        self.assertIsInstance(
            answer,
            (StaleHead, InvalidAuthorityTransition),
        )
        self.assertIsNone(
            correction_first.world.store.get_case(
                correction_first.world.schedule.case_id
            ).accepted_answer_version_id
        )

        answer_first = _prepare_answer(
            f"gate35-answer-correction-first-{uuid4().hex}"
        )
        answer, correction = _run_ordered_threads(
            lambda: self._accept_answer(
                answer_first,
                event_suffix="answer-first",
            ),
            lambda: self._correct(
                answer_first,
                suffix="after-answer",
            ),
        )
        self.assertNotIsInstance(answer, Exception)
        bundle, _ = answer
        self.assertIs(
            bundle.status,
            AnswerCandidateStatus.ACCEPTED_PROVISIONAL,
        )
        self.assertIsNotNone(bundle.answer)
        self.assertNotIsInstance(correction, Exception)
        current_authority = (
            answer_first.world.store.get_authority_snapshot(
                answer_first.world.schedule.case_id
            )
        )
        self.assertNotEqual(
            current_authority,
            answer_first.candidate.authority_snapshot,
        )
        with self.assertRaises(StaleHead):
            self._accept_answer(
                answer_first,
                event_suffix="retry-after-correction",
            )

    def test_correction_vs_settlement_covers_both_serializations(
        self,
    ) -> None:
        correction_first = _prepare_answer(
            f"gate35-correction-settlement-first-{uuid4().hex}"
        )
        accepted, _ = self._accept_answer(
            correction_first,
            event_suffix="prepare",
        )
        assert accepted.answer is not None
        correction, settlement = _run_ordered_threads(
            lambda: self._correct(
                correction_first,
                suffix="before-settlement",
            ),
            lambda: self._derive_settlement(
                correction_first,
                answer_version_id=accepted.answer.answer_version_id,
                event_suffix="after-correction",
            ),
        )
        self.assertNotIsInstance(correction, Exception)
        if isinstance(settlement, Exception):
            self.assertIsInstance(
                settlement,
                (StaleHead, InvalidAuthorityTransition),
            )
        else:
            self.assertIs(
                settlement.status,
                SettlementPreconditionStatus.BLOCKED,
            )
            self.assertIn(
                "stale_answer_authority",
                settlement.fail_reason_codes,
            )

        settlement_first = _prepare_answer(
            f"gate35-settlement-correction-first-{uuid4().hex}"
        )
        accepted, _ = self._accept_answer(
            settlement_first,
            event_suffix="prepare",
        )
        assert accepted.answer is not None
        settlement_manifest = (
            settlement_first.world.controller.build_run_trace_manifest(
                settlement_first.world.schedule.case_id
            )
        )
        settlement, correction = _run_ordered_threads(
            lambda: self._derive_settlement(
                settlement_first,
                answer_version_id=accepted.answer.answer_version_id,
                event_suffix="before-correction",
                manifest=settlement_manifest,
            ),
            lambda: self._correct(
                settlement_first,
                suffix="after-settlement",
            ),
        )
        self.assertNotIsInstance(settlement, Exception)
        self.assertIs(
            settlement.status,
            SettlementPreconditionStatus.BLOCKED,
        )
        self.assertNotIsInstance(correction, Exception)
        after_correction = _capture(
            lambda: self._derive_settlement(
                settlement_first,
                answer_version_id=accepted.answer.answer_version_id,
                event_suffix="new-check-after-correction",
                manifest=settlement_manifest,
            )
        )
        if isinstance(after_correction, Exception):
            self.assertIsInstance(
                after_correction,
                (StaleHead, InvalidAuthorityTransition),
            )
        else:
            self.assertIs(
                after_correction.status,
                SettlementPreconditionStatus.BLOCKED,
            )
            self.assertIn(
                "stale_answer_authority",
                after_correction.fail_reason_codes,
            )

    def test_validity_revoke_vs_answer_cas_covers_both_serializations(
        self,
    ) -> None:
        revoke_first = _prepare_answer(
            f"gate35-revoke-answer-first-{uuid4().hex}"
        )
        revoked, answer = _run_ordered_threads(
            lambda: self._revoke(revoke_first, suffix="before-answer"),
            lambda: self._accept_answer(
                revoke_first,
                event_suffix="after-revoke",
            ),
        )
        self.assertNotIsInstance(revoked, Exception)
        if isinstance(answer, Exception):
            self.assertIsInstance(
                answer,
                (StaleHead, InvalidAuthorityTransition),
            )
        else:
            bundle, _ = answer
            self.assertIs(bundle.status, AnswerCandidateStatus.REJECTED)
            self.assertIsNone(bundle.answer)

        answer_first = _prepare_answer(
            f"gate35-answer-revoke-first-{uuid4().hex}"
        )
        answer, revoked = _run_ordered_threads(
            lambda: self._accept_answer(
                answer_first,
                event_suffix="before-revoke",
            ),
            lambda: self._revoke(
                answer_first,
                suffix="after-answer",
            ),
        )
        self.assertNotIsInstance(answer, Exception)
        bundle, _ = answer
        assert bundle.answer is not None
        self.assertNotIsInstance(revoked, Exception)
        report = self._derive_settlement(
            answer_first,
            answer_version_id=bundle.answer.answer_version_id,
            event_suffix="after-revoke",
        )
        self.assertIs(
            report.status,
            SettlementPreconditionStatus.BLOCKED,
        )
        self.assertIn(
            "evidence_not_currently_valid",
            report.fail_reason_codes,
        )
        self.assertIn(
            "obligation_closure_changed",
            report.fail_reason_codes,
        )

    def test_reviewer_progress_does_not_supersede_answer_authority(
        self,
    ) -> None:
        prepared = _prepare_answer(
            f"gate35-reviewer-settlement-{uuid4().hex}"
        )
        bundle, _ = self._accept_answer(
            prepared,
            event_suffix="prepare",
        )
        assert bundle.answer is not None
        answer = bundle.answer
        manifest = prepared.world.controller.build_run_trace_manifest(
            prepared.world.schedule.case_id
        )
        objection = ReviewerObjection(
            objection_id=f"objection:{uuid4().hex}",
            objection_key="insufficient-counterevidence-review",
            revision_number=1,
            prior_objection_id=None,
            case_id=prepared.world.schedule.case_id,
            answer_version_id=answer.answer_version_id,
            claim_id=answer.claims[0].claim_id,
            severity=ReviewerSeverity.BLOCKING,
            status=ReviewerObjectionStatus.OPEN,
            risk_type="evidence_coverage",
            evidence_gap="需要审查反证覆盖是否充分。",
            requested_action="补充或处置 Reviewer 异议。",
            disposition_note=None,
            created_at=NOW,
            resolved_at=None,
        )
        prepared.world.store.record_reviewer_objection(
            objection,
            event_id=(
                f"event:{prepared.world.schedule.case_id}:reviewer"
            ),
        )

        report = self._derive_settlement(
            prepared,
            answer_version_id=answer.answer_version_id,
            event_suffix="after-reviewer-progress",
            manifest=manifest,
            unresolved_blocking_objection_refs=(
                objection.objection_id,
            ),
        )
        self.assertIs(
            report.status,
            SettlementPreconditionStatus.BLOCKED,
        )
        self.assertIn(
            "blocking_objection_open",
            report.fail_reason_codes,
        )
        self.assertNotIn(
            "stale_answer_authority",
            report.fail_reason_codes,
        )

    def test_journal_projector_double_worker_and_ack_loss_replay(
        self,
    ) -> None:
        case_id = f"gate35-projector-race-{uuid4().hex}"
        (
            _,
            store,
            _,
            _,
            _,
            _,
        ) = accepted_single_obligation_runtime(case_id)
        initial = store.get_workflow_read_model(
            case_id,
            realm=ExecutionRealm.CONFORMANCE,
            evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
        )
        event = store.list_events(case_id)[0]
        proposed = apply_workflow_fact(
            initial,
            journal_event_to_workflow_fact(
                event,
                current=initial,
                authority_resolver=store,
            ),
        )

        ready = Barrier(2)

        def commit_from_worker():
            ready.wait()
            return store.commit_workflow_read_model(
                proposed,
                expected_head_version=initial.head.version,
                applied_at=NOW,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(commit_from_worker)
            second = pool.submit(commit_from_worker)
            first_result = first.result()
            second_result = second.result()
        self.assertEqual(first_result, proposed)
        self.assertEqual(second_result, proposed)
        self.assertEqual(
            len(proposed.application_receipts),
            1,
        )

        # The first commit is durable even when its process loses the ACK.
        replayed_after_ack_loss = store.commit_workflow_read_model(
            proposed,
            expected_head_version=initial.head.version,
            applied_at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(replayed_after_ack_loss, proposed)
        self.assertEqual(
            store.get_workflow_read_model(
                case_id,
                realm=ExecutionRealm.CONFORMANCE,
                evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
            ),
            proposed,
        )

        projected = store.project_workflow_read_model(
            case_id,
            realm=ExecutionRealm.CONFORMANCE,
            evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
            applied_at=NOW + timedelta(seconds=2),
        )
        rebuilt = store.rebuild_workflow_read_model(
            case_id,
            realm=ExecutionRealm.CONFORMANCE,
            evidence_profile=EvidenceAdmissionProfile.CONFORMANCE,
        )
        self.assertEqual(projected, rebuilt)
        self.assertEqual(
            projected.head.last_applied_cursor,
            len(store.list_events(case_id)),
        )

    def test_sensitivity_falsification_and_reversal_fail_closed(
        self,
    ) -> None:
        fixture = answer_contract_fixtures.Gate35AnswerContractsTest()
        fixture.setUp()

        checked_proposal = replace(
            fixture.proposal,
            falsification_refs=("falsification:required",),
            reversal_refs=("reversal:required",),
        )
        checked_candidate = build_provisional_answer_candidate(
            case_id=fixture.candidate.case_id,
            current_authority=fixture.fixture.snapshot,
            plan_adoption=fixture.fixture.bundle.adoption,
            version_number=fixture.candidate.version_number,
            prior_answer_version_id=(
                fixture.candidate.prior_answer_version_id
            ),
            claims=(checked_proposal,),
            narrative_blocks=fixture.candidate.narrative_blocks,
            created_by_action_id=fixture.candidate.created_by_action_id,
            created_at=fixture.candidate.created_at,
        )
        checked_support, checked_satisfaction = fixture._support_for(
            checked_candidate,
            checked_proposal,
            validity=fixture.fixture.validity,
        )
        missing_checks = fixture._compile(
            candidate=checked_candidate,
            supports={"payment-change": (checked_support,)},
            satisfactions={
                "payment-change": (checked_satisfaction,)
            },
            check_dispositions=(),
        )
        self.assertIs(
            missing_checks.status,
            AnswerCandidateStatus.REJECTED,
        )
        self.assertIn(
            "analysis_check_disposition_missing",
            missing_checks.prechecks[0].reason_codes,
        )

        sensitivity_obligation_id = (
            "obligation:governed-sensitivity:alternative-window"
        )
        proposal = replace(
            fixture.proposal,
            obligation_ids=(
                *fixture.proposal.obligation_ids,
                sensitivity_obligation_id,
            ),
        )
        candidate = build_provisional_answer_candidate(
            case_id=fixture.candidate.case_id,
            current_authority=fixture.fixture.snapshot,
            plan_adoption=fixture.fixture.bundle.adoption,
            version_number=fixture.candidate.version_number,
            prior_answer_version_id=(
                fixture.candidate.prior_answer_version_id
            ),
            claims=(proposal,),
            narrative_blocks=fixture.candidate.narrative_blocks,
            created_by_action_id=fixture.candidate.created_by_action_id,
            created_at=fixture.candidate.created_at,
        )
        support, satisfaction = fixture._support_for(
            candidate,
            proposal,
            validity=fixture.fixture.validity,
        )
        missing_sensitivity = fixture._compile(
            candidate=candidate,
            supports={"payment-change": (support,)},
            satisfactions={"payment-change": (satisfaction,)},
        )
        self.assertIs(
            missing_sensitivity.status,
            AnswerCandidateStatus.REJECTED,
        )
        self.assertIn(
            "obligation_closure_incomplete",
            missing_sensitivity.prechecks[0].reason_codes,
        )


if __name__ == "__main__":
    unittest.main()
