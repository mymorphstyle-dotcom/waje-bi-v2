from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

import test_gate3_2_obligation_scheduler as scheduler_fixtures
import test_gate3_4_plan_query_continuity as gate34_fixtures
from gate3_5_runtime_fixtures import build_evidence_runtime_world
from gate1_fixtures import NOW, make_frame, record_reviewed_frame
from waje_vnext.domain.async_runtime import OperationIdentity
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.evidence import (
    EvidenceAdmissionProfile,
    EvidenceValidityStatus,
)
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.planning import ExecutionRealm
from waje_vnext.domain.obligation_scheduler import (
    ObligationTerminalStatus,
)
from waje_vnext.domain.workflow import (
    ExecutionState,
    ObligationState,
    WorkflowNoChangeFact,
    apply_workflow_fact,
)
from waje_vnext.domain.workflow_adapter import (
    AcceptedPlanAuthority,
    WorkflowJournalAdapterError,
    WorkflowJournalEventUnsupported,
    journal_event_to_workflow_fact,
    validate_workflow_event_policy_coverage,
)
from waje_vnext.storage import (
    InvalidAuthorityTransition,
    StaleHead,
)


class Gate35WorkflowJournalAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture = gate34_fixtures.Gate34PlanQueryContinuityTest()
        fixture.setUp()
        self.store = fixture.store
        self.case = fixture.case
        self.frame = fixture.frame
        self.bundle = fixture.bundle
        self.realm = ExecutionRealm.CONFORMANCE
        self.profile = EvidenceAdmissionProfile.CONFORMANCE

    def test_real_journal_incremental_projection_equals_full_rebuild(
        self,
    ) -> None:
        incremental = self.store.project_workflow_read_model(
            self.case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
            applied_at=NOW,
        )
        rebuilt = self.store.rebuild_workflow_read_model(
            self.case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
        )

        self.assertEqual(incremental, rebuilt)
        self.assertEqual(
            incremental.head.last_applied_cursor,
            len(self.store.list_events(self.case.case_id)),
        )
        self.assertEqual(
            incremental.snapshot.case.active_plan_revision_id,
            self.bundle.plan.plan_revision_id,
        )
        self.assertEqual(
            incremental.snapshot.accepted_plan_adoption_id,
            self.bundle.adoption.plan_adoption_id,
        )
        self.assertEqual(
            incremental.snapshot.accepted_plan_adoption_sha256,
            self.bundle.adoption.content_sha256,
        )
        self.assertEqual(
            incremental.snapshot.tasks[0].execution_state,
            ExecutionState.PENDING,
        )

    def test_irrelevant_known_events_advance_cursor_only(self) -> None:
        initial = self.store.get_workflow_read_model(
            self.case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
        )
        first_event = self.store.list_events(self.case.case_id)[0]
        fact = journal_event_to_workflow_fact(
            first_event,
            current=initial,
            authority_resolver=self.store,
        )
        projected = apply_workflow_fact(initial, fact)

        self.assertEqual(projected.head.last_applied_cursor, 1)
        self.assertEqual(projected.snapshot.tasks, ())
        self.assertIsNone(
            projected.snapshot.case.active_plan_revision_id
        )

    def test_dispatch_and_checkpoint_drive_task_execution_axis(
        self,
    ) -> None:
        (
            _,
            store,
            scheduler,
            obligations,
            _,
            _,
        ) = scheduler_fixtures.accepted_single_obligation_runtime(
            "case-workflow-execution"
        )
        schedule = scheduler.create_schedule(
            case_id="case-workflow-execution",
            causation_id="accepted-frame",
            created_at=NOW,
        )
        running = store.project_workflow_read_model(
            "case-workflow-execution",
            realm=self.realm,
            evidence_profile=self.profile,
            applied_at=NOW,
        )
        self.assertEqual(
            running.snapshot.tasks[0].execution_state,
            ExecutionState.RUNNING,
        )

        scheduler.admit_completion(
            schedule_id=schedule.schedule_id,
            obligation_id=obligations[0].obligation_id,
            status=ObligationTerminalStatus.EXECUTION_SUCCEEDED,
            result_sha256=content_sha256({"result": "accepted"}),
            completed_at=NOW,
        )
        completed = store.project_workflow_read_model(
            "case-workflow-execution",
            realm=self.realm,
            evidence_profile=self.profile,
            applied_at=NOW,
        )
        rebuilt = store.rebuild_workflow_read_model(
            "case-workflow-execution",
            realm=self.realm,
            evidence_profile=self.profile,
        )
        self.assertEqual(
            completed.snapshot.tasks[0].execution_state,
            ExecutionState.SUCCEEDED,
        )
        self.assertEqual(completed, rebuilt)

    def test_validity_revoke_reopens_workflow_obligation_axis(
        self,
    ) -> None:
        world = build_evidence_runtime_world(
            "case-workflow-validity-revoke"
        )
        lease = world.store.acquire_job_lease(
            outbox_message_id=world.envelope.outbox_message_id,
            owner_id="gate35-workflow-result-worker",
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
            world.store.release_job_lease(lease)
        outcome = world.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )
        satisfied = world.store.project_workflow_read_model(
            world.schedule.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
            applied_at=NOW,
        )
        self.assertEqual(
            satisfied.snapshot.obligations[0].obligation_state,
            ObligationState.SATISFIED,
        )

        world.store.transition_evidence_validity(
            evidence_record_id=(
                world.envelope.evidence_record.evidence_record_id
            ),
            status=EvidenceValidityStatus.REVOKED,
            reason_code="source_release_revoked",
            event_id="event:workflow-validity-revoke",
            recorded_at=NOW,
        )
        reopened = world.store.project_workflow_read_model(
            world.schedule.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
            applied_at=NOW,
        )
        rebuilt = world.store.rebuild_workflow_read_model(
            world.schedule.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
        )
        self.assertEqual(
            reopened.snapshot.obligations[0].obligation_state,
            ObligationState.BLOCKED,
        )
        self.assertEqual(reopened, rebuilt)
        self.assertEqual(
            tuple(
                event.event_type
                for event in world.store.list_events(
                    world.schedule.case_id
                )[-2:]
            ),
            (
                JournalEventType.EVIDENCE_VALIDITY_RECORDED,
                JournalEventType.OBLIGATION_SATISFACTION_RECORDED,
            ),
        )
        self.assertEqual(
            outcome.satisfaction.obligation_id,
            reopened.snapshot.obligations[0].obligation_id,
        )

    def test_policy_registry_fails_closed_when_a_known_type_is_unmapped(
        self,
    ) -> None:
        import waje_vnext.domain.workflow_adapter as adapter

        policy = adapter._EVENT_POLICIES.pop(  # noqa: SLF001
            JournalEventType.CASE_OPENED
        )
        try:
            with self.assertRaises(WorkflowJournalEventUnsupported):
                validate_workflow_event_policy_coverage()
            initial = self.store.get_workflow_read_model(
                self.case.case_id,
                realm=self.realm,
                evidence_profile=self.profile,
            )
            with self.assertRaises(WorkflowJournalEventUnsupported):
                journal_event_to_workflow_fact(
                    self.store.list_events(self.case.case_id)[0],
                    current=initial,
                    authority_resolver=self.store,
                )
        finally:
            adapter._EVENT_POLICIES[
                JournalEventType.CASE_OPENED
            ] = policy

    def test_adapter_rejects_event_that_differs_from_durable_source(
        self,
    ) -> None:
        event = next(
            item
            for item in self.store.list_events(self.case.case_id)
            if item.event_type is JournalEventType.PLAN_ACCEPTED
        )
        payload = dict(event.payload)
        payload["content_sha256"] = content_sha256(
            {"tampered": "plan"}
        )
        tampered = replace(
            event,
            payload=payload,
            operation=replace(
                event.operation,
                payload_sha256=content_sha256(payload),
            ),
        )
        current = self.store.get_workflow_read_model(
            self.case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
        )

        with self.assertRaises(InvalidAuthorityTransition):
            journal_event_to_workflow_fact(
                tampered,
                current=current,
                authority_resolver=self.store,
            )

    def test_adapter_rejects_plan_adoption_authority_drift(self) -> None:
        event = next(
            item
            for item in self.store.list_events(self.case.case_id)
            if item.event_type is JournalEventType.PLAN_ACCEPTED
        )
        authority = self.store.resolve_workflow_event_authority(event)
        self.assertIsInstance(authority, AcceptedPlanAuthority)
        assert isinstance(authority, AcceptedPlanAuthority)
        drifted = replace(
            authority,
            adoption=replace(
                authority.adoption,
                plan_content_sha256=content_sha256(
                    {"drifted": "accepted-plan"}
                ),
            ),
        )

        class DriftedResolver:
            def resolve_workflow_event_authority(self, _event):
                return drifted

        current = self.store.get_workflow_read_model(
            self.case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
        )
        with self.assertRaises(WorkflowJournalAdapterError):
            journal_event_to_workflow_fact(
                event,
                current=current,
                authority_resolver=DriftedResolver(),
            )

    def test_persisted_projection_cannot_change_realm_or_profile(
        self,
    ) -> None:
        self.store.get_workflow_read_model(
            self.case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
        )
        with self.assertRaises(InvalidAuthorityTransition):
            self.store.get_workflow_read_model(
                self.case.case_id,
                realm=self.realm,
                evidence_profile=EvidenceAdmissionProfile.PRODUCTION,
            )

    def test_projection_commit_uses_cursor_cas_and_source_event_hash(
        self,
    ) -> None:
        model = self.store.get_workflow_read_model(
            self.case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
        )
        events = self.store.list_events(self.case.case_id)
        first = apply_workflow_fact(
            model,
            journal_event_to_workflow_fact(
                events[0],
                current=model,
                authority_resolver=self.store,
            ),
        )
        first = self.store.commit_workflow_read_model(
            first,
            expected_head_version=0,
            applied_at=NOW,
        )
        second = apply_workflow_fact(
            first,
            journal_event_to_workflow_fact(
                events[1],
                current=first,
                authority_resolver=self.store,
            ),
        )
        with self.assertRaises(StaleHead):
            self.store.commit_workflow_read_model(
                second,
                expected_head_version=0,
                applied_at=NOW,
            )

        forged = apply_workflow_fact(
            first,
            WorkflowNoChangeFact(
                case_id=self.case.case_id,
                cursor=events[1].cursor,
                source_event_id=events[1].event_id,
                source_event_sha256=content_sha256(
                    {"forged": "source-event"}
                ),
            ),
        )
        with self.assertRaises(InvalidAuthorityTransition):
            self.store.commit_workflow_read_model(
                forged,
                expected_head_version=1,
                applied_at=NOW,
            )

    def test_frame_correction_fences_plan_and_late_result_is_audit_only(
        self,
    ) -> None:
        before = self.store.project_workflow_read_model(
            self.case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
            applied_at=NOW,
        )
        question = self.store.get_question(
            self.case.accepted_question_revision_id or ""
        )
        corrected_frame = make_frame(
            revision_number=2,
            frame_id="frame-2",
            prior_id=self.frame.frame_revision_id,
            question=question,
        )
        proof_id = record_reviewed_frame(
            self.store,
            corrected_frame,
        )
        corrected_case = self.store.accept_frame(
            corrected_frame,
            frame_admission_proof_id=proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame-2",
            recorded_at=corrected_frame.created_at,
        )
        after_correction = self.store.project_workflow_read_model(
            corrected_case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
            applied_at=corrected_frame.created_at,
        )

        self.assertGreater(
            after_correction.head.version,
            before.head.version,
        )
        self.assertIsNone(
            after_correction.snapshot.case.active_plan_revision_id
        )
        self.assertTrue(
            all(
                item.execution_state is ExecutionState.SUPERSEDED
                for item in after_correction.snapshot.tasks
            )
        )

        payload = {
            "receipt_id": content_sha256({"late": "receipt"}),
            "envelope_id": content_sha256({"late": "envelope"}),
        }
        self.store.append_event(
            case_id=corrected_case.case_id,
            expected_next_cursor=(
                len(self.store.list_events(corrected_case.case_id)) + 1
            ),
            event_id="event-late-capability-result",
            event_type=JournalEventType.CAPABILITY_RESULT_LANDED,
            recorded_at=corrected_frame.created_at,
            action_id=None,
            authority_ref=payload["receipt_id"],
            payload=payload,
            customer_projection={"state": "must-not-be-trusted"},
            operation=OperationIdentity(
                operation_id="operation-late-capability-result",
                idempotency_key="idempotency-late-capability-result",
                causation_id="outbox-old-plan",
                correlation_id="run-old-plan",
                authority_revision=0,
                payload_sha256=content_sha256(payload),
            ),
        )
        after_late = self.store.project_workflow_read_model(
            corrected_case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
            applied_at=corrected_frame.created_at,
        )
        rebuilt = self.store.rebuild_workflow_read_model(
            corrected_case.case_id,
            realm=self.realm,
            evidence_profile=self.profile,
        )

        self.assertEqual(after_late, rebuilt)
        self.assertIsNone(after_late.snapshot.case.active_plan_revision_id)
        self.assertTrue(
            all(
                item.execution_state is ExecutionState.SUPERSEDED
                for item in after_late.snapshot.tasks
            )
        )
        self.assertEqual(
            after_late.head.last_applied_cursor,
            len(self.store.list_events(corrected_case.case_id)),
        )


if __name__ == "__main__":
    unittest.main()
