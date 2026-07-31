from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, replace

from waje_vnext.domain.canonical import content_sha256, to_jsonable
from waje_vnext.domain.evidence import EvidenceAdmissionProfile
from waje_vnext.domain.planning import ExecutionRealm
from waje_vnext.domain.workflow import (
    WORKFLOW_PROJECTION_POLICY_SHA256,
    WORKFLOW_PROJECTION_POLICY_VERSION,
    AuthoritySupersededFact,
    DeliveryDispositionFact,
    DeliveryState,
    ExecutionState,
    ObligationDispositionFact,
    ObligationState,
    PlanAcceptedFact,
    PublicationDispositionFact,
    PublicationState,
    TaskExecutionFact,
    WorkflowCursorGap,
    WorkflowFactConflict,
    WorkflowNoChangeFact,
    WorkflowObligationDefinition,
    WorkflowTaskDefinition,
    WorkflowTransitionRejected,
    apply_workflow_fact,
    initial_workflow_read_model,
    replay_workflow,
    workflow_customer_projection,
)

CASE_ID = "case-workflow"
REALM = ExecutionRealm.CONFORMANCE
EVIDENCE_PROFILE = EvidenceAdmissionProfile.CONFORMANCE
SECRET_EVENT_ID = (
    "internal:provider=deepseek;sql=select_raw_rows;verifier_prompt=hidden;retry=3"
)


def event_identity(source_event_id: str) -> dict[str, str]:
    return {
        "source_event_id": source_event_id,
        "source_event_sha256": content_sha256(
            {
                "kind": "test-journal-event.v1",
                "case_id": CASE_ID,
                "source_event_id": source_event_id,
            }
        ),
    }


def plan_identity(plan_revision_id: str) -> dict[str, str]:
    return {
        "plan_content_sha256": content_sha256(
            {
                "kind": "test-plan.v1",
                "plan_revision_id": plan_revision_id,
            }
        ),
        "plan_adoption_id": content_sha256(
            {
                "kind": "test-plan-adoption-id.v1",
                "plan_revision_id": plan_revision_id,
            }
        ),
        "plan_adoption_sha256": content_sha256(
            {
                "kind": "test-plan-adoption.v1",
                "plan_revision_id": plan_revision_id,
            }
        ),
    }


def plan_fact(
    *,
    cursor: int = 1,
    plan_revision_id: str = "plan-1",
    prior_plan_revision_id: str | None = None,
    question_revision_id: str = "question-1",
    frame_revision_id: str = "frame-1",
) -> PlanAcceptedFact:
    source_event_id = (
        SECRET_EVENT_ID
        if plan_revision_id == "plan-1"
        else f"event-accepted-{plan_revision_id}"
    )
    return PlanAcceptedFact(
        case_id=CASE_ID,
        cursor=cursor,
        question_revision_id=question_revision_id,
        question_content_sha256=content_sha256(
            {
                "kind": "test-question.v1",
                "question_revision_id": question_revision_id,
            }
        ),
        frame_revision_id=frame_revision_id,
        frame_content_sha256=content_sha256(
            {
                "kind": "test-frame.v1",
                "frame_revision_id": frame_revision_id,
            }
        ),
        plan_revision_id=plan_revision_id,
        prior_plan_revision_id=prior_plan_revision_id,
        tasks=(
            WorkflowTaskDefinition(
                task_id="task-revenue",
                business_label="核对收入变化",
            ),
        ),
        obligations=(
            WorkflowObligationDefinition(
                obligation_id="obligation-revenue",
                task_id="task-revenue",
                business_label="形成收入变化证据",
            ),
        ),
        **event_identity(source_event_id),
        **plan_identity(plan_revision_id),
    )


def authority_superseded_fact(
    *,
    cursor: int,
    plan_revision_id: str = "plan-1",
    source_event_id: str = "event-authority-superseded",
) -> AuthoritySupersededFact:
    identity = plan_identity(plan_revision_id)
    return AuthoritySupersededFact(
        case_id=CASE_ID,
        cursor=cursor,
        superseded_plan_revision_id=plan_revision_id,
        superseded_plan_content_sha256=identity["plan_content_sha256"],
        superseded_plan_adoption_id=identity["plan_adoption_id"],
        superseded_plan_adoption_sha256=identity["plan_adoption_sha256"],
        superseding_authority_revision_id="frame-2",
        superseding_authority_content_sha256=content_sha256(
            {
                "kind": "test-frame-revision.v1",
                "frame_revision_id": "frame-2",
            }
        ),
        **event_identity(source_event_id),
    )


def initial_model():
    return initial_workflow_read_model(
        CASE_ID,
        realm=REALM,
        evidence_profile=EVIDENCE_PROFILE,
    )


def replay(facts):
    return replay_workflow(
        CASE_ID,
        facts,
        realm=REALM,
        evidence_profile=EVIDENCE_PROFILE,
    )


class Gate35WorkflowTest(unittest.TestCase):
    def test_four_axes_advance_only_from_their_own_facts(self) -> None:
        facts = (
            plan_fact(),
            TaskExecutionFact(
                case_id=CASE_ID,
                cursor=2,
                plan_revision_id="plan-1",
                task_id="task-revenue",
                state=ExecutionState.RUNNING,
                **event_identity("event-task-running"),
            ),
            TaskExecutionFact(
                case_id=CASE_ID,
                cursor=3,
                plan_revision_id="plan-1",
                task_id="task-revenue",
                state=ExecutionState.SUCCEEDED,
                **event_identity("event-task-succeeded"),
            ),
        )
        model = replay(facts)

        self.assertEqual(
            model.snapshot.tasks[0].execution_state,
            ExecutionState.SUCCEEDED,
        )
        self.assertEqual(
            model.snapshot.obligations[0].obligation_state,
            ObligationState.OPEN,
        )
        self.assertEqual(
            model.snapshot.case.publication_state,
            PublicationState.NOT_READY,
        )
        self.assertEqual(
            model.snapshot.case.delivery_state,
            DeliveryState.NOT_DELIVERED,
        )

        model = apply_workflow_fact(
            model,
            ObligationDispositionFact(
                case_id=CASE_ID,
                cursor=4,
                plan_revision_id="plan-1",
                obligation_id="obligation-revenue",
                state=ObligationState.SATISFIED,
                **event_identity("event-obligation-satisfied"),
            ),
        )
        model = apply_workflow_fact(
            model,
            PublicationDispositionFact(
                case_id=CASE_ID,
                cursor=5,
                state=PublicationState.PROVISIONAL,
                answer_version_id="answer-1",
                **event_identity("event-answer-provisional"),
            ),
        )
        self.assertEqual(
            model.snapshot.obligations[0].obligation_state,
            ObligationState.SATISFIED,
        )
        self.assertEqual(
            model.snapshot.case.publication_state,
            PublicationState.PROVISIONAL,
        )
        self.assertEqual(
            model.snapshot.case.delivery_state,
            DeliveryState.NOT_DELIVERED,
        )

    def test_new_plan_supersedes_old_plan_without_erasing_history(
        self,
    ) -> None:
        first = replay(
            (
                plan_fact(),
                TaskExecutionFact(
                    case_id=CASE_ID,
                    cursor=2,
                    plan_revision_id="plan-1",
                    task_id="task-revenue",
                    state=ExecutionState.SUCCEEDED,
                    **event_identity("event-task-success"),
                ),
                ObligationDispositionFact(
                    case_id=CASE_ID,
                    cursor=3,
                    plan_revision_id="plan-1",
                    obligation_id="obligation-revenue",
                    state=ObligationState.SATISFIED,
                    **event_identity("event-obligation-satisfied"),
                ),
                PublicationDispositionFact(
                    case_id=CASE_ID,
                    cursor=4,
                    state=PublicationState.PROVISIONAL,
                    answer_version_id="answer-1",
                    **event_identity("event-answer"),
                ),
            ),
        )
        second = apply_workflow_fact(
            first,
            plan_fact(
                cursor=5,
                plan_revision_id="plan-2",
                prior_plan_revision_id="plan-1",
            ),
        )

        self.assertEqual(
            second.snapshot.case.active_plan_revision_id,
            "plan-2",
        )
        old_task = next(
            item for item in second.snapshot.tasks if item.plan_revision_id == "plan-1"
        )
        old_obligation = next(
            item
            for item in second.snapshot.obligations
            if item.plan_revision_id == "plan-1"
        )
        new_task = next(
            item for item in second.snapshot.tasks if item.plan_revision_id == "plan-2"
        )
        self.assertEqual(
            old_task.execution_state,
            ExecutionState.SUPERSEDED,
        )
        self.assertEqual(
            old_obligation.obligation_state,
            ObligationState.SUPERSEDED,
        )
        self.assertEqual(new_task.execution_state, ExecutionState.PENDING)
        self.assertEqual(
            second.snapshot.case.publication_state,
            PublicationState.NOT_READY,
        )
        self.assertIsNone(second.snapshot.case.accepted_answer_version_id)

        late = apply_workflow_fact(
            second,
            TaskExecutionFact(
                case_id=CASE_ID,
                cursor=6,
                plan_revision_id="plan-1",
                task_id="task-revenue",
                state=ExecutionState.SUCCEEDED,
                **event_identity("late-old-plan-result"),
            ),
        )
        late_old_task = next(
            item for item in late.snapshot.tasks if item.plan_revision_id == "plan-1"
        )
        self.assertEqual(
            late_old_task.execution_state,
            ExecutionState.SUPERSEDED,
        )
        self.assertEqual(late.snapshot.applied_cursor, 6)

    def test_authority_change_immediately_fences_active_plan(self) -> None:
        before = replay(
            (
                plan_fact(),
                TaskExecutionFact(
                    case_id=CASE_ID,
                    cursor=2,
                    plan_revision_id="plan-1",
                    task_id="task-revenue",
                    state=ExecutionState.RUNNING,
                    **event_identity("event-task-running"),
                ),
                PublicationDispositionFact(
                    case_id=CASE_ID,
                    cursor=3,
                    state=PublicationState.PROVISIONAL,
                    answer_version_id="answer-1",
                    **event_identity("event-answer-provisional"),
                ),
            )
        )
        fenced = apply_workflow_fact(
            before,
            authority_superseded_fact(cursor=4),
        )

        self.assertIsNone(fenced.snapshot.case.active_plan_revision_id)
        self.assertEqual(
            fenced.snapshot.accepted_plan_revision_id,
            "plan-1",
        )
        self.assertEqual(
            fenced.snapshot.accepted_plan_content_sha256,
            plan_identity("plan-1")["plan_content_sha256"],
        )
        self.assertEqual(
            fenced.snapshot.accepted_plan_adoption_sha256,
            plan_identity("plan-1")["plan_adoption_sha256"],
        )
        self.assertEqual(
            fenced.snapshot.tasks[0].execution_state,
            ExecutionState.SUPERSEDED,
        )
        self.assertEqual(
            fenced.snapshot.obligations[0].obligation_state,
            ObligationState.SUPERSEDED,
        )
        self.assertEqual(
            fenced.snapshot.case.publication_state,
            PublicationState.NOT_READY,
        )
        self.assertIsNone(fenced.snapshot.case.accepted_answer_version_id)
        self.assertEqual(
            fenced.snapshot.case.delivery_state,
            DeliveryState.SUPERSEDED,
        )

    def test_late_stale_results_are_consumed_without_overwrite(
        self,
    ) -> None:
        fenced = replay(
            (
                plan_fact(),
                authority_superseded_fact(cursor=2),
            )
        )
        after_task = apply_workflow_fact(
            fenced,
            TaskExecutionFact(
                case_id=CASE_ID,
                cursor=3,
                plan_revision_id="plan-1",
                task_id="task-revenue",
                state=ExecutionState.SUCCEEDED,
                **event_identity("late-task-success"),
            ),
        )
        after_obligation = apply_workflow_fact(
            after_task,
            ObligationDispositionFact(
                case_id=CASE_ID,
                cursor=4,
                plan_revision_id="plan-1",
                obligation_id="obligation-revenue",
                state=ObligationState.SATISFIED,
                **event_identity("late-obligation-satisfied"),
            ),
        )

        self.assertEqual(
            after_obligation.snapshot.tasks[0].execution_state,
            ExecutionState.SUPERSEDED,
        )
        self.assertEqual(
            after_obligation.snapshot.obligations[0].obligation_state,
            ObligationState.SUPERSEDED,
        )
        self.assertIsNone(after_obligation.snapshot.case.active_plan_revision_id)
        self.assertEqual(after_obligation.snapshot.applied_cursor, 4)
        self.assertEqual(
            len(after_obligation.application_receipts),
            4,
        )

    def test_new_plan_after_authority_fence_starts_new_frame_plan_chain(
        self,
    ) -> None:
        fenced = replay(
            (
                plan_fact(),
                authority_superseded_fact(cursor=2),
            )
        )
        replacement = apply_workflow_fact(
            fenced,
            plan_fact(
                cursor=3,
                plan_revision_id="plan-2",
                prior_plan_revision_id=None,
                frame_revision_id="frame-2",
            ),
        )

        self.assertEqual(
            replacement.snapshot.case.active_plan_revision_id,
            "plan-2",
        )
        self.assertEqual(
            replacement.snapshot.accepted_plan_revision_id,
            "plan-2",
        )
        self.assertEqual(
            replacement.snapshot.case.delivery_state,
            DeliveryState.NOT_DELIVERED,
        )

    def test_authority_fence_rejects_mismatched_plan_identity(
        self,
    ) -> None:
        model = apply_workflow_fact(initial_model(), plan_fact())
        forged = replace(
            authority_superseded_fact(cursor=2),
            superseded_plan_content_sha256=content_sha256({"kind": "different-plan"}),
        )

        with self.assertRaises(WorkflowTransitionRejected):
            apply_workflow_fact(model, forged)

    def test_cursor_duplicate_gap_and_conflict_fail_closed(self) -> None:
        initial = initial_model()
        fact = plan_fact()
        applied = apply_workflow_fact(initial, fact)

        self.assertIs(apply_workflow_fact(applied, fact), applied)

        with self.assertRaises(WorkflowFactConflict):
            apply_workflow_fact(
                applied,
                replace(
                    fact,
                    **event_identity("different-event"),
                ),
            )
        with self.assertRaises(WorkflowCursorGap):
            apply_workflow_fact(
                applied,
                TaskExecutionFact(
                    case_id=CASE_ID,
                    cursor=3,
                    plan_revision_id="plan-1",
                    task_id="task-revenue",
                    state=ExecutionState.RUNNING,
                    **event_identity("event-gap"),
                ),
            )
        with self.assertRaises(WorkflowFactConflict):
            apply_workflow_fact(
                applied,
                WorkflowNoChangeFact(
                    case_id=CASE_ID,
                    cursor=2,
                    **event_identity(SECRET_EVENT_ID),
                ),
            )

    def test_irrelevant_journal_event_consumes_cursor_without_state_change(
        self,
    ) -> None:
        plan = apply_workflow_fact(
            initial_model(),
            plan_fact(),
        )
        unchanged = apply_workflow_fact(
            plan,
            WorkflowNoChangeFact(
                case_id=CASE_ID,
                cursor=2,
                **event_identity("event-frame-review-internal"),
            ),
        )

        self.assertEqual(unchanged.snapshot.applied_cursor, 2)
        self.assertEqual(
            replace(unchanged.snapshot, applied_cursor=1),
            plan.snapshot,
        )
        self.assertEqual(
            unchanged.application_receipts[-1].prior_receipt_id,
            plan.application_receipts[-1].receipt_id,
        )

    def test_incremental_reduction_equals_full_replay(self) -> None:
        facts = (
            plan_fact(),
            TaskExecutionFact(
                case_id=CASE_ID,
                cursor=2,
                plan_revision_id="plan-1",
                task_id="task-revenue",
                state=ExecutionState.RUNNING,
                **event_identity("event-running"),
            ),
            ObligationDispositionFact(
                case_id=CASE_ID,
                cursor=3,
                plan_revision_id="plan-1",
                obligation_id="obligation-revenue",
                state=ObligationState.BOUNDARY,
                **event_identity("event-boundary"),
            ),
            PublicationDispositionFact(
                case_id=CASE_ID,
                cursor=4,
                state=PublicationState.BLOCKED,
                answer_version_id=None,
                **event_identity("event-publication-blocked"),
            ),
            DeliveryDispositionFact(
                case_id=CASE_ID,
                cursor=5,
                state=DeliveryState.SUPERSEDED,
                **event_identity("event-delivery-superseded"),
            ),
        )
        incremental = initial_model()
        for fact in facts:
            incremental = apply_workflow_fact(incremental, fact)

        self.assertEqual(incremental, replay(facts))
        self.assertEqual(
            incremental.head.snapshot_sha256,
            incremental.snapshot.content_sha256,
        )
        self.assertEqual(
            len(incremental.application_receipts),
            len(facts),
        )

    def test_gate3_denies_settled_and_delivered(self) -> None:
        model = apply_workflow_fact(
            initial_model(),
            plan_fact(),
        )
        with self.assertRaises(WorkflowTransitionRejected):
            apply_workflow_fact(
                model,
                PublicationDispositionFact(
                    case_id=CASE_ID,
                    cursor=2,
                    state=PublicationState.SETTLED,
                    answer_version_id=None,
                    **event_identity("forged-settled-event"),
                ),
            )
        with self.assertRaises(WorkflowTransitionRejected):
            apply_workflow_fact(
                model,
                DeliveryDispositionFact(
                    case_id=CASE_ID,
                    cursor=2,
                    state=DeliveryState.DELIVERED,
                    **event_identity("forged-delivery-event"),
                ),
            )

    def test_customer_projection_has_fixed_safe_shape(self) -> None:
        model = apply_workflow_fact(
            initial_model(),
            plan_fact(),
        )
        customer = workflow_customer_projection(model.snapshot)
        serialized = json.dumps(
            to_jsonable(customer),
            ensure_ascii=False,
            sort_keys=True,
        )

        self.assertNotIn(SECRET_EVENT_ID, serialized)
        for forbidden in (
            "sql",
            "prompt",
            "verifier",
            "provider",
            "retry",
            "receipt_id",
            "snapshot_sha256",
            "customer_projection",
            "completed",
        ):
            self.assertNotIn(forbidden, serialized.lower())
        self.assertEqual(
            set(customer),
            {"case", "tasks", "obligations"},
        )

    def test_no_axis_defines_generic_completed_state(self) -> None:
        axis_values = {
            *(item.value for item in ExecutionState),
            *(item.value for item in ObligationState),
            *(item.value for item in PublicationState),
            *(item.value for item in DeliveryState),
        }
        self.assertNotIn("completed", axis_values)

    def test_snapshot_receipt_and_head_are_immutable_canonical_values(
        self,
    ) -> None:
        first = apply_workflow_fact(
            initial_model(),
            plan_fact(),
        )
        replayed = replay((plan_fact(),))

        self.assertEqual(first, replayed)
        self.assertEqual(
            first.head.snapshot_id,
            first.snapshot.snapshot_id,
        )
        self.assertEqual(
            first.head.last_receipt_id,
            first.application_receipts[-1].receipt_id,
        )
        self.assertEqual(first.snapshot.realm, REALM)
        self.assertEqual(
            first.snapshot.evidence_profile,
            EVIDENCE_PROFILE,
        )
        self.assertEqual(
            first.snapshot.projection_policy_version,
            WORKFLOW_PROJECTION_POLICY_VERSION,
        )
        self.assertEqual(
            first.snapshot.projection_policy_sha256,
            WORKFLOW_PROJECTION_POLICY_SHA256,
        )
        self.assertEqual(
            first.snapshot.accepted_plan_revision_id,
            "plan-1",
        )
        self.assertEqual(
            first.snapshot.accepted_plan_content_sha256,
            plan_identity("plan-1")["plan_content_sha256"],
        )
        self.assertEqual(
            first.snapshot.accepted_plan_adoption_id,
            plan_identity("plan-1")["plan_adoption_id"],
        )
        self.assertEqual(
            first.snapshot.accepted_plan_adoption_sha256,
            plan_identity("plan-1")["plan_adoption_sha256"],
        )
        self.assertEqual(
            first.application_receipts[-1].source_event_sha256,
            plan_fact().source_event_sha256,
        )
        with self.assertRaises(FrozenInstanceError):
            first.snapshot.applied_cursor = 9  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            first.head.version = 9  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            first.application_receipts[-1].cursor = 9  # type: ignore[misc]

    def test_source_event_digest_is_required_and_receipted(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            replace(plan_fact(), source_event_sha256="not-a-digest")

        applied = apply_workflow_fact(initial_model(), plan_fact())
        with self.assertRaises(WorkflowFactConflict):
            apply_workflow_fact(
                applied,
                WorkflowNoChangeFact(
                    case_id=CASE_ID,
                    cursor=2,
                    source_event_id="another-event-id",
                    source_event_sha256=plan_fact().source_event_sha256,
                ),
            )

    def test_snapshot_rejects_stale_projection_policy(self) -> None:
        snapshot = apply_workflow_fact(
            initial_model(),
            plan_fact(),
        ).snapshot

        with self.assertRaises(ValueError):
            replace(
                snapshot,
                projection_policy_sha256=content_sha256({"kind": "stale-policy"}),
            )


if __name__ == "__main__":
    unittest.main()
