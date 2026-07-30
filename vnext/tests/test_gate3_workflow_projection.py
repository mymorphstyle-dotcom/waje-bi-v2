from __future__ import annotations

import unittest
from datetime import timedelta

from tests.gate1_fixtures import NOW, make_evidence, make_frame, make_plan
from waje_vnext.domain.events import JournalEventType
from waje_vnext.projections import (
    WorkflowProjectionMode,
    WorkflowTaskStatus,
    build_workflow_projection,
)
from waje_vnext.storage import InMemoryAuthorityStore


class Gate3WorkflowProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryAuthorityStore()
        self.store.open_case(
            case_id="case-1",
            thread_id="thread-1",
            event_id="event-open",
            opened_at=NOW,
        )
        frame = make_frame()
        case = self.store.accept_frame(
            frame,
            expected_head_version=0,
            event_id="event-frame",
            recorded_at=frame.created_at,
        )
        plan = make_plan()
        self.store.accept_plan(
            plan,
            expected_head_version=case.head_version,
            event_id="event-plan",
            recorded_at=plan.created_at,
        )

    def project(self, events=None):
        case = self.store.get_case("case-1")
        return build_workflow_projection(
            case=case,
            frame=self.store.get_frame(
                case.accepted_frame_revision_id or ""
            ),
            plan=self.store.get_plan(case.accepted_plan_revision_id or ""),
            answer=None,
            events=(
                self.store.list_events("case-1")
                if events is None
                else events
            ),
            evidence=self.store.list_evidence("case-1"),
        )

    def test_projection_uses_plan_and_real_business_events(self) -> None:
        next_cursor = self.store.list_events("case-1")[-1].cursor + 1
        self.store.append_event(
            case_id="case-1",
            expected_next_cursor=next_cursor,
            event_id="event-investigating",
            event_type=JournalEventType.EFFECT_ENQUEUED,
            recorded_at=NOW + timedelta(minutes=15),
            action_id="action-probe",
            authority_ref="outbox-probe",
            payload={"payload_sha256": "0" * 64},
            customer_projection={
                "state": "investigating",
                "task_id": "task-pattern",
            },
        )

        projection = self.project()

        self.assertEqual(projection.mode, WorkflowProjectionMode.REPLAY)
        self.assertEqual(
            projection.tasks[0].status,
            WorkflowTaskStatus.INVESTIGATING,
        )
        self.assertNotIn("action-probe", repr(projection))

    def test_evidence_completion_and_incomplete_chronology_static_fallback(
        self,
    ) -> None:
        evidence = make_evidence()
        case = self.store.get_case("case-1")
        self.store.record_evidence(
            evidence,
            expected_head_version=case.head_version,
            event_id="event-evidence",
            recorded_at=evidence.created_at,
        )
        complete = self.project()
        self.assertEqual(
            complete.tasks[0].status,
            WorkflowTaskStatus.COMPLETED,
        )
        self.assertEqual(
            complete.tasks[0].evidence_record_ids,
            ("evidence-1",),
        )

        events = self.store.list_events("case-1")
        incomplete = self.project(events=(events[0],) + events[2:])
        self.assertEqual(incomplete.mode, WorkflowProjectionMode.STATIC)
        self.assertEqual(
            incomplete.tasks[0].status,
            WorkflowTaskStatus.COMPLETED,
        )


if __name__ == "__main__":
    unittest.main()
