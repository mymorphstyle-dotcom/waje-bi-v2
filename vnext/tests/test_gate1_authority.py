from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from gate1_fixtures import (
    NOW,
    accept_initial_question,
    make_answer,
    make_evidence,
    make_frame,
    make_objection,
    make_plan,
    record_reviewed_frame,
)
from gate3_plan_fixtures import record_plan_bundle
from test_gate3_3_measurement_resolver import make_trusted_verifier
from waje_vnext.domain.actions import (
    ActionEnvelope,
    ActionKind,
    AskUserPayload,
    CallCapabilityPayload,
    ReviseFramePayload,
    RunSensitivityPayload,
)
from waje_vnext.domain.admission import admit_action
from waje_vnext.domain.async_runtime import OperationIdentity
from waje_vnext.domain.authority import (
    AnswerStatus,
    CaseLifecycle,
    ClaimVerifierStatus,
    DecisionOption,
    EvidenceRecord,
    ReviewerObjectionStatus,
    WorkPlanRevision,
    WorkTask,
)
from waje_vnext.domain.context import (
    ContextEvidenceItem,
    ContextEventItem,
    ContextPacket,
    ContextUserMessageItem,
    build_context_packet,
)
from waje_vnext.domain.canonical import content_sha256
from waje_vnext.domain.events import JournalEventType
from waje_vnext.storage import (
    AuthorityConflict,
    InMemoryAuthorityStore,
    InvalidAuthorityTransition,
    StaleHead,
)


def make_frame_action_payload(reason: str) -> ReviseFramePayload:
    frame = make_frame()
    return ReviseFramePayload(
        question_revision_id=frame.question_revision_id,
        revision_reason_ref=reason,
        measurement_design=frame.measurement_design,
    )


def make_contract_task(
    task_id: str,
    depends_on_task_ids: tuple[str, ...],
) -> WorkTask:
    return WorkTask(
        task_id=task_id,
        proposal_task_key=task_id,
        business_purpose=f"Investigate {task_id}",
        capability_intent_ref=(
            "waje-vnext://capability-intent/measurement-evidence.v1"
        ),
        target_estimand_ids=("estimand-1",),
        obligation_ids=(content_sha256({"obligation": task_id}),),
        query_binding_ids=(content_sha256({"query": task_id}),),
        completion_spec_ids=("completion-1",),
        execution_success_policy_refs=("completion:all:v1",),
        execution_degrade_policy_refs=("degrade:local:v1",),
        execution_stop_policy_refs=("stop:blocked:v1",),
        depends_on_task_ids=depends_on_task_ids,
    )


class AuthorityModelTest(unittest.TestCase):
    def test_later_frame_requires_prior_revision(self) -> None:
        with self.assertRaises(ValueError):
            make_frame(revision_number=2, frame_id="frame-2")

    def test_work_plan_rejects_unknown_dependency(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown dependencies"):
            WorkPlanRevision(
                plan_revision_id="plan-invalid",
                case_id="case-1",
                frame_revision_id="frame-1",
                revision_number=1,
                prior_plan_revision_id=None,
                created_by_action_id="action-plan-invalid",
                created_at=NOW,
                revision_reason="Invalid dependency",
                resolution_outcome_ids=(content_sha256("resolution-1"),),
                tasks=(
                    make_contract_task("task-1", ("task-missing",)),
                ),
            )

    def test_work_plan_rejects_dependency_cycle(self) -> None:
        with self.assertRaisesRegex(ValueError, "acyclic"):
            WorkPlanRevision(
                plan_revision_id="plan-cycle",
                case_id="case-1",
                frame_revision_id="frame-1",
                revision_number=1,
                prior_plan_revision_id=None,
                created_by_action_id="action-plan-cycle",
                created_at=NOW,
                revision_reason="Invalid cyclic investigation",
                resolution_outcome_ids=(content_sha256("resolution-1"),),
                tasks=(
                    make_contract_task("task-a", ("task-b",)),
                    make_contract_task("task-b", ("task-a",)),
                ),
            )

    def test_evidence_payload_is_deeply_immutable(self) -> None:
        evidence = make_evidence(payload={"nested": {"value": 1}})

        with self.assertRaises(TypeError):
            evidence.inline_payload["nested"]["value"] = 2  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            evidence.business_summary = "mutated"  # type: ignore[misc]

    def test_evidence_rejects_payload_hash_mismatch(self) -> None:
        valid = make_evidence()
        values = {
            field: getattr(valid, field)
            for field in valid.__dataclass_fields__
        }
        values["payload_sha256"] = "0" * 64

        with self.assertRaisesRegex(ValueError, "does not match"):
            EvidenceRecord(**values)

    def test_gate3_rejects_every_settled_answer(self) -> None:
        with self.assertRaisesRegex(ValueError, "Gate 3"):
            make_answer(status=AnswerStatus.SETTLED)

    def test_provisional_answer_rejects_settlement_fingerprint(self) -> None:
        with self.assertRaisesRegex(ValueError, "provisional"):
            replace(make_answer(), settlement_fingerprint="0" * 64)

    def test_authority_enums_reject_untyped_strings(self) -> None:
        with self.assertRaisesRegex(TypeError, "AnswerStatus"):
            replace(make_answer(), status="settled")


class TypedActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        self.case = self.store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        self.case, self.question = accept_initial_question(
            self.store,
            self.case,
        )

    def test_action_kind_requires_matching_payload(self) -> None:
        with self.assertRaises(TypeError):
            ActionEnvelope(
                action_id="action-invalid",
                case_id="case-1",
                kind=ActionKind.REVISE_FRAME,
                expected_head_version=0,
                idempotency_key="key-invalid",
                issued_at=NOW,
                payload=CallCapabilityPayload(
                    task_id="task-pattern",
                    query_binding_id=content_sha256(
                        "query-binding-mismatch"
                    ),
                ),
            )

    def test_ask_user_requires_freeform_correction_path(self) -> None:
        options = (
            DecisionOption("recommended", "Use recommended frame", "Continue"),
            DecisionOption("change", "Change the frame", "Revise measurement"),
        )
        with self.assertRaisesRegex(ValueError, "freeform"):
            AskUserPayload(
                question="Which measurement should apply?",
                options=options,
                recommended_option_id="recommended",
                allow_freeform=False,
            )

    def test_capability_action_does_not_create_revision(self) -> None:
        frame = make_frame()
        frame_proof_id = record_reviewed_frame(self.store, frame)
        case = self.store.accept_frame(
            frame,
            frame_admission_proof_id=frame_proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )
        case, bundle = record_plan_bundle(
            store=self.store,
            case=case,
            frame=frame,
            created_at=NOW,
        )
        plan = bundle.plan
        binding = bundle.query_bindings[0]
        action = ActionEnvelope(
            action_id="action-call-1",
            case_id="case-1",
            kind=ActionKind.CALL_CAPABILITY,
            expected_head_version=case.head_version,
            idempotency_key="capability-call-1",
            issued_at=NOW,
            payload=CallCapabilityPayload(
                task_id=binding.task_id,
                query_binding_id=binding.query_binding_id,
            ),
        )

        admission = admit_action(
            case=case,
            action=action,
            current_plan=plan,
            current_query_bindings=bundle.query_bindings,
        )

        self.assertTrue(admission.accepted)
        self.assertFalse(admission.creates_frame_revision)
        self.assertFalse(admission.creates_plan_revision)
        self.assertEqual(self.store.get_case("case-1").head_version, 3)

    def test_stale_action_is_rejected_before_semantic_payload(self) -> None:
        action = ActionEnvelope(
            action_id="action-frame",
            case_id="case-1",
            kind=ActionKind.REVISE_FRAME,
            expected_head_version=9,
            idempotency_key="frame-key",
            issued_at=NOW,
            payload=make_frame_action_payload("Initial frame"),
        )

        admission = admit_action(
            case=self.case,
            action=action,
            current_plan=None,
        )

        self.assertFalse(admission.accepted)
        self.assertEqual(admission.reason_code, "stale_head")

    def test_sensitivity_action_requires_a_known_plan_task(self) -> None:
        frame = make_frame()
        frame_proof_id = record_reviewed_frame(self.store, frame)
        case = self.store.accept_frame(
            frame,
            frame_admission_proof_id=frame_proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )
        case, bundle = record_plan_bundle(
            store=self.store,
            case=case,
            frame=frame,
            created_at=NOW,
        )
        plan = bundle.plan
        action = ActionEnvelope(
            action_id="action-sensitivity",
            case_id="case-1",
            kind=ActionKind.RUN_SENSITIVITY,
            expected_head_version=case.head_version,
            idempotency_key="sensitivity-key",
            issued_at=NOW,
            payload=RunSensitivityPayload(
                task_id="task-unknown",
                query_binding_id=(
                    bundle.query_bindings[0].query_binding_id
                ),
                sensitivity_id="sensitivity:complete-month-only",
            ),
        )

        admission = admit_action(
            case=case,
            action=action,
            current_plan=plan,
            current_query_bindings=bundle.query_bindings,
        )

        self.assertFalse(admission.accepted)
        self.assertEqual(admission.reason_code, "unknown_plan_task")

    def test_action_kind_must_match_governed_capability_intent(self) -> None:
        frame = make_frame()
        frame_proof_id = record_reviewed_frame(self.store, frame)
        case = self.store.accept_frame(
            frame,
            frame_admission_proof_id=frame_proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame-for-intent",
            recorded_at=frame.created_at,
        )
        case, bundle = record_plan_bundle(
            store=self.store,
            case=case,
            frame=frame,
            created_at=NOW,
        )
        binding = bundle.query_bindings[0]
        action = ActionEnvelope(
            action_id="action-wrong-for-intent",
            case_id="case-1",
            kind=ActionKind.RUN_SENSITIVITY,
            expected_head_version=case.head_version,
            idempotency_key="wrong-intent-action-key",
            issued_at=NOW,
            payload=RunSensitivityPayload(
                task_id=binding.task_id,
                query_binding_id=binding.query_binding_id,
                sensitivity_id="sensitivity:forged",
            ),
        )

        admission = admit_action(
            case=case,
            action=action,
            current_plan=bundle.plan,
            current_query_bindings=bundle.query_bindings,
        )

        self.assertFalse(admission.accepted)
        self.assertEqual(
            admission.reason_code,
            "capability_intent_action_mismatch",
        )

    def test_terminal_case_rejects_new_actions(self) -> None:
        terminal = replace(self.case, lifecycle=CaseLifecycle.STOPPED)
        action = ActionEnvelope(
            action_id="action-stop-case",
            case_id="case-1",
            kind=ActionKind.REVISE_FRAME,
            expected_head_version=terminal.head_version,
            idempotency_key="terminal-key",
            issued_at=NOW,
            payload=make_frame_action_payload("Try to reopen"),
        )

        admission = admit_action(
            case=terminal,
            action=action,
            current_plan=None,
        )

        self.assertFalse(admission.accepted)
        self.assertEqual(admission.reason_code, "case_terminal")


class ContextPacketTest(unittest.TestCase):
    def test_context_hash_is_reproducible_from_authority_projection(self) -> None:
        store = InMemoryAuthorityStore()
        case = store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        evidence_index = (
            ContextEvidenceItem(
                evidence_record_id="evidence-1",
                evidence_type="descriptive",
                strength="quantified",
                business_summary="Measured pattern",
                limitation_count=1,
                frame_revision_id="frame-1",
                plan_revision_id="plan-1",
                task_id="task-pattern",
                snapshot_release_ref="release-1",
            ),
        )

        first = build_context_packet(
            packet_id="packet-1",
            case=case,
            user_messages=(
                ContextUserMessageItem(
                    message_id="message-1",
                    sequence=1,
                    authority_epoch=1,
                    kind="user_message",
                    content="Why is the exposure period higher?",
                ),
            ),
            relevant_event_cursor_start=1,
            relevant_event_cursor_end=1,
            accepted_question=None,
            accepted_frame=None,
            accepted_plan=None,
            accepted_answer=None,
            recent_events=(
                ContextEventItem.from_event(store.list_events("case-1")[0]),
            ),
            evidence_index=evidence_index,
            decision_index=(),
            reviewer_objection_index=(),
            built_at=NOW,
        )
        second = build_context_packet(
            packet_id="packet-2",
            case=case,
            user_messages=(
                ContextUserMessageItem(
                    message_id="message-1",
                    sequence=1,
                    authority_epoch=1,
                    kind="user_message",
                    content="Why is the exposure period higher?",
                ),
            ),
            relevant_event_cursor_start=1,
            relevant_event_cursor_end=1,
            accepted_question=None,
            accepted_frame=None,
            accepted_plan=None,
            accepted_answer=None,
            recent_events=(
                ContextEventItem.from_event(store.list_events("case-1")[0]),
            ),
            evidence_index=evidence_index,
            decision_index=(),
            reviewer_objection_index=(),
            built_at=NOW,
        )

        self.assertEqual(first.content_sha256, second.content_sha256)

        values = {
            field: getattr(first, field)
            for field in first.__dataclass_fields__
        }
        values["content_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            ContextPacket(**values)


class InMemoryAuthorityStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthorityStore(
            resolution_input_verifier=make_trusted_verifier()
        )
        self.case = self.store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        self.case, self.question = accept_initial_question(
            self.store,
            self.case,
        )

    def _accept_frame_and_plan(self) -> None:
        frame = make_frame()
        frame_proof_id = record_reviewed_frame(self.store, frame)
        self.case = self.store.accept_frame(
            frame,
            frame_admission_proof_id=frame_proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )
        self.case, self.plan_bundle = record_plan_bundle(
            store=self.store,
            case=self.case,
            frame=frame,
            created_at=NOW,
        )

    def test_open_case_retry_is_idempotent(self) -> None:
        retried = self.store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )

        self.assertEqual(retried, self.case)
        self.assertEqual(len(self.store.list_events("case-1")), 2)

    def test_head_mutation_uses_cas_and_event_retry_is_idempotent(self) -> None:
        frame = make_frame()
        frame_proof_id = record_reviewed_frame(self.store, frame)
        accepted = self.store.accept_frame(
            frame,
            frame_admission_proof_id=frame_proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )
        retried = self.store.accept_frame(
            frame,
            frame_admission_proof_id=frame_proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )

        self.assertEqual(accepted, retried)
        self.assertEqual(accepted.head_version, 2)
        self.assertEqual(len(self.store.list_events("case-1")), 3)

        with self.assertRaises(StaleHead):
            frame_2 = make_frame(
                revision_number=2,
                frame_id="frame-2",
                prior_id="frame-1",
            )
            frame_2_proof_id = record_reviewed_frame(
                self.store,
                frame_2,
            )
            self.store.accept_frame(
                frame_2,
                frame_admission_proof_id=frame_2_proof_id,
                expected_head_version=0,
                event_id="event-frame-2",
                recorded_at=NOW,
            )

    def test_evidence_recording_does_not_move_heads(self) -> None:
        self._accept_frame_and_plan()
        prior_version = self.case.head_version
        evidence = make_evidence()

        recorded = self.store.record_evidence(
            evidence,
            expected_head_version=prior_version,
            event_id="event-evidence",
            recorded_at=evidence.created_at,
        )

        self.assertEqual(recorded, evidence)
        self.assertEqual(self.store.get_case("case-1").head_version, prior_version)

    def test_evidence_id_cannot_be_reused_with_different_content(self) -> None:
        self._accept_frame_and_plan()
        evidence = make_evidence()
        self.store.record_evidence(
            evidence,
            expected_head_version=self.case.head_version,
            event_id="event-evidence",
            recorded_at=evidence.created_at,
        )

        with self.assertRaises(AuthorityConflict):
            self.store.record_evidence(
                make_evidence(
                    evidence_id="evidence-1",
                    payload={"exposure_amount": 999.0},
                ),
                expected_head_version=self.case.head_version,
                event_id="event-evidence-conflict",
                recorded_at=evidence.created_at,
            )

    def test_answer_requires_compatible_evidence(self) -> None:
        self._accept_frame_and_plan()
        evidence = make_evidence()
        self.store.record_evidence(
            evidence,
            expected_head_version=self.case.head_version,
            event_id="event-evidence",
            recorded_at=evidence.created_at,
        )
        answer = make_answer()

        self.case = self.store.accept_answer(
            answer,
            expected_head_version=self.case.head_version,
            event_id="event-answer",
            recorded_at=answer.created_at,
        )

        self.assertEqual(self.case.accepted_answer_version_id, "answer-1")
        self.assertEqual(self.case.head_version, 4)

    def test_new_frame_invalidates_plan_and_answer_heads(self) -> None:
        self._accept_frame_and_plan()
        evidence = make_evidence()
        self.store.record_evidence(
            evidence,
            expected_head_version=self.case.head_version,
            event_id="event-evidence",
            recorded_at=evidence.created_at,
        )
        answer = make_answer()
        self.case = self.store.accept_answer(
            answer,
            expected_head_version=self.case.head_version,
            event_id="event-answer",
            recorded_at=answer.created_at,
        )
        frame_2 = make_frame(
            revision_number=2,
            frame_id="frame-2",
            prior_id="frame-1",
        )
        frame_2_proof_id = record_reviewed_frame(self.store, frame_2)

        self.case = self.store.accept_frame(
            frame_2,
            frame_admission_proof_id=frame_2_proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame-2",
            recorded_at=frame_2.created_at,
        )

        self.assertEqual(self.case.accepted_frame_revision_id, "frame-2")
        self.assertIsNone(self.case.accepted_plan_revision_id)
        self.assertIsNone(self.case.accepted_answer_version_id)

        self.case, plan_bundle_2 = record_plan_bundle(
            store=self.store,
            case=self.case,
            frame=frame_2,
            created_at=frame_2.created_at,
            plan_revision_id="plan-2",
            prior_plan=self.plan_bundle.plan,
        )
        evidence_2 = make_evidence(
            evidence_id="evidence-2",
            frame_id="frame-2",
            plan_id="plan-2",
        )
        self.store.record_evidence(
            evidence_2,
            expected_head_version=self.case.head_version,
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
        self.case = self.store.accept_answer(
            answer_2,
            expected_head_version=self.case.head_version,
            event_id="event-answer-2",
            recorded_at=answer_2.created_at,
        )

        self.assertEqual(self.case.accepted_plan_revision_id, "plan-2")
        self.assertEqual(self.case.accepted_answer_version_id, "answer-2")

    def test_event_journal_enforces_monotonic_cursor(self) -> None:
        with self.assertRaises(AuthorityConflict):
            self.store.append_event(
                case_id="case-1",
                expected_next_cursor=9,
                event_id="event-checkpoint",
                event_type=JournalEventType.CHECKPOINT_RECORDED,
                recorded_at=NOW,
                action_id=None,
                authority_ref=None,
                payload={},
                customer_projection=None,
                operation=_journal_operation(
                    "event-checkpoint-invalid",
                    {},
                ),
            )

        checkpoint_payload = {"checkpoint": "checkpoint-1"}
        event = self.store.append_event(
            case_id="case-1",
            expected_next_cursor=3,
            event_id="event-checkpoint",
            event_type=JournalEventType.CHECKPOINT_RECORDED,
            recorded_at=NOW,
            action_id=None,
            authority_ref=None,
            payload=checkpoint_payload,
            customer_projection=None,
            operation=_journal_operation(
                "event-checkpoint",
                checkpoint_payload,
            ),
        )
        retried = self.store.append_event(
            case_id="case-1",
            expected_next_cursor=3,
            event_id="event-checkpoint",
            event_type=JournalEventType.CHECKPOINT_RECORDED,
            recorded_at=NOW,
            action_id=None,
            authority_ref=None,
            payload=checkpoint_payload,
            customer_projection=None,
            operation=_journal_operation(
                "event-checkpoint",
                checkpoint_payload,
            ),
        )

        self.assertEqual(event, retried)
        self.assertEqual(event.cursor, 3)

    def test_plan_for_non_current_frame_is_rejected(self) -> None:
        frame = make_frame()
        frame_proof_id = record_reviewed_frame(self.store, frame)
        self.case = self.store.accept_frame(
            frame,
            frame_admission_proof_id=frame_proof_id,
            expected_head_version=self.case.head_version,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )
        self.case, bundle = record_plan_bundle(
            store=self.store,
            case=self.case,
            frame=frame,
            created_at=NOW,
        )
        forged = replace(
            bundle,
            plan=replace(
                bundle.plan,
                plan_revision_id="plan-other-frame",
                frame_revision_id="frame-other",
                revision_number=2,
                prior_plan_revision_id=bundle.plan.plan_revision_id,
            ),
        )

        with self.assertRaises(InvalidAuthorityTransition):
            self.store.accept_plan_bundle(
                forged,
                expected_head_version=self.case.head_version,
                event_id="event-plan-other-frame",
                recorded_at=NOW,
            )

    def test_reviewer_objection_resolution_is_append_only(self) -> None:
        self._accept_frame_and_plan()
        evidence = make_evidence()
        self.store.record_evidence(
            evidence,
            expected_head_version=self.case.head_version,
            event_id="event-evidence",
            recorded_at=evidence.created_at,
        )
        provisional = make_answer(status=AnswerStatus.PROVISIONAL)
        self.case = self.store.accept_answer(
            provisional,
            expected_head_version=self.case.head_version,
            event_id="event-answer",
            recorded_at=provisional.created_at,
        )
        opened = make_objection()
        self.store.record_reviewer_objection(
            opened,
            event_id="event-objection-open",
        )

        with self.assertRaises(InvalidAuthorityTransition):
            self.store.record_reviewer_objection(
                make_objection(
                    objection_id="objection-invalid",
                    revision_number=2,
                    prior_id="objection-other",
                    status=ReviewerObjectionStatus.RESOLVED,
                ),
                event_id="event-objection-invalid",
            )

        resolved = make_objection(
            objection_id="objection-2",
            revision_number=2,
            prior_id=opened.objection_id,
            status=ReviewerObjectionStatus.RESOLVED,
        )
        recorded = self.store.record_reviewer_objection(
            resolved,
            event_id="event-objection-resolved",
        )
        retried = self.store.record_reviewer_objection(
            resolved,
            event_id="event-objection-resolved",
        )

        self.assertEqual(recorded, resolved)
        self.assertEqual(retried, resolved)
        self.assertEqual(
            tuple(event.authority_ref for event in self.store.list_events("case-1")[-2:]),
            ("objection-1", "objection-2"),
        )

    def test_authority_id_cannot_be_replayed_under_a_new_event(self) -> None:
        self._accept_frame_and_plan()
        evidence = make_evidence()
        self.store.record_evidence(
            evidence,
            expected_head_version=self.case.head_version,
            event_id="event-evidence",
            recorded_at=evidence.created_at,
        )

        with self.assertRaises(AuthorityConflict):
            self.store.record_evidence(
                evidence,
                expected_head_version=self.case.head_version,
                event_id="event-evidence-other",
                recorded_at=evidence.created_at,
            )


def _journal_operation(
    operation_id: str,
    payload: dict[str, object],
) -> OperationIdentity:
    return OperationIdentity(
        operation_id=operation_id,
        idempotency_key=f"{operation_id}:key",
        causation_id="test-journal",
        correlation_id="case-1",
        authority_revision=0,
        payload_sha256=content_sha256(payload),
    )


if __name__ == "__main__":
    unittest.main()
