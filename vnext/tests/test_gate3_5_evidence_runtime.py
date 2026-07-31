from __future__ import annotations

import unittest
from datetime import timedelta

from gate3_5_runtime_fixtures import (
    build_evidence_runtime_world,
    forge_conformance_provenance_envelope,
)
from test_gate3_2_obligation_scheduler import NOW
from waje_vnext.controller import (
    EffectExecutionResult,
    EvidenceRuntime,
    ScriptedEffectExecutor,
    WAJEController,
)
from waje_vnext.domain.actions import (
    ActionKind,
    AgentActionProposal,
    CallCapabilityPayload,
)
from waje_vnext.domain.async_runtime import MailboxMessageKind
from waje_vnext.domain.canonical import to_jsonable
from waje_vnext.domain.controller import ControllerPhase
from waje_vnext.domain.events import JournalEventType
from waje_vnext.domain.evidence import (
    EvidenceAdmissionProfile,
    EvidenceAdmissionStatus,
    EvidenceValidityStatus,
    ObligationSatisfactionStatus,
)
from waje_vnext.domain.obligation_scheduler import (
    ObligationTerminalStatus,
)
from waje_vnext.domain.runtime_amendment import JobDisposition
from waje_vnext.providers import ScriptedPrimaryAgentProvider
from waje_vnext.storage.ports import (
    AuthorityConflict,
    InvalidAuthorityTransition,
    LeaseFenceLost,
)


class _FailBeforeJobDisposition:
    def admit_completion(self, **_: object):
        raise RuntimeError("injected failure before job disposition")


class Gate35EvidenceRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        world = build_evidence_runtime_world(
            "case-gate35-evidence-runtime"
        )
        for field_name in world.__dataclass_fields__:
            setattr(self, field_name, getattr(world, field_name))

    def _land(self, *, received_at=NOW):
        lease = self.store.acquire_job_lease(
            outbox_message_id=self.envelope.outbox_message_id,
            owner_id="gate35-evidence-result-worker",
            now=received_at,
            expires_at=received_at + timedelta(minutes=5),
        )
        try:
            return self.runtime.land_result(
                envelope=self.envelope,
                job_lease=lease,
                received_at=received_at,
            )
        finally:
            self.store.release_job_lease(lease)

    def _typed_controller(self, result: EffectExecutionResult):
        return self._typed_controller_with_executor(
            ScriptedEffectExecutor((result,))
        )

    def _typed_controller_with_executor(self, effect_executor):
        prior = self.controller.resume(self.schedule.case_id)
        self.controller._checkpoint(
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
        )
        proposal = AgentActionProposal(
            kind=ActionKind.CALL_CAPABILITY,
            payload=CallCapabilityPayload(
                task_id=self.binding.task_id,
                query_binding_id=self.binding.query_binding_id,
            ),
        )
        return WAJEController(
            store=self.store,
            provider=ScriptedPrimaryAgentProvider((proposal,)),
            effect_executor=effect_executor,
            owner_id="gate35-primary-controller-worker",
            clock=lambda: NOW,
        )

    def test_t1_is_durable_without_making_job_terminal(self) -> None:
        receipt = self._land()

        self.assertEqual(
            self.store.get_capability_result_receipt(
                receipt.capability_result_receipt_id
            ),
            receipt,
        )
        self.assertIsNone(
            self.store.get_job_disposition(
                self.dispatch.outbox_message_id
            )
        )
        self.assertEqual(
            self.store.list_obligation_completions(
                self.schedule.schedule_id
            ),
            (),
        )

    def test_correction_during_typed_effect_lands_then_supersedes(
        self,
    ) -> None:
        fixture = self

        class CorrectionDuringTypedEffect:
            controller = None

            def execute(self, message):
                assert self.controller is not None
                self.controller.ingress_message(
                    case_id=message.case_id,
                    thread_id="thread-case-gate35-evidence-runtime",
                    run_id=fixture.run_id,
                    user_message="改用新的经营口径重新调查。",
                    kind=MailboxMessageKind.USER_CORRECTION,
                    idempotency_key="gate35-correction-during-typed-effect",
                )
                return EffectExecutionResult(
                    payload={
                        "capability_result_envelope": to_jsonable(
                            fixture.envelope
                        )
                    },
                    business_summary="Late typed Evidence returned.",
                )

        executor = CorrectionDuringTypedEffect()
        controller = self._typed_controller_with_executor(executor)
        executor.controller = controller
        controller.advance(self.schedule.case_id)
        controller.deliver_pending_llm(self.schedule.case_id)

        resumed = controller.deliver_pending_effect(
            self.schedule.case_id
        )

        self.assertEqual(
            resumed.phase,
            ControllerPhase.WAITING_FOR_MESSAGE_BINDING,
        )
        receipt = self.store.find_capability_result_receipt_by_outbox(
            self.dispatch.outbox_message_id
        )
        self.assertIsNotNone(receipt)
        admissions = self.store.list_evidence_admissions(
            case_id=self.schedule.case_id
        )
        self.assertEqual(len(admissions), 1)
        self.assertEqual(
            admissions[0].status,
            EvidenceAdmissionStatus.REJECTED,
        )
        self.assertEqual(
            self.store.get_job_disposition(
                self.dispatch.outbox_message_id
            ).disposition,
            JobDisposition.SUPERSEDED,
        )
    def test_primary_controller_uses_typed_t1_then_t2(self) -> None:
        controller = self._typed_controller(
            EffectExecutionResult(
                payload={
                    "capability_result_envelope": to_jsonable(
                        self.envelope
                    )
                },
                business_summary="Typed capability Evidence returned.",
            )
        )

        controller.advance(self.schedule.case_id)
        waiting = controller.deliver_pending_llm(self.schedule.case_id)
        self.assertEqual(waiting.phase, ControllerPhase.WAITING_FOR_EFFECT)
        self.assertEqual(
            waiting.pending_job_ids,
            (self.dispatch.outbox_message_id,),
        )

        landed = controller.deliver_pending_effect(self.schedule.case_id)
        self.assertEqual(
            landed.phase,
            ControllerPhase.WAITING_FOR_EVIDENCE_ADMISSION,
        )
        self.assertIsNotNone(
            self.store.find_capability_result_receipt_by_outbox(
                self.dispatch.outbox_message_id
            )
        )
        self.assertIsNone(
            self.store.get_job_disposition(
                self.dispatch.outbox_message_id
            )
        )

        admitted = controller.deliver_pending_effect(self.schedule.case_id)
        self.assertEqual(admitted.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertEqual(
            self.store.get_job_disposition(
                self.dispatch.outbox_message_id
            ).disposition,
            JobDisposition.COMPLETED,
        )
        self.assertEqual(
            len(
                self.store.list_evidence_admissions(
                    case_id=self.schedule.case_id
                )
            ),
            1,
        )
        self.assertFalse(
            any(
                event.event_type is JournalEventType.EFFECT_COMPLETED
                for event in self.store.list_events(self.schedule.case_id)
            )
        )

    def test_controller_resumes_from_t1_without_rerunning_provider(
        self,
    ) -> None:
        class MustNotExecute:
            def execute(self, _message):
                raise AssertionError("provider reran after durable T1")

        controller = self._typed_controller_with_executor(MustNotExecute())
        controller.advance(self.schedule.case_id)
        controller.deliver_pending_llm(self.schedule.case_id)
        self._land()

        recovered = controller.deliver_pending_effect(
            self.schedule.case_id
        )

        self.assertEqual(recovered.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertEqual(
            self.store.get_job_disposition(
                self.dispatch.outbox_message_id
            ).disposition,
            JobDisposition.COMPLETED,
        )
        self.assertEqual(
            self.store.list_effect_attempts(
                self.dispatch.outbox_message_id
            ),
            (),
        )

    def test_repeated_capability_action_reuses_terminal_evidence(self) -> None:
        result = EffectExecutionResult(
            payload={
                "capability_result_envelope": to_jsonable(self.envelope)
            },
            business_summary="Typed capability Evidence returned.",
        )
        controller = self._typed_controller(result)
        controller.advance(self.schedule.case_id)
        controller.deliver_pending_llm(self.schedule.case_id)
        controller.deliver_pending_effect(self.schedule.case_id)
        controller.deliver_pending_effect(self.schedule.case_id)
        attempts_before = self.store.list_effect_attempts(
            self.dispatch.outbox_message_id
        )

        replay_controller = self._typed_controller(result)
        replay_controller.advance(self.schedule.case_id)
        replayed = replay_controller.deliver_pending_llm(
            self.schedule.case_id
        )

        self.assertEqual(replayed.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertEqual(replayed.pending_job_ids, ())
        self.assertEqual(
            self.store.list_effect_attempts(
                self.dispatch.outbox_message_id
            ),
            attempts_before,
        )

    def test_primary_controller_rejects_generic_capability_success(
        self,
    ) -> None:
        controller = self._typed_controller(
            EffectExecutionResult(
                payload={"rows": 2, "direction": "higher"},
                business_summary="Generic payload must not close Evidence.",
            )
        )
        controller.advance(self.schedule.case_id)
        controller.deliver_pending_llm(self.schedule.case_id)

        recovered = controller.deliver_pending_effect(
            self.schedule.case_id
        )

        self.assertEqual(recovered.phase, ControllerPhase.READY_FOR_AGENT)
        self.assertIsNone(
            self.store.find_capability_result_receipt_by_outbox(
                self.dispatch.outbox_message_id
            )
        )
        disposition = self.store.get_job_disposition(
            self.dispatch.outbox_message_id
        )
        self.assertIsNotNone(disposition)
        self.assertEqual(
            disposition.disposition,
            JobDisposition.TERMINAL_FAILURE,
        )
        attempts = self.store.list_effect_attempts(
            self.dispatch.outbox_message_id
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(
            attempts[0].error_code,
            "invalid_evidence_result_contract",
        )
        self.assertEqual(
            self.store.list_evidence_admissions(
                case_id=self.schedule.case_id
            ),
            (),
        )

    def test_t2_atomically_admits_evidence_and_terminalizes_job(
        self,
    ) -> None:
        receipt = self._land()
        outcome = self.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )

        self.assertEqual(
            outcome.admission.status,
            EvidenceAdmissionStatus.ACCEPTED,
        )
        self.assertEqual(
            outcome.validity.status,
            EvidenceValidityStatus.ADMITTED_VALID,
        )
        self.assertEqual(
            outcome.satisfaction.status,
            ObligationSatisfactionStatus.SATISFIED,
        )
        self.assertEqual(
            outcome.completion.completion.status,
            ObligationTerminalStatus.EXECUTION_SUCCEEDED,
        )
        disposition = self.store.get_job_disposition(
            self.dispatch.outbox_message_id
        )
        self.assertIsNotNone(disposition)
        self.assertEqual(
            disposition.disposition,
            JobDisposition.COMPLETED,
        )

    def test_validity_transition_replay_uses_command_identity(self) -> None:
        receipt = self._land()
        admitted = self.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )
        transition_event_id = "event:gate35:validity-command-replay"
        first = self.store.transition_evidence_validity(
            evidence_record_id=admitted.admission.evidence_record_id,
            status=EvidenceValidityStatus.REVOKED,
            reason_code="source_release_revoked",
            event_id=transition_event_id,
            recorded_at=NOW + timedelta(seconds=1),
        )
        replayed = self.store.transition_evidence_validity(
            evidence_record_id=admitted.admission.evidence_record_id,
            status=EvidenceValidityStatus.REVOKED,
            reason_code="source_release_revoked",
            event_id=transition_event_id,
            recorded_at=NOW + timedelta(seconds=2),
        )
        canonical_t2_replay = self.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW + timedelta(seconds=3),
        )

        self.assertEqual(replayed, first)
        self.assertEqual(canonical_t2_replay, admitted)
        self.assertEqual(
            self.store.latest_evidence_validity(
                admitted.admission.evidence_record_id
            ),
            first[0],
        )
        self.assertEqual(
            self.store.latest_obligation_satisfaction(
                admitted.admission.obligation_id
            ),
            first[1],
        )
        with self.assertRaisesRegex(
            AuthorityConflict,
            "already has different content",
        ):
            self.store.transition_evidence_validity(
                evidence_record_id=admitted.admission.evidence_record_id,
                status=EvidenceValidityStatus.REVOKED,
                reason_code="different_reason",
                event_id=transition_event_id,
                recorded_at=NOW + timedelta(seconds=4),
            )

    def test_t2_failure_rolls_back_admission_and_keeps_t1(self) -> None:
        receipt = self._land()
        failing = EvidenceRuntime(
            store=self.store,
            owner_id="gate35-failing-admission-worker",
            profile=EvidenceAdmissionProfile.CONFORMANCE,
            obligation_coordinator=_FailBeforeJobDisposition(),
        )

        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            failing.admit_result(
                receipt_id=receipt.capability_result_receipt_id,
                admitted_at=NOW,
            )

        self.assertEqual(
            self.store.get_capability_result_receipt(
                receipt.capability_result_receipt_id
            ),
            receipt,
        )
        self.assertEqual(
            self.store.list_evidence_admissions(
                case_id=self.schedule.case_id
            ),
            (),
        )
        self.assertIsNone(
            self.store.get_job_disposition(
                self.dispatch.outbox_message_id
            )
        )

        recovered = self.runtime.recover_outbox(
            outbox_message_id=self.dispatch.outbox_message_id,
            admitted_at=NOW,
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(
            recovered.admission.status,
            EvidenceAdmissionStatus.ACCEPTED,
        )

    def test_correction_between_t1_and_t2_supersedes_old_result(
        self,
    ) -> None:
        receipt = self._land()
        self.controller.ingress_message(
            case_id=self.schedule.case_id,
            thread_id="thread-case-gate35-evidence-runtime",
            run_id=self.run_id,
            user_message="改用新的经营口径重新调查。",
            kind=MailboxMessageKind.USER_CORRECTION,
            idempotency_key="gate35-correction-between-t1-t2",
        )

        outcome = self.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )

        self.assertEqual(
            outcome.admission.status,
            EvidenceAdmissionStatus.REJECTED,
        )
        self.assertEqual(
            outcome.validity.status,
            EvidenceValidityStatus.NEVER_ADMITTED,
        )
        self.assertEqual(
            outcome.completion.completion.status,
            ObligationTerminalStatus.SUPERSEDED,
        )
        self.assertEqual(
            self.store.get_job_disposition(
                self.dispatch.outbox_message_id
            ).disposition,
            JobDisposition.SUPERSEDED,
        )

    def test_retries_reuse_t1_t2_and_do_not_create_business_revisions(
        self,
    ) -> None:
        case_before = self.store.get_case(self.schedule.case_id)
        receipt = self._land()
        replayed_receipt = self._land()
        first = self.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )
        replayed = self.runtime.admit_result(
            receipt_id=receipt.capability_result_receipt_id,
            admitted_at=NOW,
        )
        case_after = self.store.get_case(self.schedule.case_id)

        self.assertEqual(receipt, replayed_receipt)
        self.assertEqual(first, replayed)
        self.assertEqual(
            case_before.accepted_frame_revision_id,
            case_after.accepted_frame_revision_id,
        )
        self.assertEqual(
            case_before.accepted_plan_revision_id,
            case_after.accepted_plan_revision_id,
        )
        events = self.store.list_events(self.schedule.case_id)
        self.assertEqual(
            sum(
                event.event_type
                is JournalEventType.CAPABILITY_RESULT_LANDED
                for event in events
            ),
            1,
        )

    def test_t1_later_delivery_timestamp_returns_canonical_receipt(
        self,
    ) -> None:
        first = self._land()
        replayed = self._land(received_at=NOW + timedelta(seconds=1))

        self.assertEqual(replayed, first)
        self.assertEqual(replayed.received_at, NOW)
        self.assertEqual(
            replayed.delivery_owner_id,
            first.delivery_owner_id,
        )
        self.assertEqual(
            replayed.delivery_fencing_token,
            first.delivery_fencing_token,
        )

    def test_t1_takeover_with_different_result_conflicts(self) -> None:
        self._land()
        forged = forge_conformance_provenance_envelope(self)
        replay_at = NOW + timedelta(seconds=1)
        lease = self.store.acquire_job_lease(
            outbox_message_id=forged.outbox_message_id,
            owner_id="takeover-different-result-worker",
            now=replay_at,
            expires_at=replay_at + timedelta(minutes=5),
        )
        try:
            with self.assertRaisesRegex(
                AuthorityConflict,
                "already landed different result",
            ):
                self.runtime.land_result(
                    envelope=forged,
                    job_lease=lease,
                    received_at=replay_at,
                )
        finally:
            self.store.release_job_lease(lease)

    def test_t1_rejects_a_worker_after_lease_takeover(self) -> None:
        stale = self.store.acquire_job_lease(
            outbox_message_id=self.envelope.outbox_message_id,
            owner_id="stale-evidence-worker",
            now=NOW,
            expires_at=NOW + timedelta(seconds=1),
        )
        takeover_at = NOW + timedelta(seconds=2)
        self.storage_clock.set(takeover_at)
        current = self.store.acquire_job_lease(
            outbox_message_id=self.envelope.outbox_message_id,
            owner_id="takeover-evidence-worker",
            now=takeover_at,
            expires_at=takeover_at + timedelta(minutes=5),
        )
        try:
            with self.assertRaises(LeaseFenceLost):
                self.runtime.land_result(
                    envelope=self.envelope,
                    job_lease=stale,
                    received_at=takeover_at,
                )
        finally:
            self.store.release_job_lease(current)

    def test_t1_rejects_expired_lease_despite_backdated_receipt_time(
        self,
    ) -> None:
        stale = self.store.acquire_job_lease(
            outbox_message_id=self.envelope.outbox_message_id,
            owner_id="expired-evidence-worker",
            now=NOW,
            expires_at=NOW + timedelta(seconds=1),
        )
        self.storage_clock.set(NOW + timedelta(seconds=2))

        try:
            with self.assertRaises(LeaseFenceLost):
                self.runtime.land_result(
                    envelope=self.envelope,
                    job_lease=stale,
                    received_at=NOW - timedelta(days=30),
                )
            self.assertIsNone(
                self.store.find_capability_result_receipt_by_outbox(
                    self.envelope.outbox_message_id
                )
            )
        finally:
            self.store.release_job_lease(stale)

    def test_in_memory_rejects_forged_conformance_provenance(
        self,
    ) -> None:
        forged = forge_conformance_provenance_envelope(self)
        lease = self.store.acquire_job_lease(
            outbox_message_id=forged.outbox_message_id,
            owner_id="forged-provenance-worker",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        try:
            with self.assertRaisesRegex(
                InvalidAuthorityTransition,
                "sealed execution provenance",
            ):
                self.runtime.land_result(
                    envelope=forged,
                    job_lease=lease,
                    received_at=NOW,
                )
        finally:
            self.store.release_job_lease(lease)


if __name__ == "__main__":
    unittest.main()
