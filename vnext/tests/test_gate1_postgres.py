from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from gate1_fixtures import (
    NOW,
    accept_initial_question,
    make_frame,
    record_reviewed_frame,
)
from postgres_test_support import (
    bootstrap_postgres_test_schema,
    reset_postgres_test_data,
)
from gate3_plan_fixtures import record_plan_bundle
from test_gate3_3_measurement_resolver import make_trusted_verifier
from waje_vnext.domain.authority import AnalysisFrameRevision
from waje_vnext.storage import (
    PostgresAuthorityStore,
    StaleHead,
)


DSN = os.environ.get("WAJE_VNEXT_DATABASE_URL")


@unittest.skipUnless(DSN, "WAJE_VNEXT_DATABASE_URL is not configured")
class PostgresAuthorityStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert DSN is not None
        bootstrap_postgres_test_schema(DSN)

    def setUp(self) -> None:
        assert DSN is not None
        reset_postgres_test_data(DSN)
        self.store = PostgresAuthorityStore.connect(
            DSN,
            resolution_input_verifier=make_trusted_verifier(),
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
        case, bundle = record_plan_bundle(
            store=self.store,
            case=case,
            frame=frame,
            created_at=NOW,
            plan_revision_id="case-g3-derived:plan:1",
        )
        outcome = self.store.list_measurement_resolutions(
            frame.frame_revision_id
        )[0]
        admission = self.store.get_measurement_resolution_admission(
            outcome.resolution_outcome_id
        )
        self.assertEqual(
            self.store.get_measurement_resolution_admission(
                outcome.resolution_outcome_id
            ),
            admission,
        )
        obligation = self.store.list_evidence_obligations(
            frame.frame_revision_id
        )[0]
        self.assertEqual(
            obligation.resolution_outcome_id,
            outcome.resolution_outcome_id,
        )



if __name__ == "__main__":
    unittest.main()
